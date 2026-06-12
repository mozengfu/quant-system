#!/usr/bin/env python3
"""
Phase 2B: LightGBM Ranker 训练

任务: 预测每只股票未来 5 日收益在行业内的排名
模型: lightgbm ranker (lambdarank)
CV: 时序 5-fold
"""
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


DATA_PATH = Path(__file__).parent.parent / "data" / "factor_dataset" / "factor_2024-01-01_2026-06-09.parquet"
MODEL_DIR = Path(__file__).parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def main():
    logger.info("Loading dataset...")
    df = pd.read_parquet(DATA_PATH)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    logger.info(f"  {len(df)} samples, {df['ts_code'].nunique()} stocks, {df['trade_date'].nunique()} dates")

    # 特征列
    feature_cols = [c for c in df.columns if c not in [
        'trade_date', 'ts_code', 'industry', 'target_ret_5d',
        'ind_ret_5d', 'ret_5d_industry_relative', 'ret_5d_industry_rank',
        'main_net_5d', 'main_net_20d', 'lhb_net_5d',  # 暂时缺失, 填 0
    ]]
    logger.info(f"  {len(feature_cols)} features: {feature_cols[:5]}...")

    X = df[feature_cols].astype(float).fillna(0).values
    # Target: 行业内排名 (0-1) → 转 0-4 整数 (lambdarank 要 int)
    df['target_rank'] = df.groupby(['trade_date', 'industry'])['target_ret_5d'].rank(pct=True)
    df['target_int'] = (df['target_rank'].fillna(0.5) * 4).round().fillna(2).astype(int).clip(0, 4)
    y = df['target_int'].values

    # Group: 每天每行业 = 1 个 query
    # Lambdarank 需要 group array, 同一 query 内样本是相关的
    df['query'] = df['trade_date'].dt.strftime('%Y%m%d') + '_' + df['industry'].astype(str)
    # 按 query 排序
    df = df.sort_values('query').reset_index(drop=True)
    X = df[feature_cols].astype(float).fillna(0).values
    y = df['target_rank'].values
    # query 边界
    query_ids, group_sizes = np.unique(df['query'].values, return_counts=True)
    group = group_sizes.tolist()
    logger.info(f"  {len(group_sizes)} queries, avg size {np.mean(group_sizes):.0f}")

    # 训练: 用 lightgbm lambdarank
    import lightgbm as lgb

    # 时序 split: 用最后 20% 日期做 OOS 测试
    dates = sorted(df['trade_date'].unique())
    split_date = dates[int(len(dates) * 0.8)]
    train_mask = df['trade_date'] < split_date
    test_mask = df['trade_date'] >= split_date
    logger.info(f"  Train: {df[train_mask]['trade_date'].min()} ~ {df[train_mask]['trade_date'].max()}")
    logger.info(f"  Test:  {df[test_mask]['trade_date'].min()} ~ {df[test_mask]['trade_date'].max()}")

    # 重新计算 group (按 query, 同一日期 + 行业 = 1 group)
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

    logger.info(f"  Train: {len(X_train)} samples, {len(train_group)} queries")
    logger.info(f"  Test:  {len(X_test)} samples, {len(test_group)} queries")

    train_data = lgb.Dataset(X_train, label=y_train, group=train_group, feature_name=feature_cols)
    test_data = lgb.Dataset(X_test, label=y_test, group=test_group, feature_name=feature_cols, reference=train_data)

    params = {
        'objective': 'lambdarank',
        'metric': 'ndcg',
        'ndcg_eval_at': [5, 10, 20],
        'learning_rate': 0.05,
        'num_leaves': 31,
        'min_child_samples': 100,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'lambda_l1': 0.1,
        'lambda_l2': 0.1,
        'verbose': -1,
    }
    model = lgb.train(
        params, train_data, num_boost_round=500,
        valid_sets=[test_data], valid_names=['test'],
        callbacks=[lgb.early_stopping(50), lgb.log_evaluation(50)],
    )

    # 特征重要性
    imp = pd.DataFrame({
        'feature': feature_cols,
        'importance': model.feature_importance(importance_type='gain'),
    }).sort_values('importance', ascending=False)
    logger.info(f"\nTop 15 重要因子:\n{imp.head(15).to_string(index=False)}")

    # 评估: NDCG
    y_pred = model.predict(X_test, num_iteration=model.best_iteration)
    test_df['pred'] = y_pred
    # 按行业 NDCG
    from sklearn.metrics import ndcg_score
    ndcg_list = []
    for q, g in test_df.groupby('query'):
        if len(g) >= 5:
            true = g['target_ret_5d'].values.reshape(1, -1)
            pred = g['pred'].values.reshape(1, -1)
            try:
                ndcg_list.append(ndcg_score(true, pred, k=5))
            except: pass
    logger.info(f"\nNDCG@5 (per industry): mean={np.mean(ndcg_list):.3f}, n={len(ndcg_list)}")

    # 信息系数 (IC): 预测排名 vs 真实收益排名的 spearman 相关
    from scipy.stats import spearmanr
    ic_list = []
    for q, g in test_df.groupby('query'):
        if len(g) >= 5:
            corr, _ = spearmanr(g['pred'], g['target_ret_5d'])
            if not np.isnan(corr):
                ic_list.append(corr)
    logger.info(f"IC (per query): mean={np.mean(ic_list):.3f}, std={np.std(ic_list):.3f}")
    if ic_list:
        ic_arr = np.array(ic_list)
        ir = ic_arr.mean() / ic_arr.std() * np.sqrt(252 / 5)  # 近似年化 IR
        logger.info(f"  IR (近似年化): {ir:.3f}")

    # 保存
    bundle = {
        'model': model,
        'feature_names': feature_cols,
        'best_iteration': model.best_iteration,
        'train_period': f"{df[train_mask]['trade_date'].min()} ~ {df[train_mask]['trade_date'].max()}",
        'test_period': f"{df[test_mask]['trade_date'].min()} ~ {df[test_mask]['trade_date'].max()}",
        'ndcg5_mean': float(np.mean(ndcg_list)) if ndcg_list else 0,
        'ic_mean': float(np.mean(ic_list)) if ic_list else 0,
        'ic_std': float(np.std(ic_list)) if ic_list else 0,
        'ir': float(ir) if ic_list else 0,
        'trained_at': datetime.now().isoformat(),
    }
    out = MODEL_DIR / 'factor_ranker_v1.pkl'
    joblib.dump(bundle, out)
    logger.info(f"\nSaved → {out}")
    logger.info(f"Best iteration: {model.best_iteration}")
    logger.info(f"Test NDCG@5: {bundle['ndcg5_mean']:.3f}")
    logger.info(f"Test IC: {bundle['ic_mean']:.3f} ± {bundle['ic_std']:.3f}")
    logger.info(f"IR (近似年化): {bundle['ir']:.3f}")


if __name__ == '__main__':
    main()
