#!/usr/bin/env python3
"""
ML选股模型训练 V5 - 加入北向资金 + 板块轮动特征
新特征:
1. 北向资金 (north_moneyflow 表):
   - north_chg: 北向资金日变化
   - north_ma5/10/20: 移动平均
   - north_trend: north_ma5 / north_ma10
   - north_cum5: 5日累计流入
2. 板块轮动 (从 daily_price + stock_info 计算):
   - ind_mom_5/10/20: 所在行业 5/10/20 日动量
   - ind_rank: 行业在全市场的排名百分位
   - stock_vs_ind: 个股相对行业超额收益
   - ind_breadth: 行业内上涨股票占比
"""

import os, sys, json, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import numpy as np, pandas as pd, pymysql, lightgbm as lgb
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import mean_squared_error, r2_score, roc_auc_score
from scipy.stats import spearmanr
import joblib

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db', 'connect_timeout': 5,
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
MODEL_PATH = os.path.join(DATA_DIR, 'ml_stock_model.pkl')
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'ml_feature_config.json')

LABEL_HORIZONS = {'label_3d': (3, 2.0), 'label_5d': (5, 3.0)}
EXCLUDE_PREFIXES = ('68', '83', '87', '8', '4', '9', '16')
ROLLING_DAYS = 600


def get_db():
    return pymysql.connect(**DB_CONFIG)


def load_data():
    logger.info("加载数据...")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT %s", (ROLLING_DAYS,))
    dates = sorted([r[0] for r in cur.fetchall()])
    if not dates:
        logger.error("无交易日期"); conn.close(); return None
    min_date, max_date = dates[0], dates[-1]
    logger.info(f"数据范围: {min_date} ~ {max_date} ({len(dates)} 个交易日)")
    
    extra_start = min_date - timedelta(days=90)
    
    daily = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio, vol,
               ma5, ma10, ma20, rps_20, high_52w, low_52w
        FROM daily_price WHERE trade_date >= %s ORDER BY ts_code, trade_date
    """, conn, params=(extra_start,))
    
    moneyflow = pd.read_sql("""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE trade_date >= %s
    """, conn, params=(min_date,))
    
    # 指数数据
    index_data = pd.read_sql("""
        SELECT trade_date, close_price, change_pct FROM market_index_daily
        WHERE index_name='上证指数' ORDER BY trade_date
    """, conn)
    
    # 股票行业信息
    stock_info = pd.read_sql("SELECT ts_code, industry FROM stock_info", conn)
    
    # 北向资金
    north_data = pd.read_sql("SELECT trade_date, north_money FROM north_moneyflow ORDER BY trade_date", conn)
    
    cur.close(); conn.close()
    
    for df, cols in [(daily, ['open','high','low','close','pre_close','pct_chg','turnover_rate',
                              'volume_ratio','vol','ma5','ma10','ma20','rps_20','high_52w','low_52w']),
                     (moneyflow, ['main_net','net_mf_amount','buy_sm_amount','sell_sm_amount',
                                  'buy_lg_amount','sell_lg_amount']),
                     (index_data, ['close_price','change_pct']),
                     (north_data, ['north_money'])]:
        for c in cols:
            if c in df.columns: df[c] = pd.to_numeric(df[c], errors='coerce')
    
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    index_data['trade_date'] = pd.to_datetime(index_data['trade_date'])
    north_data['trade_date'] = pd.to_datetime(north_data['trade_date'])
    
    logger.info(f"行情: {len(daily):,} 行, {daily['ts_code'].nunique()} 股 | "
                f"资金流: {len(moneyflow):,} 行 | 指数: {len(index_data):,} 行 | "
                f"北向: {len(north_data):,} 行 | 股票: {stock_info.shape[0]} 只")
    return daily, moneyflow, index_data, north_data, stock_info, pd.Timestamp(min_date), pd.Timestamp(max_date)


def build_features(daily, moneyflow, index_data, north_data, stock_info, min_date):
    logger.info("构建特征...")
    
    # === 1. 指数特征 ===
    idx = index_data.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    idx['idx_pct_5d'] = idx['close_price'] / idx['close_price'].shift(5) - 1
    idx['idx_vol_10d'] = idx['change_pct'].rolling(10).std()
    idx['idx_future_3d'] = idx['close_price'].shift(-3) / idx['close_price'] - 1
    idx['idx_future_5d'] = idx['close_price'].shift(-5) / idx['close_price'] - 1
    idx['idx_trend'] = 0
    idx.loc[idx['idx_ma5_ratio'] > 0, 'idx_trend'] = 1
    idx.loc[idx['idx_ma5_ratio'] < -0.02, 'idx_trend'] = -1
    idx_feat = idx[['trade_date','idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d','idx_trend',
                    'idx_future_3d','idx_future_5d']]
    
    # === 2. 北向资金特征 ===
    north = north_data.sort_values('trade_date').copy()
    north['north_chg'] = north['north_money'].diff()
    north['north_ma5'] = north['north_money'].rolling(5).mean()
    north['north_ma10'] = north['north_money'].rolling(10).mean()
    north['north_ma20'] = north['north_money'].rolling(20).mean()
    north['north_trend'] = north['north_ma5'] / north['north_ma10'].replace(0, np.nan) - 1
    north['north_cum5'] = north['north_chg'].rolling(5).sum()
    north['north_cum10'] = north['north_chg'].rolling(10).sum()
    north['north_ma5_ratio'] = north['north_ma5'] / north['north_ma20'].replace(0, np.nan) - 1
    north_feat = north[['trade_date','north_chg','north_ma5','north_ma10','north_ma20',
                        'north_trend','north_cum5','north_cum10','north_ma5_ratio']]
    logger.info(f"北向资金特征: {north_feat.shape[1]-1} 个指标, {len(north_feat)} 天")
    
    # === 3. 行业/板块特征 (从 daily_price 计算) ===
    logger.info("计算行业板块特征...")
    si = stock_info.dropna(subset=['industry'])
    si = si[si['industry'].str.strip() != '']
    
    # 给每日数据加入行业标签
    daily_ind = daily.merge(si[['ts_code','industry']], on='ts_code', how='left')
    
    # 计算每个行业每天的涨跌幅均值
    ind_daily = daily_ind.groupby(['trade_date','industry'])['pct_chg'].mean().reset_index()
    ind_daily.columns = ['trade_date','industry','ind_pct']
    
    # 计算每个行业每天的动量 (5/10/20日)
    ind_daily['ind_mom_5'] = ind_daily.groupby('industry')['ind_pct'].transform(lambda x: x.shift(1).rolling(5).sum())
    ind_daily['ind_mom_10'] = ind_daily.groupby('industry')['ind_pct'].transform(lambda x: x.shift(1).rolling(10).sum())
    ind_daily['ind_mom_20'] = ind_daily.groupby('industry')['ind_pct'].transform(lambda x: x.shift(1).rolling(20).sum())
    
    # 计算行业当天排名百分位 (所有行业中该行业动量的排名)
    for col in ['ind_mom_5','ind_mom_10','ind_mom_20']:
        ind_daily[col+'_rank'] = ind_daily.groupby('trade_date')[col].rank(pct=True)
    
    # 计算行业内上涨股票占比 (breadth)
    ind_breadth = daily_ind.groupby(['trade_date','industry']).apply(
        lambda x: (x['pct_chg'] > 0).mean(), include_groups=False
    ).reset_index()
    ind_breadth.columns = ['trade_date','industry','ind_breadth']
    
    # 合并板块特征
    ind_all = ind_daily.merge(ind_breadth, on=['trade_date','industry'], how='left')
    
    # === 4. 逐股构建特征 ===
    results = []
    total = 0
    
    for ts_code, group in daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE_PREFIXES: continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 60: continue
        
        # 获取行业
        ind = si[si['ts_code'] == ts_code]['industry'].values
        if len(ind) == 0 or ind[0] == '':
            continue
        industry = ind[0]
        
        # 获取该行业的板块特征
        ind_data = ind_all[ind_all['industry'] == industry].copy()
        
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
        
        # Alpha 因子
        g['vol_price_corr_10d'] = g['vol'].rolling(10).corr(g['pct_chg'])
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].rolling(5).mean()
        
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
        
        # 资金流特征
        mf = moneyflow[moneyflow['ts_code'] == ts_code].sort_values('trade_date').reset_index(drop=True)
        if not mf.empty:
            g = g.merge(mf[['trade_date','main_net',
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
        
        # 大盘环境特征
        g = g.merge(idx_feat, on='trade_date', how='left')
        
        # 北向资金特征
        g = g.merge(north_feat, on='trade_date', how='left')
        
        # 板块轮动特征
        g = g.merge(ind_data[['trade_date','ind_mom_5','ind_mom_10','ind_mom_20',
                              'ind_mom_5_rank','ind_mom_10_rank','ind_mom_20_rank',
                              'ind_breadth']], on='trade_date', how='left')
        
        # 个股相对行业超额收益
        g['stock_vs_ind_5'] = g['chg_5d'] - g['ind_mom_5']
        g['stock_vs_ind_10'] = g['chg_10d'] - g['ind_mom_10']
        
        # 回归标签
        g['target_3d'] = g['close'].shift(-3) / g['close'] - 1
        g['target_5d'] = g['close'].shift(-5) / g['close'] - 1
        
        for ln, (h, t) in LABEL_HORIZONS.items():
            abs_ret = g['close'].shift(-h) / g['close'] - 1
            idx_future = g[f'idx_future_{h}d']
            rel_ret = abs_ret - idx_future
            g[ln] = (rel_ret >= t / 100).astype(int)
        
        # 特征列
        feature_cols = [
            'pct_chg','turnover_rate','volume_ratio',
            'vol_5d','vol_10d','vol_20d',
            'ma5_ma10_ratio','ma10_ma20_ratio','price_ma5_ratio','price_ma20_ratio',
            'chg_3d','chg_5d','chg_10d','vol_trend','pos_52w',
            'rps_20','rps_change','up_ratio_5d','up_ratio_10d','vol_pct_corr',
            'ma_pattern',
            'macd_diff','macd_signal_line','macd_hist',
            'rsi_14',
            'main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
            'main_streak','main_vs_retail','lg_ratio','main_cum5','main_cum10',
            'idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d','idx_trend',
            'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
            'amihud', 'amihud_ma5',
            'main_accel_3d', 'smart_div_count',
            # 北向资金特征
            'north_chg','north_ma5','north_ma10','north_ma20',
            'north_trend','north_cum5','north_cum10','north_ma5_ratio',
            # 板块轮动特征
            'ind_mom_5','ind_mom_10','ind_mom_20',
            'ind_mom_5_rank','ind_mom_10_rank','ind_mom_20_rank',
            'ind_breadth',
            'stock_vs_ind_5','stock_vs_ind_10'
        ]
        
        valid = g.dropna(subset=['target_3d','target_5d'])
        valid = valid[valid['trade_date'] >= min_date]
        if len(valid) < 10: continue
        
        results.append(valid[feature_cols + ['ts_code','trade_date','target_3d','target_5d','label_3d','label_5d']].copy())
        total += 1
    
    if not results:
        logger.warning("无有效样本"); return pd.DataFrame(), {}
    
    result = pd.concat(results, ignore_index=True)
    
    global_medians = {}
    for col in feature_cols:
        if col in result.columns:
            med = result[col].median()
            global_medians[col] = float(med) if not np.isnan(med) else 0.0
    
    for col in feature_cols:
        if col in result.columns and result[col].isna().any():
            result[col] = result[col].fillna(global_medians[col])
    
    logger.info(f"构建完成: {len(result):,} 样本, {total} 股, {len(feature_cols)} 特征")
    return result, global_medians, feature_cols


def train_model(df, global_medians, feature_cols):
    logger.info("训练回归模型（主目标: target_5d）...")
    
    df_sorted = df.sort_values('trade_date')
    X = df_sorted[feature_cols].values
    y = df_sorted['target_5d'].values
    y_3d = df_sorted['target_3d'].values
    y_cls = df_sorted['label_5d'].values
    
    logger.info(f"收益率分布: 均值={y.mean()*100:.2f}%, 标准差={y.std()*100:.2f}%, "
                f"中位数={np.median(y)*100:.2f}%")
    logger.info(f"正收益占比: {(y>0).mean()*100:.1f}%")
    logger.info(f"特征数: {X.shape[1]}")
    
    tscv = TimeSeriesSplit(n_splits=4)
    cv_results, cv_3d, cv_auc = [], [], []
    
    for fold, (ti, vi) in enumerate(tscv.split(X)):
        td = lgb.Dataset(X[ti], label=y[ti])
        vd = lgb.Dataset(X[vi], label=y[vi], reference=td)
        
        params = {'objective':'regression','metric':'rmse','boosting_type':'gbdt',
                  'num_leaves':31,'learning_rate':0.05,'feature_fraction':0.8,
                  'bagging_fraction':0.8,'bagging_freq':5,
                  'min_child_samples':50,'verbose':-1,'seed':42}
        
        model = lgb.train(params, td, num_boost_round=500, valid_sets=[vd],
                         callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)])
        
        vp = model.predict(X[vi])
        
        mse = mean_squared_error(y[vi], vp)
        r2 = r2_score(y[vi], vp)
        spearman = spearmanr(y[vi], vp)[0]
        
        try:
            auc = roc_auc_score(y_cls[vi], vp)
        except:
            auc = 0.5
        
        cv_results.append({'fold':fold+1, 'mse':mse, 'rmse':np.sqrt(mse), 'r2':r2, 'spearman':spearman, 'auc':auc})
        cv_3d.append({'spearman':spearmanr(y_3d[vi], vp)[0]})
        cv_auc.append({'auc':auc})
        
        r = cv_results[-1]
        logger.info(f"Fold {fold+1}: RMSE={r['rmse']:.4f}, R²={r['r2']:.4f}, "
                    f"Spearman={r['spearman']:.3f}, AUC(排序)={r['auc']:.3f}")
    
    avg = {k:np.mean([r[k] for r in cv_results]) for k in cv_results[0] if k!='fold'}
    avg3 = {k:np.mean([r[k] for r in cv_3d]) for k in cv_3d[0]}
    avg_auc = {k:np.mean([r[k] for r in cv_auc]) for k in cv_auc[0]}
    
    logger.info(f"\n回归CV(5d): RMSE={avg['rmse']:.4f}, R²={avg['r2']:.4f}, Spearman={avg['spearman']:.3f}")
    logger.info(f"辅助评估: AUC(排序)={avg_auc['auc']:.3f}, Spearman(3d)={avg3['spearman']:.3f}")
    
    # 最终模型
    final = lgb.train({**params}, lgb.Dataset(X, label=y), num_boost_round=300)
    
    imp = dict(zip(feature_cols, final.feature_importance()))
    imp = dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))
    logger.info("\n特征重要性 Top 20:")
    for k, v in list(imp.items())[:20]:
        logger.info(f"  {k}: {v}")
    
    os.makedirs(DATA_DIR, exist_ok=True)
    bundle = {'model':final, 'feature_cols':feature_cols, 'cv_results':cv_results,
              'avg_results':avg, 'avg_results_3d':avg3, 'avg_auc':avg_auc['auc'],
              'importance':imp, 'label_horizons':LABEL_HORIZONS,
              'global_medians':global_medians,
              'model_type':'regression',
              'trained_at':datetime.now().isoformat(),
              'trained_on_data':f"{len(df):,} samples, {df['ts_code'].nunique()} stocks, {len(feature_cols)} features"}
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"模型保存: {MODEL_PATH}")
    
    config = {'feature_cols':feature_cols, 'label_horizons':LABEL_HORIZONS,
              'avg_cv_results':avg, 'feature_importance_top15':dict(list(imp.items())[:15]),
              'model_path':MODEL_PATH, 'rolling_days':ROLLING_DAYS,
              'model_type':'regression'}
    with open(FEATURE_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, default=str)
    return bundle


def main():
    start = datetime.now()
    data = load_data()
    if data is None: return
    daily, moneyflow, index_data, north_data, stock_info, min_date, max_date = data
    features, medians, fcols = build_features(daily, moneyflow, index_data, north_data, stock_info, min_date)
    if features.empty: return
    train_model(features, medians, fcols)
    logger.info(f"完成! 耗时: {(datetime.now()-start).total_seconds():.1f}s")

if __name__ == '__main__':
    main()
