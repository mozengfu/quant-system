#!/usr/bin/env python3
"""Phase 2D: Ranker V2 训练 - 包含 V11/market/sector 特征"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


DATA_PATH = Path(__file__).parent.parent / "data" / "factor_dataset" / "factor_v2_2024-01-01_2026-06-09.parquet"
MODEL_DIR = Path(__file__).parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def main():
    logger.info("Loading V2 dataset...")
    if not DATA_PATH.exists():
        logger.error(f"Dataset not found: {DATA_PATH}. Run build_factor_dataset_v2.py first.")
        return
    df = pd.read_parquet(DATA_PATH)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    logger.info(f"  {len(df)} samples, {df['ts_code'].nunique()} stocks, {df['trade_date'].nunique()} dates")

    feature_cols = [c for c in df.columns if c not in [
        'trade_date', 'ts_code', 'industry', 'sector',
        'target_ret_3d', 'target_ret_5d', 'target_ret_10d',
        'ind_ret_3d', 'ind_ret_5d', 'ind_ret_10d',
        'ret_3d_industry_rel', 'ret_5d_industry_rel', 'ret_5d_industry_rank',
        'first_time', 'open_times_x',  # 各种衍生
    ]]
    feature_cols = [c for c in feature_cols if df[c].dtype in [np.float64, np.int64, np.float32, np.int32, 'float64', 'int64']]
    logger.info(f"  {len(feature_cols)} features")
    logger.info(f"  Sample: {feature_cols[:8]}")

    # 行业 rank (5 档)
    df['target_rank'] = df.groupby(['trade_date', 'industry'])['target_ret_5d'].rank(pct=True)
    df['target_int'] = df['target_rank'].fillna(0.5).multiply(4).round().fillna(2).astype(int).clip(0, 4)

    # query: trade_date + industry
    df['query'] = df['trade_date'].dt.strftime('%Y%m%d') + '_' + df['industry'].astype(str)
    df = df.sort_values('query').reset_index(drop=True)

    X = df[feature_cols].astype(float).fillna(0).values
    y = df['target_int'].values

    # Group
    query_ids, group_sizes = np.unique(df['query'].values, return_counts=True)
    group = group_sizes.tolist()
    logger.info(f"  {len(group_sizes)} queries, avg size {np.mean(group_sizes):.0f}")

    # 时序 split: 80% 训练, 20% 测试
    dates = sorted(df['trade_date'].unique())
    split_date = dates[int(len(dates) * 0.8)]
    train_mask = df['trade_date'] < split_date
    test_mask = df['trade_date'] >= split_date
    logger.info(f"  Train: {df[train_mask]['trade_date'].min()} ~ {df[train_mask]['trade_date'].max()}")
    logger.info(f"  Test:  {df[test_mask]['trade_date'].min()} ~ {df[test_mask]['trade_date'].max()}")

    train_df = df[train_mask].sort_values('query').reset_index(drop=True)
    test_df = df[test_mask].sort_values('query').reset_index(drop=True)
    _, train_group = np.unique(train_df['query'], return_counts=True)
    _, test_group = np.unique(test_df['query'], return_counts=True)
    train_group = train_group.tolist()
    test_group = test_group.tolist()

    X_train = train_df[feature_cols].astype(float).fillna(0).values
    y_train = train_df['target_int'].values
    X_test = test_df[feature_cols].astype(float).fillna(0).values
    y_test = test_df['target_int'].values

    logger.info(f"  Train: {len(X_train)}, Test: {len(X_test)}")

    import lightgbm as lgb
    train_data = lgb.Dataset(X_train, label=y_train, group=train_group, feature_name=feature_cols)
    test_data = lgb.Dataset(X_test, label=y_test, group=test_group, feature_name=feature_cols, reference=train_data)

    params = {
        'objective': 'lambdarank',
        'metric': 'ndcg',
        'ndcg_eval_at': [5, 10, 20],
        'learning_rate': 0.04,
        'num_leaves': 31,
        'min_child_samples': 100,
        'feature_fraction': 0.7,
        'bagging_fraction': 0.7,
        'bagging_freq': 5,
        'lambda_l1': 0.1,
        'lambda_l2': 0.1,
        'verbose': -1,
    }
    model = lgb.train(
        params, train_data, num_boost_round=800,
        valid_sets=[test_data], valid_names=['test'],
        callbacks=[lgb.early_stopping(80), lgb.log_evaluation(50)],
    )

    # 重要性
    imp = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importance(importance_type='gain'),
    }).sort_values('importance', ascending=False)
    logger.info(f"\nTop 20 因子:\n{imp.head(20).to_string(index=False)}")

    # 评估
    from scipy.stats import spearmanr
    from sklearn.metrics import ndcg_score
    y_pred = model.predict(X_test, num_iteration=model.best_iteration)
    test_df['pred'] = y_pred
    ndcg_list, ic_list = [], []
    for q, g in test_df.groupby('query'):
        if len(g) >= 5:
            ndcg_list.append(ndcg_score(g['target_ret_5d'].values.reshape(1,-1), g['pred'].values.reshape(1,-1), k=5))
            corr, _ = spearmanr(g['pred'], g['target_ret_5d'])
            if not np.isnan(corr): ic_list.append(corr)
    logger.info(f"\nNDCG@5: {np.mean(ndcg_list):.3f} (n={len(ndcg_list)})")
    logger.info(f"IC: {np.mean(ic_list):.4f} ± {np.std(ic_list):.4f}")
    if ic_list:
        ir = np.array(ic_list).mean() / np.array(ic_list).std() * np.sqrt(252/5)
        logger.info(f"IR (年化): {ir:.3f}")

    # 保存
    bundle = {
        'model': model,
        'feature_names': feature_cols,
        'best_iteration': model.best_iteration,
        'ndcg5_mean': float(np.mean(ndcg_list)) if ndcg_list else 0,
        'ic_mean': float(np.mean(ic_list)) if ic_list else 0,
        'ic_std': float(np.std(ic_list)) if ic_list else 0,
        'ir': float(ir) if ic_list else 0,
        'trained_at': datetime.now().isoformat(),
    }
    out_path = MODEL_DIR / 'factor_ranker_v2.pkl'
    joblib.dump(bundle, out_path)
    logger.info(f"Saved → {out_path}")
    logger.info(f"V2 vs V1: NDCG@{bundle['ndcg5_mean']:.3f} vs 0.615, IC={bundle['ic_mean']:.3f} vs 0.029")


if __name__ == '__main__':
    main()
