#!/usr/bin/env python3
"""
底部苏醒策略回测（全内存版）— 过去 40 个交易日的候选数分布
一次性加载全量历史数据，全部在 pandas/Numpy 中计算
"""
import os, sys
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

import pymysql
import pandas as pd
import numpy as np
from quant_app.utils.config import get_db_config

print("加载全量历史数据...", flush=True)

conn = pymysql.connect(**get_db_config())
cur = conn.cursor()

# 1. 获取所有交易日
cur.execute("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT 45")
dates = sorted([
    row[0].strftime('%Y-%m-%d') if hasattr(row[0], 'strftime') else str(row[0])[:10]
    for row in cur.fetchall()
])
print(f"交易日: {len(dates)} 个, {dates[0]} ~ {dates[-1]}", flush=True)

# 2. 一次性加载所有股票的日线数据（过去 400 天）
lookback_start = (datetime.strptime(dates[0], '%Y-%m-%d') - timedelta(days=400)).strftime('%Y-%m-%d')
cur.execute("""
    SELECT ts_code, trade_date, close, high, low, vol, pct_chg,
           turnover_rate, volume_ratio, ma5, ma10, ma20, rps_20
    FROM daily_price
    WHERE trade_date >= %s
    ORDER BY ts_code, trade_date
""", (lookback_start,))
raw = cur.fetchall()
cur.close()

price_df = pd.DataFrame(raw, columns=[
    'ts_code', 'trade_date', 'close', 'high', 'low', 'vol',
    'pct_chg', 'turnover_rate', 'volume_ratio', 'ma5', 'ma10', 'ma20', 'rps_20'
])
for c in ['close','high','low','vol','pct_chg','turnover_rate','volume_ratio','ma5','ma10','ma20','rps_20']:
    price_df[c] = pd.to_numeric(price_df[c], errors='coerce').fillna(0)
price_df['trade_date_str'] = price_df['trade_date'].apply(
    lambda d: d.strftime('%Y%m%d') if hasattr(d, 'strftime') else str(d)[:8]
)
print(f"日线数据: {len(price_df)} 行", flush=True)

# 预计算远期收益（参考 backtest_v6_3_fast.py Pattern D）
HOLD_DAYS = [1, 3, 5, 10]
price_df['trade_date_key'] = price_df['trade_date'].apply(
    lambda d: d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]
)
future_ret = {}
for ts_code, grp in price_df[['ts_code', 'trade_date_key', 'close']].groupby('ts_code'):
    grp = grp.sort_values('trade_date_key')
    closes = grp['close'].values
    date_keys = grp['trade_date_key'].values
    for i in range(len(date_keys)):
        if closes[i] <= 0:
            continue
        rets = {}
        for hd in HOLD_DAYS:
            if i + hd < len(closes) and closes[i + hd] > 0:
                rets[hd] = (closes[i + hd] - closes[i]) / closes[i] * 100
        future_ret[(ts_code, date_keys[i])] = rets
print(f"远期收益映射: {len(future_ret)} 条", flush=True)

# 3. 股票名称/行业
cur = conn.cursor()
cur.execute("SELECT ts_code, name, industry FROM stock_info")
info = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
cur.close()

# 4. 主力资金流（最近 45 天）
cur = conn.cursor()
cur.execute(f"""
    SELECT ts_code, trade_date, main_net FROM moneyflow_daily
    WHERE trade_date >= %s
""", (dates[0],))
mf_raw = cur.fetchall()
cur.close()
conn.close()

mf_df = pd.DataFrame(mf_raw, columns=['ts_code', 'trade_date', 'main_net'])
mf_df['trade_date_str'] = mf_df['trade_date'].apply(
    lambda d: d.strftime('%Y%m%d') if hasattr(d, 'strftime') else str(d)[:8]
)
mf_df['main_net'] = pd.to_numeric(mf_df['main_net'], errors='coerce').fillna(0)
mf_lookup = mf_df.set_index(['ts_code', 'trade_date_str'])['main_net'].to_dict()

# 5. 过滤条件
def is_valid_stock(ts_code, name):
    if not name: return False
    if 'ST' in name or '退' in name: return False
    if ts_code.startswith(('688','8','4','9')): return False
    return True

print("开始回测...", flush=True)

results = []
candidates_2x_all = []   # (ts_code, query_date, score)
candidates_1_5x_all = [] # (ts_code, query_date, score)
candidates_selected_all = [] # follow production rule: 2.0x if >=3, else 1.5x

for query_date in dates[-40:]:
    date_str = query_date.replace('-', '')
    day_data = price_df[price_df['trade_date_str'] == date_str].copy()

    if day_data.empty:
        continue

    # 基本过滤
    day_data['name'] = day_data['ts_code'].map(lambda c: info.get(c, ('',''))[0])
    day_data['industry'] = day_data['ts_code'].map(lambda c: info.get(c, ('',''))[1])
    day_data = day_data[
        day_data['ts_code'].apply(lambda c: is_valid_stock(c, day_data.loc[day_data['ts_code']==c,'name'].iloc[0] if not day_data.loc[day_data['ts_code']==c].empty else ''))
    ]

    # 这个过滤方式有点复杂，让我换个方法
    # 重置用简单的行过滤
    mask = day_data['close'] > 5
    mask &= ~day_data['ts_code'].str.startswith('688')
    mask &= ~day_data['ts_code'].str.startswith('8')
    mask &= ~day_data['ts_code'].str.startswith('4')
    mask &= ~day_data['ts_code'].str.startswith('9')
    nm = day_data['ts_code'].map(lambda c: info.get(c, ('',''))[0])
    mask &= ~nm.str.contains('ST|退', na=False)
    day_data = day_data[mask].copy()

    if day_data.empty:
        results.append({'date': query_date, 'cnt_2x': 0, 'cnt_1_5x': 0, 'pool': 0})
        continue

    codes = day_data['ts_code'].unique().tolist()

    # 52 周范围和 60 日均量：从全量数据中提取
    hist = price_df[price_df['ts_code'].isin(codes)].copy()
    hist_52 = hist[hist['trade_date_str'] < date_str].copy()
    lookback_52w = (datetime.strptime(query_date, '%Y-%m-%d') - timedelta(days=365)).strftime('%Y%m%d')
    hist_52 = hist_52[hist_52['trade_date_str'] >= lookback_52w]

    if hist_52.empty:
        results.append({'date': query_date, 'cnt_2x': 0, 'cnt_1_5x': 0, 'pool': 0})
        continue

    range_52 = hist_52.groupby('ts_code').agg(h52w=('high','max'), l52w=('low','min'))
    range_52 = range_52[(range_52['h52w'] > 0) & (range_52['l52w'] > 0) & (range_52['h52w'] > range_52['l52w'])]

    # 60 日均量
    lookback_60 = (datetime.strptime(query_date, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y%m%d')
    hist_60 = hist[hist['trade_date_str'] >= lookback_60]
    hist_60 = hist_60[hist_60['trade_date_str'] < date_str]
    vol_avg = hist_60.groupby('ts_code')['vol'].mean()
    vol_avg = vol_avg[vol_avg > 0]

    # 计算 52 周位置
    day_data = day_data.set_index('ts_code')
    day_data['h52w'] = day_data.index.map(range_52['h52w'])
    day_data['l52w'] = day_data.index.map(range_52['l52w'])
    day_data['avg_vol_60'] = day_data.index.map(vol_avg)

    # 过滤：有 52 周范围且 pos < 50%
    valid_mask = day_data['h52w'].notna() & day_data['l52w'].notna() & (day_data['h52w'] > day_data['l52w'])
    day_data = day_data[valid_mask].copy()
    day_data['pos_52w'] = np.maximum(0, (day_data['close'] - day_data['l52w']) / (day_data['h52w'] - day_data['l52w']) * 100)
    day_data = day_data[day_data['pos_52w'] < 50].copy()

    pool_size = len(day_data)
    if pool_size == 0:
        print(f"{query_date}  2.0x:   0  1.5x:   0  底部池:    0  (无底部股)", flush=True)
        results.append({'date': query_date, 'cnt_2x': 0, 'cnt_1_5x': 0, 'pool': 0})
        continue

    # 评分函数：独立实现以避免依赖 _score_bottom_awakening 的特定列名
    def score_row(row, threshold):
        close = float(row['close'])
        pct = float(row['pct_chg'])
        vr = float(row['volume_ratio'])
        tr = float(row['turnover_rate'])
        ma5 = float(row['ma5'])
        ma10 = float(row['ma10'])
        ma20 = float(row['ma20'])
        current_vol = float(row['vol'])
        pos = float(row['pos_52w'])
        avg_vol = float(row['avg_vol_60'])

        if close <= 0 or ma5 <= 0 or avg_vol <= 0 or current_vol <= 0:
            return None

        vol_expansion = current_vol / avg_vol
        if vol_expansion < threshold:
            return None

        vol_score = min(vol_expansion, 10) * 10
        position_score = max(0, 50 - pos) * 2

        bonus = 0
        if ma5 > ma10 > ma20:
            bonus += 20
        if vr > 1.5:
            bonus += 15
        if pct > 0:
            bonus += 10
        if pos < 30:
            bonus += 10

        return int(vol_score + position_score + bonus)

    def filter_by_threshold(thresh):
        scores = day_data.apply(lambda r: score_row(r, thresh), axis=1)
        return scores.dropna()

    scores_2x = filter_by_threshold(2.0)
    scores_1_5x = filter_by_threshold(1.5)

    cnt_2x = len(scores_2x)
    cnt_1_5x = len(scores_1_5x)
    degraded = '★降级' if cnt_2x < 3 else ''

    # 收集候选股明细（用于远期收益分析）
    for tc, sc in scores_2x.items():
        candidates_2x_all.append((tc, query_date, int(sc)))
    for tc, sc in scores_1_5x.items():
        candidates_1_5x_all.append((tc, query_date, int(sc)))

    # 按生产规则：2.0x >= 3 只用 2.0x，否则用 1.5x
    if cnt_2x >= 3:
        for tc, sc in scores_2x.items():
            candidates_selected_all.append((tc, query_date, int(sc), '2.0x'))
    else:
        for tc, sc in scores_1_5x.items():
            candidates_selected_all.append((tc, query_date, int(sc), '1.5x'))

    print(f"{query_date}  2.0x: {cnt_2x:>3}  1.5x: {cnt_1_5x:>3}  底部池: {pool_size:>4}  {degraded}", flush=True)
    results.append({'date': query_date, 'cnt_2x': cnt_2x, 'cnt_1_5x': cnt_1_5x, 'pool': pool_size})

print()
valid = len(results)
if valid:
    avg_2x = sum(r['cnt_2x'] for r in results) / valid
    avg_1_5x = sum(r['cnt_1_5x'] for r in results) / valid
    degraded_days = sum(1 for r in results if r['cnt_2x'] < 3)
    zero_days = sum(1 for r in results if r['cnt_2x'] == 0)
    gt5_days = sum(1 for r in results if r['cnt_2x'] >= 5)
    gt10_days = sum(1 for r in results if r['cnt_2x'] >= 10)
    print(f"{'='*50}")
    print(f"  底部苏醒策略回测 — {valid} 个交易日")
    print(f"{'='*50}")
    print(f"  底部池(52周<50%):   {sum(r['pool'] for r in results)//valid:.0f} 只/日")
    print(f"  日均候选(2.0x):      {avg_2x:.1f}")
    print(f"  日均候选(1.5x):      {avg_1_5x:.1f}")
    print(f"  降级天数(2.0x<3):    {degraded_days} ({degraded_days/valid*100:.0f}%)")
    print(f"  零候选天数:          {zero_days} ({zero_days/valid*100:.0f}%)")
    print(f"  候选>=5 天数:         {gt5_days} ({gt5_days/valid*100:.0f}%)")
    print(f"  候选>=10 天数:        {gt10_days} ({gt10_days/valid*100:.0f}%)")

# ========== 远期收益分析 ==========

def analyze_forward_returns(candidates, label):
    """计算一组候选股的远期收益统计"""
    if not candidates:
        return

    rows = []
    for item in candidates:
        if len(item) == 3:
            tc, qd, sc = item
            thresh_label = label
        else:
            tc, qd, sc, thresh_label = item

        fr = future_ret.get((tc, qd), {})
        r = {'ts_code': tc, 'date': qd, 'score': sc, 'threshold': thresh_label}
        for hd in HOLD_DAYS:
            r[f'ret_{hd}d'] = fr.get(hd, np.nan)
        rows.append(r)

    df = pd.DataFrame(rows)
    total = len(df)

    print(f"\n{'='*60}")
    print(f"  远期收益分析 — {label} ({total} 样本)")
    print(f"{'='*60}")

    for hd in HOLD_DAYS:
        col = f'ret_{hd}d'
        valid_df = df[df[col].notna()]
        n = len(valid_df)
        if n == 0:
            print(f"  {hd}日收益: 无数据")
            continue
        vals = valid_df[col]
        win_rate = (vals > 0).mean() * 100
        avg_ret = vals.mean()
        med_ret = vals.median()
        pos_avg = vals[vals > 0].mean() if (vals > 0).any() else 0
        neg_avg = vals[vals < 0].mean() if (vals < 0).any() else 0

        print(f"  {hd}日收益: 胜率={win_rate:.1f}%  "
              f"平均={avg_ret:+.2f}%  "
              f"中位={med_ret:+.2f}%  "
              f"正均={pos_avg:+.2f}%  "
              f"负均={neg_avg:+.2f}%  "
              f"(有效{n}/{total})")

    # 按月统计胜率
    df_valid = df.copy()
    for hd in HOLD_DAYS:
        col = f'ret_{hd}d'
        if col in df_valid.columns:
            df_valid[col] = pd.to_numeric(df_valid[col], errors='coerce')
    df_valid['month'] = df_valid['date'].str[:7]
    months = sorted(df_valid['month'].unique())

    monthly_win_rates = {}
    for hd in HOLD_DAYS:
        col = f'ret_{hd}d'
        monthly_win_rates[hd] = {}
        for m in months:
            m_df = df_valid[(df_valid['month'] == m) & (df_valid[col].notna())]
            if len(m_df) >= 3:
                monthly_win_rates[hd][m] = (m_df[col] > 0).mean() * 100

    print(f"\n  按月胜率(>3样本):")
    print(f"  {'月份':<10}", end='')
    for hd in HOLD_DAYS:
        print(f"  {hd}日", end='')
    print(f"  样本")
    print(f"  {'-'*50}")
    for m in months:
        m_df = df_valid[df_valid['month'] == m]
        n_m = len(m_df)
        if n_m < 3:
            continue
        print(f"  {m:<8}", end='')
        for hd in HOLD_DAYS:
            wr = monthly_win_rates[hd].get(m)
            if wr is not None:
                print(f"  {wr:>5.1f}%", end='')
            else:
                print(f"     --", end='')
        print(f"  {n_m}")


# 按阈值分析
analyze_forward_returns(candidates_2x_all, "2.0x 阈值")
analyze_forward_returns(candidates_1_5x_all, "1.5x 阈值")
analyze_forward_returns(candidates_selected_all, "生产规则(2.0x优先,不足降级)")
