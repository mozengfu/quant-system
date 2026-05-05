
import os, joblib, numpy as np, pandas as pd, pymysql
import lightgbm as lgb

bundle = joblib.load('data/ml_stock_model_v3.pkl')
model = bundle['model']
feature_cols = bundle['feature_cols']
medians = bundle['global_medians']

conn = pymysql.connect(host='localhost', unix_socket='/tmp/mysql.sock', user='root', password=os.environ.get('MYSQL_PASSWORD', ''), database='quant_db')
latest = '20260428'

# 获取一只股票的完整特征
from ml_predict import _build_full_market_features_v3
features, latest_date = _build_full_market_features_v3(conn)

# 取第一只股票
stock_row = features.iloc[0]
print(f"股票: {stock_row['ts_code']}")

# 提取特征向量
X = features[feature_cols].values.astype(np.float32)
print(f"特征矩阵形状: {X.shape}")

# 检查是否有异常值
print(f"\n特征统计:")
print(f"  min: {X.min():.3f}")
print(f"  max: {X.max():.3f}")
print(f"  mean: {X.mean():.3f}")
print(f"  std: {X.std():.3f}")

# 检查是否有inf
print(f"  inf数量: {np.isinf(X).sum()}")
print(f"  nan数量: {np.isnan(X).sum()}")

# 预测前100只股票
probs = model.predict(X[:100])
print(f"\n前100只股票预测:")
print(f"  均值: {probs.mean():.3f}")
print(f"  分布: {np.histogram(probs, bins=10)[0]}")

# 检查模型是否是"常数预测器"
# 用不同的输入测试
X_test1 = np.random.randn(10, len(feature_cols)).astype(np.float32) * 0.1  # 接近中位数
probs1 = model.predict(X_test1)
print(f"\n接近中位数样本预测: {probs1.mean():.3f}")

X_test2 = np.random.randn(10, len(feature_cols)).astype(np.float32) * 2.0  # 较大波动
probs2 = model.predict(X_test2)
print(f"较大波动样本预测: {probs2.mean():.3f}")

# 检查树深度
print(f"\n模型信息:")
print(f"  树数量: {model.num_trees()}")
print(f"  叶子数: {model.num_leaves()}")

# 检查树的预测分布
# 取前5棵树，检查它们的预测
for i in range(min(5, model.num_trees())):
    tree_pred = model.predict(X[:10], start_iteration=i, num_iteration=1)
    print(f"  树{i}平均预测: {tree_pred.mean():.3f}")

conn.close()
