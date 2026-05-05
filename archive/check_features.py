
import os, joblib, numpy as np, pandas as pd, pymysql

bundle = joblib.load('data/ml_stock_model_v3.pkl')
feature_cols = bundle['feature_cols']
medians = bundle['global_medians']

conn = pymysql.connect(host='localhost', unix_socket='/tmp/mysql.sock', user='root', password=os.environ.get('MYSQL_PASSWORD', ''), database='quant_db')
latest = '20260428'

# 用内置函数获取特征
from ml_predict import _build_full_market_features_v3
features, latest_date = _build_full_market_features_v3(conn)

print(f"股票数: {len(features)}")
print(f"最新日期: {latest_date}")

# 检查几个关键特征的实际值分布
for col in ['lg_ratio', 'vol_10d', 'vol_20d', 'idx_vol_10d', 'macd_signal_line']:
    if col in features.columns:
        vals = features[col].dropna()
        print(f"\n{col}:")
        print(f"  实际分布: min={vals.min():.3f}, max={vals.max():.3f}, median={vals.median():.3f}")
        print(f"  训练中位数: {medians.get(col, 'N/A')}")
        print(f"  NaN数量: {features[col].isna().sum()}")

# 检查是否有特征全是NaN
nan_counts = features[feature_cols].isna().sum()
print(f"\nNaN统计:")
print(f"  有NaN的特征数: {(nan_counts > 0).sum()}")
print(f"  全NaN的特征: {nan_counts[nan_counts == len(features)].index.tolist()}")

# 检查特征是否超出训练范围
print("\n特征范围对比（实际 vs 训练）:")
for col in feature_cols[:10]:
    if col in features.columns:
        vals = features[col].dropna()
        if len(vals) > 0:
            print(f"  {col}: 实际[{vals.min():.3f}, {vals.max():.3f}], 训练中位={medians.get(col, 0):.3f}")

conn.close()
