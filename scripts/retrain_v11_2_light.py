#!/usr/bin/env python3
"""
V11.2 轻量重训练 — 预计算特征 + 简化模型 + 状态交互特征

策略：
1. 复用 V11.0 特征构建管线，只生成训练特征（不训练完整ensemble）
2. 加载 market_regime_daily 并加入交互特征
3. 只训练一个 LightGBM（而非18个子模型ensemble）
4. 最近6个月数据/超时止损/正则化更强
"""
import logging
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
from datetime import datetime, timedelta

import joblib
import lightgbm as lgb
import pymysql
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

# Paths
V11_0_MODEL = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_0.pkl')
V11_2_MODEL = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_2.pkl')

logger.info(f"V11.0 model: {V11_0_MODEL}")
logger.info(f"V11.2 output: {V11_2_MODEL}")

# ================================================================
# 1. Build training features for recent period
# ================================================================
conn = pymysql.connect(**DB_CONFIG)
cur = conn.cursor()

# Get dates (last 200 trading days)
cur.execute("SELECT MAX(trade_date) FROM daily_price")
max_date = str(cur.fetchone()[0])
min_date = (datetime.strptime(max_date, '%Y-%m-%d') - timedelta(days=365)).strftime('%Y-%m-%d')
logger.info(f"Data window: {min_date} ~ {max_date}")

# Build candidate pool dates (every 3 days, last 100 days)
cur.execute(f"""
    SELECT DISTINCT trade_date FROM daily_price 
    WHERE trade_date >= '{min_date}' AND trade_date <= '{max_date}'
    ORDER BY trade_date
""")
all_dates = [str(r[0]) for r in cur.fetchall()]
sample_dates = all_dates[::3]
if len(sample_dates) > 30:
    sample_dates = sample_dates[-30:]
logger.info(f"Sample dates: {len(sample_dates)} (from {all_dates[0]} to {all_dates[-1]})")

# Load V11.0 model for feature building
logger.info("Loading V11.0 model...")
v11_bundle = joblib.load(V11_0_MODEL)
feature_cols = v11_bundle['feature_cols']
logger.info(f"V11.0 has {len(feature_cols)} features, {v11_bundle.get('n_models', '?')} models")

from ml_predict import _ensemble_predict
from scripts.predict_v11 import build_features_v11_inference

# Build training data
all_features = []
all_labels = []
all_codes = []
all_dates_list = []

for i, buy_date in enumerate(sample_dates):
    if (i+1) % 5 == 0:
        logger.info(f"Progress: {i+1}/{len(sample_dates)}")

    # Get prev trade date
    cur.execute(f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date < '{buy_date}'")
    prev_date = str(cur.fetchone()[0])

    # Get candidate pool (top 500 by volume)
    cur.execute(f"""
        SELECT ts_code FROM daily_price 
        WHERE trade_date = '{prev_date}' 
        AND LEFT(ts_code,1) NOT IN ('8','4','9')
        AND close <= 200 AND close >= 3
        ORDER BY amount DESC LIMIT 500
    """)
    codes = [r[0] for r in cur.fetchall()]
    if len(codes) < 100:
        continue

    # Build V11.0 features
    feat = build_features_v11_inference(conn, codes, as_of_date=buy_date)
    if feat is None or feat.empty or len(feat) < 50:
        continue

    # Add regime interaction features
    cur.execute(f"""
        SELECT trade_date, prob_bull, prob_bear, prob_panic, prob_range, prob_overheated,
               sh_trend, market_breadth, volatility, volume_trend, momentum_breadth, zt_ratio
        FROM market_regime_daily
        WHERE trade_date <= '{buy_date}'
        ORDER BY trade_date DESC LIMIT 1
    """)
    regime_row = cur.fetchone()
    if regime_row:
        rp = {  # regime probabilities as dict
            'prob_bull': float(regime_row[1] or 0),
            'prob_bear': float(regime_row[2] or 0),
            'prob_panic': float(regime_row[3] or 0),
            'prob_range': float(regime_row[4] or 0),
            'prob_overheated': float(regime_row[5] or 0),
            'sh_trend': float(regime_row[6] or 0),
            'market_breadth': float(regime_row[7] or 0),
            'volatility': float(regime_row[8] or 0),
            'volume_trend': float(regime_row[9] or 0),
            'momentum_breadth': float(regime_row[10] or 0),
            'zt_ratio': float(regime_row[11] or 0),
        }
    else:
        rp = {k: 0.0 for k in ['prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                               'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio']}

    for k, v in rp.items():
        feat[k] = v

    # Interaction features
    feat['chg_5d_x_panic'] = feat.get('chg_5d', 0) * rp.get('prob_panic', 0)
    feat['chg_10d_x_panic'] = feat.get('chg_10d', 0) * rp.get('prob_panic', 0)
    feat['rsi_14_x_panic'] = feat.get('rsi_14', 0) * rp.get('prob_panic', 0)
    feat['volume_ratio_x_panic'] = feat.get('volume_ratio', 0) * rp.get('prob_panic', 0)
    if 'main_net_ratio' in feat.columns:
        feat['main_net_ratio_x_panic'] = feat['main_net_ratio'] * rp.get('prob_panic', 0)
    if 'vol_price_divergence' in feat.columns:
        feat['vol_price_divergence_x_panic'] = feat['vol_price_divergence'] * rp.get('prob_panic', 0)
    feat['volume_ratio_x_overheated'] = feat.get('volume_ratio', 0) * rp.get('prob_overheated', 0)
    feat['chg_5d_x_bull'] = feat.get('chg_5d', 0) * rp.get('prob_bull', 0)
    feat['breadth_x_main_net'] = rp.get('market_breadth', 0) * feat.get('main_net_ratio', 0)
    feat['volatility_x_rsi'] = rp.get('volatility', 0) * feat.get('rsi_14', 0)

    # Compute labels: forward 5-day return (with -7% stop loss)
    pred = _ensemble_predict(feat.fillna(0), v11_bundle)
    codes_this = feat['ts_code'].tolist()

    for idx, tc in enumerate(codes_this):
        if pred[idx] < -2.0:  # skip very negative predictions (noise)
            continue

        # Get forward price data
        cur.execute(f"""
            SELECT trade_date, pct_chg, close FROM daily_price 
            WHERE ts_code = '{tc}' AND trade_date > '{buy_date}'
            ORDER BY trade_date LIMIT 6
        """)
        fwd = cur.fetchall()
        if len(fwd) < 2:
            continue

        entry_close = float(fwd[0][2])
        exit_idx = len(fwd) - 1
        for j in range(1, len(fwd)):
            cur_close = float(fwd[j][2])
            if entry_close > 0 and (cur_close - entry_close) / entry_close <= -0.07:
                exit_idx = j
                break

        ret_vals = [float(fwd[k][1] or 0) / 100.0 for k in range(exit_idx + 1) if fwd[k][1] is not None]
        ret_vals = [r for r in ret_vals if not np.isnan(r)]
        if len(ret_vals) == 0:
            continue

        fwd_ret = float((1 + np.array(ret_vals)).prod() - 1) * 100
        if abs(fwd_ret) > 50:  # outlier filter
            continue

        # Get features
        feat_row = feat.iloc[idx]

        all_features.append(feat_row)
        all_labels.append(fwd_ret)
        all_codes.append(tc)
        all_dates_list.append(buy_date)

conn.close()

if len(all_features) == 0:
    logger.error("No training data generated!")
    sys.exit(1)

# Convert to DataFrame
X_df = pd.DataFrame(all_features)
y = np.array(all_labels)
X_dates = all_dates_list
X_codes = all_codes

logger.info(f"Training data: {len(y)} samples, {len(X_df.columns)} features")

# ================================================================
# 2. Feature engineering
# ================================================================
# All features for training
v11_2_feature_cols = feature_cols + [
    'prob_bull', 'prob_bear', 'prob_panic', 'prob_range', 'prob_overheated',
    'sh_trend', 'market_breadth', 'volatility', 'volume_trend', 'momentum_breadth', 'zt_ratio',
    'chg_5d_x_panic', 'chg_10d_x_panic', 'rsi_14_x_panic',
    'volume_ratio_x_panic', 'main_net_ratio_x_panic', 'vol_price_divergence_x_panic',
    'volume_ratio_x_overheated', 'chg_5d_x_bull',
    'breadth_x_main_net', 'volatility_x_rsi',
]

# Fill missing features
for col in v11_2_feature_cols:
    if col not in X_df.columns:
        X_df[col] = 0.0

X = X_df[v11_2_feature_cols].values.astype(np.float32)
global_medians = {col: float(X_df[col].median()) for col in v11_2_feature_cols}

# Replace outliers (clip at 3-sigma)
for i in range(X.shape[1]):
    col_data = X[:, i]
    mean_val = np.nanmean(col_data)
    std_val = np.nanstd(col_data)
    if std_val > 0:
        X[:, i] = np.clip(col_data, mean_val - 3*std_val, mean_val + 3*std_val)

X = np.nan_to_num(X, nan=0.0)

# ================================================================
# 3. Train LightGBM
# ================================================================
logger.info(f"Training LightGBM: {X.shape} features={len(v11_2_feature_cols)}")
logger.info(f"Label stats: mean={y.mean():.2f}% median={np.median(y):.2f}% std={y.std():.2f}%")

# Time-based split (last 20% as validation)
split_idx = int(len(y) * 0.8)
train_mask = np.zeros(len(y), dtype=bool)
val_mask = np.zeros(len(y), dtype=bool)
# Sort by date
date_order = np.argsort([str(d) for d in X_dates])
train_mask[date_order[:split_idx]] = True
val_mask[date_order[split_idx:]] = True

X_train, X_val = X[train_mask], X[val_mask]
y_train, y_val = y[train_mask], y[val_mask]

logger.info(f"Train: {len(y_train)}, Val: {len(y_val)}")
logger.info(f"Train label mean={y_train.mean():.2f}% Val label mean={y_val.mean():.2f}%")

# Params: stronger regularization to prevent overfitting on limited data
params = {
    'objective': 'regression',
    'metric': 'mae',
    'boosting_type': 'gbdt',
    'num_leaves': 48,
    'max_depth': 7,
    'learning_rate': 0.03,
    'feature_fraction': 0.7,
    'bagging_fraction': 0.7,
    'bagging_freq': 5,
    'lambda_l1': 1.0,
    'lambda_l2': 2.0,
    'min_data_in_leaf': 20,
    'verbose': -1,
    'seed': 42,
}

train_data = lgb.Dataset(X_train, label=y_train)
val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

model = lgb.train(
    params,
    train_data,
    num_boost_round=500,
    valid_sets=[val_data],
    callbacks=[lgb.early_stopping(30), lgb.log_evaluation(50)],
)

# Validation
y_val_pred = model.predict(X_val)

# Rank IC
from scipy.stats import spearmanr

rank_ic, _ = spearmanr(y_val, y_val_pred)
logger.info(f"Validation Rank IC: {rank_ic:.4f}")

# ================================================================

# Top/Bottom decile analysis
try:
    val_idx = np.where(val_mask)[0]
    val_df = pd.DataFrame({'y_true': y_val, 'y_pred': y_val_pred, 'date': [str(X_dates[i]) for i in val_idx]})
    val_df['decile'] = pd.qcut(val_df['y_pred'].rank(method='first'), 10, labels=False)
    top_dec = float(val_df[val_df['decile'] == 9]['y_true'].mean())
    bot_dec = float(val_df[val_df['decile'] == 0]['y_true'].mean())
    logger.info(f"Top decile avg: {top_dec:.2f}%, Bottom decile avg: {bot_dec:.2f}%, Spread: {top_dec - bot_dec:.2f}%")
except Exception as _ae:
    logger.warning(f"Decile analysis failed: {_ae}")
    top_dec = 0.0
    bot_dec = 0.0

v11_2_bundle = {
    'model_type': 'v11_2_single_lgb',
    'version': 'v11.2',
    'model': model,
    'feature_cols': v11_2_feature_cols,
    'global_medians': global_medians,
    'v11_0_model_path': V11_0_MODEL,
    'trained_at': datetime.now().isoformat(),
    'n_samples': len(y),
    'val_rank_ic': float(rank_ic),
    'val_samples': len(y_val),
    'top_decile_avg': float(top_dec),
    'bot_decile_avg': float(bot_dec),
    'params': params,
    'label_stats': {'mean': float(y.mean()), 'std': float(y.std()), 'median': float(np.median(y))},
}

joblib.dump(v11_2_bundle, V11_2_MODEL)
logger.info(f"Model saved to {V11_2_MODEL}")
logger.info(f"Size: {os.path.getsize(V11_2_MODEL)} bytes")

# ================================================================
# 5. Summary
# ================================================================
print(f"\n{'='*55}")
print("  V11.2 轻量重训练 完成")
print(f"{'='*55}")
print(f"  Training samples: {len(y_train)}")
print(f"  Validation samples: {len(y_val)}")
print(f"  Features: {len(v11_2_feature_cols)}")
print(f"  Validation Rank IC: {rank_ic:.4f}")
print(f"  Top decile avg return: {top_dec:.2f}%")
print(f"  Bottom decile avg return: {bot_dec:.2f}%")
print(f"  Spread: {top_dec - bot_dec:.2f}%")
print(f"  Model saved: {V11_2_MODEL}")
print(f"{'='*55}")
