#!/usr/bin/env python3
"""
ML选股模型训练 - 用LightGBM替换规则打分
从MySQL历史数据提取特征，预测3天后涨跌概率
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd
import pymysql
import lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
import joblib

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = {
    'host': 'localhost',
    'unix_socket': '/tmp/mysql.sock',
    'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db',
    'connect_timeout': 5,
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
MODEL_PATH = os.path.join(DATA_DIR, 'ml_stock_model.pkl')
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'ml_feature_config.json')

LABEL_THRESHOLD = 2.0  # 3天后涨幅>=2%为正样本
HORIZON = 3

EXCLUDE_PREFIXES = ('68', '83', '87', '8', '4', '9', '16')


def get_db():
    return pymysql.connect(**DB_CONFIG)


def load_all_data():
    logger.info("加载MySQL数据...")
    conn = get_db()
    df = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, high_52w, low_52w
        FROM daily_price
        ORDER BY ts_code, trade_date
    """, conn)
    conn.close()
    
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    numeric_cols = ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg',
                    'turnover_rate', 'volume_ratio',
                    'ma5', 'ma10', 'ma20', 'rps_20', 'high_52w', 'low_52w']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    logger.info(f"加载 {len(df)} 行，{df['ts_code'].nunique()} 只股票")
    return df


def build_features_for_stock(group):
    """单只股票构建特征"""
    group = group.sort_values('trade_date').reset_index(drop=True)
    n = len(group)
    if n < 30:
        return pd.DataFrame()
    
    # 技术指标
    group['vol_5d'] = group['pct_chg'].rolling(5).std()
    group['vol_10d'] = group['pct_chg'].rolling(10).std()
    group['ma5_ma10_ratio'] = group['ma5'] / group['ma10'].replace(0, np.nan)
    group['ma10_ma20_ratio'] = group['ma10'] / group['ma20'].replace(0, np.nan)
    group['price_ma5_ratio'] = group['close'] / group['ma5'].replace(0, np.nan)
    group['price_ma20_ratio'] = group['close'] / group['ma20'].replace(0, np.nan)
    group['chg_3d'] = group['close'] / group['close'].shift(3) - 1
    group['chg_5d'] = group['close'] / group['close'].shift(5) - 1
    group['chg_10d'] = group['close'] / group['close'].shift(10) - 1
    
    # 量趋势（用volume_ratio替代turnover_rate，因为turnover_rate缺失严重）
    group['vr_ma5'] = group['volume_ratio'].rolling(5).mean()
    group['vr_ma10'] = group['volume_ratio'].rolling(10).mean()
    group['vol_trend'] = group['vr_ma5'] / group['vr_ma10'].replace(0, np.nan)
    
    group['pos_52w'] = (group['close'] - group['low_52w']) / (group['high_52w'] - group['low_52w']).replace(0, np.nan)
    group['rps_change'] = group['rps_20'].diff(5)
    group['up_ratio_5d'] = (group['pct_chg'] > 0).rolling(5).mean()
    group['up_ratio_10d'] = (group['pct_chg'] > 0).rolling(10).mean()
    group['vol_pct_corr'] = group['volume_ratio'].rolling(5).corr(group['pct_chg'])
    
    group['ma_pattern'] = 1
    group.loc[(group['ma5'] > group['ma10']) & (group['ma10'] > group['ma20']), 'ma_pattern'] = 2
    group.loc[(group['ma5'] < group['ma10']) & (group['ma10'] < group['ma20']), 'ma_pattern'] = 0
    
    group['ema12'] = group['close'].ewm(span=12, adjust=False).mean()
    group['ema26'] = group['close'].ewm(span=26, adjust=False).mean()
    group['macd_diff'] = group['ema12'] - group['ema26']
    group['macd_signal'] = group['macd_diff'].ewm(span=9, adjust=False).mean()
    group['macd_hist'] = group['macd_diff'] - group['macd_signal']
    
    # 标签
    group['future_return'] = group['close'].shift(-HORIZON) / group['close'] - 1
    group['label'] = (group['future_return'] >= LABEL_THRESHOLD / 100).astype(int)
    
    feature_cols = [
        'pct_chg', 'turnover_rate', 'volume_ratio',
        'vol_5d', 'vol_10d',
        'ma5_ma10_ratio', 'ma10_ma20_ratio', 'price_ma5_ratio', 'price_ma20_ratio',
        'chg_3d', 'chg_5d', 'chg_10d',
        'vol_trend',
        'pos_52w',
        'rps_20', 'rps_change',
        'up_ratio_5d', 'up_ratio_10d',
        'vol_pct_corr',
        'ma_pattern',
        'macd_diff', 'macd_signal', 'macd_hist',
    ]
    
    # 用中位数填充NaN，而不是丢弃行
    for col in feature_cols:
        if group[col].isna().any():
            median_val = group[col].median()
            group[col] = group[col].fillna(median_val)
    
    # 移除标签NaN（最后HORIZON天无未来数据）
    valid = group.dropna(subset=['label', 'future_return']).copy()
    
    if len(valid) < 10:
        return pd.DataFrame()
    
    result = valid[feature_cols + ['ts_code', 'trade_date', 'label', 'future_return']].copy()
    result['ts_code'] = valid['ts_code']
    result['trade_date'] = valid['trade_date']
    
    return result


def build_features(df):
    logger.info("构建特征...")
    
    results = []
    total_stocks = 0
    
    for ts_code, group in df.groupby('ts_code'):
        prefix = ts_code[:2]
        if prefix in EXCLUDE_PREFIXES:
            continue
        
        feat = build_features_for_stock(group)
        if len(feat) > 0:
            results.append(feat)
            total_stocks += 1
    
    if not results:
        logger.warning("无有效样本")
        return pd.DataFrame()
    
    result = pd.concat(results, ignore_index=True)
    logger.info(f"构建完成: {len(result)} 条样本，{total_stocks} 只股票")
    return result


def train_model(df):
    logger.info("开始训练模型...")
    
    feature_cols = [c for c in df.columns if c not in 
                    ['ts_code', 'trade_date', 'label', 'future_return']]
    
    df_sorted = df.sort_values('trade_date')
    X = df_sorted[feature_cols].values
    y = df_sorted['label'].values
    
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    scale = n_neg / n_pos if n_pos > 0 else 1
    logger.info(f"正样本: {n_pos} ({n_pos/len(y)*100:.1f}%), 负样本: {n_neg} ({n_neg/len(y)*100:.1f}%), scale_pos_weight={scale:.2f}")
    
    tscv = TimeSeriesSplit(n_splits=4)
    cv_results = []
    
    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_train, X_val = X[train_idx], X[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        
        train_data = lgb.Dataset(X_train, label=y_train)
        val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)
        
        params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'scale_pos_weight': scale,
            'min_child_samples': 50,
            'verbose': -1,
            'seed': 42,
        }
        
        model = lgb.train(
            params,
            train_data,
            num_boost_round=500,
            valid_sets=[val_data],
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)],
        )
        
        val_pred = model.predict(X_val)
        val_label = (val_pred >= 0.5).astype(int)
        
        results = {
            'fold': fold + 1,
            'accuracy': accuracy_score(y_val, val_label),
            'precision': precision_score(y_val, val_label, zero_division=0),
            'recall': recall_score(y_val, val_label, zero_division=0),
            'auc': roc_auc_score(y_val, val_pred),
        }
        cv_results.append(results)
        logger.info(f"Fold {fold+1}: acc={results['accuracy']:.3f}, prec={results['precision']:.3f}, "
                    f"recall={results['recall']:.3f}, auc={results['auc']:.3f}")
    
    avg = {k: np.mean([r[k] for r in cv_results]) for k in cv_results[0].keys() if k != 'fold'}
    logger.info(f"CV平均: acc={avg['accuracy']:.3f}, prec={avg['precision']:.3f}, "
                f"recall={avg['recall']:.3f}, auc={avg['auc']:.3f}")
    
    # 最终模型
    train_data_full = lgb.Dataset(X, label=y)
    final_model = lgb.train(
        {'objective': 'binary', 'metric': 'auc', 'boosting_type': 'gbdt',
         'num_leaves': 31, 'learning_rate': 0.05, 'feature_fraction': 0.8,
         'bagging_fraction': 0.8, 'bagging_freq': 5, 'scale_pos_weight': scale,
         'min_child_samples': 50, 'verbose': -1, 'seed': 42},
        train_data_full,
        num_boost_round=300,
    )
    
    importance = dict(zip(feature_cols, final_model.feature_importance()))
    importance_sorted = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))
    logger.info(f"\n特征重要性 Top 10:")
    for k, v in list(importance_sorted.items())[:10]:
        logger.info(f"  {k}: {v}")
    
    os.makedirs(DATA_DIR, exist_ok=True)
    model_bundle = {
        'model': final_model,
        'feature_cols': feature_cols,
        'cv_results': cv_results,
        'avg_results': avg,
        'importance': importance_sorted,
        'label_threshold': LABEL_THRESHOLD,
        'horizon': HORIZON,
        'scale_pos_weight': scale,
        'trained_at': datetime.now().isoformat(),
        'trained_on_data': f"{len(df)} samples, {df['ts_code'].nunique()} stocks",
    }
    joblib.dump(model_bundle, MODEL_PATH)
    logger.info(f"模型已保存: {MODEL_PATH}")
    
    config = {
        'feature_cols': feature_cols,
        'label_threshold': LABEL_THRESHOLD,
        'horizon': HORIZON,
        'avg_cv_results': avg,
        'feature_importance_top15': dict(list(importance_sorted.items())[:15]),
        'model_path': MODEL_PATH,
    }
    with open(FEATURE_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, default=str)
    
    return model_bundle


def main():
    start = datetime.now()
    df = load_all_data()
    features = build_features(df)
    if len(features) == 0:
        logger.error("无训练数据，退出")
        return
    model = train_model(features)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"完成! 总耗时: {elapsed:.1f}s")


if __name__ == '__main__':
    main()
