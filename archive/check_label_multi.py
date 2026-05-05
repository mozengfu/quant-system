
import pandas as pd, numpy as np

# 模拟多股票场景
dates = pd.date_range('2026-01-01', periods=20, freq='B')
stocks = ['000001.SZ', '000002.SZ', '000003.SZ', '000004.SZ', '000005.SZ']

rows = []
for stock in stocks:
    for date in dates:
        rows.append({
            'ts_code': stock,
            'trade_date': date,
            'close': np.random.randn() + 100,
            'target_5d': np.random.randn() * 0.02,  # 模拟未来收益
        })

df = pd.DataFrame(rows)

# 最后5天的target_5d设为NaN（因为没有未来数据）
last_5_dates = dates[-5:]
df.loc[df['trade_date'].isin(last_5_dates), 'target_5d'] = np.nan

print("样本数:", len(df))
print("有效样本数:", df['target_5d'].notna().sum())

# 过滤有效样本
valid = df.dropna(subset=['target_5d'])

# 按日期分组排名
def rank_label(group):
    n = len(group)
    if n < 10:
        return pd.Series(0, index=group.index)
    threshold = group['target_5d'].quantile(0.80)
    return (group['target_5d'] >= threshold).astype(int)

df_sorted = valid.sort_values(['trade_date', 'target_5d'])
df_sorted['label_5d'] = df_sorted.groupby('trade_date').apply(rank_label).values

print("\n标签分配成功!")
print("正样本占比:", df_sorted['label_5d'].mean())

# 检查最新日期的标签分布
latest = df_sorted['trade_date'].max()
latest_labels = df_sorted[df_sorted['trade_date'] == latest]
print(f"\n最新日期 {latest}:")
print(f"  股票数: {len(latest_labels)}")
print(f"  正样本数: {latest_labels['label_5d'].sum()}")
print(f"  正样本占比: {latest_labels['label_5d'].mean()*100:.1f}%")

# 问题可能在于.values()的索引对齐
print("\n调试groupby().apply()的返回值:")
grouped = df_sorted.groupby('trade_date').apply(rank_label)
print("  返回值类型:", type(grouped))
print("  返回值索引类型:", type(grouped.index))
print("  前5个索引:", grouped.index[:5].tolist())

# 检查索引是否与df_sorted对齐
print("\n索引对齐检查:")
print("  df_sorted索引:", df_sorted.index[:5].tolist())
print("  grouped索引:", grouped.index[:5].tolist())
