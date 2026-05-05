
import os, joblib, numpy as np, pandas as pd

bundle = joblib.load('data/ml_stock_model_v3.pkl')
feature_cols = bundle['feature_cols']
medians = bundle['global_medians']

# 从CSV读取预测特征
features = pd.read_csv('v3_stock_pool.csv')
print(f"CSV中的股票数: {len(features)}")

# 但CSV只有ts_code和score，需要重新获取特征
from ml_predict import _build_full_market_features_v3
import pymysql

conn = pymysql.connect(host='localhost', unix_socket='/tmp/mysql.sock', user='root', password=os.environ.get('MYSQL_PASSWORD', ''), database='quant_db')
features, latest = _build_full_market_features_v3(conn)

# 检查哪些特征有NaN
nan_counts = features[feature_cols].isna().sum()
print("\nNaN统计:")
for col in feature_cols:
    if nan_counts[col] > 0:
        print(f"  {col}: {nan_counts[col]} NaN ({nan_counts[col]/len(features)*100:.1f}%)")

# 取一只股票，逐步检查预测过程
test_stock = features.iloc[0]
print(f"\n测试股票: {test_stock['ts_code']}")

# 检查特征值
print("\n特征值（前15个）:")
for i, col in enumerate(feature_cols[:15]):
    val = test_stock.get(col, medians.get(col, 0))
    med = medians.get(col, 0)
    print(f"  {i+1:2d}. {col}: {val:.3f} (训练中位: {med:.3f})")

# 预测
X = features[feature_cols].values.astype(np.float32)
probs = bundle['model'].predict(X)

# 检查哪些树的贡献最大
print(f"\n预测分布:")
print(f"  均值: {probs.mean():.3f}")
print(f"  >0.99: {(probs > 0.99).sum()}")
print(f"  0.90-0.99: {((probs >= 0.90) & (probs <= 0.99)).sum()}")
print(f"  0.50-0.90: {((probs >= 0.50) & (probs < 0.90)).sum()}")
print(f"  <0.50: {(probs < 0.50).sum()}")

conn.close()
