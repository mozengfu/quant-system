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
import pandas as pd
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
    compute_pool_forward_returns,
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
pure_ml_filtered_results = []  # 风控过滤后

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

        # ---- 风控过滤：全候选池过滤，再取前 TOP_N ----
        risk_q = ','.join(["'" + c + "'" for c in valid_all])
        risk_df = pd.read_sql(f"""
            SELECT d.ts_code, d.close, d.pct_chg, d.volume_ratio, d.rps_20, d.high_52w, d.low_52w
            FROM daily_price d
            WHERE d.trade_date = '{prev_date}' AND d.ts_code IN ({risk_q})
        """, ENGINE)
        risk_info = {}
        for _, r in risk_df.iterrows():
            tc = r['ts_code']
            close = float(r.get('close') or 0)
            pct = float(r.get('pct_chg') or 0)
            vr = float(r.get('volume_ratio') or 0)
            rps = float(r.get('rps_20') or 0)
            h52w = float(r.get('high_52w') or 0)
            l52w = float(r.get('low_52w') or 0)
            reasons = []
            if pct > 9:
                reasons.append('涨停追高')
            if h52w > l52w > 0:
                pos = (close - l52w) / (h52w - l52w) * 100
                if pos > 85:
                    reasons.append('52周高位')
            if pct > 5 and vr > 5:
                reasons.append('异常放量')
            if rps > 95 and pct > 4:
                reasons.append('RPS过热')
            risk_info[tc] = reasons

        # 严格模式：全风控排除 → 取前 TOP_N
        passed_ml = [c for c in valid_all if len(risk_info.get(c, [])) == 0]
        passed_ml = sorted(passed_ml, key=lambda x: ml_map[x], reverse=True)
        # 宽松模式：仅排除涨停追高
        relaxed_ml = [c for c in valid_all if '涨停追高' not in risk_info.get(c, [])]
        relaxed_ml = sorted(relaxed_ml, key=lambda x: ml_map[x], reverse=True)

        if len(passed_ml) >= TOP_N:
            selected = passed_ml[:TOP_N]
        elif len(relaxed_ml) >= TOP_N:
            selected = relaxed_ml[:TOP_N]
        else:
            selected = valid_all[:TOP_N]

        pure_ml_filtered_results.append({'date': buy_date, 'avg_ret': round(np.mean([fwd_rets[c] for c in selected]), 2)})

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

header = f"{'策略':<25} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>8} {'均值':>7} {'样本':>6}"
print(header)
print(f"{'-'*72}")

# Pure ML (无风控)
pr = np.array([r['avg_ret'] for r in pure_ml_results])
pc = float((1 + pr / 100).prod() - 1) * 100
pw = int((pr > 0).sum())
ps = float(pr.mean() / pr.std() * np.sqrt(252/HOLD_DAYS)) if pr.std() > 0 else 0
pd_ = float(min(0, (pr / 100).min()))
print(f"{'Pure ML (无风控)':<25} {pc:>+10.2f}% {pw/len(pr)*100:>7.1f}% {ps:>8.2f} {pd_*100:>8.2f}% {pr.mean():>+6.2f}% {len(pr):>6d}")

# Pure ML (有风控)
if pure_ml_filtered_results:
    pfr = np.array([r['avg_ret'] for r in pure_ml_filtered_results])
    pfc = float((1 + pfr / 100).prod() - 1) * 100
    pfw = int((pfr > 0).sum())
    pfs = float(pfr.mean() / pfr.std() * np.sqrt(252/HOLD_DAYS)) if pfr.std() > 0 else 0
    pfd = float(min(0, (pfr / 100).min()))
    print(f"{'Pure ML (有风控)':<25} {pfc:>+10.2f}% {pfw/len(pfr)*100:>7.1f}% {pfs:>8.2f} {pfd*100:>8.2f}% {pfr.mean():>+6.2f}% {len(pfr):>6d}")

for size in pool_sizes:
    store = results[size]
    if not store:
        continue
    rets = np.array([r['avg_ret'] for r in store])
    cum = float((1 + rets / 100).prod() - 1) * 100
    wins = int((rets > 0).sum())
    total = len(rets)
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252/HOLD_DAYS)) if std > 0 else 0
    dd = float(min(0, (rets / 100).min()))
    print(f"{'V4 Pool='+str(size)+' + ML Top3':<25} {cum:>+10.2f}% {wins/total*100:>7.1f}% {sharpe:>8.2f} {dd*100:>8.2f}% {avg:>+6.2f}% {total:>6d}")

# Save
output = {'params': {'interval': SAMPLE_INTERVAL, 'top_n': TOP_N, 'hold_days': HOLD_DAYS},
          'pure_ml': pure_ml_results,
          'pure_ml_filtered': pure_ml_filtered_results,
          'v4_ml': {str(s): results[s] for s in pool_sizes}}
out_path = os.path.join(BASE_DIR, 'data', 'backtest_v4_pool.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
