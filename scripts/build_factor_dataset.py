#!/usr/bin/env python3
"""
Phase 2A: 因子选股数据集构建

为每天每只股票计算 30+ 因子, 写入 Parquet.

训练目标: 未来 5 日行业内相对排名
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

OUTPUT_DIR = Path(__file__).parent.parent / "data" / "factor_dataset"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _rolling_mean(s, n):
    return s.rolling(n, min_periods=1).mean()


def _rolling_std(s, n):
    return s.rolling(n, min_periods=2).std()


def compute_factors_for_stock(df_one):
    """输入单只股票的 K 线 DataFrame, 输出 30 维因子"""
    g = df_one.sort_values('trade_date').reset_index(drop=True)
    if len(g) < 30:
        return None
    close = g['close'].astype(float)
    high = g['high'].astype(float)
    low = g['low'].astype(float)
    vol = g['vol'].astype(float)
    amount = g['amount'].astype(float)
    open_p = g['open'].astype(float)
    pct = g['pct_chg'].astype(float)

    out = pd.DataFrame(index=g.index)
    out['trade_date'] = g['trade_date']
    out['ts_code'] = g['ts_code']

    # 收益类
    for n in [1, 3, 5, 10, 20]:
        out[f'ret_{n}d'] = (close / close.shift(n) - 1) * 100
    # 距 52 周新高
    out['high_52w_pct'] = (close / high.rolling(252, min_periods=20).max() - 1) * 100

    # 波动
    for n in [5, 10, 20]:
        out[f'vol_{n}d'] = pct.rolling(n, min_periods=2).std() * np.sqrt(252)
    # 换手率
    out['turnover_5d'] = g['turnover_rate'].rolling(5).mean()
    out['turnover_20d'] = g['turnover_rate'].rolling(20).mean()
    # 量比
    out['amount_ratio_5d_20d'] = amount.rolling(5).mean() / amount.rolling(20, min_periods=10).mean()
    # 涨跌比
    out['up_down_ratio_5d'] = (pct > 0).rolling(5).mean()

    # 技术
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_dif = ema12 - ema26
    macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
    out['macd_hist'] = (macd_dif - macd_dea) * 2
    # RSI 14
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out['rsi_14'] = 100 - (100 / (1 + rs))
    # MA 偏离
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    out['ma5_ma20_diff'] = (ma5 / ma20 - 1) * 100
    # 布林带位置
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    out['boll_pos'] = (close - bb_mid) / (bb_std * 2 + 1e-6)
    # 量价相关
    out['vol_price_corr_10d'] = close.rolling(10).corr(vol)

    # 形态
    # 近 20 日涨停次数
    out['limit_up_count_20d'] = (pct > 9.5).rolling(20).sum()
    # 横盘 (近 10 日振幅均值小)
    out['amplitude_10d'] = ((high - low) / close.shift(1) * 100).rolling(10).mean()
    # 5 日量比 vs 20 日
    vol_ma5 = vol.rolling(5).mean()
    vol_ma20 = vol.rolling(20).mean()
    out['vol_breakout'] = vol_ma5 / (vol_ma20 + 1e-6)

    # 资金流 (从 moneyflow 表 join 进来)
    # 缺失的先填 0
    for c in ['main_net_5d', 'main_net_20d', 'lhb_net_5d']:
        if c not in out.columns:
            out[c] = 0.0

    return out


def compute_target(df_one, horizon=5):
    """未来 horizon 日收益 (作为 target)"""
    g = df_one.sort_values('trade_date').reset_index(drop=True)
    close = g['close'].astype(float)
    fut = close.shift(-horizon) / close - 1
    out = pd.DataFrame({
        'trade_date': g['trade_date'],
        'ts_code': g['ts_code'],
        f'target_ret_{horizon}d': fut * 100,
    })
    return out


def build_dataset(start='2023-01-01', end='2026-06-09', sample_freq=5):
    """
    构建因子数据集: 每 (sample_freq) 个交易日, 所有股票
    """
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()

    # 1) 加载日线
    logger.info("Loading daily_price...")
    cur.execute("""SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, turnover_rate
                  FROM daily_price
                  WHERE trade_date BETWEEN %s AND %s
                    AND SUBSTRING(ts_code,1,2) IN ('60','00','30','68')
                  ORDER BY ts_code, trade_date""", (start, end))
    price_rows = cur.fetchall()
    logger.info(f"  {len(price_rows)} rows")

    # 2) 加载行业
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = dict(cur.fetchall())
    logger.info(f"  {len(ind_map)} industries")

    # 3) 转 DataFrame
    cols = ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'amount', 'pct_chg', 'turnover_rate']
    df = pd.DataFrame(price_rows, columns=cols)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    df['vol'] = df['vol'].astype(float)
    df['amount'] = df['amount'].astype(float)
    df['pct_chg'] = df['pct_chg'].astype(float)
    df['turnover_rate'] = df['turnover_rate'].astype(float)
    df['industry'] = df['ts_code'].map(ind_map).fillna('OTHER')

    # 4) 采样交易日 (每 sample_freq 天)
    all_dates = sorted(df['trade_date'].unique())
    sample_dates = all_dates[::sample_freq]
    logger.info(f"  {len(all_dates)} total days, {len(sample_dates)} sample dates")

    # 5) 计算每只股票的因子
    logger.info("Computing factors per stock...")
    pieces = []
    target_pieces = []
    t0 = time.time()
    n_stocks = df['ts_code'].nunique()
    for i, (code, g) in enumerate(df.groupby('ts_code', sort=False)):
        if len(g) < 30:
            continue
        factors = compute_factors_for_stock(g)
        if factors is None: continue
        target = compute_target(g, horizon=5)
        # 合并 (加 industry 列)
        factors['industry'] = g['industry'].iloc[0] if 'industry' in g.columns else 'OTHER'
        merged = factors.merge(target, on=['trade_date', 'ts_code'], how='inner')
        # 只保留 sample dates
        merged = merged[merged['trade_date'].isin(sample_dates)]
        if len(merged) == 0: continue
        pieces.append(merged)
        if (i+1) % 500 == 0:
            logger.info(f"  {i+1}/{n_stocks} stocks, elapsed={time.time()-t0:.0f}s")

    logger.info(f"Concatenating {len(pieces)} pieces...")
    df_all = pd.concat(pieces, ignore_index=True)
    logger.info(f"  {len(df_all)} samples total")
    logger.info(f"  {df_all['ts_code'].nunique()} unique stocks")
    logger.info(f"  {df_all['trade_date'].nunique()} unique dates")
    logger.info(f"  Avg samples per date: {len(df_all)/df_all['trade_date'].nunique():.0f}")

    # 6) 加行业相对收益 (未来 5 日)
    logger.info("Computing industry-relative features...")
    df_all = df_all.sort_values(['trade_date', 'industry', 'ts_code'])
    df_all['ind_ret_5d'] = df_all.groupby(['trade_date', 'industry'])['target_ret_5d'].transform('mean')
    df_all['ret_5d_industry_relative'] = df_all['target_ret_5d'] - df_all['ind_ret_5d']
    # 行业内 rank
    df_all['ret_5d_industry_rank'] = df_all.groupby(['trade_date', 'industry'])['target_ret_5d'].rank(pct=True)

    # 7) 缺失值处理
    feature_cols = [c for c in df_all.columns if c not in
                    ['trade_date', 'ts_code', 'industry', 'target_ret_5d', 'ind_ret_5d', 'ret_5d_industry_relative', 'ret_5d_industry_rank']]
    for c in feature_cols:
        df_all[c] = pd.to_numeric(df_all[c], errors='coerce').fillna(0)

    # 8) 写 Parquet
    out_path = OUTPUT_DIR / f"factor_{start}_{end}.parquet"
    df_all.to_parquet(out_path, index=False)
    logger.info(f"Saved → {out_path}")
    logger.info(f"Features: {len(feature_cols)}")
    logger.info(f"  {feature_cols[:10]}")

    return df_all, feature_cols


if __name__ == '__main__':
    df_all, feats = build_dataset(start='2024-01-01', end='2026-06-09', sample_freq=3)
    print("\n=== Dataset summary ===")
    print(f"  Rows: {len(df_all)}")
    print(f"  Features: {len(feats)}")
    print(f"  Date range: {df_all['trade_date'].min()} ~ {df_all['trade_date'].max()}")
    print(f"  Stock count: {df_all['ts_code'].nunique()}")
    print(f"  Industry count: {df_all['industry'].nunique()}")
    print("\nTarget stats:")
    print(df_all['target_ret_5d'].describe())
