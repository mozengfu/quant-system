#!/usr/bin/env python3
"""
回测：什么条件下买入最不容易被套？
用2025-2026年真实数据，按不同买入条件分组，统计被套概率和胜率。
"""

import os, pymysql, pandas as pd, numpy as np, warnings
from datetime import timedelta
warnings.filterwarnings('ignore')

# 从.env文件读取密码
pwd = ''
try:
    with open('.env') as f:
        for line in f:
            if line.startswith('MYSQL_PASSWORD='):
                pwd = line.strip().split('=', 1)[1].strip('"').strip("'")
                break
except Exception:
    pwd = os.environ.get('MYSQL_PASSWORD', '')

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': pwd,
    'database': 'quant_db'
}
conn = pymysql.connect(**DB_CONFIG)

print("="*60)
print("买入条件回测：什么情况下不被套？")
print("="*60)

# 加载最近300个交易日数据
df = pd.read_sql("""
    SELECT ts_code, trade_date, open, high, low, close, pct_chg, vol,
           ma5, ma10, ma20, turnover_rate, volume_ratio, rps_20
    FROM daily_price 
    WHERE trade_date >= (SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT 301, 1)
    ORDER BY ts_code, trade_date
""", conn)

mf = pd.read_sql("""
    SELECT ts_code, trade_date, main_net, net_mf_amount
    FROM moneyflow_daily 
    WHERE trade_date >= (SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT 301, 1)
""", conn)

df = df.drop(columns=['main_net', 'net_mf_amount'], errors='ignore').merge(mf, on=['ts_code', 'trade_date'], how='left')

# 补齐缺失的资金流数据
df['main_net'] = df['main_net'].fillna(0)
df['net_mf_amount'] = df['net_mf_amount'].fillna(0)

print(f"\n数据: {len(df):,} 行, {df['ts_code'].nunique()} 只股票")
print(f"日期范围: {df['trade_date'].min()} ~ {df['trade_date'].max()}")

# 对每只股票，计算每个交易日作为"买入点"后的表现
# 条件定义：
def compute_signals(df):
    """计算各种买入条件信号"""
    g = df.groupby('ts_code')
    
    # 1. 均线多头排列
    df['ma_bullish'] = (df['ma5'] > df['ma10']) & (df['ma10'] > df['ma20'])
    
    # 2. 股价在20日均线上方
    df['above_ma20'] = df['close'] > df['ma20']
    
    # 3. 主力资金连续3天净流入
    df['main_inflow_3d'] = g['main_net'].transform(
        lambda x: x.rolling(3).sum() > 0
    )
    
    # 4. 量比>1.5（放量）
    df['high_volume'] = df['volume_ratio'] > 1.5
    
    # 5. RPS>60（相对强势）
    df['high_rps'] = df['rps_20'] > 60
    
    # 6. 换手率3-10%（适中活跃度）
    df['normal_turnover'] = (df['turnover_rate'] >= 3) & (df['turnover_rate'] <= 10)
    
    return df

df = compute_signals(df)

# 排除新股（数据少于20天）
counts = df['ts_code'].value_counts()
valid_codes = counts[counts >= 20].index
df = df[df['ts_code'].isin(valid_codes)]

# 对每个买入点，计算买入后10天的表现
# 被"套"定义：最低价低于买入价的5%
results = []
for code, group in df.groupby('ts_code'):
    group = group.sort_values('trade_date').reset_index(drop=True)
    for i in range(len(group) - 10):
        buy_price = group.loc[i, 'close']
        buy_date = group.loc[i, 'trade_date']
        future = group.iloc[i+1:i+11]  # 后10天
        
        max_profit = (future['high'].max() - buy_price) / buy_price
        max_loss = (future['low'].min() - buy_price) / buy_price
        exit_return = (group.loc[i+10, 'close'] - buy_price) / buy_price if i+10 < len(group) else np.nan
        
        results.append({
            'ts_code': code,
            'buy_date': buy_date,
            'buy_price': buy_price,
            'max_profit': max_profit,
            'max_loss': max_loss,
            'exit_return': exit_return,
            'is_trap': max_loss < -0.05,  # 被套：最低价低于成本5%
            'is_profit': exit_return > 0.02,  # 盈利：10天后涨>2%
        })

res = pd.DataFrame(results)
res = res.merge(df[['ts_code', 'trade_date', 'ma_bullish', 'above_ma20', 
                     'main_inflow_3d', 'high_volume', 'high_rps', 'normal_turnover']],
                left_on=['ts_code', 'buy_date'], 
                right_on=['ts_code', 'trade_date'], how='left')

print(f"\n总买入点: {len(res):,} 个")
print(f"基线（无过滤）: 被套率={res['is_trap'].mean()*100:.1f}%, 盈利10天={res['is_profit'].mean()*100:.1f}%")

# 分析不同条件的胜率
conditions = {
    '均线多头': 'ma_bullish',
    '股价>20日线': 'above_ma20',
    '主力连3天流入': 'main_inflow_3d',
    '量比>1.5': 'high_volume',
    'RPS>60': 'high_rps',
    '换手3-10%': 'normal_turnover',
}

print("\n" + "="*60)
print("单一条件筛选效果:")
print("="*60)
print(f"{'条件':<15} {'样本数':>8} {'被套率':>8} {'10天盈利':>8} {'平均收益':>8}")

for name, col in conditions.items():
    subset = res[res[col] == True]
    if len(subset) > 100:
        avg_ret = subset['exit_return'].mean() * 100
        print(f"{name:<15} {len(subset):>8,} {subset['is_trap'].mean()*100:>7.1f}% {subset['is_profit'].mean()*100:>7.1f}% {avg_ret:>7.1f}%")

# 组合条件
print("\n" + "="*60)
print("组合条件效果:")
print("="*60)

combos = [
    ('均线多头 + 主力流入', ['ma_bullish', 'main_inflow_3d']),
    ('均线多头 + 量比>1.5', ['ma_bullish', 'high_volume']),
    ('均线多头 + RPS>60', ['ma_bullish', 'high_rps']),
    ('主力流入 + 量比>1.5', ['main_inflow_3d', 'high_volume']),
    ('均线多头 + 主力流入 + 量比', ['ma_bullish', 'main_inflow_3d', 'high_volume']),
    ('均线多头 + 主力流入 + RPS>60', ['ma_bullish', 'main_inflow_3d', 'high_rps']),
    ('均线多头 + 主力流入 + 量比 + RPS', ['ma_bullish', 'main_inflow_3d', 'high_volume', 'high_rps']),
    ('股价>20日线 + 主力流入', ['above_ma20', 'main_inflow_3d']),
    ('股价>20日线 + 主力流入 + 量比', ['above_ma20', 'main_inflow_3d', 'high_volume']),
]

print(f"{'条件':<35} {'样本数':>8} {'被套率':>8} {'10天盈利':>8} {'平均收益':>8}")
print("-"*80)

for name, cols in combos:
    mask = pd.Series(True, index=res.index)
    for c in cols:
        mask &= res[c] == True
    subset = res[mask]
    if len(subset) > 50:
        avg_ret = subset['exit_return'].mean() * 100
        median_ret = subset['exit_return'].median() * 100
        profit3d = (subset['exit_return'] > 0.03).mean() * 100
        print(f"{name:<35} {len(subset):>8,} {subset['is_trap'].mean()*100:>7.1f}% {subset['is_profit'].mean()*100:>7.1f}% {avg_ret:>7.1f}%")

# 大盘环境的影响
print("\n" + "="*60)
print("大盘环境对买入胜率的影响:")
print("="*60)

# 计算每日大盘涨跌
idx_daily = pd.read_sql("""
    SELECT trade_date, change_pct 
    FROM market_index_daily 
    WHERE index_code='000001.SH' 
    ORDER BY trade_date
""", conn)
idx_daily.rename(columns={'change_pct': 'pct_chg'}, inplace=True)

# 标记大盘环境
idx_daily['market_trend'] = idx_daily['pct_chg'].rolling(20).mean()  # 20日均线方向
idx_daily['market_up'] = idx_daily['market_trend'] > 0

# 合并到结果
res = res.merge(idx_daily[['trade_date', 'market_up']], 
                left_on='buy_date', right_on='trade_date', how='left', suffixes=('', '_mkt'))

# 在大盘上涨趋势中买入
for cond_name, cond_col in conditions.items():
    up_mkt = res[(res[cond_col] == True) & (res['market_up'] == True)]
    down_mkt = res[(res[cond_col] == True) & (res['market_up'] == False)]
    
    if len(up_mkt) > 50 and len(down_mkt) > 50:
        print(f"\n{cond_name}:")
        print(f"  大盘上升期: {len(up_mkt):,}样本, 被套{up_mkt['is_trap'].mean()*100:.1f}%, 盈利{up_mkt['is_profit'].mean()*100:.1f}%")
        print(f"  大盘下降期: {len(down_mkt):,}样本, 被套{down_mkt['is_trap'].mean()*100:.1f}%, 盈利{down_mkt['is_profit'].mean()*100:.1f}%")

conn.close()
print("\n" + "="*60)
print("回测完成")
print("="*60)
