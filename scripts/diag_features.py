"""诊断2：特征与标签的关联性 + 训练流程"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import pymysql
from scipy.stats import spearmanr

from quant_app.utils.config import get_db_config

DB = get_db_config()

def load_data():
    conn = pymysql.connect(**DB)
    max_expr = "(SELECT MAX(trade_date) FROM daily_price)"
    bound = f"trade_date >= {max_expr} - INTERVAL 600 DAY AND trade_date < {max_expr}"
    daily = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               vol, amount, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20
        FROM daily_price WHERE {bound}
    """, conn)
    stock_info = pd.read_sql("SELECT ts_code, industry FROM stock_info", conn)
    conn.close()
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    return daily, stock_info

def build_features_and_labels(daily, stock_info):
    """简化的特征+标签构建,对照V11逻辑"""
    daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    ind_map = {r['ts_code']: r.get('industry', 'OTHER') for _, r in stock_info.iterrows()}
    results = []
    EXCLUDE = {'BJ', 'st'}

    n = 0
    for ts_code, group in daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE or ts_code[0] == '8':
            continue
        if len(group) < 60:
            continue
        n += 1
        if n % 500 == 0:
            print(f"  processing {n} stocks...", flush=True)
        g = group.sort_values('trade_date').reset_index(drop=True)

        # Labels
        g['raw_5d'] = g['close'].shift(-5) / g['close'] - 1

        # Features (all use shift(1) to avoid look-ahead)
        g['ret_1d'] = g['pct_chg'].shift(1) / 100
        g['ret_5d'] = g['close'].shift(1) / g['close'].shift(6) - 1
        g['ret_20d'] = g['close'].shift(1) / g['close'].shift(21) - 1
        g['vol_5d'] = g['pct_chg'].shift(1).rolling(5).std()
        g['vol_20d'] = g['pct_chg'].shift(1).rolling(20).std()
        g['turnover_5d'] = g['turnover_rate'].shift(1).rolling(5).mean()
        g['ma5_ratio'] = g['close'].shift(1) / g['ma5'].shift(1)
        g['ma20_ratio'] = g['close'].shift(1) / g['ma20'].shift(1)
        g['amount_5d'] = g['amount'].shift(1).rolling(5).mean()
        g['amount_20d'] = g['amount'].shift(1).rolling(20).mean()
        g['amount_ratio'] = g['amount_5d'] / (g['amount_20d'] + 1)

        features = ['ret_1d', 'ret_5d', 'ret_20d', 'vol_5d', 'vol_20d',
                    'turnover_5d', 'ma5_ratio', 'ma20_ratio', 'amount_ratio']
        keep = features + ['ts_code', 'trade_date', 'raw_5d']
        valid = g.dropna(subset=features + ['raw_5d'])[keep]
        if len(valid) < 10:
            continue
        results.append(valid)

    df = pd.concat(results, ignore_index=True)
    print(f"  total: {len(df)} samples, {df['ts_code'].nunique()} stocks")
    return df

def main():
    print("=" * 60)
    print("V11 特征-标签关联诊断")
    print("=" * 60)

    print("\n[1] 加载数据...")
    daily, stock_info = load_data()

    print("\n[2] 构建特征+标签...")
    df = build_features_and_labels(daily, stock_info)

    features = ['ret_1d', 'ret_5d', 'ret_20d', 'vol_5d', 'vol_20d',
                'turnover_5d', 'ma5_ratio', 'ma20_ratio', 'amount_ratio']

    print("\n[3] 单特征 Rank IC (cross-section per day):")
    ics = {}
    dates = sorted(df['trade_date'].unique())
    for feat in features:
        ic_list = []
        for dt in dates[::5]:  # every 5 days for speed
            day = df[df['trade_date'] == dt]
            if len(day) < 30:
                continue
            valid = day[[feat, 'raw_5d']].dropna()
            if len(valid) < 30:
                continue
            r, _ = spearmanr(valid[feat], valid['raw_5d'])
            ic_list.append(r)
        if ic_list:
            arr = np.array(ic_list)
            ics[feat] = (arr.mean(), arr.std(), np.mean(arr > 0), arr.mean() / (arr.std() + 1e-8))

    for feat, (mean_ic, std_ic, hit_rate, icir) in sorted(ics.items(), key=lambda x: -abs(x[1][0])):
        print(f"  {feat:20s}: IC={mean_ic:+.4f}, std={std_ic:.4f}, hit={hit_rate:.1%}, ICIR={icir:+.2f}")

    print("\n[4] 按日期标准化后，检查标签可预测性:")
    # Standardize features and label per day
    df_z = df.copy()
    for col in features + ['raw_5d']:
        df_z[col + '_z'] = df.groupby('trade_date')[col].transform(lambda x: (x - x.mean()) / (x.std() + 1e-8))

    feat_z = [f + '_z' for f in features]
    X = df_z[feat_z].values
    y = df_z['raw_5d_z'].values

    # Simple check: top decile by feature vs label
    for feat in feat_z[:5]:
        top = df_z.nlargest(int(len(df_z)*0.1), feat)
        bot = df_z.nsmallest(int(len(df_z)*0.1), feat)
        print(f"  {feat:20s}: top10%均值={top['raw_5d_z'].mean():+.4f}, bot10%均值={bot['raw_5d_z'].mean():+.4f}, spread={top['raw_5d_z'].mean()-bot['raw_5d_z'].mean():+.4f}")

    print("\n[5] 结论:")
    print("  如果单特征IC普遍<0.01，说明特征本身缺少预测力")
    print("  如果top/bot spread接近0，说明特征不区分涨跌股")
    print("  如果ICIR<0.5，说明预测不稳定，模型学到的可能只是噪声")

if __name__ == '__main__':
    main()
