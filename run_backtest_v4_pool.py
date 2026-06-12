#!/usr/bin/env python3
"""
V4+ML 候选池大小对比回测
测试：V4 初筛不同规模候选池 (30/50/100/200/500) → ML 排序 Top3 的收益差异
"""
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings('ignore')
import numpy as np
import pymysql
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import joblib
from sqlalchemy import create_engine

from ml_predict import _ensemble_predict
from quant_app.backtest.utils import (
    backtest_stats,
    compute_pool_forward_returns,
    format_backtest_table,
    get_candidate_pool,
    get_prev_trade_date,
    get_trade_dates,
)
from quant_app.services.strategy_service import _v4_score_single
from quant_app.utils.config import config, get_db_config
from quant_app.utils.model_loader import get_model_path
from scripts.predict_v11 import build_features_v11_inference

DB_CONFIG = get_db_config()
ENGINE = create_engine(config.mysql.url, pool_pre_ping=True, pool_size=5)

model_path = get_model_path("v11.0")
bundle = joblib.load(model_path)
logger.info(f"V11.0 loaded: {bundle.get('n_models','?')} models, {len(bundle.get('feature_cols',[]))} features")

START_DATE, END_DATE = "2024-11-01", "2026-05-15"
SAMPLE_INTERVAL, TOP_N, HOLD_DAYS = 5, 3, 5

all_dates = get_trade_dates(ENGINE, START_DATE, END_DATE)
sample_dates = [d for d in all_dates[::SAMPLE_INTERVAL] if d > all_dates[5]]
logger.info(f"Sample dates: {len(sample_dates)}")

conn = pymysql.connect(**DB_CONFIG)

# Pool sizes to test
pool_sizes = [30, 50, 100, 200, 500]
results = {size: [] for size in pool_sizes}
pure_ml_results = []

for di, buy_date in enumerate(sample_dates):
    if (di + 1) % 10 == 0:
        logger.info(f"Progress: {di+1}/{len(sample_dates)}")

    prev_date = get_prev_trade_date(ENGINE, buy_date)
    if not prev_date:
        continue
    top_codes = get_candidate_pool(ENGINE, prev_date, limit=500)
    if len(top_codes) < 100:
        continue

    try:
        feat = build_features_v11_inference(conn, top_codes, as_of_date=buy_date)
    except Exception:
        continue
    if feat is None or feat.empty or len(feat) < 50:
        continue

    v80f = bundle['feature_cols']
    medians = bundle.get('global_medians', {})
    for col in v80f:
        if col not in feat.columns:
            feat[col] = medians.get(col, 0.0)
    feat = feat.fillna(0)
    ml_preds = _ensemble_predict(feat, bundle)
    codes = feat['ts_code'].tolist()
    ml_map = dict(zip(codes, ml_preds))

    # V4 scores
    v4_picks = []
    for _, row in feat.iterrows():
        sc = _v4_score_single(row)
        if sc >= 0:
            v4_picks.append((row['ts_code'], sc))
    v4_picks.sort(key=lambda x: -x[1])

    # Forward returns
    fwd_rets = compute_pool_forward_returns(ENGINE, codes, buy_date, HOLD_DAYS)

    # Pure ML (from full 500 pool)
    valid_all = [c for c in codes if c in fwd_rets and c in ml_map]
    if len(valid_all) >= TOP_N:
        pure_top = sorted(valid_all, key=lambda x: ml_map[x], reverse=True)[:TOP_N]
        pure_ml_results.append({'date': buy_date, 'avg_ret': round(np.mean([fwd_rets[c] for c in pure_top]), 2)})

    # V4+ML with different pool sizes
    for pool_size in pool_sizes:
        v4_candidates = [c for c, _ in v4_picks[:pool_size]]
        valid = [c for c in v4_candidates if c in fwd_rets and c in ml_map]
        if len(valid) < TOP_N:
            continue
        ml_ranked = sorted(valid, key=lambda x: ml_map[x], reverse=True)[:TOP_N]
        results[pool_size].append({'date': buy_date, 'avg_ret': round(np.mean([fwd_rets[c] for c in ml_ranked]), 2)})

conn.close()

# ===== Results =====
print(f"\n{'='*70}")
print("V4 候选池大小对 V4+ML 收益的影响")
print(f"{'='*70}")
print(f"区间: {START_DATE} ~ {END_DATE} | 持仓: {HOLD_DAYS}天 | Top{TOP_N}")
print()

pure_stats = backtest_stats(pure_ml_results)
all_stats = [("Pure ML (无V4)", pure_stats)]
for size in pool_sizes:
    s = backtest_stats(results[size])
    all_stats.append((f"V4 Pool={size} + ML Top3", s))

print(format_backtest_table(all_stats))

# Save
output = {
    'params': {'interval': SAMPLE_INTERVAL, 'top_n': TOP_N, 'hold_days': HOLD_DAYS},
    'pure_ml': pure_ml_results,
    'v4_ml': {str(s): results[s] for s in pool_sizes},
    'stats': {name: s for name, s in all_stats},
}
out_path = os.path.join(BASE_DIR, 'data', 'backtest_v4_pool.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
