
import pandas as pd, numpy as np

# 模拟标签计算
dates = pd.date_range('2026-01-01', periods=20, freq='B')
df = pd.DataFrame({
    'trade_date': dates,
    'close': np.random.randn(20).cumsum() + 100,
})

# 计算target_5d
df['target_5d'] = df['close'].shift(-5) / df['close'] - 1

print("日期和target_5d:")
print(df[['trade_date', 'close', 'target_5d']].tail(10))

# 过滤有效样本
valid = df.dropna(subset=['target_5d'])
print("\n有效样本（target_5d非空）:")
print(valid[['trade_date', 'target_5d']].tail(10))

# 按日期分组排名
def rank_label(group):
    n = len(group)
    if n < 10:
        return pd.Series(0, index=group.index)
    threshold = group['target_5d'].quantile(0.80)
    return (group['target_5d'] >= threshold).astype(int)

# 正确的做法
df_sorted = valid.sort_values(['trade_date', 'target_5d'])
df_sorted['label_5d'] = df_sorted.groupby('trade_date').apply(rank_label).values

print("\n标签分配:")
print(df_sorted[['trade_date', 'target_5d', 'label_5d']].tail(10))

# 检查最新日期的标签分布
latest = df_sorted['trade_date'].max()
latest_labels = df_sorted[df_sorted['trade_date'] == latest]
print(f"\n最新日期 {latest}:")
print(f"  股票数: {len(latest_labels)}")
print(f"  正样本占比: {latest_labels['label_5d'].mean()*100:.1f}%")
