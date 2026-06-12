#!/usr/bin/env python3
"""
主升浪标签构建 - 流式版本
- 用 pymysql SSCursor 按 ts_code 流式读, 不一次性加载 1.4M 行
- 处理完一只股票就释放内存
- 直接写 MySQL (批量 insert) + 最终写 parquet
"""
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_PARQUET = Path(__file__).parent.parent / "data" / "main_wave_labels.parquet"

# 阈值
PCT_RANGE = (3.0, 9.5)
VOL_MULT = 1.8
RANGE_AVG_20D = 8.0
BREAKOUT_LOOKBACK = 20
FWD_DAYS = 3
RETURN_3D_THRESHOLD = 5.0
DRAWDOWN_3D_THRESHOLD = -8.0


def stream_one_stock(cur, ts_code):
    """流式读取单只股票, 返回 DataFrame"""
    cur.execute("""
        SELECT trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_price
        WHERE ts_code = %s
        ORDER BY trade_date
    """, (ts_code,))
    rows = cur.fetchall()
    if len(rows) < 60:
        return None
    # 转 float (避免 Decimal 进 numpy 触发 InvalidOperation)
    rows_f = []
    for r in rows:
        rows_f.append((r[0],
                       float(r[1]) if r[1] is not None else 0.0,
                       float(r[2]) if r[2] is not None else 0.0,
                       float(r[3]) if r[3] is not None else 0.0,
                       float(r[4]) if r[4] is not None else 0.0,
                       float(r[5]) if r[5] is not None else 0.0,
                       float(r[6]) if r[6] is not None else 0.0,
                       float(r[7]) if r[7] is not None else 0.0))
    df = pd.DataFrame(rows_f, columns=['trade_date','open','high','low','close','vol','amount','pct_chg'])
    return df


def build_labels_for_stock(df):
    """对单只股票计算主升浪标签"""
    g = df.sort_values('trade_date').reset_index(drop=True)
    n = len(g)
    if n < 60:
        return None
    close = g['close'].values
    high = g['high'].values
    low = g['low'].values
    vol = g['vol'].values
    pct = g['pct_chg'].values

    # MA20
    ma20 = pd.Series(close).rolling(20).mean().values
    vol_ma5 = pd.Series(vol).rolling(5).mean().values
    range_pct = (high - low) / np.roll(close, 1) * 100
    range_pct[0] = 0
    range_ma20 = pd.Series(range_pct).rolling(20).mean().values
    ma20_slope = np.zeros(n)
    ma20_slope[10:] = (ma20[10:] / ma20[:-10] - 1) * 100
    high_20d = pd.Series(high).rolling(BREAKOUT_LOOKBACK).max().values
    close_20d_high = (close >= high_20d * 0.999).astype(int)
    # 未来收益
    close_fwd_3d = np.roll(close, -FWD_DAYS)
    return_3d = (close_fwd_3d / close - 1) * 100
    return_3d[-FWD_DAYS:] = np.nan
    # 启动后最大回撤 (向量化)
    max_dd_3d = np.zeros(n)
    for i in range(n - FWD_DAYS):
        max_dd_3d[i] = close[i+1:i+1+FWD_DAYS].min() / close[i] - 1
    max_dd_3d *= 100
    max_dd_3d[-FWD_DAYS:] = np.nan

    # 条件
    c1 = (pct >= PCT_RANGE[0]) & (pct <= PCT_RANGE[1])
    c2 = vol / np.where(vol_ma5 > 0, vol_ma5, 1) >= VOL_MULT
    c3 = (range_ma20 <= RANGE_AVG_20D) & (np.abs(ma20_slope) < 3)
    c4 = close_20d_high == 1
    c5 = return_3d >= RETURN_3D_THRESHOLD
    c6 = max_dd_3d >= DRAWDOWN_3D_THRESHOLD

    main_wave = (c1 & c2 & c3 & c4 & c5 & c6).astype(int)
    main_wave_relaxed = (c1 & c2 & c4).astype(int)

    out = pd.DataFrame({
        'trade_date': g['trade_date'].values,
        'ts_code': g.attrs.get('ts_code', ''),
        'pct_chg': pct,
        'vol': vol,
        'vol_ma5': vol_ma5,
        'range_ma20': range_ma20,
        'ma20_slope': ma20_slope,
        'close_20d_high': close_20d_high,
        'return_3d': np.where(np.isnan(return_3d), 0, return_3d),
        'return_5d': 0,
        'max_dd_3d': np.where(np.isnan(max_dd_3d), 0, max_dd_3d),
        'main_wave': main_wave,
        'main_wave_relaxed': main_wave_relaxed,
        'trigger_type': np.where(main_wave==1, np.where(pct>=9.5, 'limit_up', 'breakout'), 'none'),
        'label': main_wave,
    })
    return out


def main():
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()
    # 拿所有股票
    cur.execute("SELECT DISTINCT ts_code FROM daily_price")
    codes = [r[0] for r in cur.fetchall()]
    logger.info(f"Processing {len(codes)} stocks (stream mode)...")

    all_pieces = []
    pos_count = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        try:
            df = stream_one_stock(cur, code)
            if df is None: continue
            df.attrs['ts_code'] = code
            labels = build_labels_for_stock(df)
            if labels is None or labels.empty: continue
            all_pieces.append(labels)
            pos_count += int(labels['label'].sum())
            if (i+1) % 500 == 0:
                logger.info(f"  {i+1}/{len(codes)} stocks, pos={pos_count}, "
                           f"elapsed={time.time()-t0:.0f}s, "
                           f"kept={sum(len(p) for p in all_pieces)} rows")
        except Exception as e:
            logger.error(f"  {code}: {e}")
            continue

    logger.info(f"Combining {len(all_pieces)} pieces...")
    df_all = pd.concat(all_pieces, ignore_index=True)
    pos = df_all[df_all['label'] == 1]
    neg_pool = df_all[(df_all['label'] == 0) & (df_all['main_wave_relaxed'] == 0)]
    neg = neg_pool.sample(n=min(len(pos) * 4, len(neg_pool)), random_state=42)
    df_out = pd.concat([pos, neg], ignore_index=True)

    # 拿行业信息
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = {r[0]: r[1] for r in cur.fetchall()}
    df_out['industry'] = df_out['ts_code'].map(ind_map).fillna('OTHER')

    out_cols = ['trade_date', 'ts_code', 'label', 'return_3d', 'return_5d',
                'max_dd_3d', 'trigger_type', 'industry', 'pct_chg', 'vol',
                'vol_ma5', 'range_ma20', 'ma20_slope', 'close_20d_high',
                'main_wave', 'main_wave_relaxed']
    df_out = df_out[out_cols]

    OUTPUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_parquet(OUTPUT_PARQUET, index=False)
    logger.info(f"Wrote {len(df_out)} labels → {OUTPUT_PARQUET}")
    logger.info(f"  pos: {(df_out['label']==1).sum()}  neg: {(df_out['label']==0).sum()}")
    logger.info(f"  base rate: {df_out['label'].mean()*100:.3f}%")

    # 写 MySQL (删旧 + 批量 insert)
    cur.execute("DELETE FROM main_wave_labels")
    rows = [tuple(r) for r in df_out[['trade_date','ts_code','label','return_3d','return_5d',
                                       'max_dd_3d','trigger_type','industry']]
                    .rename(columns={'max_dd_3d':'max_drawdown_3d'}).itertuples(index=False, name=None)]
    BATCH = 5000
    for s in range(0, len(rows), BATCH):
        cur.executemany("""INSERT INTO main_wave_labels
            (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows[s:s+BATCH])
    conn.commit()
    logger.info(f"  inserted {len(rows)} rows into main_wave_labels")
    conn.close()


if __name__ == '__main__':
    main()
