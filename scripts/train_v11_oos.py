#!/usr/bin/env python3
"""
V11.0 OOS训练 — 简化版，只用可用特征，严格隔离回测期
"""
import logging
import os
import sys
import warnings
from datetime import datetime

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import pymysql
from scipy.stats import spearmanr

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

TRAIN_END = "2025-05-31"
TRAIN_START = "2023-01-01"
MODEL_OUT = os.path.join(BASE_DIR, "data", "ml_stock_model_v11_0_oos_v3.pkl")

def build_features(conn):
    """直接SQL构建特征+标签，避免复杂的管线依赖"""
    logger.info("加载数据...")

    # 行情 + 资金流 + daily_basic 联合查询
    df = pd.read_sql(f"""
        SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close, d.pre_close,
               d.vol, d.amount, d.pct_chg, d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20, d.rps_20,
               COALESCE(mf.main_net, 0) as main_net,
               COALESCE(mf.net_mf_amount, 0) as net_mf_amount,
               COALESCE(mf.buy_lg_amount, 0) as buy_lg_amount,
               COALESCE(mf.sell_lg_amount, 0) as sell_lg_amount,
               COALESCE(mf.buy_sm_amount, 0) as buy_sm_amount,
               COALESCE(mf.sell_sm_amount, 0) as sell_sm_amount,
               COALESCE(db2.pe_ttm, 0) as pe_ttm,
               COALESCE(db2.pb, 0) as pb,
               COALESCE(db2.total_mv, 0) as total_mv,
               COALESCE(db2.circ_mv, 0) as circ_mv
        FROM daily_price d
        LEFT JOIN moneyflow_daily mf ON d.ts_code=mf.ts_code AND d.trade_date=mf.trade_date
        LEFT JOIN daily_basic db2 ON d.ts_code=db2.ts_code AND d.trade_date=db2.trade_date
        WHERE d.trade_date >= '{TRAIN_START}' AND d.trade_date <= '{TRAIN_END}'
          AND LEFT(d.ts_code,1) NOT IN ('8','4','9') AND d.close <= 200 AND d.close > 1
        ORDER BY d.ts_code, d.trade_date
    """, conn)
    logger.info(f"原始数据: {len(df)} 行, {df['ts_code'].nunique()} 只")

    df['trade_date'] = pd.to_datetime(df['trade_date'])

    # 排序并构建时间序列特征
    df = df.sort_values(['ts_code','trade_date']).reset_index(drop=True)

    g = df.groupby('ts_code')

    # 未来5日收益（标签）
    df['future_close'] = g['close'].shift(-5)
    df['label'] = (df['future_close'] / df['close'] - 1).clip(-0.5, 0.5)

    # 动量特征
    df['chg_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['chg_10d'] = g['pct_chg'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['chg_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).sum())

    # 波动率
    df['volatility_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).std())
    df['volatility_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).std())

    # 收益/波动比
    df['ret_vol_ratio'] = df['chg_5d'] / (df['volatility_5d'] + 0.01)

    # 资金流累计
    df['main_cum5'] = g['main_net'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['main_cum10'] = g['main_net'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['main_cum20'] = g['main_net'].transform(lambda x: x.rolling(20, min_periods=1).sum())

    # 资金流背离
    df['flow_div_5d'] = df['main_cum5'] - df['chg_5d']
    df['flow_div_10d'] = df['main_cum10'] - df['chg_10d']

    # 价格位置
    df['high_20d'] = g['high'].transform(lambda x: x.rolling(20, min_periods=1).max())
    df['low_20d'] = g['low'].transform(lambda x: x.rolling(20, min_periods=1).min())
    df['pos_20d'] = (df['close'] - df['low_20d']) / (df['high_20d'] - df['low_20d'] + 0.01)

    # 均线偏离
    df['ma5_bias'] = (df['close'] - df['ma5']) / (df['ma5'] + 0.01)
    df['ma10_bias'] = (df['close'] - df['ma10']) / (df['ma10'] + 0.01)
    df['ma20_bias'] = (df['close'] - df['ma20']) / (df['ma20'] + 0.01)

    # 量比
    df['vol_change_5d'] = df['vol'] / (g['vol'].shift(5) + 1)

    # 换手率变化
    df['turnover_change'] = df['turnover_rate'] / (g['turnover_rate'].shift(5) + 0.01)

    # 大单净买入比
    df['lg_net'] = df['buy_lg_amount'] - df['sell_lg_amount']
    df['sm_net'] = df['buy_sm_amount'] - df['sell_sm_amount']
    df['lg_net_ratio'] = df['lg_net'] / (df['amount'] + 1)

    # 对数市值
    df['log_mv'] = np.log(df['total_mv'].clip(lower=1e7) + 1)

    # 反转
    df['rev_1d'] = -df['pct_chg']

    # 振幅
    df['amplitude'] = (df['high'] - df['low']) / (df['pre_close'] + 0.01) * 100

    # 删除NaN
    df = df.dropna(subset=['label'])
    df = df.replace([np.inf, -np.inf], 0)

    feature_cols = [
        'chg_5d', 'chg_10d', 'chg_20d', 'volatility_5d', 'volatility_20d',
        'ret_vol_ratio', 'main_cum5', 'main_cum10', 'main_cum20',
        'flow_div_5d', 'flow_div_10d', 'pos_20d',
        'ma5_bias', 'ma10_bias', 'ma20_bias', 'vol_change_5d',
        'turnover_change', 'lg_net_ratio', 'log_mv', 'rev_1d', 'amplitude',
        'rps_20', 'volume_ratio', 'turnover_rate', 'pe_ttm', 'pb',
        'net_mf_amount', 'main_net', 'lg_net', 'sm_net',
    ]

    feature_cols = [c for c in feature_cols if c in df.columns]
    logger.info(f"可用特征: {len(feature_cols)}")

    # 只取成交量前500的股票做训练（流动性筛选）
    df['rank_date'] = df['trade_date'].astype(str)
    top_vol = df.groupby('rank_date', group_keys=False).apply(
        lambda x: x.nlargest(500, 'amount')
    ).reset_index(drop=True)

    logger.info(f"Top500筛选后: {len(top_vol)} 样本")

    return top_vol, feature_cols

def main():
    conn = pymysql.connect(**DB_CONFIG)
    df, feature_cols = build_features(conn)
    conn.close()

    # 时间序列分割：最后60天做验证
    dates = sorted(df['trade_date'].unique())
    valid_cutoff = dates[-60]
    train_df = df[df['trade_date'] < valid_cutoff]
    valid_df = df[df['trade_date'] >= valid_cutoff]
    logger.info(f"训练: {len(train_df)}, 验证: {len(valid_df)}")

    X_train = train_df[feature_cols].fillna(0).values.astype(np.float32)
    y_train = train_df['label'].values.astype(np.float32)
    X_valid = valid_df[feature_cols].fillna(0).values.astype(np.float32)
    y_valid = valid_df['label'].values.astype(np.float32)

    # 特征分组（9个子模型）
    subsets = {
        'momentum': ['chg_5d','chg_10d','chg_20d','volatility_5d','volatility_20d','ret_vol_ratio','rps_20','rev_1d'],
        'volume': ['volume_ratio','turnover_rate','vol_change_5d','turnover_change','amplitude'],
        'moneyflow': ['main_net','main_cum5','main_cum10','main_cum20','flow_div_5d','flow_div_10d',
                      'net_mf_amount','lg_net','sm_net','lg_net_ratio'],
        'price_pos': ['pos_20d','ma5_bias','ma10_bias','ma20_bias'],
        'value': ['pe_ttm','pb','log_mv'],
        'general1': ['chg_5d','volume_ratio','main_cum5','pos_20d','log_mv','rev_1d','volatility_5d'],
        'general2': ['chg_10d','turnover_rate','main_cum10','ma5_bias','pe_ttm','amplitude','flow_div_5d'],
        'general3': ['chg_20d','vol_change_5d','main_cum20','ma10_bias','pb','lg_net_ratio','ret_vol_ratio'],
        'general4': ['chg_5d','chg_10d','volatility_20d','ma20_bias','turnover_change','flow_div_10d','net_mf_amount'],
    }

    lgb_params = {
        'objective': 'regression', 'metric': 'rmse',
        'num_leaves': 31, 'learning_rate': 0.05, 'min_child_samples': 500,
        'lambda_l1': 0.1, 'lambda_l2': 1.0,
        'feature_fraction': 0.7, 'bagging_fraction': 0.7, 'bagging_freq': 5,
        'verbose': -1, 'seed': 42, 'n_jobs': -1,
    }

    models = []
    cv_results = []
    global_medians = {}

    for i, c in enumerate(feature_cols):
        global_medians[c] = float(np.median(X_train[:, i]))

    for subset_name, desired in subsets.items():
        avail = [c for c in desired if c in feature_cols]
        if len(avail) < 2: continue

        idxs = [feature_cols.index(c) for c in avail]
        Xs = X_train[:, idxs]
        Xv = X_valid[:, idxs]

        logger.info(f"训练 {subset_name} ({len(avail)}特征)...")
        dtrain = lgb.Dataset(Xs, label=y_train)
        dvalid = lgb.Dataset(Xv, label=y_valid, reference=dtrain)

        model = lgb.train(lgb_params, dtrain, num_boost_round=500,
                         valid_sets=[dtrain, dvalid], valid_names=['train','valid'],
                         callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)])
        models.append(model)

        pred = model.predict(Xv)
        ic = spearmanr(pred, y_valid)[0]
        cv_results.append({'subset': subset_name, 'features': len(avail), 'valid_ic': round(ic, 4)})
        logger.info(f"  {subset_name}: Valid IC={ic:.4f}")

    # 集成IC
    all_preds = []
    for mi in range(len(models)):
        subset_name = list(subsets.keys())[mi]
        avail = [c for c in subsets[subset_name] if c in feature_cols]
        idxs = [feature_cols.index(c) for c in avail]
        all_preds.append(models[mi].predict(X_valid[:, idxs]))

    ensemble_pred = np.mean(np.column_stack(all_preds), axis=1)
    ensemble_ic = spearmanr(ensemble_pred, y_valid)[0]
    logger.info(f"集成 Valid RankIC: {ensemble_ic:.4f}")

    # 保存
    bundle = {
        'model_type': 'lgb_ensemble',
        'version': 'v11.0_oos',
        'models': models,
        'feature_subsets': {k: [c for c in v if c in feature_cols] for k, v in subsets.items()},
        'feature_cols': feature_cols,
        'global_medians': global_medians,
        'trained_at': datetime.now().isoformat(),
        'n_samples': len(X_train),
        'n_features': len(feature_cols),
        'n_models': len(models),
        'data_range': f"{TRAIN_START} ~ {TRAIN_END}",
        'final_rank_ic': round(ensemble_ic, 4),
        'cv_results': cv_results,
    }

    if os.path.exists(MODEL_OUT):
        backup = MODEL_OUT.replace('.pkl', '_backup.pkl')
        os.rename(MODEL_OUT, backup)

    joblib.dump(bundle, MODEL_OUT)
    logger.info(f"=== 完成 OOS训练: {MODEL_OUT}, IC={ensemble_ic:.4f} ===")

if __name__ == "__main__":
    main()
