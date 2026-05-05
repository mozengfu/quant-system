#!/usr/bin/env python3
"""
ML选股模型训练 V4 — 分市场状态建模 + 改进标签定义

改进点：
1. 分市场状态（顺市/震荡/逆市）各训练一个子模型，解决"一个模型通吃不精"问题
2. 标签定义改为 Top 20% vs Bottom 20%（去掉中间60%噪音样本），分类边界更清晰
3. 新增量价背离、资金加速度、均线发散度等Alpha特征
"""

import os, sys, json, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import numpy as np, pandas as pd, pymysql, lightgbm as lgb
from sklearn.metrics import roc_auc_score
from scipy.stats import spearmanr
import joblib

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MODEL_PATHS = {
    'bull': os.path.join(DATA_DIR, 'ml_stock_model_v4_bull.pkl'),
    'sideways': os.path.join(DATA_DIR, 'ml_stock_model_v4_sideways.pkl'),
    'bear': os.path.join(DATA_DIR, 'ml_stock_model_v4_bear.pkl'),
}
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'feature_config_v4.json')

EXCLUDE_PREFIXES = ('68', '83', '87', '43')

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db', 'connect_timeout': 5,
}

def get_db():
    return pymysql.connect(**DB_CONFIG)


def classify_market_regime(idx_row):
    """根据指数特征判定市场状态"""
    ma20 = idx_row.get('idx_ma20_ratio', 0)
    vol = idx_row.get('idx_vol_10d', 0)
    
    if ma20 > 0.02:
        return 'bull'
    elif ma20 < -0.02:
        return 'bear'
    else:
        return 'sideways'


def load_data():
    logger.info("加载最近400个交易日数据...")
    conn = get_db()
    
    daily = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               vol, amount, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 500 DAY
    """, conn)
    
    moneyflow = pd.read_sql("""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 500 DAY
    """, conn)
    
    index_data = pd.read_sql("""
        SELECT trade_date, change_pct, close_price
        FROM market_index_daily
        WHERE index_code='000001.SH'
        AND trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 500 DAY
    """, conn)
    
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    index_data['trade_date'] = pd.to_datetime(index_data['trade_date'])
    
    dates = sorted(daily['trade_date'].unique())
    min_date = dates[-400] if len(dates) > 400 else dates[0]
    max_date = dates[-1]
    
    conn.close()
    
    logger.info(f"行情: {len(daily):,} 行, {daily['ts_code'].nunique()} 股 | "
                f"资金流: {len(moneyflow):,} 行 | 指数: {len(index_data):,} 行")
    return daily, moneyflow, index_data, min_date, max_date


def build_features(daily, moneyflow, index_data, min_date, max_date):
    logger.info("构建特征...")
    
    # === 指数特征 ===
    idx = index_data.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma60'] = idx['close_price'].rolling(60).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    idx['idx_ma60_ratio'] = idx['close_price'] / idx['idx_ma60'].replace(0, np.nan) - 1
    idx['idx_pct_5d'] = idx['close_price'] / idx['close_price'].shift(5) - 1
    idx['idx_pct_20d'] = idx['close_price'] / idx['close_price'].shift(20) - 1
    idx['idx_vol_10d'] = idx['change_pct'].rolling(10).std()
    idx['idx_vol_20d'] = idx['change_pct'].rolling(20).std()
    # 趋势判定
    idx['idx_trend'] = 0
    idx.loc[idx['idx_ma5_ratio'] > 0, 'idx_trend'] = 1
    idx.loc[idx['idx_ma5_ratio'] < -0.02, 'idx_trend'] = -1
    # 均线发散度（牛熊信号）
    idx['ma_spread'] = idx['idx_ma5_ratio'] - idx['idx_ma20_ratio']
    
    results = []
    
    # 预处理资金流
    mf_dict = {}
    for ts_code, mf_group in moneyflow.groupby('ts_code'):
        mf_dict[ts_code] = mf_group.sort_values('trade_date')
    
    for ts_code, group in daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE_PREFIXES:
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 60:
            continue
        
        g = group.copy()
        
        # === 技术指标 ===
        g['vol_5d'] = g['pct_chg'].rolling(5).std()
        g['vol_10d'] = g['pct_chg'].rolling(10).std()
        g['vol_20d'] = g['pct_chg'].rolling(20).std()
        g['ma5_ma10_ratio'] = g['ma5'] / g['ma10'].replace(0, np.nan)
        g['ma10_ma20_ratio'] = g['ma10'] / g['ma20'].replace(0, np.nan)
        g['price_ma5_ratio'] = g['close'] / g['ma5'].replace(0, np.nan)
        g['price_ma20_ratio'] = g['close'] / g['ma20'].replace(0, np.nan)
        g['chg_3d'] = g['close'] / g['close'].shift(3) - 1
        g['chg_5d'] = g['close'] / g['close'].shift(5) - 1
        g['chg_10d'] = g['close'] / g['close'].shift(10) - 1
        g['chg_20d'] = g['close'] / g['close'].shift(20) - 1
        g['vr_ma5'] = g['volume_ratio'].rolling(5).mean()
        g['vr_ma10'] = g['volume_ratio'].rolling(10).mean()
        g['vol_trend'] = g['vr_ma5'] / g['vr_ma10'].replace(0, np.nan)
        g['pos_52w'] = (g['close'] - g['low_52w']) / (g['high_52w'] - g['low_52w']).replace(0, np.nan)
        g['rps_change'] = g['rps_20'].diff(5)
        g['up_ratio_5d'] = (g['pct_chg'] > 0).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].rolling(10).corr(g['pct_chg'])
        g['ma_pattern'] = 1
        g.loc[(g['ma5']>g['ma10'])&(g['ma10']>g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5']<g['ma10'])&(g['ma10']<g['ma20']), 'ma_pattern'] = 0
        
        # MACD
        g['ema12'] = g['close'].ewm(span=12, adjust=False).mean()
        g['ema26'] = g['close'].ewm(span=26, adjust=False).mean()
        g['macd_diff'] = (g['ema12'] - g['ema26']) / g['close']
        g['macd_signal_line'] = g['macd_diff'].ewm(span=9, adjust=False).mean()
        g['macd_hist'] = g['macd_diff'] - g['macd_signal_line']
        
        # RSI
        delta = g['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g['rsi_14'] = 100 - (100 / (1 + rs))
        
        # Alpha 因子（V3基础 + 新增）
        g['vol_price_corr_10d'] = g['vol'].rolling(10).corr(g['pct_chg'])
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].rolling(5).mean()
        
        # 新增：量价背离（价涨量缩 or 价跌量增 = 反转信号）
        g['vol_price_divergence'] = g['chg_5d'] * g['vol_trend']  # 正=量价齐升，负=背离
        g['vol_price_div_10d'] = g['chg_10d'] * g['vol_pct_corr']
        
        # 新增：均线发散度（个股级别）
        g['ma_spread_stock'] = g['price_ma5_ratio'] - g['price_ma20_ratio']
        
        # === 资金流特征 ===
        if ts_code in mf_dict:
            mf = mf_dict[ts_code]
            g = g.merge(mf[['trade_date','main_net','net_mf_amount',
                           'buy_sm_amount','sell_sm_amount','buy_lg_amount','sell_lg_amount']],
                       on='trade_date', how='left')
            g['amount_est'] = g['vol'] * g['close'] * 100
            g['main_net_ratio'] = g['main_net'] / g['amount_est'].replace(0, np.nan)
            g['main_net_ma5'] = g['main_net_ratio'].rolling(5).mean()
            g['main_net_ma10'] = g['main_net_ratio'].rolling(10).mean()
            g['main_trend'] = g['main_net_ma5'] / g['main_net_ma10'].replace(0, np.nan)
            g['main_pos'] = (g['main_net'] > 0).astype(int)
            g['main_streak'] = g['main_pos'].rolling(5).sum()
            g['retail_net'] = g['buy_sm_amount'].fillna(0) - g['sell_sm_amount'].fillna(0)
            g['main_vs_retail'] = (g['main_net'] - g['retail_net']) / g['amount_est'].replace(0, np.nan)
            g['lg_ratio'] = (g['buy_lg_amount'].fillna(0) + g['sell_lg_amount'].fillna(0)) / \
                           (g['buy_sm_amount'].fillna(0) + g['sell_sm_amount'].fillna(0) + 1)
            g['main_cum5'] = g['main_net_ratio'].rolling(5).sum()
            g['main_cum10'] = g['main_net_ratio'].rolling(10).sum()
            g['main_accel_3d'] = g['main_net_ratio'].diff(3)
            g['smart_div_count'] = ((g['pct_chg'] < 0) & (g['main_net'] > 0)).rolling(5).sum()
            # 新增：资金流比例化特征
            g['main_inflow_ratio'] = g['buy_lg_amount'].fillna(0) / (g['main_net'].abs() + 1)
            g['main_flow_accel'] = g['main_net_ratio'].diff(1) / (g['main_net_ratio'].abs().rolling(5).mean() + 1e-9)
        else:
            for c in ['main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
                      'main_streak','main_vs_retail','lg_ratio','main_cum5','main_cum10',
                      'main_accel_3d', 'smart_div_count', 'main_inflow_ratio', 'main_flow_accel']:
                g[c] = np.nan
        
        # 大盘环境特征
        g = g.merge(idx[['trade_date','idx_ma5_ratio','idx_ma20_ratio','idx_ma60_ratio',
                         'idx_pct_5d','idx_pct_20d','idx_vol_10d','idx_vol_20d','idx_trend','ma_spread']],
                    on='trade_date', how='left')
        
        # 未来5天收益（用于标签）
        g['target_5d'] = g['close'].shift(-5) / g['close'] - 1
        
        valid = g.dropna(subset=['target_5d'])
        valid = valid[valid['trade_date'] >= pd.Timestamp(min_date)]
        if len(valid) < 10:
            continue
        
        results.append(valid)
    
    if not results:
        logger.warning("无有效样本"); return pd.DataFrame(), {}, []
    
    result = pd.concat(results, ignore_index=True)
    
    # === 改进标签：Top 20% vs Bottom 20% ===
    logger.info("生成二极化横截面标签 (Top 20% vs Bottom 20%)...")
    
    result = result[result['target_5d'].notna()].copy()
    result = result.sort_values(['trade_date', 'target_5d'])
    
    def bipolar_label(group):
        """Top 20% = 1, Bottom 20% = 0, 中间60%丢弃"""
        n = len(group)
        if n < 10:
            return pd.Series(-1, index=group.index)  # -1 = 丢弃
        high = group['target_5d'].quantile(0.80)
        low = group['target_5d'].quantile(0.20)
        
        labels = pd.Series(-1, index=group.index)
        labels[group['target_5d'] >= high] = 1
        labels[group['target_5d'] <= low] = 0
        return labels
    
    labels = result.groupby('trade_date', group_keys=False).apply(bipolar_label)
    result['label_5d'] = labels.reindex(result.index).fillna(-1).astype(int)
    
    # 丢弃中间样本
    n_total = len(result)
    result = result[result['label_5d'] >= 0].copy()
    n_kept = len(result)
    pos_ratio = result['label_5d'].mean()
    logger.info(f"二极化标签: 保留 {n_kept:,}/{n_total:,} ({n_kept/n_total*100:.0f}%), "
                f"正样本占比 {pos_ratio*100:.1f}%")
    
    # === 特征列 ===
    feature_cols = [
        # 量价基础
        'pct_chg','turnover_rate','volume_ratio',
        'vol_5d','vol_10d','vol_20d',
        'ma5_ma10_ratio','ma10_ma20_ratio','price_ma5_ratio','price_ma20_ratio',
        'chg_3d','chg_5d','chg_10d','chg_20d','vol_trend','pos_52w',
        'rps_20','rps_change','up_ratio_5d','up_ratio_10d','vol_pct_corr',
        'ma_pattern',
        # MACD/RSI
        'macd_diff','macd_signal_line','macd_hist',
        'rsi_14',
        # 资金流
        'main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
        'main_streak','main_vs_retail','lg_ratio','main_cum5','main_cum10',
        'main_accel_3d', 'smart_div_count',
        'main_inflow_ratio', 'main_flow_accel',
        # 大盘环境
        'idx_ma5_ratio','idx_ma20_ratio','idx_ma60_ratio',
        'idx_pct_5d','idx_pct_20d','idx_vol_10d','idx_vol_20d','idx_trend','ma_spread',
        # Alpha因子
        'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
        'amihud', 'amihud_ma5',
        # 新增Alpha
        'vol_price_divergence', 'vol_price_div_10d', 'ma_spread_stock',
    ]
    
    for col in feature_cols:
        if col not in result.columns:
            result[col] = np.nan
    
    # 填充NaN
    global_medians = {}
    for col in feature_cols:
        med = result[col].median()
        global_medians[col] = float(med) if not np.isnan(med) else 0.0
        result[col] = result[col].fillna(global_medians[col])
    
    logger.info(f"构建完成: {len(result):,} 样本, {result['ts_code'].nunique()} 股, {len(feature_cols)} 个特征")
    return result, global_medians, feature_cols


def walk_forward_regime_train(df, feature_cols, regime):
    """按市场状态做Walk-Forward验证"""
    logger.info(f"Walk-Forward [{regime}] 滚动窗口验证...")
    
    df_sorted = df.sort_values('trade_date').copy()
    dates = sorted(df_sorted['trade_date'].unique())
    
    window_size = len(dates) // 7
    
    cv_results = []
    models = []
    
    for fold in range(5):
        train_end_idx = (fold + 1) * window_size
        val_start_idx = train_end_idx
        val_end_idx = min((fold + 2) * window_size, len(dates))
        
        if val_start_idx >= len(dates) or val_end_idx <= val_start_idx:
            break
        
        train_dates = dates[:train_end_idx]
        val_dates = dates[val_start_idx:val_end_idx]
        
        train_mask = df_sorted['trade_date'].isin(train_dates)
        val_mask = df_sorted['trade_date'].isin(val_dates)
        
        X_train = df_sorted.loc[train_mask, feature_cols].values
        y_train = df_sorted.loc[train_mask, 'label_5d'].values
        X_val = df_sorted.loc[val_mask, feature_cols].values
        y_val = df_sorted.loc[val_mask, 'label_5d'].values
        
        if len(X_train) < 500 or len(X_val) < 50:
            logger.info(f"  Fold {fold+1}: 样本不足 (train={len(X_train)}, val={len(X_val)}), 跳过")
            continue
        
        # 检查标签分布
        if len(np.unique(y_train)) < 2 or len(np.unique(y_val)) < 2:
            logger.info(f"  Fold {fold+1}: 标签类别不足, 跳过")
            continue
        
        td = lgb.Dataset(X_train, label=y_train)
        vd = lgb.Dataset(X_val, label=y_val, reference=td)
        
        params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'min_child_samples': 50,  # 样本减少，降低阈值
            'verbose': -1,
            'seed': 42 + fold,
            'is_unbalance': True,
        }
        
        model = lgb.train(
            params, td, num_boost_round=500, valid_sets=[vd],
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)]
        )
        
        vp = model.predict(X_val)
        auc = roc_auc_score(y_val, vp)
        spearman = spearmanr(y_val, vp)[0]
        
        n_select = max(10, int(len(vp) * 0.10))
        top_idx = np.argsort(vp)[-n_select:]
        top_win_rate = y_val[top_idx].mean() * 100
        
        cv_results.append({
            'fold': fold + 1,
            'train_period': f"{train_dates[0]} ~ {train_dates[-1]}",
            'val_period': f"{val_dates[0]} ~ {val_dates[-1]}",
            'auc': auc,
            'spearman': spearman,
            'top10_win_rate': top_win_rate,
        })
        
        models.append(model)
        
        r = cv_results[-1]
        logger.info(f"  Fold {fold+1}: AUC={r['auc']:.3f}, Spearman={r['spearman']:.3f}, "
                    f"Top10%胜率={r['top10_win_rate']:.1f}% | "
                    f"训练期: {r['train_period']} → 验证期: {r['val_period']}")
    
    if not cv_results:
        logger.warning(f"  [{regime}] Walk-Forward验证失败，无有效fold")
        return None, None
    
    avg_auc = np.mean([r['auc'] for r in cv_results])
    avg_spearman = np.mean([r['spearman'] for r in cv_results])
    avg_top10 = np.mean([r['top10_win_rate'] for r in cv_results])
    
    logger.info(f"  [{regime}] 平均: AUC={avg_auc:.3f}, Spearman={avg_spearman:.3f}, Top10%胜率={avg_top10:.1f}%")
    
    return cv_results, models


def train_final_model(df, feature_cols):
    """用全部数据训练最终模型"""
    df_sorted = df.sort_values('trade_date')
    X = df_sorted[feature_cols].values
    y = df_sorted['label_5d'].values
    
    params = {
        'objective': 'binary',
        'metric': 'auc',
        'boosting_type': 'gbdt',
        'num_leaves': 31,
        'learning_rate': 0.05,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'min_child_samples': 50,
        'verbose': -1,
        'seed': 42,
        'is_unbalance': True,
    }
    
    final_model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=300)
    
    imp = dict(zip(feature_cols, final_model.feature_importance()))
    imp = dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))
    
    logger.info("  特征重要性 Top 10:")
    for k, v in list(imp.items())[:10]:
        logger.info(f"    {k}: {v}")
    
    return final_model, imp


def main():
    start = datetime.now()
    
    # 第1步：加载数据
    daily, moneyflow, index_data, min_date, max_date = load_data()
    
    # 第2步：构建特征
    features, global_medians, feature_cols = build_features(
        daily, moneyflow, index_data, min_date, max_date
    )
    if features.empty:
        logger.error("特征构建失败")
        return
    
    # 第3步：按市场状态拆分训练数据
    # 需要先添加市场状态标签
    logger.info("按市场状态拆分数据...")
    
    # 获取每个日期的市场状态
    idx = index_data.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma60'] = idx['close_price'].rolling(60).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    
    regime_map = {}
    for _, row in idx.iterrows():
        if row['idx_ma20_ratio'] > 0.02:
            regime_map[row['trade_date']] = 'bull'
        elif row['idx_ma20_ratio'] < -0.02:
            regime_map[row['trade_date']] = 'bear'
        else:
            regime_map[row['trade_date']] = 'sideways'
    
    features['regime'] = features['trade_date'].map(regime_map).fillna('sideways')
    
    regime_counts = features['regime'].value_counts()
    logger.info(f"市场状态分布: {dict(regime_counts)}")
    
    # 第4步：按状态分别训练
    os.makedirs(DATA_DIR, exist_ok=True)
    
    all_bundles = {}
    
    for regime in ['bull', 'sideways', 'bear']:
        logger.info(f"\n{'='*60}")
        logger.info(f"训练 [{regime.upper()}] 模型")
        logger.info(f"{'='*60}")
        
        regime_df = features[features['regime'] == regime].copy()
        
        if len(regime_df) < 500:
            logger.warning(f"  [{regime}] 样本不足 ({len(regime_df)})，跳过")
            continue
        
        # Walk-Forward 验证
        cv_results, fold_models = walk_forward_regime_train(regime_df, feature_cols, regime)
        
        if cv_results is None:
            logger.warning(f"  [{regime}] 验证失败，跳过")
            continue
        
        # 训练最终模型
        final_model, importance = train_final_model(regime_df, feature_cols)
        
        # 保存bundle
        bundle = {
            'model': final_model,
            'feature_cols': feature_cols,
            'cv_results': cv_results,
            'importance': importance,
            'global_medians': global_medians,
            'model_type': 'binary_classification',
            'label_type': 'bipolar_cross_section',  # 新标签类型
            'regime': regime,
            'validation_strategy': 'walk_forward',
            'trained_at': datetime.now().isoformat(),
            'avg_auc': np.mean([r['auc'] for r in cv_results]),
            'avg_spearman': np.mean([r['spearman'] for r in cv_results]),
            'avg_top10_win_rate': np.mean([r['top10_win_rate'] for r in cv_results]),
        }
        
        joblib.dump(bundle, MODEL_PATHS[regime])
        logger.info(f"  [{regime}] 模型保存: {MODEL_PATHS[regime]}")
        
        all_bundles[regime] = bundle
    
    # 第5步：保存特征配置
    config = {
        'feature_cols': feature_cols,
        'global_medians': global_medians,
        'model_paths': {k: v for k, v in MODEL_PATHS.items()},
        'model_type': 'binary_classification',
        'label_type': 'bipolar_cross_section',
        'regime_models': list(all_bundles.keys()),
        'validation_strategy': 'walk_forward',
        'trained_at': datetime.now().isoformat(),
    }
    with open(FEATURE_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, default=str)
    
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"\n{'='*60}")
    logger.info(f"完成! 耗时: {elapsed:.1f}s")
    logger.info(f"{'='*60}")
    
    # 汇总结果
    for regime, bundle in all_bundles.items():
        logger.info(f"[{regime.upper()}] AUC={bundle['avg_auc']:.3f}, "
                     f"Spearman={bundle['avg_spearman']:.3f}, "
                     f"Top10%胜率={bundle['avg_top10_win_rate']:.1f}%")


if __name__ == '__main__':
    main()
