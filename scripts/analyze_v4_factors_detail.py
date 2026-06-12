#!/usr/bin/env python3
"""V4策略新因子详细分析 — 多维度拆解"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymysql

from quant_app.utils.config import get_db_config

PY = "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
DB = get_db_config()
START_DATE, END_DATE = "2025-01-02", "2026-04-30"
HOLD_DAYS = [1, 3, 5]
TOP_N = 10

def fmt(k, v):
    if isinstance(v, float):
        return f"{v:+.2f}"
    return str(v)

# ============================================================
# 数据加载
# ============================================================
print("=" * 80)
print("加载数据...")
conn = pymysql.connect(**DB)
cur = conn.cursor()

# 行情
cur.execute("""
    SELECT d.ts_code, d.trade_date, d.close, d.pct_chg, d.turnover_rate,
           d.volume_ratio, d.ma5, d.ma10, d.ma20, d.rps_20,
           d.high_52w, d.low_52w, d.vol, d.amount, d.high, d.low, d.open,
           s.name, s.industry
    FROM daily_price d
    JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
    WHERE d.trade_date >= %s AND d.trade_date <= %s
      AND s.is_st = 0 AND d.close > 5
      AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '92%%'
      AND d.ts_code NOT LIKE '83%%' AND d.ts_code NOT LIKE '87%%'
      AND d.ts_code NOT LIKE '43%%'
""", (START_DATE, END_DATE))

by_date = defaultdict(list)
for r in cur.fetchall():
    by_date[str(r[1])].append({
        'ts_code': r[0], 'trade_date': str(r[1]), 'close': float(r[2] or 0),
        'pct_chg': float(r[3] or 0), 'turnover': float(r[4] or 0),
        'vol_ratio': float(r[5] or 0), 'ma5': float(r[6] or 0),
        'ma10': float(r[7] or 0), 'ma20': float(r[8] or 0),
        'rps': float(r[9] or 0), 'high_52w': float(r[10] or 0),
        'low_52w': float(r[11] or 0), 'vol': float(r[12] or 0),
        'amount': float(r[13] or 0), 'high': float(r[14] or 0),
        'low': float(r[15] or 0), 'open': float(r[16] or 0),
        'name': r[17] or '', 'industry': r[18] or '',
    })

trade_dates = sorted(by_date.keys())
print(f"  交易日: {len(trade_dates)}, {trade_dates[0]} ~ {trade_dates[-1]}")

# 大盘每日平均涨跌幅
market_daily = {}
for d in trade_dates:
    stocks = by_date[d]
    if stocks:
        market_daily[d] = sum(s['pct_chg'] for s in stocks) / len(stocks)

# 资金流向
cur.execute("SELECT ts_code, trade_date, main_net FROM moneyflow_daily WHERE trade_date>=%s AND trade_date<=%s",
            (START_DATE, END_DATE))
mf_map = defaultdict(dict)
for r in cur.fetchall():
    mf_map[r[0]][str(r[1])] = float(r[2] or 0)

# 龙虎榜
cur.execute("SELECT ts_code, trade_date, net_buy FROM dragon_tiger WHERE trade_date>=%s AND trade_date<=%s AND net_buy!=0",
            (START_DATE, END_DATE))
dt_map = defaultdict(list)
for r in cur.fetchall():
    dt_map[r[0]].append((str(r[1]), float(r[2] or 0)))

# 龙虎榜机构
cur.execute("""SELECT ts_code, trade_date, net_buy FROM dragon_tiger_inst
               WHERE trade_date>=%s AND trade_date<=%s AND net_buy!=0""",
            (START_DATE, END_DATE))
dti_map = defaultdict(list)
for r in cur.fetchall():
    dti_map[r[0]].append((str(r[1]), float(r[2] or 0)))

# 股东人数
cur.execute("SELECT ts_code, end_date, holder_num_change FROM holder_change WHERE end_date>=%s AND end_date<=%s",
            (START_DATE, END_DATE))
hc_map = defaultdict(list)
for r in cur.fetchall():
    hc_map[r[0]].append((str(r[1]), int(r[2] or 0)))

cur.close()

# 预计算未来收益
print("  预计算未来收益...")
price_by_code = defaultdict(list)
for d in trade_dates:
    for s in by_date[d]:
        price_by_code[s['ts_code']].append((d, s['close']))

future_ret = {}
for tc, pl in price_by_code.items():
    for i, (d, p) in enumerate(pl):
        if p <= 0: continue
        rets = {}
        for hd in HOLD_DAYS:
            if i + hd < len(pl) and pl[i+hd][1] > 0:
                rets[hd] = (pl[i+hd][1] - p) / p * 100
        future_ret[(tc, d)] = rets
print(f"  未来收益: {len(future_ret)}条")

# ============================================================
# 评分函数
# ============================================================
def v4_score(s, mf_map, date):
    sc = 0
    reasons = []
    pct = s['pct_chg']; vr = s['vol_ratio']; tr = s['turnover']
    ma5, ma10, ma20 = s['ma5'], s['ma10'], s['ma20']
    rps = s['rps']

    if not (1.0 < vr < 10 and tr > 1.5 and ma5 > ma10 > ma20 and s['close'] > ma5):
        if not (pct > 4.0 and vr > 2.0 and s['close'] > ma5):
            return 0, []

    if -3 <= pct < 0: sc += 30; reasons.append(f'回调{pct:.1f}%')
    elif 0 <= pct <= 3: sc += 25; reasons.append(f'涨幅{pct:.1f}%')
    elif 3 < pct <= 5: sc += 30; reasons.append(f'涨幅{pct:.1f}%')
    elif 5 < pct <= 10: sc += 20; reasons.append(f'大涨{pct:.1f}%')
    else: return 0, []

    if vr > 3: sc += 30
    elif vr > 1.5: sc += 25
    elif vr > 1.0: sc += 10

    if 5 <= tr <= 10: sc += 20
    elif 3 <= tr < 5: sc += 15
    elif 2 <= tr < 3: sc += 8
    elif tr > 20: sc += 5

    sc += 30 if ma5 > ma10 > ma20 else 16

    if rps >= 80: sc += 20
    elif rps >= 60: sc += 15
    elif rps >= 40: sc += 10

    h52, l52 = s['high_52w'], s['low_52w']
    if h52 and l52 and h52 > l52 > 0:
        pos = (s['close'] - l52) / (h52 - l52) * 100
        if pos < 60: sc += 15
        elif pos >= 85: return 0, []

    mf = mf_map.get(s['ts_code'], {})
    main_net = sum(mf.get(d, 0) for d in [date])
    if main_net > 5000: sc += 15
    elif main_net > 1000: sc += 10
    elif main_net > 0: sc += 5

    return sc, reasons


def get_dragon_bonus(ts_code, date, dt_map, dti_map):
    date_str = str(date)[:10]
    td_30 = (datetime.strptime(date_str, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    inst_net = sum(nb for td, nb in dti_map.get(ts_code, []) if td >= td_30)
    if inst_net > 30000000: return 15
    elif inst_net > 5000000: return 12
    listed = sum(1 for td, _ in dt_map.get(ts_code, []) if td >= td_30)
    if listed > 0: return 8
    return 0


def get_holder_bonus(ts_code, date, hc_map):
    date_str = str(date)[:10]
    rows = [(td, chg) for td, chg in hc_map.get(ts_code, []) if td <= date_str]
    rows.sort(key=lambda x: x[0], reverse=True)
    if len(rows) < 2: return 0
    decreases = sum(1 for _, chg in rows[:4] if chg < 0)
    if decreases >= 3: return 10
    elif decreases >= 2: return 7
    elif decreases >= 1: return 4
    return 0


# ============================================================
# 回测（带详细记录）
# ============================================================
def run_backtest_detailed(use_factors=False, label=""):
    trades = []  # 每笔: {date, code, name, industry, score, dt_bonus, hc_bonus, pct_chg, ret_1d, ret_3d, ret_5d, market_pct}

    for idx, date in enumerate(trade_dates):
        stocks = by_date.get(date, [])
        if not stocks: continue

        candidates = []
        for s in stocks:
            sc, reasons = v4_score(s, mf_map, date)
            if sc == 0: continue

            dt_bonus = 0
            hc_bonus = 0
            if use_factors:
                dt_bonus = get_dragon_bonus(s['ts_code'], date, dt_map, dti_map)
                hc_bonus = get_holder_bonus(s['ts_code'], date, hc_map)
                sc += dt_bonus + hc_bonus

            s['_score'] = sc
            s['_dt_bonus'] = dt_bonus
            s['_hc_bonus'] = hc_bonus
            candidates.append(s)

        if not candidates: continue
        candidates.sort(key=lambda x: x['_score'], reverse=True)
        selected = candidates[:TOP_N]

        for s in selected:
            fr = future_ret.get((s['ts_code'], date), {})
            trades.append({
                'date': date,
                'code': s['ts_code'],
                'name': s['name'],
                'industry': s['industry'],
                'score': s['_score'],
                'base_score': sc - dt_bonus - hc_bonus,
                'dt_bonus': dt_bonus,
                'hc_bonus': hc_bonus,
                'pct_chg': s['pct_chg'],
                'ret_1d': fr.get(1),
                'ret_3d': fr.get(3),
                'ret_5d': fr.get(5),
                'market_pct': market_daily.get(date, 0),
            })

    return trades


# ============================================================
# 分析
# ============================================================
def analyze(label, trades):
    results = {}

    # 总体
    for hd in [1, 3, 5]:
        vals = [t[f'ret_{hd}d'] for t in trades if t.get(f'ret_{hd}d') is not None]
        if not vals: continue
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n
        median = sorted(vals)[n // 2]
        p25 = sorted(vals)[n // 4]
        p75 = sorted(vals)[n * 3 // 4]
        best = max(vals)
        worst = min(vals)
        results[f'{hd}d'] = {
            'n': n, 'wr': wins/n*100, 'avg': avg, 'median': median,
            'p25': p25, 'p75': p75, 'best': best, 'worst': worst
        }
    return results


# 运行三组回测
print("\n" + "=" * 80)
print("回测运行中...")

t0 = run_backtest_detailed(use_factors=False, label="V4原版")
print(f"  V4原版: {len(t0)}笔")

t1 = run_backtest_detailed(use_factors=True, label="V4+新因子")
print(f"  V4+新因子: {len(t1)}笔")

# ============================================================
# 输出报告
# ============================================================
out = {}
data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

def section(title):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")

# --- 1. 基础对比 ---
section("一、基础对比（原版 vs +新因子）")

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    r = analyze(label, trades)
    out[label] = r
    print(f"\n【{label}】({len(trades)}笔)")
    print(f"{'持有期':>5} | {'胜率':>7} | {'均收益':>8} | {'中位数':>8} | {'P25':>8} | {'P75':>8} | {'最大':>8} | {'最小':>8} | {'笔数':>5}")
    print(f"{'-'*80}")
    for hd in [1, 3, 5]:
        if hd not in r: continue
        d = r[hd]
        print(f"{hd:>3}天 | {d['wr']:5.1f}% | {d['avg']:+7.2f}% | {d['median']:+7.2f}% | {d['p25']:+7.2f}% | {d['p75']:+7.2f}% | {d['best']:+7.2f}% | {d['worst']:+7.2f}% | {d['n']:>5}")

# --- 2. 改善幅度 ---
section("二、改善幅度（V4+新因子 vs V4原版）")
for hd in [1, 3, 5]:
    r0 = out["V4原版"].get(hd, {})
    r1 = out["V4+新因子"].get(hd, {})
    if not r0 or not r1: continue
    wr_imp = r1['wr'] - r0['wr']
    avg_imp = r1['avg'] - r0['avg']
    print(f"  {hd}日持有: 胜率{wr_imp:+.1f}pp | 均收益{avg_imp:+.2f}pp")

# --- 3. 按市场环境分类 ---
section("三、按市场环境分类")

def classify_market(pct):
    if pct > 0.5: return "上涨"
    elif pct < -0.5: return "下跌"
    else: return "震荡"

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    print(f"\n【{label}】")
    for regime in ["上涨", "下跌", "震荡"]:
        subset = [t for t in trades if classify_market(t['market_pct']) == regime]
        if not subset: continue
        for hd in [1, 3, 5]:
            vals = [t[f'ret_{hd}d'] for t in subset if t.get(f'ret_{hd}d') is not None]
            if not vals: continue
            n = len(vals)
            wins = sum(1 for v in vals if v > 0)
            avg = sum(vals) / n
            print(f"  {regime}市 {hd}日: 胜率{wins/n*100:.1f}% | 均收益{avg:+.2f}% | {n}笔")

# --- 4. 按板块分类 ---
section("四、按板块分类")

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    print(f"\n【{label}】")
    industries = defaultdict(list)
    for t in trades:
        ind = t['industry'] or "未知"
        industries[ind].append(t)

    # 取前10个板块
    sorted_inds = sorted(industries.items(), key=lambda x: len(x[1]), reverse=True)[:10]
    for ind, subset in sorted_inds:
        vals3 = [t['ret_3d'] for t in subset if t.get('ret_3d') is not None]
        if not vals3: continue
        n = len(vals3)
        wins = sum(1 for v in vals3 if v > 0)
        avg = sum(vals3) / n
        print(f"  {ind:<12}: 3日胜率{wins/n*100:.1f}% | 均收益{avg:+.2f}% | {n}笔")

# --- 5. 新因子贡献拆解 ---
section("五、新因子贡献拆解")

# 有龙虎榜加分的
dt_trades = [t for t in t1 if t['dt_bonus'] > 0]
hc_trades = [t for t in t1 if t['hc_bonus'] > 0]
both_trades = [t for t in t1 if t['dt_bonus'] > 0 and t['hc_bonus'] > 0]
neither_trades = [t for t in t1 if t['dt_bonus'] == 0 and t['hc_bonus'] == 0]

for label, subset in [
    ("仅龙虎榜加分", [t for t in t1 if t['dt_bonus'] > 0 and t['hc_bonus'] == 0]),
    ("仅股东加分", [t for t in t1 if t['dt_bonus'] == 0 and t['hc_bonus'] > 0]),
    ("两者都有", both_trades),
    ("两者都无", neither_trades),
]:
    if not subset: continue
    for hd in [1, 3, 5]:
        vals = [t[f'ret_{hd}d'] for t in subset if t.get(f'ret_{hd}d') is not None]
        if not vals: continue
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n
        print(f"  {label} {hd}日: 胜率{wins/n*100:.1f}% | 均收益{avg:+.2f}% | {n}笔")

# --- 6. 月度胜率趋势 ---
section("六、月度胜率趋势（3日持有）")

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    print(f"\n【{label}】")
    monthly = defaultdict(list)
    for t in trades:
        if t.get('ret_3d') is not None:
            month = t['date'][:7]
            monthly[month].append(t['ret_3d'])

    for month in sorted(monthly.keys()):
        vals = monthly[month]
        n = len(vals)
        wins = sum(1 for v in vals if v > 0)
        avg = sum(vals) / n
        print(f"  {month}: 胜率{wins/n*100:.1f}% | 均收益{avg:+.2f}% | {n}笔")

# --- 7. 连续亏损/盈利分析 ---
section("七、连续盈亏分析（3日持有）")

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    print(f"\n【{label}】")
    vals = [t['ret_3d'] for t in trades if t.get('ret_3d') is not None]

    max_consec_loss = 0
    curr_loss = 0
    max_consec_win = 0
    curr_win = 0
    worst_drawdown = 0
    curr_peak = 0
    cumsum = 0

    for v in vals:
        cumsum += v
        if v > 0:
            curr_win += 1
            max_consec_win = max(max_consec_win, curr_win)
            curr_loss = 0
        else:
            curr_loss += 1
            max_consec_loss = max(max_consec_loss, curr_loss)
            curr_win = 0

        curr_peak = max(curr_peak, cumsum)
        worst_drawdown = min(worst_drawdown, cumsum - curr_peak)

    print(f"  最大连续亏损: {max_consec_loss}次")
    print(f"  最大连续盈利: {max_consec_win}次")
    print(f"  最大回撤: {worst_drawdown:+.2f}%")

# --- 保存结果 ---
out_path = os.path.join(data_dir, 'backtest_v4_factors_detail.json')

# 序列化结果
detail_out = {
    'base_comparison': {},
    'by_market_regime': {},
    'by_factor': {},
    'monthly': {}
}

for label, trades in [("V4原版", t0), ("V4+新因子", t1)]:
    detail_out['base_comparison'][label] = analyze(label, trades)

    # 市场环境
    for regime in ["上涨", "下跌", "震荡"]:
        key = f"{label}_{regime}"
        subset = [t for t in trades if classify_market(t['market_pct']) == regime]
        detail_out['by_market_regime'][key] = {}
        for hd in [1, 3, 5]:
            vals = [t[f'ret_{hd}d'] for t in subset if t.get(f'ret_{hd}d') is not None]
            if vals:
                detail_out['by_market_regime'][key][f'{hd}d'] = {
                    'n': len(vals),
                    'wr': sum(1 for v in vals if v > 0)/len(vals)*100,
                    'avg': sum(vals)/len(vals)
                }

    # 因子贡献
    detail_out['by_factor'][label] = {}
    for flabel, fsubset in [
        ("仅龙虎榜", [t for t in trades if t['dt_bonus'] > 0 and t['hc_bonus'] == 0]),
        ("仅股东", [t for t in trades if t['dt_bonus'] == 0 and t['hc_bonus'] > 0]),
        ("两者都有", [t for t in trades if t['dt_bonus'] > 0 and t['hc_bonus'] > 0]),
        ("两者都无", [t for t in trades if t['dt_bonus'] == 0 and t['hc_bonus'] == 0]),
    ]:
        detail_out['by_factor'][label][flabel] = {}
        for hd in [1, 3, 5]:
            vals = [t[f'ret_{hd}d'] for t in fsubset if t.get(f'ret_{hd}d') is not None]
            if vals:
                detail_out['by_factor'][label][flabel][f'{hd}d'] = {
                    'n': len(vals),
                    'wr': sum(1 for v in vals if v > 0)/len(vals)*100,
                    'avg': sum(vals)/len(vals)
                }

    # 月度
    detail_out['monthly'][label] = {}
    monthly = defaultdict(list)
    for t in trades:
        if t.get('ret_3d') is not None:
            monthly[t['date'][:7]].append(t['ret_3d'])
    for month in sorted(monthly.keys()):
        vals = monthly[month]
        detail_out['monthly'][label][month] = {
            'n': len(vals),
            'wr': sum(1 for v in vals if v > 0)/len(vals)*100,
            'avg': sum(vals)/len(vals)
        }

with open(out_path, 'w') as f:
    json.dump(detail_out, f, ensure_ascii=False, indent=2)

print(f"\n结果保存: {out_path}")
