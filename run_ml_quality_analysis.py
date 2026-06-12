#!/usr/bin/env python3
"""
验证假设：ML 排序质量提升对 V4+ML 收益的影响
方法论：
- 对每个采样日，取 V4 初筛 30 只候选股
- 用它们的真实前向收益作为"完美排序"的基准
- 模拟不同精度的 ML 排序（α 从 0% 到 100%，α 越高质量越好）
- 对比 V4+Random、V4+V11、V4+Perfect 的累积收益曲线
"""
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import joblib
import pymysql
from sqlalchemy import create_engine

from ml_predict import _ensemble_predict
from quant_app.services.strategy_service import _v4_score_single
from quant_app.utils.config import get_db_config
from quant_app.utils.model_loader import get_model_path
from scripts.predict_v11 import build_features_v11_inference

DB_CONFIG = get_db_config()
DB_CONFIG.pop('unix_socket', None)
DB_CONFIG['host'] = '127.0.0.1'
DB_CONFIG['port'] = 3306
ENGINE = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset=utf8mb4",
    pool_pre_ping=True, pool_size=5
)

START_DATE, END_DATE = "2024-11-01", "2026-05-15"
SAMPLE_INTERVAL, TOP_N, HOLD_DAYS = 5, 3, 5

# Load V11 model
model_path = get_model_path("v11.0")
bundle = joblib.load(model_path)

# Get trade dates
all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' ORDER BY trade_date",
    ENGINE
)['trade_date'].astype(str).tolist())
sample_dates = [d for d in all_dates[::SAMPLE_INTERVAL] if d > all_dates[5]]
logger.info(f"Sample dates: {len(sample_dates)}")

conn = pymysql.connect(**DB_CONFIG)

# Store results for each alpha level
# alpha = 0 → pure random, alpha = 1 → perfect ML (sorted by forward return)
alphas = [0.0, 0.25, 0.50, 0.75, 0.90, 1.0]
results = {a: [] for a in alphas}
v11_results_v4ml = []
v11_results_pureml = []
synth_v11_results = []  # synthetic V11 quality: use actual V11 ranking within V4 pool

for di, buy_date in enumerate(sample_dates):
    if (di + 1) % 10 == 0:
        logger.info(f"Progress: {di+1}/{len(sample_dates)}")

    # Volume-sorted candidates from yesterday
    prev_date = pd.read_sql(
        f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date < '{buy_date}'", ENGINE
    ).iloc[0, 0]
    top_codes = pd.read_sql(f"""
        SELECT ts_code FROM daily_price
        WHERE trade_date = '{prev_date}' AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200
        ORDER BY amount DESC LIMIT 500
    """, ENGINE)['ts_code'].tolist()
    if len(top_codes) < 100:
        continue

    # Build features & ML prediction
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

    # V4 score candidates
    v4_picks = []
    for _, row in feat.iterrows():
        sc = _v4_score_single(row)
        if sc >= 0:
            v4_picks.append((row['ts_code'], sc))
    v4_picks.sort(key=lambda x: -x[1])
    v4_top30 = [c for c, _ in v4_picks[:30]]
    if len(v4_top30) < TOP_N:
        continue

    # Get forward returns for ALL V4 candidates
    fwd_rets = {}
    for tc in v4_top30:
        ret_df = pd.read_sql(f"""
            SELECT pct_chg FROM daily_price
            WHERE ts_code = '{tc}' AND trade_date > '{buy_date}'
            ORDER BY trade_date LIMIT {HOLD_DAYS}
        """, ENGINE)
        if len(ret_df) < 2:
            continue
        rets = ret_df['pct_chg'].values[:HOLD_DAYS] / 100.0
        rets = rets[~np.isnan(rets)]
        if len(rets) == 0:
            continue
        fwd_rets[tc] = float((1 + rets).prod() - 1) * 100

    if len(fwd_rets) < TOP_N:
        continue

    # Actual V11 ranking within V4 pool (this is what production uses)
    v4_ml_scores = {c: ml_map.get(c, 0) for c in v4_top30 if c in ml_map}
    v11_ranked = sorted(v4_ml_scores.keys(), key=lambda x: v4_ml_scores[x], reverse=True)[:TOP_N]
    v11_ret = np.mean([fwd_rets.get(c, 0) for c in v11_ranked])
    v11_results_v4ml.append({'date': buy_date, 'avg_ret': round(v11_ret, 2)})

    # Pure ML (for reference, from full 500 pool)
    pure_ml_top = sorted(codes, key=lambda x: ml_map.get(x, 0), reverse=True)[:TOP_N]
    pure_ret = np.mean([fwd_rets.get(c, 0) for c in pure_ml_top if c in fwd_rets])
    if not np.isnan(pure_ret):
        v11_results_pureml.append({'date': buy_date, 'avg_ret': round(pure_ret, 2)})

    # For each alpha level, simulate ML of that quality
    for alpha in alphas:
        # Generate simulated ML scores: blend of random and perfect
        # alpha=0: pure random, alpha=1: perfect (sorted by forward return)
        sim_scores = {}
        for c in v4_top30:
            if c not in fwd_rets:
                continue
            # "Perfect score" = forward return itself
            perfect = fwd_rets[c]
            # Random score: uniform [0, 1]
            random = np.random.random()
            # Blend: alpha * perfect + (1-alpha) * random
            # But perfect is in percentage (e.g. 2.5), random is [0,1]
            # We need to normalize: rank-based blend
            sim_scores[c] = (1 - alpha) * random + alpha * (perfect / 100 + 0.5)

        # Simulate non-determinism: average over 5 trials for alpha < 1
        n_trials = 1 if alpha >= 1.0 else 5
        trial_rets = []
        for _ in range(n_trials):
            if alpha < 1.0:
                # Resample random scores
                sim_scores = {}
                for c in v4_top30:
                    if c not in fwd_rets:
                        continue
                    perfect = fwd_rets[c]
                    random = np.random.random()
                    sim_scores[c] = (1 - alpha) * random + alpha * (perfect / 100 + 0.5)
            sim_top = sorted(sim_scores.keys(), key=lambda x: sim_scores[x], reverse=True)[:TOP_N]
            trial_rets.append(np.mean([fwd_rets.get(c, 0) for c in sim_top]))
        results[alpha].append({'date': buy_date, 'avg_ret': round(float(np.mean(trial_rets)), 2)})

conn.close()

# ===== Results =====
print(f"\n{'='*65}")
print("ML 排序精度对 V4+ML 策略收益的影响")
print(f"{'='*65}")
print(f"区间: {START_DATE} ~ {END_DATE}, 采样: 每{SAMPLE_INTERVAL}天, 持仓: {HOLD_DAYS}天, Top{TOP_N}")
print("说明: α=ML排序准确度 (0%=完全随机 → 100%=完美排序)")
print()

# V11 actual results
v11_rets = np.array([r['avg_ret'] for r in v11_results_v4ml])
v11_cum = float((1 + v11_rets / 100).prod() - 1) * 100
v11_win = int((v11_rets > 0).sum())
v11_sharpe = float(v11_rets.mean() / v11_rets.std() * np.sqrt(252 / HOLD_DAYS)) if v11_rets.std() > 0 else 0
v11_dd = float(min(0, (v11_rets / 100).min()))

pure_rets = np.array([r['avg_ret'] for r in v11_results_pureml])
pure_cum = float((1 + pure_rets / 100).prod() - 1) * 100 if len(pure_rets) > 0 else 0

print(f"  {'策略':<25} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>10} {'样本':>6}")
print(f"  {'-'*67}")
print(f"  {'V4 + 纯随机(α=0%)':<25}   ...正在计算...")
print(f"  {'V4 + V11(α≈40%)':<25} {v11_cum:>+10.2f}% {v11_win/len(v11_rets)*100:>7.1f}% {v11_sharpe:>8.2f} {v11_dd*100:>9.2f}% {len(v11_rets):>6d}")
print(f"  {'纯 ML V11':<25} {pure_cum:>+10.2f}% {'':>8} {'':>8} {'':>10} {len(pure_rets):>6d}")
print()

print(f"  ML 提升模拟 (V4 候选池内 Top{TOP_N}):")
print(f"  {'α(精度)':<12} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>10} {'均值':>8}")
print(f"  {'-'*56}")

for alpha in alphas:
    store = results[alpha]
    if not store:
        continue
    rets = np.array([r['avg_ret'] for r in store])
    cum = float((1 + rets / 100).prod() - 1) * 100
    wins = int((rets > 0).sum())
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252 / HOLD_DAYS)) if std > 0 else 0
    dd = float(min(0, (rets / 100).min()))
    label = f"{alpha*100:.0f}%"
    if alpha == 0: label += " (随机)"
    elif alpha == 0.5: label += " (部分)"
    elif alpha == 1.0: label += " (完美)"
    print(f"  {label:<12} {cum:>+10.2f}% {wins/len(rets)*100:>7.1f}% {sharpe:>8.2f} {dd*100:>9.2f}% {avg:>+7.2f}%")

# Save
output = {
    'v4_plus_v11_actual': {'cum_ret': round(v11_cum, 2), 'sharpe': round(v11_sharpe, 2), 'win_rate': round(v11_win/len(v11_rets)*100, 1), 'max_dd': round(v11_dd*100, 2)},
    'pure_ml_v11_actual': {'cum_ret': round(pure_cum, 2)},
    'simulation': {f'alpha_{int(a*100)}': {
        'cum_ret': round(float((1 + np.array(results[a])['avg_ret'] / 100).prod() - 1) * 100, 2) if results[a] else 0,
        'n_samples': len(results[a])
    } for a in alphas},
}
out_path = os.path.join(BASE_DIR, 'data', 'ml_quality_analysis.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
