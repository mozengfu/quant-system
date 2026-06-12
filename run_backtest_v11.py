#!/usr/bin/env python3
"""V4 + V11.0 回测运行器 - 优化版（使用 SQLAlchemy 减少警告）

注意: 此回测使用单一模型（在全量数据上训练），
模型中的 global_medians 包含回测期间之后的数据 → 存在数据泄露风险。
如需无泄漏回测，使用 run_backtest_v11_walkforward.py（Walk-Forward 时序交叉验证）。
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

from sqlalchemy import create_engine

from ml_predict import _ensemble_predict
from quant_app.services.strategy_service import _v4_score_single
from quant_app.utils.config import get_db_config
from quant_app.utils.model_loader import get_model_path

DB_CONFIG = get_db_config()
DB_CONFIG.pop('unix_socket', None)
DB_CONFIG['host'] = '127.0.0.1'
DB_CONFIG['port'] = 3306
ENGINE = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset=utf8mb4",
    pool_pre_ping=True, pool_size=5
)

START_DATE, END_DATE = "2024-11-01", "2026-05-15"
SAMPLE_INTERVAL = 5
TOP_N = 3
HOLD_DAYS = 5
STOP_LOSS = float(os.environ.get('STOP_LOSS', '-0.07'))  # -7% 止损，0=不止损

# Load V11 model
model_path = get_model_path("v11.0")
logger.info(f"Loading V11.0 model from {model_path}")
import joblib

bundle = joblib.load(model_path)
logger.info(f"Loaded: {bundle.get('n_models', '?')} models, {len(bundle.get('feature_cols', []))} features")

# Load V11 feature builder
from scripts.predict_v11 import build_features_v11_inference

# Get trade dates
all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' ORDER BY trade_date",
    ENGINE
)['trade_date'].astype(str).tolist())

sample_dates = all_dates[::SAMPLE_INTERVAL]
sample_dates = [d for d in sample_dates if d > all_dates[5]]
logger.info(f"Trade dates: {len(all_dates)}, Sample dates: {len(sample_dates)}")

conn = pymysql.connect(**DB_CONFIG)
v4ml_results = []
pure_ml_results = []

for di, buy_date in enumerate(sample_dates):
    if (di + 1) % 5 == 0:
        logger.info(f"Progress: {di+1}/{len(sample_dates)}")

    # Volume-sorted candidates from yesterday
    prev_date = pd.read_sql(
        f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date < '{buy_date}'",
        ENGINE
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

    # Build V11 features
    try:
        feat = build_features_v11_inference(conn, top_codes, as_of_date=buy_date)
    except Exception as e:
        logger.warning(f"Feature build failed: {e}")
        continue
    if feat is None or feat.empty or len(feat) < 50:
        continue

    # ML prediction
    v80f = bundle['feature_cols']
    medians = bundle.get('global_medians', {})
    for col in v80f:
        if col not in feat.columns:
            feat[col] = medians.get(col, 0.0)
    feat = feat.fillna(0)
    ml_preds = _ensemble_predict(feat, bundle)
    codes = feat['ts_code'].tolist()

    # V4+ML
    v4ml_top = []
    try:
        v4_picks = []
        for _, row in feat.iterrows():
            sc = _v4_score_single(row)
            if sc >= 0:
                v4_picks.append((row['ts_code'], sc))
        v4_picks.sort(key=lambda x: -x[1])
        v4_top30 = [c for c, _ in v4_picks[:30]]
        if v4_top30:
            v4_scores = {c: ml_preds[codes.index(c)] for c in v4_top30 if c in codes}
            v4ml_top = [c for c, _ in sorted(v4_scores.items(), key=lambda x: -x[1])[:TOP_N]]
    except Exception as e:
        logger.warning(f"V4+ML failed: {e}")

    # Pure ML
    pure_ml_top = []
    try:
        ranked = sorted(zip(codes, ml_preds), key=lambda x: -x[1])
        pure_ml_top = [c for c, _ in ranked[:TOP_N]]
    except Exception as e:
        logger.warning(f"Pure ML failed: {e}")

    # Forward returns (含止损)
    for label, top, store in [('V4+ML', v4ml_top, v4ml_results), ('Pure ML', pure_ml_top, pure_ml_results)]:
        rets = []
        for tc in top:
            ret_df = pd.read_sql(f"""
                SELECT trade_date, pct_chg, close FROM daily_price
                WHERE ts_code = '{tc}' AND trade_date > '{buy_date}'
                ORDER BY trade_date LIMIT {HOLD_DAYS}
            """, ENGINE)
            if len(ret_df) < 2:
                continue
            # 止损检查
            entry_close = float(ret_df['close'].iloc[0])
            exit_idx = len(ret_df) - 1
            if STOP_LOSS < 0:
                for j in range(1, len(ret_df)):
                    cur_close = float(ret_df['close'].iloc[j])
                    if cur_close > 0 and entry_close > 0:
                        cur_ret = (cur_close - entry_close) / entry_close
                        if cur_ret <= STOP_LOSS:
                            exit_idx = j
                            break
            rets_vals = ret_df['pct_chg'].iloc[:exit_idx + 1].values / 100.0
            rets_vals = rets_vals[~np.isnan(rets_vals)]
            if len(rets_vals) == 0:
                continue
            rets.append(float((1 + rets_vals).prod() - 1) * 100)
        if rets:
            store.append({'date': buy_date, 'avg_ret': round(float(np.mean(rets)), 2), 'n': len(rets)})

conn.close()

# ===== Results =====
print(f"\n{'='*55}")
print(f"V4 + V11.0 回测结果（{START_DATE} ~ {END_DATE}）")
print(f"{'='*55}")
print(f"模型: V11.0, 采样: 每{SAMPLE_INTERVAL}天, 持仓: {HOLD_DAYS}天, Top{TOP_N}")
print()

for label, store in [('V4+ML', v4ml_results), ('Pure ML', pure_ml_results)]:
    if not store:
        print(f"  {label}: 无有效交易")
        continue
    rets = np.array([r['avg_ret'] for r in store])
    wins = int((rets > 0).sum())
    total = len(rets)
    cum = float((1 + rets / 100).prod() - 1) * 100
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252 / HOLD_DAYS)) if std > 0 else 0
    dd = float(min(0, (rets / 100).min()))
    print(f"  {label}:")
    print(f"    采样次数: {total}")
    print(f"    累积收益: {cum:+.2f}%")
    print(f"    单次均值: {avg:+.2f}%")
    print(f"    胜率:     {wins/total*100:.1f}% ({wins}W/{total-wins}L)")
    print(f"    夏普:     {sharpe:.2f}")
    print(f"    最大回撤: {dd*100:.2f}%")
    print()

# Save results
output = {
    'params': {'interval': SAMPLE_INTERVAL, 'top_n': TOP_N, 'hold_days': HOLD_DAYS},
    'v4ml': v4ml_results,
    'pure_ml': pure_ml_results,
}
out_path = os.path.join(BASE_DIR, 'data', 'backtest_v4_v11.json')
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
logger.info(f"Saved to {out_path}")
