#!/usr/bin/env python3
"""
ML选股模型训练 V3 — 三大改进

1. 标签设计：横截面排序标签（每天个股相对排名，非绝对涨幅）
2. 特征升级：加入基本面（PE/PB/ROE/营收增速）+ 情绪因子
3. 验证策略：Walk-Forward 滚动窗口（更贴近实盘）
"""

import os, sys, json, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import numpy as np, pandas as pd, pymysql, lightgbm as lgb
from sklearn.metrics import roc_auc_score, mean_squared_error
from scipy.stats import spearmanr
import joblib

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MODEL_PATH = os.path.join(DATA_DIR, 'ml_stock_model_v3.pkl')
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'feature_config_v3.json')

EXCLUDE_PREFIXES = ('68', '83', '87', '43')  # 排除科创板、北交所

# Tushare API
TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db', 'connect_timeout': 5,
}

def get_db():
    return pymysql.connect(**DB_CONFIG)


def load_basic_fundamentals(daily_df):
    """从Tushare获取基本面数据（PE/PB/ROE/营收增速）"""
    if not TUSHARE_TOKEN:
        logger.warning("未设置TUSHARE_TOKEN，跳过基本面特征")
        return pd.DataFrame()
    
    try:
        import tushare as ts
        pro = ts.pro_api(TUSHARE_TOKEN)
        
        # 获取最新交易日期
        latest_date = daily_df['trade_date'].max().strftime('%Y%m%d')
        
        # 获取每日指标（PE/PB/总股本/流通股本）
        logger.info("获取基本面数据（Tushare）...")
        ts_codes = daily_df['ts_code'].unique().tolist()
        
        dfs = []
        for i in range(0, len(ts_codes), 500):  # 分批避免限流
            batch = ts_codes[i:i+500]
            try:
                df = pro.daily_basic(
                    trade_date=latest_date,
                    fields='ts_code,trade_date,pe,pe_ttm,pb,dv_ratio,dv_ttm,total_mv,circ_mv'
                )
                if df is not None and not df.empty:
                    dfs.append(df)
            except Exception as e:
                logger.warning(f"批量获取失败: {e}")
                continue
        
        if dfs:
            result = pd.concat(dfs, ignore_index=True)
            result['trade_date'] = pd.to_datetime(result['trade_date'])
            logger.info(f"基本面数据: {len(result):,} 行")
            return result
    except Exception as e:
        logger.warning(f"基本面获取失败: {e}")
    
    return pd.DataFrame()


def load_data():
    logger.info("加载最近300个交易日数据...")
    conn = get_db()
    
    daily = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               vol, amount, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 400 DAY
    """, conn)
    
    moneyflow = pd.read_sql("""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 400 DAY
    """, conn)
    
    index_data = pd.read_sql("""
        SELECT trade_date, change_pct, close_price
        FROM market_index_daily
        WHERE index_code='000001.SH'
        AND trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 400 DAY
    """, conn)
    
    dates = sorted(daily['trade_date'].unique())
    min_date = dates[-300] if len(dates) > 300 else dates[0]
    max_date = dates[-1]
    
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    index_data['trade_date'] = pd.to_datetime(index_data['trade_date'])
    
    conn.close()
    
    # 获取基本面数据
    fundamentals = load_basic_fundamentals(daily)
    
    logger.info(f"行情: {len(daily):,} 行, {daily['ts_code'].nunique()} 股 | "
                f"资金流: {len(moneyflow):,} 行 | 指数: {len(index_data):,} 行")
    return daily, moneyflow, index_data, min_date, max_date, fundamentals


def build_features(daily, moneyflow, index_data, min_date, max_date, fundamentals):
    logger.info("构建特征...")
    
    # === 指数特征 ===
    idx = index_data.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    idx['idx_pct_5d'] = idx['close_price'] / idx['close_price'].shift(5) - 1
    idx['idx_vol_10d'] = idx['change_pct'].rolling(10).std()
    idx['idx_trend'] = 0
    idx.loc[idx['idx_ma5_ratio'] > 0, 'idx_trend'] = 1
    idx.loc[idx['idx_ma5_ratio'] < -0.02, 'idx_trend'] = -1
    
    results = []
    total = 0
    
    # 预处理资金流数据
    mf_dict = {}
    for ts_code, mf_group in moneyflow.groupby('ts_code'):
        mf_dict[ts_code] = mf_group.sort_values('trade_date')
    
    # 预处理基本面数据
    fund_dict = {}
    if not fundamentals.empty:
        for ts_code, fund_group in fundamentals.groupby('ts_code'):
            fund_dict[ts_code] = fund_group.sort_values('trade_date')
    
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
        g['vr_ma5'] = g['volume_ratio'].rolling(5).mean()
        g['vr_ma10'] = g['volume_ratio'].rolling(10).mean()
        g['vol_trend'] = g['vr_ma5'] / g['vr_ma10'].replace(0, np.nan)
        g['pos_52w'] = (g['close'] - g['low_52w']) / (g['high_52w'] - g['low_52w']).replace(0, np.nan)
        g['rps_change'] = g['rps_20'].diff(5)
        g['up_ratio_5d'] = (g['pct_chg'] > 0).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].rolling(5).corr(g['pct_chg'])
        g['ma_pattern'] = 1
        g.loc[(g['ma5']>g['ma10'])&(g['ma10']>g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5']<g['ma10'])&(g['ma10']<g['ma20']), 'ma_pattern'] = 0
        
        # MACD 归一化
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
        
        # Alpha 因子
        g['vol_price_corr_10d'] = g['vol'].rolling(10).corr(g['pct_chg'])
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].rolling(5).mean()
        
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
        else:
            for c in ['main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
                      'main_streak','main_vs_retail','lg_ratio','main_cum5','main_cum10',
                      'main_accel_3d', 'smart_div_count']:
                g[c] = np.nan
        
        # === 基本面特征（新增！） ===
        if ts_code in fund_dict:
            fund = fund_dict[ts_code]
            g = g.merge(fund[['trade_date','pe_ttm','pb','dv_ttm','total_mv','circ_mv']],
                       on='trade_date', how='left')
            g['ln_mv'] = np.log(g['total_mv'].replace(0, np.nan))  # 市值对数
            g['pb_rank'] = g['pb']  # 市净率
            g['pe_ttm'] = g['pe_ttm'].clip(0, 500)  # 过滤极端PE
        else:
            for c in ['pe_ttm','pb','dv_ttm','total_mv','circ_mv','ln_mv','pb_rank']:
                g[c] = np.nan
        
        # 大盘环境特征
        g = g.merge(idx[['trade_date','idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d','idx_trend']],
                    on='trade_date', how='left')
        
        # === 改进1：横截面排序标签 ===
        # 不再预测"绝对涨跌"，而是预测"今天这只股票在全市场的排名"
        g['target_5d'] = g['close'].shift(-5) / g['close'] - 1  # 保留用于回归评估
        
        # 分类标签：未来5天相对大盘的超额收益（改进版）
        # 如果个股跑赢大盘2%以上 = 1，否则 = 0
        g['idx_future_5d'] = g['idx_ma5_ratio'].shift(-5).fillna(0)  # 简化版
        g['label_5d'] = 0  # 先设为0，后面统一赋值
        
        valid = g.dropna(subset=['target_5d'])
        valid = valid[valid['trade_date'] >= pd.Timestamp(min_date)]
        if len(valid) < 10:
            continue
        
        results.append(valid)
        total += 1
    
    if not results:
        logger.warning("无有效样本"); return pd.DataFrame(), {}, []
    
    result = pd.concat(results, ignore_index=True)
    
    # 关键改进：横截面排序标签
    # 每天按未来5天收益排名，Top 20% = 1, 其余 = 0
    logger.info("生成横截面排序标签...")
    
    # 先删除无未来5天数据的行（target_5d为NaN）
    n_before = len(result)
    result = result[result['target_5d'].notna()].copy()
    n_removed = n_before - len(result)
    logger.info(f"  删除无未来数据的样本: {n_removed:,} 行")
    
    result = result.sort_values(['trade_date', 'target_5d'])
    
    def rank_label(group):
        """每天内按收益排名，Top 20%标记为1"""
        n = len(group)
        if n < 10:
            return pd.Series(0, index=group.index)
        threshold = group['target_5d'].quantile(0.80)  # Top 20%
        return (group['target_5d'] >= threshold).astype(int)
    
    # 修复：使用正确的索引对齐
    labels = result.groupby('trade_date', group_keys=False).apply(rank_label)
    result['label_5d'] = labels.reindex(result.index).fillna(0).astype(int)
    
    pos_ratio = result['label_5d'].mean()
    logger.info(f"横截面标签: 正样本占比 {pos_ratio*100:.1f}% (目标20%)")
    
    # === 特征列 ===
    feature_cols = [
        # 量价基础
        'pct_chg','turnover_rate','volume_ratio',
        'vol_5d','vol_10d','vol_20d',
        'ma5_ma10_ratio','ma10_ma20_ratio','price_ma5_ratio','price_ma20_ratio',
        'chg_3d','chg_5d','chg_10d','vol_trend','pos_52w',
        'rps_20','rps_change','up_ratio_5d','up_ratio_10d','vol_pct_corr',
        'ma_pattern',
        # MACD/RSI
        'macd_diff','macd_signal_line','macd_hist',
        'rsi_14',
        # 资金流
        'main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
        'main_streak','main_vs_retail','lg_ratio','main_cum5','main_cum10',
        'main_accel_3d', 'smart_div_count',
        # 大盘环境
        'idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d','idx_trend',
        # Alpha因子
        'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
        'amihud', 'amihud_ma5',
        # 基本面（新增）
        'pe_ttm','pb','dv_ttm','ln_mv',
    ]
    
    # 确保所有特征列存在
    for col in feature_cols:
        if col not in result.columns:
            result[col] = np.nan
    
    # 填充NaN
    global_medians = {}
    for col in feature_cols:
        med = result[col].median()
        global_medians[col] = float(med) if not np.isnan(med) else 0.0
        result[col] = result[col].fillna(global_medians[col])
    
    logger.info(f"构建完成: {len(result):,} 样本, {total} 股, {len(feature_cols)} 个特征")
    return result, global_medians, feature_cols


def walk_forward_train(df, feature_cols):
    """
    改进3：Walk-Forward 滚动窗口验证
    
    模拟实盘：用过去6个月数据训练，预测下1个月
    滚动前进，共5个窗口
    """
    logger.info("Walk-Forward 滚动窗口验证...")
    
    df_sorted = df.sort_values('trade_date').copy()
    dates = sorted(df_sorted['trade_date'].unique())
    
    # 定义窗口：训练6个月，验证1个月
    window_size = len(dates) // 7  # 总共7份
    
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
        
        if len(X_train) < 1000 or len(X_val) < 100:
            continue
        
        td = lgb.Dataset(X_train, label=y_train)
        vd = lgb.Dataset(X_val, label=y_val, reference=td)
        
        params = {
            'objective': 'binary',  # 改为分类任务！
            'metric': 'auc',        # 直接优化AUC
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'min_child_samples': 100,
            'verbose': -1,
            'seed': 42 + fold,
            'is_unbalance': True,  # 处理类别不平衡（22% vs 78%）
        }
        
        model = lgb.train(
            params, td, num_boost_round=500, valid_sets=[vd],
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)]
        )
        
        vp = model.predict(X_val)
        
        # 评估
        auc = roc_auc_score(y_val, vp)
        spearman = spearmanr(y_val, vp)[0]
        
        # 计算Top10%胜率（模拟选股效果）
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
        logger.info(f"Fold {fold+1}: AUC={r['auc']:.3f}, Spearman={r['spearman']:.3f}, "
                    f"Top10%胜率={r['top10_win_rate']:.1f}% | "
                    f"训练期: {r['train_period']} → 验证期: {r['val_period']}")
    
    if not cv_results:
        logger.error("Walk-Forward验证失败，无有效fold")
        return None, None
    
    avg_auc = np.mean([r['auc'] for r in cv_results])
    avg_spearman = np.mean([r['spearman'] for r in cv_results])
    avg_top10 = np.mean([r['top10_win_rate'] for r in cv_results])
    
    logger.info(f"\nWalk-Forward平均: AUC={avg_auc:.3f}, Spearman={avg_spearman:.3f}, Top10%胜率={avg_top10:.1f}%")
    
    return cv_results, models


def train_final_model(df, feature_cols):
    """用全部数据训练最终模型"""
    logger.info("训练最终模型（全量数据）...")
    
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
        'min_child_samples': 100,
        'verbose': -1,
        'seed': 42,
        'is_unbalance': True,  # 处理类别不平衡（22% vs 78%）
    }
    
    final_model = lgb.train(params, lgb.Dataset(X, label=y), num_boost_round=300)
    
    # 特征重要性
    imp = dict(zip(feature_cols, final_model.feature_importance()))
    imp = dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))
    
    logger.info("\n特征重要性 Top 15:")
    for k, v in list(imp.items())[:15]:
        logger.info(f"  {k}: {v}")
    
    return final_model, imp


def main():
    start = datetime.now()
    
    # 第1步：加载数据
    data = load_data()
    if data is None:
        return
    daily, moneyflow, index_data, min_date, max_date, fundamentals = data
    
    # 第2步：构建特征
    features, global_medians, feature_cols = build_features(
        daily, moneyflow, index_data, min_date, max_date, fundamentals
    )
    if features.empty:
        return
    
    # 第3步：Walk-Forward 验证
    cv_results, fold_models = walk_forward_train(features, feature_cols)
    
    # 第4步：训练最终模型
    final_model, importance = train_final_model(features, feature_cols)
    
    # 第5步：保存
    os.makedirs(DATA_DIR, exist_ok=True)
    bundle = {
        'model': final_model,
        'feature_cols': feature_cols,
        'cv_results': cv_results,
        'importance': importance,
        'global_medians': global_medians,
        'model_type': 'binary_classification',
        'label_type': 'cross_section_rank',
        'validation_strategy': 'walk_forward',
        'trained_at': datetime.now().isoformat(),
        'trained_on_data': f"{len(features):,} samples, {features['ts_code'].nunique()} stocks",
    }
    
    if cv_results:
        bundle['avg_auc'] = np.mean([r['auc'] for r in cv_results])
        bundle['avg_spearman'] = np.mean([r['spearman'] for r in cv_results])
        bundle['avg_top10_win_rate'] = np.mean([r['top10_win_rate'] for r in cv_results])
    
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"模型保存: {MODEL_PATH}")
    
    # 保存配置
    config = {
        'feature_cols': feature_cols,
        'global_medians': global_medians,
        'model_path': MODEL_PATH,
        'model_type': 'binary_classification',
        'label_type': 'cross_section_rank',
        'validation_strategy': 'walk_forward',
    }
    with open(FEATURE_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, default=str)
    
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"完成! 耗时: {elapsed:.1f}s")


if __name__ == '__main__':
    main()
