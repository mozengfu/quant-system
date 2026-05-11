#!/usr/bin/env python3
"""
回测 V3: Alpha Hunter 模型效果验证
验证：在逆市中，模型能否选出涨的股票？
"""

import os, pymysql, pandas as pd, numpy as np, joblib, warnings
from sklearn.metrics import roc_auc_score
warnings.filterwarnings('ignore')

pwd = ''
try:
    with open('.env') as f:
        for line in f:
            if line.startswith('MYSQL_PASSWORD='):
                pwd = line.strip().split('=', 1)[1].strip('"').strip("'")
except Exception as _e:
    print(f"Error in backtest_alpha.py: {_e}")

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': pwd,
    'database': 'quant_db'
}
conn = pymysql.connect(**DB_CONFIG)

print("="*60)
print("Alpha Hunter 模型回测")
print("="*60)

# 加载模型
bundle = joblib.load('data/ml_stock_model.pkl')
fcols = bundle['feature_cols']

# 加载数据
daily = pd.read_sql("SELECT * FROM daily_price WHERE trade_date >= '2025-01-01'", conn)
mf = pd.read_sql("SELECT ts_code, trade_date, main_net FROM moneyflow_daily WHERE trade_date >= '2025-01-01'", conn)
mkt = pd.read_sql("SELECT trade_date, change_pct as mkt_chg FROM market_index_daily WHERE index_code='000001.SH' AND trade_date >= '2025-01-01'", conn)

daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left')
daily['main_net'] = daily['main_net'].fillna(0)
daily = daily.merge(mkt, on='trade_date', how='left')
daily['mkt_chg'] = daily['mkt_chg'].fillna(0)

# 计算标签 (5天后涨幅 > 3%)
daily = daily.sort_values(['ts_code', 'trade_date'])
daily['future_ret'] = daily.groupby('ts_code')['close'].transform(lambda x: x.shift(-5) / x - 1)
daily['label'] = (daily['future_ret'] > 0.03).astype(int)

# 计算特征
g = daily.groupby('ts_code')
daily['chg_3d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(3).sum())
daily['chg_5d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(5).sum())
daily['vol_10d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(10).std())
daily['vol_20d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(20).std())
daily['pos_52w'] = (daily['close'] - daily['low_52w']) / (daily['high_52w'] - daily['low_52w']).replace(0, np.nan)
daily['ma5_ma10_ratio'] = daily['ma5'] / daily['ma10'].replace(0, np.nan)
daily['ma10_ma20_ratio'] = daily['ma10'] / daily['ma20'].replace(0, np.nan)
daily['price_ma5_ratio'] = daily['close'] / daily['ma5'].replace(0, np.nan)
daily['price_ma20_ratio'] = daily['close'] / daily['ma20'].replace(0, np.nan)
daily['macd_diff'] = (daily['ma5'] - daily['ma10']) / daily['close']
daily['main_net_ratio'] = daily['main_net'] / daily['close'].replace(0, np.nan)
daily['main_inflow_ratio_3d'] = g['main_net'].transform(lambda x: x.rolling(3).sum()) / daily['close'].replace(0, np.nan)
daily['mkt_chg_3d'] = g['mkt_chg'].transform(lambda x: x.rolling(3).sum())
daily['mkt_chg_10d'] = g['mkt_chg'].transform(lambda x: x.rolling(10).sum())
daily['alpha_1d'] = daily['pct_chg'] - daily['mkt_chg']
daily['alpha_3d'] = daily['chg_3d'] - daily['mkt_chg_3d']
daily['resilience_10d'] = g['alpha_1d'].transform(lambda x: (x > 0).rolling(10).sum() / (x.rolling(10).count() + 1))
daily['is_bear_day'] = (daily['mkt_chg'] < -0.5).astype(int)
daily['is_inflow'] = (daily['main_net'] > 0).astype(int)
daily['bear_inflow_flag'] = daily['is_bear_day'] * daily['is_inflow']

# 填充 NaN
for col, val in bundle['global_medians'].items():
    if col in daily.columns:
        daily[col] = daily[col].fillna(val)

# 过滤
valid = daily[daily['label'].notna()].copy()
# 过滤前缀
valid = valid[~valid['ts_code'].str.startswith(('68', '83', '87', '8', '4', '9', '16'))]

print(f"总样本: {len(valid):,}")

# 预测
valid['prob'] = bundle['model'].predict_proba(valid[fcols])[:, 1]

# 分组评估
def eval_group(name, df):
    if len(df) < 10: return
    auc = roc_auc_score(df['label'], df['prob'])
    # Top 10% 预测胜率
    top = df.nlargest(int(len(df)*0.1), 'prob')
    win_rate = (top['label'].sum() / len(top)) * 100
    # 实际胜率 (大盘跌但涨了)
    bear_win = (df['label'].sum() / len(df)) * 100
    print(f"{name:<20} | 样本: {len(df):>6,} | AUC: {auc:.3f} | 实际胜率: {bear_win:.1f}% | 模型Top10%胜率: {win_rate:.1f}%")

print("\n--- 总体 ---")
eval_group("所有样本", valid)

print("\n--- 按大盘环境 ---")
bull = valid[valid['mkt_chg'] >= 0]
bear = valid[valid['mkt_chg'] < 0]
eval_group("大盘上涨", bull)
eval_group("大盘下跌", bear)

print("\n--- 极端逆市 (大盘跌 > 1%) ---")
extreme_bear = valid[valid['mkt_chg'] < -1.0]
eval_group("大盘暴跌", extreme_bear)

# 寻找逆势牛股
print("\n--- 逆市龙头识别能力 ---")
# 在大盘下跌时，预测概率 > 0.6 的股票
bear_high_prob = bear[bear['prob'] > 0.55]
if not bear_high_prob.empty:
    win_rate_bear_high = (bear_high_prob['label'].sum() / len(bear_high_prob)) * 100
    print(f"大盘下跌且模型概率 > 55% 的样本: {len(bear_high_prob):,} 个")
    print(f"  实际胜率 (5天后涨>3%): {win_rate_bear_high:.1f}% (基线: {bear['label'].mean()*100:.1f}%)")
    
    # 对比：大盘下跌且模型概率 < 0.45
    bear_low_prob = bear[bear['prob'] < 0.45]
    if not bear_low_prob.empty:
        win_rate_bear_low = (bear_low_prob['label'].sum() / len(bear_low_prob)) * 100
        print(f"大盘下跌且模型概率 < 45% 的样本: {len(bear_low_prob):,} 个")
        print(f"  实际胜率: {win_rate_bear_low:.1f}%")
    else:
        print("大盘下跌且模型概率 < 45% 的样本: 0 个 (模型可能太悲观或太乐观)")

conn.close()
