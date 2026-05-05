#!/usr/bin/env python3
"""
分析：逆市中是否有机会？
检查在大盘下跌日，有多少股票能逆势上涨 > 3%
"""

import os, pymysql, pandas as pd, numpy as np

pwd = ''
with open('.env') as f:
    for line in f:
        if line.startswith('MYSQL_PASSWORD='):
            pwd = line.strip().split('=', 1)[1].strip('"').strip("'")
            break

conn = pymysql.connect(host='localhost', unix_socket='/tmp/mysql.sock', user='root', password=pwd, database='quant_db')

# 1. 获取指数数据
idx = pd.read_sql("SELECT trade_date, change_pct FROM market_index_daily WHERE index_code='000001.SH'", conn)
idx.rename(columns={'change_pct': 'idx_chg'}, inplace=True)
idx['market_state'] = idx['idx_chg'].apply(lambda x: '暴跌(<-1%)' if x < -1 else ('下跌(-1%~0%)' if x < 0 else ('微涨(0%~1%)' if x < 1 else '大涨(>1%)')))

# 2. 获取个股数据
daily = pd.read_sql("SELECT ts_code, trade_date, pct_chg FROM daily_price WHERE trade_date >= '2025-01-01'", conn)

# 3. 合并
merged = daily.merge(idx, on='trade_date', how='left')

print("="*60)
print("逆市机会分析 (2025年至今)")
print("="*60)

# 分组统计
stats = merged.groupby('market_state').agg(
    days=('trade_date', 'nunique'),
    total_stocks=('ts_code', 'count'),
    up_3pct=('pct_chg', lambda x: (x > 3).sum()),
    up_5pct=('pct_chg', lambda x: (x > 5).sum()),
).reset_index()

stats['胜率'] = (stats['up_3pct'] / stats['total_stocks'] * 100).round(2)

print(f"{'市场环境':<15} {'交易天数':>6} {'总样本':>8} {'逆势>3%':>8} {'逆势>5%':>8} {'胜率':>6}")
for _, row in stats.iterrows():
    print(f"{row['market_state']:<15} {row['days']:>6} {row['total_stocks']:>8,} {row['up_3pct']:>8,} {row['up_5pct']:>8,} {row['胜率']:>5.1f}%")

# 4. 寻找“真·龙头”：连续3天大盘跌，但股票连涨3天
print("\n" + "="*60)
print("寻找逆市龙头 (连续3天大盘跌，个股连涨)")
print("="*60)

# 连续3天大盘跌的日期
idx['idx_down_3d'] = idx['idx_chg'].rolling(3).max() < 0
down_dates = idx[idx['idx_down_3d']]['trade_date'].tolist()
print(f"大盘连续3天跌的日期: {len(down_dates)} 天")

if down_dates:
    # 在这些日期里，找连续3天涨的股票
    # 简化：找在这些日期涨幅>3%的股票
    sample = merged[(merged['trade_date'].isin(down_dates)) & (merged['pct_chg'] > 3)]
    print(f"在逆市日里涨幅>3%的个股出现次数: {len(sample)}")
    if not sample.empty:
        print("Top 5 活跃逆市股:")
        print(sample.groupby('ts_code').size().nlargest(5))

conn.close()
