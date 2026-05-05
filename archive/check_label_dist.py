
import os, sys
sys.path.insert(0, '/Users/mozengfu/workspace/quant-system')
os.chdir('/Users/mozengfu/workspace/quant-system')

# 模拟训练过程的前半部分
from ml_train_v3 import load_data, build_features
import numpy as np, pandas as pd

print("加载数据...")
data = load_data()
if data is None:
    print("数据加载失败")
    sys.exit(1)

daily, moneyflow, index_data, min_date, max_date, fundamentals = data

print("构建特征（只到标签生成前）...")
features, global_medians, feature_cols = build_features(
    daily, moneyflow, index_data, min_date, max_date, fundamentals
)

if features.empty:
    print("特征构建失败")
    sys.exit(1)

# 打印标签分布
print("\n标签分布:")
print(f"  正样本数: {features['label_5d'].sum()}")
print(f"  负样本数: {(1 - features['label_5d']).sum()}")
print(f"  正样本占比: {features['label_5d'].mean()*100:.1f}%")
print(f"  总样本数: {len(features)}")

# 打印特征统计
print("\n特征统计（最新日期）:")
latest = features['trade_date'].max()
latest_features = features[features['trade_date'] == latest]
print(f"  股票数: {len(latest_features)}")
print(f"  正样本数: {latest_features['label_5d'].sum()}")
print(f"  正样本占比: {latest_features['label_5d'].mean()*100:.1f}%")

# 打印几个关键特征的分布
print("\n关键特征分布（全量训练数据）:")
for col in ['lg_ratio', 'vol_10d', 'idx_vol_10d']:
    if col in features.columns:
        print(f"  {col}: 均值={features[col].mean():.3f}, 中位={features[col].median():.3f}, std={features[col].std():.3f}")
