import pymysql, pandas as pd, numpy as np, joblib, warnings
warnings.filterwarnings('ignore')

pwd = ''
with open('.env') as f:
    for line in f:
        if line.startswith('MYSQL_PASSWORD='):
            pwd = line.strip().split('=', 1)[1].strip('"').strip("'")

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': pwd,
    'database': 'quant_db'
}
conn = pymysql.connect(**DB_CONFIG)

bear_bundle = joblib.load('data/ml_bear_model.pkl')
daily = pd.read_sql("SELECT * FROM daily_price WHERE trade_date >= '2025-01-01'", conn)
mf = pd.read_sql("SELECT ts_code, trade_date, main_net FROM moneyflow_daily WHERE trade_date >= '2025-01-01'", conn)
mkt = pd.read_sql("SELECT trade_date, change_pct as mkt_chg FROM market_index_daily WHERE index_code='000001.SH' AND trade_date >= '2025-01-01'", conn)

daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left').merge(mkt, on='trade_date', how='left')
daily['main_net'] = daily['main_net'].fillna(0)
daily['mkt_chg'] = daily['mkt_chg'].fillna(0)
daily = daily.sort_values(['ts_code', 'trade_date'])
daily['future_ret'] = daily.groupby('ts_code')['close'].transform(lambda x: x.shift(-5) / x - 1)
daily['label'] = (daily['future_ret'] > 0.03).astype(int)

g = daily.groupby('ts_code')
daily['chg_3d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(3).sum())
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
daily['alpha_1d'] = daily['pct_chg'] - daily['mkt_chg']
daily['alpha_3d'] = daily['chg_3d'] - g['mkt_chg'].transform(lambda x: x.rolling(3).sum())
daily['resilience_10d'] = g['alpha_1d'].transform(lambda x: (x > 0).rolling(10).sum() / (x.rolling(10).count() + 1))
daily['is_inflow'] = (daily['main_net'] > 0).astype(int)
daily['chg_5d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(5).sum())

for col, val in bear_bundle['global_medians'].items():
    if col in daily.columns:
        daily[col] = daily[col].fillna(val)

bear = daily[daily['mkt_chg'] < -0.5].copy()
valid = bear[~bear['ts_code'].str.startswith(('68', '83', '87', '8', '4', '9', '16'))].copy()
fcols = bear_bundle['feature_cols']
valid['prob'] = bear_bundle['model'].predict_proba(valid[fcols].values)[:, 1]

print("=== 逆市模型概率分布与胜率 ===")
# 概率 > 0.40
top40 = valid[valid['prob'] >= 0.40]
print(f"概率 >= 0.40: {len(top40):,} 样本, 实际胜率: {top40['label'].mean()*100:.1f}%")
# 概率 > 0.38
top38 = valid[valid['prob'] >= 0.38]
print(f"概率 >= 0.38: {len(top38):,} 样本, 实际胜率: {top38['label'].mean()*100:.1f}%")
# 概率 > 0.35
top35 = valid[valid['prob'] >= 0.35]
print(f"概率 >= 0.35: {len(top35):,} 样本, 实际胜率: {top35['label'].mean()*100:.1f}%")

print("\n基线 (逆市随便买):", valid['label'].mean()*100, "%")

conn.close()
