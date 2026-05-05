#!/usr/bin/env python3
# DEPRECATED: 使用 scripts/run_backtest.py compare 替代
"""
V4.1 vs ML 多维度验证（修复版）
1. 重叠度分析（交集频率 & 交集胜率）
2. 分阶段胜率对比（牛市/震荡/熊市）
3. 独立策略对比（V4.1单独 / ML单独 / 交集 / 并集）
"""
import os, sys, logging
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
import pymysql
import pandas as pd
import numpy as np

HOLD_DAYS = [1, 3, 5]
TOP_N = 10
ML_TOP_N = 15

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# 1. 加载数据
# ============================================================
def load_df(sql, date_cols=('trade_date',)):
    conn = pymysql.connect(**get_db_config())
    c = conn.cursor()
    c.execute(sql)
    cols = [d[0] for d in c.description]
    df = pd.DataFrame(c.fetchall(), columns=cols)
    conn.close()
    for dc in date_cols:
        if dc in df.columns:
            df[dc] = pd.to_datetime(df[dc])
    # 数值列转换
    for col in df.select_dtypes(include=['object']).columns:
        if col not in date_cols and col != 'ts_code':
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df

logger.info("加载数据...")
daily = load_df("""
    SELECT ts_code, trade_date, close, pct_chg, turnover_rate, volume_ratio,
           ma5, ma10, ma20, rps_20, high_52w, low_52w, vol, amount
    FROM daily_price
    WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
""")
for c in ['vol','amount','close']:
    daily[c] = daily[c].fillna(0)

mf = load_df("""
    SELECT ts_code, trade_date, main_net FROM moneyflow_daily
    WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
""")
mf['main_net'] = mf['main_net'].fillna(0)

dt_df = load_df("""
    SELECT ts_code, trade_date, net_buy FROM dragon_tiger
    WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY AND net_buy != 0
""")
dti_df = load_df("""
    SELECT ts_code, trade_date, net_buy FROM dragon_tiger_inst
    WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY AND net_buy != 0
""")
hc_df = load_df("""
    SELECT ts_code, end_date as trade_date, holder_num_change FROM holder_change
    WHERE end_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
""")

idx = load_df("""
    SELECT trade_date, close_price FROM market_index_daily
    WHERE index_code='000001.SH' ORDER BY trade_date
""")

# ============================================================
# 2. 合并主力数据
# ============================================================
daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left')
daily['main_net'] = daily['main_net'].fillna(0)
daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

# ============================================================
# 3. 未来收益映射 (trade_date 用字符串)
# ============================================================
logger.info("计算未来收益...")
future_ret = {}
for tc, grp in daily.groupby('ts_code'):
    grp = grp.sort_values('trade_date')
    closes = grp['close'].values; dates = grp['trade_date'].values
    for i in range(len(dates)):
        if closes[i] <= 0: continue
        date_str = pd.Timestamp(dates[i]).strftime('%Y-%m-%d')
        rets = {}
        for hd in HOLD_DAYS:
            if i + hd < len(closes) and closes[i+hd] > 0:
                rets[hd] = (closes[i+hd] - closes[i]) / closes[i] * 100
        future_ret[(tc, date_str)] = rets

# ============================================================
# 4. 加载 ML 预测
# ============================================================
logger.info("加载 ML 预测...")
ml_preds = pd.read_parquet("data/ml_preds_v6_3.parquet")
ml_preds['trade_date'] = pd.to_datetime(ml_preds['trade_date'])
ml_lookup = ml_preds.set_index(['ts_code', 'trade_date'])['_ml_pred'].to_dict()

# ============================================================
# 5. 龙虎榜 / 股东数据字典
# ============================================================
dt_d = defaultdict(list)
if not dt_df.empty:
    for _, r in dt_df.iterrows():
        dt_d[r['ts_code']].append((pd.Timestamp(r['trade_date']).strftime('%Y-%m-%d'), float(r.get('net_buy',0) or 0)))
dti_d = defaultdict(list)
if not dti_df.empty:
    for _, r in dti_df.iterrows():
        dti_d[r['ts_code']].append((pd.Timestamp(r['trade_date']).strftime('%Y-%m-%d'), float(r.get('net_buy',0) or 0)))
hc_d = defaultdict(list)
if not hc_df.empty:
    for _, r in hc_df.iterrows():
        hc_d[r['ts_code']].append((pd.Timestamp(r['trade_date']).strftime('%Y-%m-%d'), int(r.get('holder_num_change',0) or 0)))

# ============================================================
# 6. V4.1 评分
# ============================================================
def v4_score_from_row(row):
    pct = float(row.get('pct_chg', 0))
    vr  = float(row.get('volume_ratio', 0))
    tr  = float(row.get('turnover_rate', 0))
    ma5 = float(row.get('ma5', 0))
    ma10 = float(row.get('ma10', 0))
    ma20 = float(row.get('ma20', 0))
    rps = float(row.get('rps_20', 0))
    close = float(row.get('close', 0))
    h52w = float(row.get('high_52w', 0) or 0)
    l52w = float(row.get('low_52w', 0) or 0)
    main_net = float(row.get('main_net', 0) or 0)

    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return -1

    # 条件过滤
    cond1 = (1.0 < vr < 10 and tr > 1.5 and ma5 > ma10 > ma20 and close > ma5)
    cond2 = (pct > 4.0 and vr > 2.0 and close > ma5)
    if not cond1 and not cond2:
        return -1

    sc = 0
    # 涨幅
    if -3 <= pct < 0: sc += 30
    elif 0 <= pct <= 3: sc += 25
    elif 3 < pct <= 5: sc += 30
    elif 5 < pct <= 10: sc += 20
    else: return -1

    # 量比
    if vr > 3: sc += 30
    elif vr > 1.5: sc += 25
    elif vr > 1.0: sc += 10

    # 换手率
    if 5 <= tr <= 10: sc += 20
    elif 3 <= tr < 5: sc += 15
    elif 2 <= tr < 3: sc += 8
    elif tr > 20: sc += 5

    # 均线多头
    sc += 30 if ma5 > ma10 > ma20 else 16

    # RPS
    if rps >= 80: sc += 20
    elif rps >= 60: sc += 15
    elif rps >= 40: sc += 10

    # 52周位置
    if h52w and l52w and h52w > l52w > 0:
        pos = (close - l52w) / (h52w - l52w) * 100
        if pos < 60: sc += 15
        elif pos >= 85: return -1

    # 主力资金
    if main_net > 5000: sc += 15
    elif main_net > 1000: sc += 10
    elif main_net > 0: sc += 5

    return sc

def dragon_bonus(tc, date):
    td_30 = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    inst = sum(nb for td, nb in dti_d.get(tc, []) if td >= td_30)
    if inst > 30000000: return 15
    elif inst > 5000000: return 12
    if sum(1 for td, _ in dt_d.get(tc, []) if td >= td_30) > 0: return 8
    return 0

def holder_bonus(tc, date):
    rows = sorted([(td, c) for td, c in hc_d.get(tc, []) if td <= date], reverse=True)
    if len(rows) < 2: return 0
    dec = sum(1 for _, c in rows[:4] if c < 0)
    if dec >= 3: return 10
    elif dec >= 2: return 7
    elif dec >= 1: return 4
    return 0

# ============================================================
# 7. 每日选股
# ============================================================
logger.info("每日选股...")
daily['trade_date_str'] = daily['trade_date'].dt.strftime('%Y-%m-%d')
trade_dates = sorted(daily['trade_date_str'].unique())
START_DATE, END_DATE = "2025-01-02", "2026-04-30"
trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]

daily_v4 = defaultdict(list)
daily_ml = defaultdict(list)

for date in trade_dates:
    day = daily[daily['trade_date_str'] == date]
    if day.empty: continue

    v4_cands = []
    for _, row in day.iterrows():
        tc = row['ts_code']
        sc = v4_score_from_row(row)
        if sc < 0: continue
        sc += dragon_bonus(tc, date) + holder_bonus(tc, date)
        v4_cands.append((tc, sc))
    v4_cands.sort(key=lambda x: x[1], reverse=True)
    daily_v4[date] = [(tc, sc) for tc, sc in v4_cands[:TOP_N]]

    ml_cands = []
    for tc in day['ts_code'].values:
        ms = ml_lookup.get((tc, pd.Timestamp(date)), 0)
        ml_cands.append((tc, ms))
    ml_cands.sort(key=lambda x: x[1], reverse=True)
    daily_ml[date] = [(tc, ms) for tc, ms in ml_cands[:ML_TOP_N]]

# ============================================================
# 8. 策略评估
# ============================================================
def eval_strategy(name, daily_selections):
    win = {1:0, 3:0, 5:0}; total = {1:0, 3:0, 5:0}; ret_sum = {1:0.0, 3:0.0, 5:0.0}
    trade_count = 0
    for date, selections in daily_selections.items():
        if not selections: continue
        if isinstance(selections[0], tuple):
            selected = [tc for tc, _ in selections]
        else:
            selected = selections
        for tc in selected:
            fr = future_ret.get((tc, date), {})
            trade_count += 1
            for hd in HOLD_DAYS:
                r = fr.get(hd)
                if r is not None:
                    total[hd] += 1; ret_sum[hd] += r
                    if r > 0: win[hd] += 1
    return {
        'name': name,
        'trades': trade_count,
        'win_1': win[1]/total[1]*100 if total[1] else 0,
        'win_3': win[3]/total[3]*100 if total[3] else 0,
        'win_5': win[5]/total[5]*100 if total[5] else 0,
        'avg_1': ret_sum[1]/total[1] if total[1] else 0,
        'avg_3': ret_sum[3]/total[3] if total[3] else 0,
        'avg_5': ret_sum[5]/total[5] if total[5] else 0,
    }

v41_sel = {d: daily_v4[d] for d in trade_dates}
ml_sel = {d: daily_ml[d] for d in trade_dates}

intersection_sel = {}
for d in trade_dates:
    v4_set = set(tc for tc, _ in daily_v4[d])
    ml_set = set(tc for tc, _ in daily_ml[d])
    inter = v4_set & ml_set
    if inter:
        intersection_sel[d] = list(inter)

union_sel = {}
for d in trade_dates:
    v4_set = set(tc for tc, _ in daily_v4[d])
    ml_set = set(tc for tc, _ in daily_ml[d])
    union_sel[d] = list(v4_set | ml_set)

r_v41 = eval_strategy("V4.1 Top10", v41_sel)
r_ml = eval_strategy("ML Top15", ml_sel)
r_inter = eval_strategy("V4.1∩ML交集", intersection_sel)
r_union = eval_strategy("V4.1∪ML并集", union_sel)

# ============================================================
# 9. 重叠度
# ============================================================
overlap_counts = defaultdict(int)
for d in trade_dates:
    v4_set = set(tc for tc, _ in daily_v4[d])
    ml_set = set(tc for tc, _ in daily_ml[d])
    overlap_counts[len(v4_set & ml_set)] += 1
avg_overlap = sum(n*c for n,c in overlap_counts.items()) / len(trade_dates)

# ============================================================
# 10. 分阶段分析
# ============================================================
logger.info("划分市场阶段...")
idx = idx.sort_values('trade_date')
idx['date_str'] = idx['trade_date'].dt.strftime('%Y-%m-%d')
idx['ma60'] = idx['close_price'].rolling(60).mean()
idx['trend'] = idx['close_price'] / idx['ma60'] - 1

idx_trade = idx.set_index('date_str')['trend'].to_dict()

def get_regime(date_str):
    # 找最近一个有指数的交易日
    best = None
    for d in trade_dates:
        if d <= date_str:
            if d in idx_trade:
                best = d
    if not best: return '震荡'
    t = idx_trade[best]
    if pd.isna(t): return '震荡'
    if t > 0.03: return '牛市'
    elif t < -0.03: return '熊市'
    else: return '震荡'

def eval_by_regime(daily_selections):
    by_regime = defaultdict(lambda: {1:{'w':0,'t':0,'r':0.0}, 3:{'w':0,'t':0,'r':0.0}, 5:{'w':0,'t':0,'r':0.0}})
    for date, selections in daily_selections.items():
        regime = get_regime(date)
        if not selections: continue
        if isinstance(selections[0], tuple): selected = [tc for tc, _ in selections]
        else: selected = selections
        for tc in selected:
            fr = future_ret.get((tc, date), {})
            for hd in HOLD_DAYS:
                r = fr.get(hd)
                if r is not None:
                    by_regime[regime][hd]['t'] += 1
                    by_regime[regime][hd]['r'] += r
                    if r > 0: by_regime[regime][hd]['w'] += 1
    return by_regime

regimes_v4 = eval_by_regime(v41_sel)
regimes_ml = eval_by_regime(ml_sel)
regimes_inter = eval_by_regime(intersection_sel)

# ============================================================
# 11. 输出
# ============================================================
print("\n" + "="*60)
print("=== V4.1 vs ML 多维度验证报告 ===")
print(f"回测区间: {START_DATE} ~ {END_DATE}")
print("="*60)

print("\n【独立策略回测】")
print(f"{'策略':<20} {'交易数':>6} {'1日胜率':>8} {'3日胜率':>8} {'5日胜率':>8} {'3日均收益':>10}")
for r in [r_v41, r_ml, r_inter, r_union]:
    print(f"{r['name']:<20} {r['trades']:>6} {r['win_1']:>7.1f}% {r['win_3']:>7.1f}% {r['win_5']:>7.1f}% {r['avg_3']:>+9.2f}%")

print("\n【重叠度分析】")
print(f"总交易日: {len(trade_dates)}")
for n in sorted(overlap_counts.keys()):
    pct = overlap_counts[n]/len(trade_dates)*100
    print(f"  交集{n}只: {overlap_counts[n]}天 ({pct:.1f}%)")
print(f"  平均每交易日交集: {avg_overlap:.1f}只")
if r_inter['trades'] > 0:
    print(f"  交集股票3日胜率: {r_inter['win_3']:.1f}% ({r_inter['trades']}笔)")

print("\n【分阶段胜率对比 (3日持有期)】")
print(f"{'阶段':<8} {'V4.1胜率':>10} {'V4.1均收益':>12} {'ML胜率':>10} {'ML均收益':>12} {'交集胜率':>10}")
for regime in ['牛市', '震荡', '熊市']:
    rv = regimes_v4[regime]
    rm = regimes_ml[regime]
    ri = regimes_inter[regime]
    w3_v = rv[3]['w']/rv[3]['t']*100 if rv[3]['t'] else 0
    a3_v = rv[3]['r']/rv[3]['t'] if rv[3]['t'] else 0
    w3_m = rm[3]['w']/rm[3]['t']*100 if rm[3]['t'] else 0
    a3_m = rm[3]['r']/rm[3]['t'] if rm[3]['t'] else 0
    w3_i = ri[3]['w']/ri[3]['t']*100 if ri[3]['t'] else 0
    print(f"{regime:<8} {w3_v:>9.1f}% {a3_v:>+11.2f}% {w3_m:>9.1f}% {a3_m:>+11.2f}% {w3_i:>9.1f}%")

print("\n【结论】")
best = max([('V4.1', r_v41['win_3']), ('ML', r_ml['win_3']), ('交集', r_inter['win_3'])], key=lambda x: x[1])
print(f"  → 3日胜率最高: {best[0]} ({best[1]:.1f}%)")
if avg_overlap <= 1.5:
    print(f"  → V4.1和ML选股几乎无重叠（日均{avg_overlap:.1f}只交集），说明两者逻辑独立，互补性强")
else:
    print(f"  → V4.1和ML有较高重叠（日均{avg_overlap:.1f}只交集），说明两者选股逻辑相似")

print(f"\n报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
