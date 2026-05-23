#!/usr/bin/env python3
"""
ML Top15 真实性验证 — 深挖 72.5% 胜率来源
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
import pymysql
import pandas as pd
import numpy as np
from collections import defaultdict

# 加载数据
conn = pymysql.connect(**get_db_config())
c = conn.cursor()
c.execute("""
    SELECT ts_code, trade_date, close, pct_chg, volume_ratio, turnover_rate,
           ma5, ma10, ma20, rps_20, high_52w, low_52w, vol, amount
    FROM daily_price
    WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
""")
cols = [d[0] for d in c.description]
daily = pd.DataFrame(c.fetchall(), columns=cols)
for col in ['close','pct_chg','volume_ratio','turnover_rate','ma5','ma10','ma20','rps_20','high_52w','low_52w','vol','amount']:
    daily[col] = pd.to_numeric(daily[col], errors='coerce')
daily['trade_date'] = pd.to_datetime(daily['trade_date'])
daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
conn.close()

# ============================================================
# 1. 计算实际未来收益（按交易日顺序，非日历日）
# ============================================================
def compute_fwd_returns(daily, hold_days=[1,3,5]):
    """按交易日索引计算T+N收益率"""
    fwd = {}
    for tc, grp in daily.groupby('ts_code'):
        grp = grp.sort_values('trade_date')
        closes = grp['close'].values
        dates = grp['trade_date'].values
        for i in range(len(dates)):
            if closes[i] <= 0: continue
            date_str = pd.Timestamp(dates[i]).strftime('%Y-%m-%d')
            rets = {}
            for hd in hold_days:
                if i + hd < len(closes) and closes[i+hd] > 0:
                    rets[hd] = (closes[i+hd] - closes[i]) / closes[i] * 100
            fwd[(tc, date_str)] = rets
    return fwd

fwd = compute_fwd_returns(daily)

# ============================================================
# 2. 加载 ML 预测 & 每日选股
# ============================================================
ml_preds = pd.read_parquet("data/ml_preds_v6_3.parquet")
ml_preds['trade_date'] = pd.to_datetime(ml_preds['trade_date'])
ml_preds['date_str'] = ml_preds['trade_date'].dt.strftime('%Y-%m-%d')

# 按交易日选 Top15
START, END = "2025-01-02", "2026-04-30"
ml_period = ml_preds[(ml_preds['trade_date'] >= START) & (ml_preds['trade_date'] <= END)]

daily_ml = {}
for date, grp in ml_period.groupby('trade_date'):
    date_str = pd.Timestamp(date).strftime('%Y-%m-%d')
    top = grp.nlargest(15, '_ml_pred')
    daily_ml[date_str] = top['ts_code'].tolist()

print(f"ML 选股交易日: {len(daily_ml)}天")

# ============================================================
# 3. 实际收益分析
# ============================================================
all_returns_3 = []  # 所有3日收益
by_code = defaultdict(list)  # 每只股票的收益记录
by_date = defaultdict(list)  # 每天的收益记录

for date, codes in daily_ml.items():
    for tc in codes:
        fr = fwd.get((tc, date), {})
        if 3 in fr:
            r = fr[3]
            all_returns_3.append(r)
            by_code[tc].append(r)
            by_date[date].append(r)

arr = np.array(all_returns_3)
print(f"\n=== ML Top15 实际3日收益统计 ===")
print(f"总样本: {len(arr)}笔")
print(f"胜率(>0): {(arr>0).sum()/len(arr)*100:.1f}%")
print(f"中位数收益: {np.median(arr):.2f}%")
print(f"平均收益: {np.mean(arr):.2f}%")
print(f"标准差: {np.std(arr):.2f}%")
print(f"最小: {np.min(arr):.2f}%")
print(f"最大: {np.max(arr):.2f}%")
print()
print("收益分布:")
for b in [(-100, -10), (-10, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 10), (10, 100)]:
    cnt = ((arr >= b[0]) & (arr < b[1])).sum()
    pct = cnt/len(arr)*100
    print(f"  [{b[0]:+6.0f}% ~ {b[1]:+6.0f}%): {cnt:4d}笔 ({pct:5.1f}%)")

# ============================================================
# 4. 关键问题1: 是不是靠"涨一点点就算赢"？
# ============================================================
print(f"\n=== 问题1: 胜率 vs 实际盈利幅度 ===")
win_tiny = ((arr > 0) & (arr < 0.5)).sum()  # 涨0~0.5%也算赢
win_small = ((arr > 0.5) & (arr < 2)).sum()  # 涨0.5~2%
win_med = ((arr >= 2) & (arr < 5)).sum()
win_big = (arr >= 5).sum()
print(f"  微涨0~0.5%: {win_tiny}笔 ({win_tiny/len(arr)*100:.1f}%)")
print(f"  小涨0.5~2%: {win_small}笔 ({win_small/len(arr)*100:.1f}%)")
print(f"  中涨2~5%: {win_med}笔 ({win_med/len(arr)*100:.1f}%)")
print(f"  大涨>5%: {win_big}笔 ({win_big/len(arr)*100:.1f}%)")
print(f"  → 真正有意义(>2%)的胜率: {(win_med+win_big)/len(arr)*100:.1f}%")

# ============================================================
# 5. 关键问题2: ML 选股的风格特征
# ============================================================
print(f"\n=== 问题2: ML 选股 vs V4.1 选股风格 ===")

# ML 选股日特征 — 用 merge 而不是 loc
ml_with_feat = ml_period.merge(
    daily[['ts_code','trade_date','volume_ratio','turnover_rate','pct_chg','rps_20','amount']],
    on=['ts_code','trade_date'], how='left')

ml_top15_rows = []
for date, grp in ml_period.groupby('trade_date'):
    date_str = pd.Timestamp(date).strftime('%Y-%m-%d')
    top = grp.nlargest(15, '_ml_pred')
    for _, row in top.iterrows():
        ml_top15_rows.append((row['ts_code'], date_str))

ml_sel_feat = ml_with_feat.merge(
    pd.DataFrame(ml_top15_rows, columns=['ts_code', 'date_str']),
    left_on=['ts_code', ml_with_feat['trade_date'].dt.strftime('%Y-%m-%d')],
    right_on=['ts_code', 'date_str'], how='inner')

# V4.1 选股日特征（从之前的回测结果拿）
# 需要重新跑一下V4.1选股获取特征
# 简化：直接用 daily 中满足V4.1条件的股票
daily_sel = daily[
    (daily['trade_date'] >= START) & (daily['trade_date'] <= END) &
    (daily['close'] > 0) & (daily['ma5'] > 0) & (daily['ma10'] > 0) & (daily['ma20'] > 0)
].copy()
daily_sel['trade_date_str'] = daily_sel['trade_date'].dt.strftime('%Y-%m-%d')

# 每天选Top10（简化版V4.1）
v41_feat_list = []
for date_str, grp in daily_sel.groupby('trade_date_str'):
    cond1 = (1.0 < grp['volume_ratio']) & (grp['volume_ratio'] < 10) & \
            (grp['turnover_rate'] > 1.5) & (grp['ma5'] > grp['ma10']) & \
            (grp['ma10'] > grp['ma20']) & (grp['close'] > grp['ma5'])
    cond2 = (grp['pct_chg'] > 4.0) & (grp['volume_ratio'] > 2.0) & (grp['close'] > grp['ma5'])
    passed = grp[cond1 | cond2]
    if len(passed) > 0:
        top = passed.nlargest(10, 'volume_ratio')  # 简化：按量比排序
        v41_feat_list.extend(top.index.tolist())
v41_sel_feat = daily_sel.loc[v41_feat_list]

print(f"{'特征':<15} {'ML Top15':>12} {'V4.1 Top10':>12} {'差异':>10}")
for col in ['pct_chg', 'volume_ratio', 'turnover_rate', 'rps_20', 'amount']:
    ml_m = ml_sel_feat[col].median()
    v4_m = v41_sel_feat[col].median()
    diff = f"+{(ml_m-v4_m)/v4_m*100:.0f}%" if v4_m != 0 else "N/A"
    print(f"{col:<15} {ml_m:>11.2f} {v4_m:>11.2f} {diff:>10}")

# ============================================================
# 6. 关键问题3: 是否存在数据泄露？
# ============================================================
print(f"\n=== 问题3: 检查数据泄露 ===")
# 看 ML 预测值和当日涨跌幅的相关性
corr = ml_sel_feat['_ml_pred'].corr(ml_sel_feat['pct_chg'])
print(f"  ML预测值 vs 当日涨幅相关系数: {corr:.4f}")
if abs(corr) > 0.3:
    print(f"  ⚠️ 警告：相关系数较高，可能存在未来数据泄露！")
else:
    print(f"  ✓ 相关系数低，无明显泄露")

# 检查 ML 预测值和未来3日收益的相关性
ml_returns = []
ml_scores = []
for idx in ml_sel_feat.index:
    row = ml_sel_feat.loc[idx]
    tc = row['ts_code']
    date_str = pd.Timestamp(row['trade_date']).strftime('%Y-%m-%d')
    fr = fwd.get((tc, date_str), {})
    if 3 in fr:
        ml_returns.append(fr[3])
        ml_scores.append(row['_ml_pred'])
if len(ml_returns) > 0:
    fwd_corr = np.corrcoef(ml_scores, ml_returns)[0,1]
    print(f"  ML预测值 vs 未来3日收益相关系数: {fwd_corr:.4f}")
    if fwd_corr > 0.1:
        print(f"  ✓ 有正向预测能力")
    else:
        print(f"  ⚠️ 预测能力极弱，高胜率可能来自选股偏差")

# ============================================================
# 7. 关键问题4: 逐日胜率分布（是否集中在某些天？）
# ============================================================
print(f"\n=== 问题4: 逐日胜率分布 ===")
daily_win_rates = []
for date, returns in by_date.items():
    if len(returns) >= 5:  # 至少有5只
        wr = sum(1 for r in returns if r > 0) / len(returns) * 100
        daily_win_rates.append(wr)

arr_wr = np.array(daily_win_rates)
print(f"  有效交易日: {len(arr_wr)}天")
print(f"  逐日胜率中位数: {np.median(arr_wr):.1f}%")
print(f"  逐日胜率均值: {np.mean(arr_wr):.1f}%")
print(f"  逐日胜率标准差: {np.std(arr_wr):.1f}%")
print(f"  胜率>60%的天数: {(arr_wr>60).sum()}天 ({(arr_wr>60).sum()/len(arr_wr)*100:.0f}%)")
print(f"  胜率<40%的天数: {(arr_wr<40).sum()}天 ({(arr_wr<40).sum()/len(arr_wr)*100:.0f}%)")

# 最差的10天
worst = sorted(by_date.items(), key=lambda x: sum(1 for r in x[1] if r>0)/len(x[1])*100)[:10]
print(f"\n  最差的10天:")
for date, returns in worst:
    wr = sum(1 for r in returns if r > 0) / len(returns) * 100
    avg = np.mean(returns)
    print(f"    {date}: 胜率{wr:.0f}%, 均收益{avg:+.2f}%")

print("\n=== 总结 ===")
med_ret = np.median(arr)
mean_ret = np.mean(arr)
win = (arr > 0).sum() / len(arr) * 100
meaningful_win = ((arr >= 2).sum()) / len(arr) * 100

if med_ret < 1 and win > 60:
    print("⚠️ ML 高胜率来自'微涨就算赢'，中位数收益仅 {:.2f}%，实际价值有限".format(med_ret))
elif mean_ret > 3 and win > 60:
    print("✓ ML 确实有效，高胜率配合高收益")
else:
    print("? ML 表现需要综合评估")

print(f"  最终判断依据：")
print(f"  - 中位数收益: {med_ret:+.2f}% (如果<1%，说明靠微涨刷胜率)")
print(f"  - 真正有意义的胜率(>2%): {meaningful_win:.1f}%")
print(f"  - 逐日胜率稳定性: 中位数{np.median(arr_wr):.1f}%")
