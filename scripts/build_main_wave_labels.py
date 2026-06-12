#!/usr/bin/env python3
"""
主升浪标签构建 — 离线一次性跑完，产出 main_wave_labels 表 + parquet

主升浪精确定义（事后标注）:
  触发日 d = 满足以下全部:
    1. 当日涨幅 ∈ [3%, 9.5%]
    2. 成交量 >= 5日均量 × 1.8
    3. 前 5~20 日横盘 (振幅均值 ≤ 8%)
    4. 当日收盘价 >= 20 日新高
    5. 未来 3 日累计收益 >= 5%
    6. 未来 3 日最大回撤 >= -8%

label = 1: 同时满足 1-6
label = 0: 至少 1-3 中两条不满足 (作为负样本对照)
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()
OUTPUT_PARQUET = Path(__file__).parent.parent / "data" / "main_wave_labels.parquet"

PCT_RANGE = (3.0, 9.5)
VOL_MULT = 1.8
RANGE_AVG_20D = 8.0
BREAKOUT_LOOKBACK = 20
FWD_DAYS_3D = 3
FWD_DAYS_5D = 5
RETURN_3D_THRESHOLD = 5.0
DRAWDOWN_3D_THRESHOLD = -8.0


def _load_daily_price(start_date, end_date):
    conn = pymysql.connect(**DB_CONFIG)
    try:
        sql = """SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
                 FROM daily_price
                 WHERE trade_date BETWEEN %s AND %s
                 ORDER BY ts_code, trade_date"""
        df = pd.read_sql(sql, conn, params=(start_date, end_date), parse_dates=['trade_date'])
        return df
    finally:
        conn.close()


def _build_per_stock(g):
    g = g.sort_values('trade_date').reset_index(drop=True)
    if len(g) < 60:
        return None
    g['ma20'] = g['close'].rolling(20).mean()
    g['vol_ma5'] = g['vol'].rolling(5).mean()
    g['range_pct'] = (g['high'] - g['low']) / g['close'].shift(1) * 100
    g['range_ma20'] = g['range_pct'].rolling(20).mean()
    g['ma20_slope'] = (g['ma20'] - g['ma20'].shift(10)) / g['ma20'].shift(10) * 100
    g['high_20d'] = g['high'].rolling(BREAKOUT_LOOKBACK).max()
    g['close_20d_high'] = (g['close'] >= g['high_20d']).astype(int)

    g['close_fwd_3d'] = g['close'].shift(-FWD_DAYS_3D)
    g['close_fwd_5d'] = g['close'].shift(-FWD_DAYS_5D)
    g['return_3d'] = (g['close_fwd_3d'] / g['close'] - 1) * 100
    g['return_5d'] = (g['close_fwd_5d'] / g['close'] - 1) * 100

    # 启动后最大回撤 (vectorized, 快 10x)
    closes = g['close'].values
    fwd_min = np.full(len(g), np.nan)
    for i in range(len(g) - FWD_DAYS_3D):
        fwd_min[i] = closes[i+1:i+1+FWD_DAYS_3D].min() / closes[i] - 1
    g['max_dd_3d'] = fwd_min * 100

    c1 = g['pct_chg'].between(*PCT_RANGE)
    c2 = (g['vol'] / g['vol_ma5']) >= VOL_MULT
    c3 = (g['range_ma20'] <= RANGE_AVG_20D) & (g['ma20_slope'].abs() < 3)
    c4 = g['close_20d_high'] == 1
    c5 = g['return_3d'] >= RETURN_3D_THRESHOLD
    c6 = g['max_dd_3d'] >= DRAWDOWN_3D_THRESHOLD

    g['main_wave'] = (c1 & c2 & c3 & c4 & c5 & c6).astype(int)
    g['main_wave_relaxed'] = (c1 & c2 & c4).astype(int)
    g['trigger_type'] = np.where(g['main_wave']==1,
                                 np.where(g['pct_chg']>=9.5, 'limit_up', 'breakout'),
                                 'none')
    return g


def main():
    logger.info("Loading daily_price (2024-01-01 to today)...")
    df_all = _load_daily_price('2024-01-01', datetime.now().strftime('%Y-%m-%d'))
    logger.info(f"  {len(df_all)} rows, {df_all['ts_code'].nunique()} stocks")

    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as c:
            c.execute("SELECT ts_code, industry FROM stock_info")
            industry_map = {r[0]: r[1] for r in c.fetchall()}
    finally:
        conn.close()

    logger.info("Building per-stock features...")
    pieces = []
    grouped = df_all.groupby('ts_code', sort=False)
    for i, (ts_code, g) in enumerate(grouped):
        feat = _build_per_stock(g)
        if feat is not None and not feat.empty:
            pieces.append(feat)
        if (i+1) % 500 == 0:
            logger.info(f"  processed {i+1} stocks, kept {sum(len(p) for p in pieces)} rows")

    df = pd.concat(pieces, ignore_index=True)
    df['industry'] = df['ts_code'].map(industry_map).fillna('OTHER')
    df['label'] = df['main_wave']

    # 平衡: 正样本全留, 负样本按 4:1 采样
    pos = df[df['label'] == 1]
    neg_pool = df[(df['label'] == 0) & (df['main_wave_relaxed'] == 0)]
    neg = neg_pool.sample(n=min(len(pos) * 4, len(neg_pool)), random_state=42)
    out = pd.concat([pos, neg], ignore_index=True)
    out = out[['trade_date', 'ts_code', 'label', 'pct_chg', 'vol', 'vol_ma5',
               'range_ma20', 'ma20_slope', 'close_20d_high',
               'return_3d', 'return_5d', 'max_dd_3d', 'trigger_type', 'industry']]

    OUTPUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUTPUT_PARQUET, index=False)
    logger.info(f"Wrote {len(out)} labels → {OUTPUT_PARQUET}")
    logger.info(f"  pos: {(out['label']==1).sum()}  neg: {(out['label']==0).sum()}  base rate: {out['label'].mean()*100:.3f}%")

    # 同时写 MySQL 标签表
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM main_wave_labels")  # 增量刷新
            rows = [tuple(r) for r in out[['trade_date','ts_code','label','return_3d','return_5d',
                                            'max_dd_3d','trigger_type','industry']]
                            .rename(columns={'max_dd_3d':'max_drawdown_3d'}).itertuples(index=False, name=None)]
            c.executemany("""INSERT INTO main_wave_labels
                (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
            conn.commit()
            logger.info(f"  inserted {len(rows)} rows into main_wave_labels")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
