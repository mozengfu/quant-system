#!/usr/bin/env python3
"""
ML 排序精度验证（快速版）
- 对每个采样日，取 V4 候选池 + 前向收益
- 模拟不同排序精度对 Top3 收益的影响
- 不需要重新构建 ML 特征，直接用 V4 候选池数据
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

from sqlalchemy import create_engine

from quant_app.services.strategy_service import _v4_score_single
from quant_app.utils.config import get_db_config

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

np.random.seed(42)

# Get trade dates
all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' ORDER BY trade_date",
    ENGINE
)['trade_date'].astype(str).tolist())
sample_dates = [d for d in all_dates[::SAMPLE_INTERVAL] if d > all_dates[5]]
logger.info(f"Sample dates: {len(sample_dates)}")

results = {}

for di, buy_date in enumerate(sample_dates):
    if (di + 1) % 10 == 0:
        logger.info(f"Progress: {di+1}/{len(sample_dates)}")

    # Volume-sorted candidates from yesterday (to stay consistent with backtest)
    prev_date = pd.read_sql(
        f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date < '{buy_date}'", ENGINE
    ).iloc[0, 0]

    # Get daily data from DB for V4 scoring
    df = pd.read_sql(f"""
        SELECT ts_code, close, pct_chg, volume_ratio, turnover_rate,
               ma5, ma10, ma20, rps_20, high_52w, low_52w,
               amount
        FROM daily_price
        WHERE trade_date = '{prev_date}'
          AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200
        ORDER BY amount DESC LIMIT 500
    """, ENGINE)

    if df.empty:
        continue

    # V4 scoring
    v4_ranked = []
    v4_candidates_meta = []
    for _, row in df.iterrows():
        sc = _v4_score_single(row)
        ts_code = row['ts_code']
        if sc >= 0:
            v4_ranked.append((ts_code, sc))
    v4_ranked.sort(key=lambda x: -x[1])
    v4_top30 = [c for c, _ in v4_ranked[:30]]

    if len(v4_top30) < TOP_N:
        continue

    # Get forward returns for each V4 candidate
    codes_str = ','.join([f"'{c}'" for c in v4_top30])
    fwd = pd.read_sql(f"""
        SELECT ts_code, pct_chg, trade_date FROM daily_price
        WHERE ts_code IN ({codes_str})
          AND trade_date > '{buy_date}'
        ORDER BY ts_code, trade_date
    """, ENGINE)

    fwd_rets = {}
    for tc in v4_top30:
        ts_fwd = fwd[fwd['ts_code'] == tc]['pct_chg'].values[:HOLD_DAYS] / 100.0
        ts_fwd = ts_fwd[~np.isnan(ts_fwd)]
        if len(ts_fwd) >= 2:
            fwd_rets[tc] = float((1 + ts_fwd).prod() - 1) * 100

    valid_codes = [c for c in v4_top30 if c in fwd_rets]
    if len(valid_codes) < TOP_N:
        continue

    fwd_values = np.array([fwd_rets[c] for c in valid_codes])

    # === Simulate different ML quality levels ===
    # We model ML score as: score = alpha * truth + (1-alpha) * noise
    # where alpha is the "ML quality" parameter
    # truth = forward return (normalized)
    # noise = random uniform noise

    truth_normalized = (fwd_values - fwd_values.mean()) / fwd_values.std()
    if np.std(truth_normalized) == 0:
        continue

    # V11 actual: use the actual ML predictions from the earlier backtest
    # We can't reproduce them here without the model, so we skip this
    # Instead, we simulate across alpha values

    for alpha_pct in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        alpha = alpha_pct / 100.0
        key = f"V4+ML(α={alpha_pct}%)"

        if key not in results:
            results[key] = {'rets': [], 'dates': []}

        # Simulate ML scores (average over n_trials for non-deterministic alphas)
        n_trials = 20 if alpha < 1.0 else 1
        trial_avgs = []
        for _ in range(n_trials):
            noise = np.random.normal(0, 1, len(valid_codes))
            sim_scores = alpha * truth_normalized + (1 - alpha) * noise

            # Pick Top3 by simulated score
            top3_idx = np.argsort(sim_scores)[-TOP_N:]
            top3_ret = np.mean([fwd_rets[valid_codes[i]] for i in top3_idx])
            trial_avgs.append(top3_ret)

        results[key]['rets'].append(float(np.mean(trial_avgs)))
        results[key]['dates'].append(buy_date)

# ===== Print Results =====
print(f"\n{'='*70}")
print("ML 排序精度对 V4+ML 策略收益的影响")
print(f"{'='*70}")
print(f"区间: {START_DATE} ~ {END_DATE} | 持仓: {HOLD_DAYS}天 | Top{TOP_N}")
print("α = ML 排序准确度 (0%=纯随机 → 100%=完美排序)")
print()

header = f"{'策略':<22} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>8} {'均值':>7} {'样本':>6}"
print(header)
print(f"{'-'*69}")

for alpha_pct in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
    key = f"V4+ML(α={alpha_pct}%)"
    if key not in results:
        continue
    rets = np.array(results[key]['rets'])
    cum = float((1 + rets / 100).prod() - 1) * 100
    wins = int((rets > 0).sum())
    total = len(rets)
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252 / HOLD_DAYS)) if std > 0 else 0
    dd = float(min(0, (rets / 100).min()))

    label = f"α={alpha_pct}%"
    if alpha_pct == 0: label += " 随机"
    elif alpha_pct == 50: label += " 半精度"
    elif alpha_pct == 100: label += " 完美"

    print(f"{label:<22} {cum:>+10.2f}% {wins/total*100:>7.1f}% {sharpe:>8.2f} {dd*100:>8.2f}% {avg:>+6.2f}% {total:>6d}")

print()
print(f"{'='*70}")
print("结论：α 每提升一定百分比，V4+ML 累积收益的提升比例")
print(f"{'='*70}")

# Compute marginal benefit
prev_cum = 0
for alpha_pct in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
    key = f"V4+ML(α={alpha_pct}%)"
    if key not in results:
        continue
    rets = np.array(results[key]['rets'])
    cum = float((1 + rets / 100).prod() - 1) * 100
    if prev_cum != 0:
        gain = cum - prev_cum
        print(f"  α {alpha_pct-10}% → {alpha_pct}%: +{gain:+.2f}pp 累积收益")
    prev_cum = cum

# Save
output = {
    'params': {'start': START_DATE, 'end': END_DATE, 'hold_days': HOLD_DAYS, 'top_n': TOP_N},
    'results': {
        k: {
            'cum_ret': round(float((1 + np.array(v['rets'])/100).prod() - 1) * 100, 2),
            'sharpe': round(np.mean(v['rets']) / np.std(v['rets']) * np.sqrt(252/HOLD_DAYS), 2) if np.std(v['rets']) > 0 else 0,
            'win_rate': round(int((np.array(v['rets']) > 0).sum()) / len(v['rets']) * 100, 1),
            'max_dd': round(min(0, (np.array(v['rets'])/100).min()) * 100, 2),
            'n_samples': len(v['rets']),
        } for k, v in results.items()
    }
}
out_path = os.path.join(BASE_DIR, 'data', 'ml_quality_analysis.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
