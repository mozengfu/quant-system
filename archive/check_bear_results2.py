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

bundle = joblib.load('data/ml_stock_model.pkl')
fcols = bundle['feature_cols']

# 加载数据
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
daily['mkt_chg_3d'] = g['mkt_chg'].transform(lambda x: x.rolling(3).sum())
daily['mkt_chg_10d'] = g['mkt_chg'].transform(lambda x: x.rolling(10).sum())
daily['vol_10d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(10).std())
daily['vol_20d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(20).std())
daily['ma5_ma10_ratio'] = daily['ma5'] / daily['ma10'].replace(0, np.nan)
daily['ma10_ma20_ratio'] = daily['ma10'] / daily['ma20'].replace(0, np.nan)
daily['price_ma5_ratio'] = daily['close'] / daily['ma5'].replace(0, np.nan)
daily['price_ma20_ratio'] = daily['close'] / daily['ma20'].replace(0, np.nan)
daily['pos_52w'] = (daily['close'] - daily['low_52w']) / (daily['high_52w'] - daily['low_52w']).replace(0, np.nan)
daily['macd_diff'] = (daily['ma5'] - daily['ma10']) / daily['close']
daily['main_net_ratio'] = daily['main_net'] / daily['close'].replace(0, np.nan)
daily['main_inflow_ratio_3d'] = g['main_net'].transform(lambda x: x.rolling(3).sum()) / daily['close'].replace(0, np.nan)
daily['alpha_1d'] = daily['pct_chg'] - daily['mkt_chg']
daily['alpha_3d'] = daily['chg_3d'] - daily['mkt_chg_3d']
daily['resilience_10d'] = g['alpha_1d'].transform(lambda x: (x > 0).rolling(10).sum() / (x.rolling(10).count() + 1))
daily['is_bear_day'] = (daily['mkt_chg'] < -0.5).astype(int)
daily['is_inflow'] = (daily['main_net'] > 0).astype(int)
daily['bear_inflow_flag'] = daily['is_bear_day'] * daily['is_inflow']
daily['chg_5d'] = g['pct_chg'].transform(lambda x: x.shift(1).rolling(5).sum())

for col, val in bundle['global_medians'].items():
    if col in daily.columns:
        daily[col] = daily[col].fillna(val)

valid = daily[daily['label'].notna() & ~daily['ts_code'].str.startswith(('68', '83', '87', '8', '4', '9', '16'))].copy()
valid['prob'] = bundle['model'].predict_proba(valid[fcols])[:, 1]

bear = valid[valid['mkt_chg'] < -0.5]
print(f"大盘跌样本总数: {len(bear)}")
print(f"大盘跌时最大概率: {bear['prob'].max():.3f}")
print(f"大盘跌时概率 > 0.50 样本: {len(bear[bear['prob'] > 0.50])}")
print(f"大盘跌时概率 > 0.45 样本: {len(bear[bear['prob'] > 0.45])}")

# 看看 >0.45 的胜率
high_prob = bear[bear['prob'] > 0.45]
if len(high_prob) > 0:
    print(f"概率 > 0.45 实际胜率: {high_prob['label'].mean()*100:.1f}%")
    print(f"概率 > 0.45 数量: {len(high_prob)}")
    for _, r in high_prob.nlargest(3, 'prob').iterrows():
        print(f"    {r['ts_code']} {r['trade_date']} 概率:{r['prob']:.1%} 实际:{r['future_ret']:.1%}")

conn.close()
