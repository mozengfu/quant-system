"""诊断：标签定义对信号的影响"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import pymysql
from scipy.stats import spearmanr

from quant_app.utils.config import get_db_config

DB = get_db_config()

def load_daily():
    conn = pymysql.connect(**DB)
    max_expr = "(SELECT MAX(trade_date) FROM daily_price)"
    bound = f"trade_date >= {max_expr} - INTERVAL 600 DAY AND trade_date < {max_expr}"
    daily = pd.read_sql(f"""
        SELECT ts_code, trade_date, close, pct_chg
        FROM daily_price WHERE {bound}
    """, conn)
    stock_info = pd.read_sql("SELECT ts_code, industry FROM stock_info", conn)
    conn.close()
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    return daily, stock_info

def compute_labels(daily, stock_info):
    """逐层计算标签并对比"""
    daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    results = []

    for ts_code, g in daily.groupby('ts_code'):
        if len(g) < 60:
            continue
        g = g.sort_values('trade_date').reset_index(drop=True)

        # Layer 1: 原始5日收益
        g['raw_5d'] = g['close'].shift(-5) / g['close'] - 1

        # Layer 2: 波动率调整
        vol = g['pct_chg'].rolling(20).std().shift(1).values
        g['vol_20d'] = vol * np.sqrt(5)
        g['vol_adj_5d'] = g['raw_5d'] / (g['vol_20d'] + 0.01)

        valid = g.dropna(subset=['raw_5d', 'vol_adj_5d'])
        if len(valid) < 10:
            continue
        results.append(valid[['ts_code', 'trade_date', 'raw_5d', 'vol_adj_5d', 'vol_20d']])

    df = pd.concat(results, ignore_index=True)

    # Layer 3: 行业中性化
    ind = stock_info[['ts_code', 'industry']].dropna()
    df = df.merge(ind, on='ts_code', how='left')
    df['industry'] = df['industry'].fillna('OTHER')
    df['ind_avg'] = df.groupby(['trade_date', 'industry'])['vol_adj_5d'].transform('mean')
    df['alpha_5d'] = df['vol_adj_5d'] - df['ind_avg']

    return df

def main():
    print("=" * 60)
    print("V11 标签诊断")
    print("=" * 60)

    print("\n[1] 加载数据...")
    daily, stock_info = load_daily()
    print(f"    {len(daily):,} 行, {daily['ts_code'].nunique()} 股, "
          f"{daily['trade_date'].dt.date.nunique()} 交易日")

    print("\n[2] 计算标签...")
    df = compute_labels(daily, stock_info)
    print(f"    有效样本: {len(df):,}")

    print("\n[3] 标签分布:")
    for col in ['raw_5d', 'vol_adj_5d', 'alpha_5d']:
        x = df[col].dropna()
        # Winsorize 99%
        lo, hi = x.quantile(0.005), x.quantile(0.995)
        xw = x.clip(lo, hi)
        print(f"  {col:15s}: mean={xw.mean():.4f}, std={xw.std():.4f}, "
              f"skew={xw.skew():+.2f}, kurt={xw.kurtosis():+.2f}, "
              f"pos_ratio={100*(xw>0).mean():.1f}%")

    print("\n[4] 标签稳定性（按日期排名 IC，各层与 raw_5d 相关性）:")
    # Group by date, compute rank correlation between each label and raw_5d
    dates = sorted(df['trade_date'].unique())
    sample_dates = dates[::max(1, len(dates)//20)][:20]

    corr_results = {'vol_adj_5d': [], 'alpha_5d': []}
    for dt in sample_dates:
        day = df[df['trade_date'] == dt]
        if len(day) < 50:
            continue
        for tgt in ['vol_adj_5d', 'alpha_5d']:
            valid = day[['raw_5d', tgt]].dropna()
            if len(valid) < 50:
                continue
            r, _ = spearmanr(valid['raw_5d'], valid[tgt])
            corr_results[tgt].append(r)

    for tgt, vals in corr_results.items():
        if vals:
            arr = np.array(vals)
            print(f"  corr(raw_5d, {tgt:15s}): mean={arr.mean():.4f}, std={arr.std():.4f}")

    print("\n[5] 自相关保留（raw_5d 行业内自相关 vs alpha_5d）:")
    # For stocks in same industry on same day, compute mean of raw_5d
    # See if alpha_5d preserves stock-specific alpha
    df['raw_5d_rank'] = df.groupby('trade_date')['raw_5d'].rank(pct=True)
    df['alpha_5d_rank'] = df.groupby('trade_date')['alpha_5d'].rank(pct=True)
    r, _ = spearmanr(df['raw_5d_rank'], df['alpha_5d_rank'])
    print(f"  交叉排名相关(raw, alpha): {r:.4f}")

    # Check: does raw_5d have any predictive power?
    # Look at next period's return vs current raw_5d
    print("\n[6] 信号衰减检查（每层变换后的信息损失）:")
    df['raw_5d_std'] = df.groupby('trade_date')['raw_5d'].transform(lambda x: (x - x.mean()) / x.std())
    df['vol_adj_std'] = df.groupby('trade_date')['vol_adj_5d'].transform(lambda x: (x - x.mean()) / x.std())
    df['alpha_std'] = df.groupby('trade_date')['alpha_5d'].transform(lambda x: (x - x.mean()) / x.std())

    # Variance of each standardized label
    for col in ['raw_5d_std', 'vol_adj_std', 'alpha_std']:
        print(f"  {col:15s}: var={df[col].var():.4f}, "
              f"99%好={df[col].quantile(0.99):.2f}, 1%坏={df[col].quantile(0.01):.2f}")

    print("\n[7] 结论:")
    print("  如果 vol_adj 与 raw 的 rank 相关性 > 0.8，则波动率调整保留了信号")
    print("  如果 alpha 与 raw 的 rank 相关性 < 0.5，则行业中性化过度")
    print("  如果 alpha 方差显著小于 raw，则标签被压缩")

if __name__ == '__main__':
    main()
