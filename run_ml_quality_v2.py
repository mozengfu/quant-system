#!/usr/bin/env python3
"""
ML 排序精度验证 V2 — 基于真实 V11 模型预测的排序质量
- 在每个采样日：V4 候选 30 只 → V11 模型预测并排序 → 取 Top3
- 测量 V11 预测值与前向收益的 Spearman 秩相关
- 然后模拟不同 IC 提升幅度对策略收益的影响
"""
import os, sys, json, logging, warnings
warnings.filterwarnings('ignore')
import numpy as np, pandas as pd
from scipy.stats import spearmanr
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.model_loader import get_model_path
from quant_app.utils.config import get_db_config
from ml_predict import _ensemble_predict
from quant_app.services.strategy_service import _v4_score_single
from scripts.predict_v11 import build_features_v11_inference
from sqlalchemy import create_engine
import joblib, pymysql

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
np.random.seed(42)

all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' ORDER BY trade_date",
    ENGINE
)['trade_date'].astype(str).tolist())
sample_dates = [d for d in all_dates[::SAMPLE_INTERVAL] if d > all_dates[5]]
logger.info(f"Sample dates: {len(sample_dates)}")

conn = pymysql.connect(**DB_CONFIG)

actual_rets = []       # V4 + V11 actual
random_rets = []       # V4 + random
ic_values = []         # Spearman IC per date
improved_rets = {f"+{pct}%IC": [] for pct in [10, 20, 30, 50]}

for di, buy_date in enumerate(sample_dates):
    if (di + 1) % 10 == 0:
        logger.info(f"Progress: {di+1}/{len(sample_dates)}")

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

    # V4 candidates
    v4_picks = []
    for _, row in feat.iterrows():
        sc = _v4_score_single(row)
        if sc >= 0:
            v4_picks.append((row['ts_code'], sc))
    v4_picks.sort(key=lambda x: -x[1])
    v4_top30 = [c for c, _ in v4_picks[:30]]
    if len(v4_top30) < TOP_N:
        continue

    # Forward returns
    codes_str = ','.join(f"'{c}'" for c in v4_top30)
    fwd = pd.read_sql(f"""
        SELECT ts_code, pct_chg FROM daily_price
        WHERE ts_code IN ({codes_str}) AND trade_date > '{buy_date}'
        ORDER BY ts_code, trade_date
    """, ENGINE)

    fwd_rets = {}
    for tc in v4_top30:
        ts_fwd = fwd[fwd['ts_code'] == tc]['pct_chg'].values[:HOLD_DAYS] / 100.0
        ts_fwd = ts_fwd[~np.isnan(ts_fwd)]
        if len(ts_fwd) >= 2:
            fwd_rets[tc] = float((1 + ts_fwd).prod() - 1) * 100

    valid = [c for c in v4_top30 if c in fwd_rets and c in ml_map]
    if len(valid) < TOP_N:
        continue

    # === Measure V11 IC within V4 pool ===
    v11_scores = np.array([ml_map[c] for c in valid])
    fwd_vals = np.array([fwd_rets[c] for c in valid])
    ic, _ = spearmanr(v11_scores, fwd_vals)
    if np.isnan(ic):
        ic = 0.0
    ic_values.append(ic)

    # === Actual V4+V11 ===
    v11_ranked = sorted(valid, key=lambda x: ml_map[x], reverse=True)[:TOP_N]
    actual_rets.append(np.mean([fwd_rets[c] for c in v11_ranked]))

    # === V4 + Random ===
    random_top = np.random.choice(valid, TOP_N, replace=False)
    random_rets.append(np.mean([fwd_rets[c] for c in random_top]))

    # === V4 + Improved IC ===
    for pct in [10, 20, 30, 50]:
        target_ic = min(ic * (1 + pct/100), 0.99)
        if target_ic <= ic or target_ic <= 0:
            improved_rets[f"+{pct}%IC"].append(np.mean([fwd_rets[c] for c in v11_ranked]))
            continue
        
        # Generate improved scores with target IC
        n_trials = 10
        trial_rets = []
        for _ in range(n_trials):
            noise = np.random.normal(0, 1, len(valid))
            # Create improved signal with target IC
            current_std = np.std(v11_scores)
            noise_std = np.std(noise)
            alpha = target_ic / max(abs(ic), 0.001)
            improved = v11_scores * alpha + noise * (1 - alpha) * current_std / max(noise_std, 0.001)
            improved_top = [valid[i] for i in np.argsort(improved)[-TOP_N:]]
            trial_rets.append(np.mean([fwd_rets[c] for c in improved_top]))
        improved_rets[f"+{pct}%IC"].append(np.mean(trial_rets))

conn.close()

# === Results ===
avg_ic = float(np.mean(ic_values)) if ic_values else 0
print(f"\n{'='*70}")
print(f"V11 模型在 V4 候选池内的排序质量分析")
print(f"{'='*70}")
print(f"区间: {START_DATE} ~ {END_DATE} | 持仓: {HOLD_DAYS}天 | Top{TOP_N}")
print(f"V4 候选池平均规模: ~30只, 采样次数: {len(actual_rets)}")
print(f"V11 Spearman IC (V4池内): {avg_ic:.4f}")
print()

# V4+Random
r_rets = np.array(random_rets)
r_cum = float((1 + r_rets / 100).prod() - 1) * 100
r_win = int((r_rets > 0).sum())
r_sharpe = float(r_rets.mean() / r_rets.std() * np.sqrt(252/HOLD_DAYS)) if r_rets.std() > 0 else 0
r_dd = float(min(0, (r_rets / 100).min()))

# V4+V11 (Actual)
a_rets = np.array(actual_rets)
a_cum = float((1 + a_rets / 100).prod() - 1) * 100
a_win = int((a_rets > 0).sum())
a_sharpe = float(a_rets.mean() / a_rets.std() * np.sqrt(252/HOLD_DAYS)) if a_rets.std() > 0 else 0
a_dd = float(min(0, (a_rets / 100).min()))

print(f"  {'策略':<25} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>8} {'均值':>7}")
print(f"  {'-'*66}")
print(f"  {'V4 + 随机 (α=0%)':<25} {r_cum:>+10.2f}% {r_win/len(r_rets)*100:>7.1f}% {r_sharpe:>8.2f} {r_dd*100:>8.2f}% {r_rets.mean():>+6.2f}%")
print(f"  {'V4 + V11 实际':<25} {a_cum:>+10.2f}% {a_win/len(a_rets)*100:>7.1f}% {a_sharpe:>8.2f} {a_dd*100:>8.2f}% {a_rets.mean():>+6.2f}%")

print()
print(f"  V11 IC 提升对 V4+ML 收益的影响模拟:")
print(f"  {'提升幅度':<12} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>8} {'均值':>7}")
print(f"  {'-'*53}")

# Current: V4+V11
curr_ic = avg_ic
print(f"  {'当前(IC='+f'{curr_ic:.3f})':<12} {a_cum:>+10.2f}% {a_win/len(a_rets)*100:>7.1f}% {a_sharpe:>8.2f} {a_dd*100:>8.2f}% {a_rets.mean():>+6.2f}%")

for pct in [10, 20, 30, 50]:
    rets = np.array(improved_rets[f"+{pct}%IC"])
    if len(rets) == 0:
        continue
    cum = float((1 + rets / 100).prod() - 1) * 100
    wins = int((rets > 0).sum())
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252/HOLD_DAYS)) if std > 0 else 0
    dd = float(min(0, (rets / 100).min()))
    target_ic = curr_ic * (1 + pct/100)
    print(f"  {'+'+str(pct)+'%IC('+f'{target_ic:.3f})':<12} {cum:>+10.2f}% {wins/len(rets)*100:>7.1f}% {sharpe:>8.2f} {dd*100:>8.2f}% {avg:>+6.2f}%")

# Save
output = {
    'metrics': {'avg_spearman_ic_v4_pool': round(avg_ic, 4), 'n_samples': len(actual_rets)},
    'actual': {'strategy': 'V4+V11', 'cum_ret': round(a_cum, 2), 'win_rate': round(a_win/len(a_rets)*100, 1) if a_rets.size else 0, 'sharpe': round(a_sharpe, 2), 'max_dd': round(a_dd*100, 2)},
    'simulation': {f'+{pct}%IC': {
        'cum_ret': round(float((1 + np.array(improved_rets[f"+{pct}%IC"])/100).prod() - 1) * 100, 2),
        'n_samples': len(improved_rets[f"+{pct}%IC"])
    } for pct in [10, 20, 30, 50]},
}
out_path = os.path.join(BASE_DIR, 'data', 'ml_quality_v2.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
