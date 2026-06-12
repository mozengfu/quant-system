#!/usr/bin/env python3
"""
Phase 2D: 因子数据集 V2 - 加 V11 预测, 大盘状态, 板块动量
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


def build_v2(start='2024-01-01', end='2026-06-09', sample_freq=1):
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()

    # 1) 日线
    logger.info("Loading daily_price...")
    cur.execute("""SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg, turnover_rate
                  FROM daily_price WHERE trade_date BETWEEN %s AND %s
                    AND SUBSTRING(ts_code,1,2) IN ('60','00','30','68')
                  ORDER BY ts_code, trade_date""", (start, end))
    price_rows = cur.fetchall()
    df_price = pd.DataFrame(price_rows, columns=['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'amount', 'pct_chg', 'turnover_rate'])
    df_price['trade_date'] = pd.to_datetime(df_price['trade_date'])
    for c in ['open','high','low','close','vol','amount','pct_chg','turnover_rate']:
        df_price[c] = df_price[c].astype(float)
    logger.info(f"  {len(df_price)} price rows")

    # 2) 行业
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = dict(cur.fetchall())
    df_price['industry'] = df_price['ts_code'].map(ind_map).fillna('OTHER')

    # 3) V11 预测 (用 predict_batch 一次性算所有股票的所有日期)
    logger.info("Computing V11 predictions for all dates/stocks...")
    from ml_predict import predict_batch

    v11_records = []
    all_dates = sorted(df_price['trade_date'].unique())
    all_codes = df_price['ts_code'].unique().tolist()
    t0 = time.time()
    for d in all_dates:
        try:
            preds = predict_batch(all_codes, db_conn=conn, as_of_date=d.strftime('%Y%m%d'))
            for code, p in preds.items():
                v11_records.append({
                    'trade_date': d,
                    'ts_code': code,
                    'v11_prob': float(p.get('probability', 0.5)),
                    'v11_pred_ret': float(p.get('predicted_return', 0)),
                })
        except Exception as e:
            logger.warning(f"V11 {d}: {e}")
            continue
    logger.info(f"  V11 done in {time.time()-t0:.0f}s, {len(v11_records)} records")
    df_v11 = pd.DataFrame(v11_records)

    # 4) 大盘状态 (V2 stage 1)
    logger.info("Computing market state (V2)...")
    from quant_app.models.market_direction_v2 import predict as predict_market
    market_records = []
    for d in all_dates:
        try:
            m = predict_market(conn, d.strftime('%Y-%m-%d'))
            market_records.append({
                'trade_date': d,
                'market_dir': m['direction'],
                'market_prob': m['prob'],
                'market_er': m['expected_return'],
            })
        except: continue
    df_market = pd.DataFrame(market_records)
    # 转 dummy
    if len(df_market) > 0:
        df_market = pd.get_dummies(df_market, columns=['market_dir'], prefix='mkt')
    logger.info(f"  Market: {len(df_market)} records")

    # 5) 板块动量 (从 sector_moneyflow)
    cur.execute("""SELECT trade_date, sector_name, net_amount, pct_change
                  FROM sector_moneyflow WHERE trade_date BETWEEN %s AND %s""", (start, end))
    sec_rows = cur.fetchall()
    df_sec = pd.DataFrame(sec_rows, columns=['trade_date', 'sector', 'net_amount', 'pct_change'])
    df_sec['trade_date'] = pd.to_datetime(df_sec['trade_date'])
    # 5/10 日动量
    df_sec = df_sec.sort_values(['sector', 'trade_date'])
    df_sec['sec_mom_5d'] = df_sec.groupby('sector')['pct_change'].transform(lambda s: s.rolling(5, min_periods=2).sum())
    df_sec['sec_mom_20d'] = df_sec.groupby('sector')['pct_change'].transform(lambda s: s.rolling(20, min_periods=5).sum())
    df_sec['sec_net_5d'] = df_sec.groupby('sector')['net_amount'].transform(lambda s: s.rolling(5, min_periods=2).sum())
    df_sec = df_sec[['trade_date', 'sector', 'sec_mom_5d', 'sec_mom_20d', 'sec_net_5d']].drop_duplicates(['trade_date', 'sector'])
    logger.info(f"  Sector: {len(df_sec)} rows")

    # 6) 涨停股
    cur.execute("""SELECT trade_date, ts_code, open_times, first_time
                  FROM limit_list_d WHERE trade_date BETWEEN %s AND %s""", (start, end))
    limit_rows = cur.fetchall()
    df_limit = pd.DataFrame(limit_rows, columns=['trade_date', 'ts_code', 'open_times', 'first_time'])
    df_limit['trade_date'] = pd.to_datetime(df_limit['trade_date'])
    df_limit['is_limit_up'] = 1
    # 20 日涨停次数
    df_limit = df_limit.sort_values(['ts_code', 'trade_date'])
    df_limit['limit_up_20d'] = df_limit.groupby('ts_code')['is_limit_up'].transform(lambda s: s.rolling(20, min_periods=1).sum())
    df_limit = df_limit[['trade_date', 'ts_code', 'open_times', 'limit_up_20d']].drop_duplicates(['ts_code', 'trade_date'])
    logger.info(f"  Limit: {len(df_limit)} rows")

    # 7) 计算基础因子
    logger.info("Computing base factors...")
    pieces = []
    n_stocks = df_price['ts_code'].nunique()
    for i, (code, g) in enumerate(df_price.groupby('ts_code', sort=False)):
        if len(g) < 30: continue
        g = g.sort_values('trade_date').reset_index(drop=True)
        close = g['close'].astype(float)
        high = g['high'].astype(float)
        low = g['low'].astype(float)
        vol = g['vol'].astype(float)
        pct = g['pct_chg'].astype(float)

        out = pd.DataFrame()
        out['trade_date'] = g['trade_date']
        out['ts_code'] = g['ts_code']
        out['industry'] = g['industry'].iloc[0]

        # 收益
        for n in [1, 3, 5, 10, 20]:
            out[f'ret_{n}d'] = (close / close.shift(n) - 1) * 100
        out['high_52w_pct'] = (close / high.rolling(252, min_periods=20).max() - 1) * 100
        # 波动
        for n in [5, 10, 20]:
            out[f'vol_{n}d'] = pct.rolling(n, min_periods=2).std() * np.sqrt(252)
        out['turnover_5d'] = g['turnover_rate'].rolling(5).mean()
        out['amount_ratio'] = g['amount'].rolling(5).mean() / g['amount'].rolling(20, min_periods=10).mean()
        out['up_ratio_5d'] = (pct > 0).rolling(5).mean()
        # 技术
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        out['macd_hist'] = (ema12 - ema26 - (ema12 - ema26).ewm(span=9, adjust=False).mean()) * 2
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        out['rsi_14'] = 100 - (100 / (1 + rs))
        out['ma_diff'] = (close.rolling(5).mean() / close.rolling(20).mean() - 1) * 100
        bb_mid = close.rolling(20).mean()
        bb_std = close.rolling(20).std()
        out['boll_pos'] = (close - bb_mid) / (bb_std * 2 + 1e-6)
        out['vol_corr_10d'] = close.rolling(10).corr(vol)
        out['amp_10d'] = ((high - low) / close.shift(1) * 100).rolling(10).mean()
        out['vol_breakout'] = vol.rolling(5).mean() / vol.rolling(20).mean()
        # 目标
        for n in [3, 5, 10]:
            out[f'target_ret_{n}d'] = (close.shift(-n) / close - 1) * 100

        pieces.append(out)
        if (i+1) % 1000 == 0:
            logger.info(f"  {i+1}/{n_stocks} stocks")

    df = pd.concat(pieces, ignore_index=True)
    logger.info(f"  Base: {len(df)} rows")

    # 8) 合并 V11 / 大盘 / 板块 / 涨停
    logger.info("Merging V11 + market + sector + limit_up features...")
    df = df.merge(df_v11, on=['trade_date', 'ts_code'], how='left')
    df['v11_prob'] = df['v11_prob'].fillna(0.5)
    df['v11_pred_ret'] = df['v11_pred_ret'].fillna(0)
    df = df.merge(df_market, on='trade_date', how='left')
    for c in [col for col in df_market.columns if col.startswith('mkt_')]:
        df[c] = df[c].fillna(0)
    df = df.merge(df_sec, left_on=['trade_date', 'industry'], right_on=['trade_date', 'sector'], how='left')
    for c in ['sec_mom_5d', 'sec_mom_20d', 'sec_net_5d']:
        df[c] = df[c].fillna(0)
    df = df.drop(columns=['sector'], errors='ignore')
    df = df.merge(df_limit, on=['trade_date', 'ts_code'], how='left')
    df['open_times'] = df['open_times'].fillna(0)
    df['limit_up_20d'] = df['limit_up_20d'].fillna(0)
    logger.info(f"  After merge: {df.shape}")

    # 9) 行业相对收益
    logger.info("Computing industry-relative features...")
    for n in [3, 5, 10]:
        df[f'ind_ret_{n}d'] = df.groupby(['trade_date', 'industry'])[f'target_ret_{n}d'].transform('mean')
        df[f'ret_{n}d_industry_rel'] = df[f'target_ret_{n}d'] - df[f'ind_ret_{n}d']
    # 行业内 rank
    df['ret_5d_rank'] = df.groupby(['trade_date', 'industry'])['target_ret_5d'].rank(pct=True)

    # 10) 采样
    sample_dates = all_dates[::sample_freq]
    df = df[df['trade_date'].isin(sample_dates)].copy()
    logger.info(f"  After sample: {len(df)} rows")

    # 11) 填充 + 保存
    feature_cols = [c for c in df.columns if c not in
                    ['trade_date', 'ts_code', 'industry', 'sector',
                     'target_ret_3d', 'target_ret_5d', 'target_ret_10d',
                     'ind_ret_3d', 'ind_ret_5d', 'ind_ret_10d',
                     'ret_3d_industry_rel', 'ret_5d_industry_rel',
                     'ret_3d_industry_relative', 'ret_5d_industry_rank', 'ret_5d_industry_relative']]
    for c in feature_cols:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    out_path = OUTPUT_DIR / f"factor_v2_{start}_{end}.parquet"
    df.to_parquet(out_path, index=False)
    logger.info(f"Saved → {out_path}")
    logger.info(f"  {len(df)} rows, {len(feature_cols)} features")
    return df, feature_cols


if __name__ == '__main__':
    df, feats = build_v2(start='2024-01-01', end='2026-06-09', sample_freq=1)
    print("\n=== Dataset V2 summary ===")
    print(f"  Rows: {len(df)}")
    print(f"  Features: {len(feats)}")
    print(f"  Date range: {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    print(f"  Stock count: {df['ts_code'].nunique()}")
    print(f"  Industry count: {df['industry'].nunique()}")
    print(f"  Features: {feats}")
