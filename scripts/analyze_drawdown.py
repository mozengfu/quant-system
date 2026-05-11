#!/usr/bin/env python3
"""分析 V4.1→V6.5 级联策略回撤分布"""
import os, sys, json
from datetime import datetime
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
import pymysql
import pandas as pd

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
START_DATE, END_DATE = "2025-10-01", "2026-04-30"
START_INT = START_DATE.replace('-', '')
END_INT = END_DATE.replace('-', '')

TOP_N = 5
V41_CANDIDATE_LIMIT = 30
INITIAL_CASH = 100000.0
MAX_POSITIONS = 5
MAX_HOLD_DAYS = 7
COMMISSION = 0.0003
SLIPPAGE = 0.0001
STOP_LOSS = -0.03
TAKE_PROFIT_TIERS = [(0.06, 1/3), (0.10, 1/3), (0.18, 1.0)]

def load_df(sql):
    conn = pymysql.connect(**get_db_config())
    df = pd.read_sql(sql, conn)
    conn.close()
    return df

# ========== 加载数据 ==========
print("加载数据...")
daily = load_df(f"""
    SELECT ts_code, trade_date, close, pct_chg, turnover_rate, volume_ratio,
           ma5, ma10, ma20, rps_20, high_52w, low_52w, vol, amount
    FROM daily_price
    WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
""")
for c in ['vol','amount','close']:
    daily[c] = daily[c].fillna(0)

mf = load_df(f"""
    SELECT ts_code, trade_date, main_net FROM moneyflow_daily
    WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
""")
mf['main_net'] = mf['main_net'].fillna(0)
daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left')
daily['main_net'] = daily['main_net'].fillna(0)
daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

# V4.1 评分
def v4_score(row):
    pct, vr, tr = float(row.get('pct_chg',0)), float(row.get('volume_ratio',0)), float(row.get('turnover_rate',0))
    ma5, ma10, ma20 = float(row.get('ma5',0)), float(row.get('ma10',0)), float(row.get('ma20',0))
    rps, close = float(row.get('rps_20',0)), float(row.get('close',0))
    h52w, l52w = float(row.get('high_52w',0) or 0), float(row.get('low_52w',0) or 0)
    main_net = float(row.get('main_net',0) or 0)
    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0: return -1
    cond1 = (1.0 < vr < 10 and tr > 1.5 and ma5 > ma10 > ma20 and close > ma5)
    cond2 = (pct > 4.0 and vr > 2.0 and close > ma5)
    if not cond1 and not cond2: return -1
    sc = 0
    if -3 <= pct < 0: sc += 30
    elif 0 <= pct <= 3: sc += 25
    elif 3 < pct <= 5: sc += 30
    elif 5 < pct <= 10: sc += 20
    else: return -1
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
    if h52w and l52w and h52w > l52w > 0:
        pos = (close - l52w) / (h52w - l52w) * 100
        if pos < 60: sc += 15
        elif pos >= 85: return -1
    if main_net > 5000: sc += 15
    elif main_net > 1000: sc += 10
    elif main_net > 0: sc += 5
    return sc

# 预计算每日候选
dly = daily.copy()
dly['trade_date'] = pd.to_datetime(dly['trade_date'])
dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')
trade_dates = sorted(dly['date_str'].unique())
trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]

daily_v41_candidates = {}
for date in trade_dates:
    day = dly[dly['date_str'] == date]
    if day.empty: continue
    cands = []
    for _, row in day.iterrows():
        sc = v4_score(row)
        if sc < 0: continue
        cands.append((row['ts_code'], sc))
    cands.sort(key=lambda x: x[1], reverse=True)
    daily_v41_candidates[date] = [tc for tc,_ in cands[:V41_CANDIDATE_LIMIT]]

# 加载 ML 模型
print("加载 V6.5 模型...")
from ml_predict import _load_best_model, _build_features_for_stocks_v6_3, _ensemble_predict
bundle, version = _load_best_model()

conn = pymysql.connect(**get_db_config())
ml_cache = {}
for di, date in enumerate(trade_dates):
    cands = daily_v41_candidates.get(date, [])
    if not cands: continue
    try:
        feat_df = _build_features_for_stocks_v6_3(conn, cands, as_of_date=date)
        if feat_df is not None and not feat_df.empty:
            preds = _ensemble_predict(feat_df, bundle)
            for i, (_, row) in enumerate(feat_df.iterrows()):
                ml_cache[(row['ts_code'], date)] = float(preds[i])
    except Exception as e:
        pass
    for tc in cands:
        if (tc, date) not in ml_cache:
            ml_cache[(tc, date)] = 0.0
conn.close()

daily_buys = {}
for date in trade_dates:
    cands = daily_v41_candidates.get(date, [])
    if not cands: continue
    scored = [(tc, ml_cache.get((tc, date), 0.0)) for tc in cands]
    scored.sort(key=lambda x: x[1], reverse=True)
    daily_buys[date] = [tc for tc, _ in scored[:TOP_N]]

# ========== 回测引擎（记录每日详细净值） ==========
print("运行回测...")
price_data = {}
for _, row in daily.iterrows():
    tc = row['ts_code']
    dt_ymd = str(row['trade_date'])[:10].replace('-', '')
    price_data[(tc, dt_ymd)] = (float(row['close'] or 0), float(row['pct_chg'] or 0))

trade_dates_ymd = [d.replace('-', '') for d in trade_dates]

cash = INITIAL_CASH
positions = {}
equity_curve = []  # 记录每天的详细数据

for i in range(len(trade_dates_ymd) - 1):
    today = trade_dates_ymd[i]
    tomorrow = trade_dates_ymd[i + 1]
    today_str = trade_dates[i]

    # 卖出
    sell_codes = []
    for code, pos in list(positions.items()):
        price_info = price_data.get((code, tomorrow))
        if not price_info or price_info[0] <= 0:
            pos['days_held'] += 1
            if pos['days_held'] >= MAX_HOLD_DAYS:
                sell_codes.append((code, price_info[0] if price_info else pos['buy_price']))
            continue
        close = price_info[0]
        pct = (close - pos['buy_price']) / pos['buy_price']
        pos['days_held'] += 1
        if pct < STOP_LOSS:
            sell_codes.append((code, close))
            continue
        # 分段止盈（简化：只记现金变化）
        remaining = 1.0 - pos.get('tiers_sold', 0.0)
        if remaining > 0:
            for ti, (tp_level, tp_ratio) in enumerate(TAKE_PROFIT_TIERS):
                if pct >= tp_level and not pos.get(f'tier_{ti}_sold', False):
                    sell_shares = int(pos['shares'] * tp_ratio * remaining)
                    if sell_shares > 0:
                        sell_value = sell_shares * close * (1 - COMMISSION - SLIPPAGE)
                        cash += sell_value
                        pos['tiers_sold'] = pos.get('tiers_sold', 0.0) + tp_ratio * remaining
                        pos[f'tier_{ti}_sold'] = True
                    break
        if pos['days_held'] >= MAX_HOLD_DAYS:
            sell_codes.append((code, close))

    for code, sell_price in sell_codes:
        if code in positions:
            pos = positions.pop(code)
            remaining_shares = pos['shares'] * (1 - pos.get('tiers_sold', 0.0))
            if remaining_shares > 0:
                sell_value = remaining_shares * sell_price * (1 - COMMISSION - SLIPPAGE)
                cash += sell_value

    # 买入
    if today_str in daily_buys:
        for code in daily_buys[today_str]:
            if len(positions) >= MAX_POSITIONS: break
            if code in positions: continue
            price_info = price_data.get((code, today))
            if not price_info or price_info[0] <= 0: continue
            buy_price = price_info[0]
            price_info_tomorrow = price_data.get((code, tomorrow))
            if price_info_tomorrow and price_info_tomorrow[0] > 0:
                gap_pct = (price_info_tomorrow[0] - buy_price) / buy_price * 100
                if gap_pct > 2: continue
            position_cash = cash / (MAX_POSITIONS - len(positions))
            shares = int(position_cash / (buy_price * (1 + COMMISSION)))
            if shares <= 0: continue
            cost = shares * buy_price * (1 + COMMISSION)
            if cost > cash:
                shares = int(cash / (buy_price * (1 + COMMISSION)))
                if shares <= 0: continue
                cost = shares * buy_price * (1 + COMMISSION)
            cash -= cost
            positions[code] = {'buy_date': today, 'buy_price': buy_price, 'shares': shares, 'days_held': 0, 'tiers_sold': 0.0}

    # 净值
    pos_value = 0.0
    for code, pos in list(positions.items()):
        pi = price_data.get((code, today))
        if pi and pi[0] > 0:
            remaining = 1.0 - pos.get('tiers_sold', 0.0)
            pos_value += pos['shares'] * pi[0] * remaining
    total = cash + pos_value
    equity_curve.append({'date': today_str, 'total': round(total, 2), 'cash': round(cash, 2), 'position': round(pos_value, 2), 'n_positions': len(positions)})

# 最后一天平仓
for code, pos in list(positions.items()):
    pi = price_data.get((code, trade_dates_ymd[-1]))
    sell_price = pi[0] if pi and pi[0] > 0 else pos['buy_price']
    remaining = 1.0 - pos.get('tiers_sold', 0.0)
    if remaining > 0 and pos['shares'] > 0:
        sell_value = pos['shares'] * remaining * sell_price * (1 - COMMISSION - SLIPPAGE)
        cash += sell_value

final_value = cash
equity_curve.append({'date': trade_dates[-1], 'total': round(final_value, 2), 'cash': round(final_value, 2), 'position': 0, 'n_positions': 0})

# ========== 回撤分析 ==========
equity_vals = [e['total'] for e in equity_curve]
dates = [e['date'] for e in equity_curve]

# 计算每日回撤
peak = equity_vals[0]
max_dd = 0
peak_date = dates[0]
dd_start = None
drawdown_periods = []  # (start_date, end_date, peak_val, trough_val, dd_pct, duration_days)
current_dd_start = None
current_peak = equity_vals[0]
current_peak_date = dates[0]
in_drawdown = False

daily_dd = []  # (date, equity, dd_pct)

for j, (val, dt) in enumerate(zip(equity_vals, dates)):
    if val > current_peak:
        # 如果在回撤中，结束上一个回撤期
        if in_drawdown:
            trough_val = min(equity_vals[dd_start_idx:j])
            trough_idx = dd_start_idx + equity_vals[dd_start_idx:j].index(trough_val)
            trough_date = dates[trough_idx]
            dd_pct = (current_peak - trough_val) / current_peak * 100
            duration = (datetime.strptime(trough_date, '%Y-%m-%d') - datetime.strptime(current_peak_date, '%Y-%m-%d')).days
            drawdown_periods.append((current_peak_date, trough_date, round(current_peak, 2), round(trough_val, 2), round(dd_pct, 2), duration))
            in_drawdown = False
        
        current_peak = val
        current_peak_date = dt
        dd_pct = 0
    else:
        dd_pct = (current_peak - val) / current_peak * 100
        if dd_pct > 0 and not in_drawdown:
            in_drawdown = True
            dd_start_idx = j
            current_dd_start = dt
    
    daily_dd.append((dt, round(val, 2), round(dd_pct, 2)))

# 最后一个回撤期
if in_drawdown:
    trough_val = min(equity_vals[dd_start_idx:])
    trough_idx = dd_start_idx + equity_vals[dd_start_idx:].index(trough_val)
    trough_date = dates[trough_idx]
    dd_pct = (current_peak - trough_val) / current_peak * 100
    duration = (datetime.strptime(trough_date, '%Y-%m-%d') - datetime.strptime(current_peak_date, '%Y-%m-%d')).days
    drawdown_periods.append((current_peak_date, trough_date, round(current_peak, 2), round(trough_val, 2), round(dd_pct, 2), duration))

# ========== 输出分析 ==========
print(f"\n{'='*65}")
print(f"  V4.1→V6.5 级联策略 — 回撤分布分析")
print(f"{'='*65}")

# 1. 回撤区间分布
print(f"\n【1. 回撤区间分布】")
dd_vals = [d[2] for d in daily_dd if d[2] > 0]
if dd_vals:
    buckets = [(0, 3), (3, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 100)]
    print(f"  {'回撤区间':<15} {'天数':>6} {'占比':>8} {'累计天数':>10}")
    print(f"  {'-'*45}")
    cum_days = 0
    for lo, hi in buckets:
        count = sum(1 for d in dd_vals if lo <= d < hi)
        pct = count / len(daily_dd) * 100
        cum_days += count
        hi_str = f"{hi}%" if hi < 100 else "100%+"
        bar = '█' * max(1, int(pct / 2))
        print(f"  {lo}%~{hi_str:<11} {count:>6} {pct:>7.1f}% {cum_days:>10} {bar}")
    print(f"  {'有回撤天数':<15} {len(dd_vals):>6} {len(dd_vals)/len(daily_dd)*100:>7.1f}%")
    print(f"  {'平均回撤':<15} {np.mean(dd_vals):>6.2f}%")
    print(f"  {'中位数回撤':<14} {np.median(dd_vals):>6.2f}%")

# 2. 回撤期详情（按严重程度排序）
print(f"\n【2. 回撤期详情（按最大回撤排序）】")
drawdown_periods.sort(key=lambda x: x[4], reverse=True)
top_n = min(10, len(drawdown_periods))
print(f"  {'#':<4} {'峰值日期':<12} {'谷值日期':<12} {'峰值':>12} {'谷值':>12} {'回撤%':>8} {'持续天数':>8}")
print(f"  {'-'*75}")
for idx, (pk_d, tr_d, pk_v, tr_v, dd_p, dur) in enumerate(drawdown_periods[:top_n], 1):
    marker = ' ⚠️ 最大' if idx == 1 else ''
    print(f"  {idx:<4} {pk_d:<12} {tr_d:<12} {pk_v:>12,.0f} {tr_v:>12,.0f} {dd_p:>7.1f}% {dur:>8}天{marker}")

# 3. 回撤恢复分析
print(f"\n【3. 回撤恢复分析】")
recovery_times = []
for pk_d, tr_d, pk_v, tr_v, dd_p, dur in drawdown_periods:
    # 找谷值之后恢复到峰值的日期
    tr_idx = dates.index(tr_d) if tr_d in dates else -1
    if tr_idx < 0: continue
    recovered = False
    for k in range(tr_idx + 1, len(dates)):
        if equity_vals[k] >= pk_v:
            recovery_days = (datetime.strptime(dates[k], '%Y-%m-%d') - datetime.strptime(tr_d, '%Y-%m-%d')).days
            recovery_times.append((dd_p, recovery_days))
            recovered = True
            break
    if not recovered:
        recovery_times.append((dd_p, None))

if recovery_times:
    recovered_list = [(dd, days) for dd, days in recovery_times if days is not None]
    unrecovered = [(dd, days) for dd, days in recovery_times if days is None]
    
    if recovered_list:
        dd_bins = recovered_list
        print(f"  {'回撤幅度':>10} {'恢复天数':>10}")
        print(f"  {'-'*25}")
        for dd_p, days in sorted(recovered_list, key=lambda x: x[0], reverse=True)[:10]:
            print(f"  {dd_p:>9.1f}% {days:>10}天")
    
    if unrecovered:
        print(f"\n  未恢复的回撤: {len(unrecovered)} 个")
        for dd_p, _ in sorted(unrecovered, key=lambda x: x[0], reverse=True):
            print(f"    - 回撤 {dd_p:.1f}% 至期末未恢复")

# 4. 回撤与持仓关系
print(f"\n【4. 最大回撤期持仓分析】")
max_dd_idx = daily_dd.index(max(daily_dd, key=lambda x: x[2]))
max_dd_date = daily_dd[max_dd_idx][0]
max_dd_val = daily_dd[max_dd_idx][2]
max_dd_equity = daily_dd[max_dd_idx][1]

# 找最大回撤期附近的数据
nearby = [(d, eq, dd, n_positions) for (d, eq, dd), n_positions in 
          zip(daily_dd, [e['n_positions'] for e in equity_curve]) 
          if dd > 10]  # 回撤>10%的日子
if nearby:
    print(f"  回撤>10%的天数: {len(nearby)} 天")
    avg_pos = np.mean([n for _, _, _, n in nearby])
    print(f"  这些天平均持仓数: {avg_pos:.1f}")
    
    # 最大回撤前5天的交易情况
    start_idx = max(0, max_dd_idx - 5)
    print(f"\n  最大回撤前后交易情况（{daily_dd[start_idx][0]} ~ {daily_dd[min(max_dd_idx+5, len(daily_dd)-1)][0]}）:")
    for j in range(start_idx, min(max_dd_idx + 6, len(daily_dd))):
        d, eq, dd = daily_dd[j]
        n_pos = equity_curve[j]['n_positions']
        marker = ' ← 最大回撤' if j == max_dd_idx else ''
        print(f"    {d}: 净值 {eq:,.0f}, 回撤 {dd:.1f}%, 持仓 {n_pos}{marker}")

# 5. 月度回撤统计
print(f"\n【5. 月度回撤统计】")
monthly = {}
for d, eq, dd in daily_dd:
    month = d[:7]
    if month not in monthly:
        monthly[month] = []
    monthly[month].append(dd)

print(f"  {'月份':<10} {'平均回撤':>10} {'最大回撤':>10} {'回撤天数占比':>12}")
print(f"  {'-'*48}")
for month in sorted(monthly.keys()):
    dds = monthly[month]
    avg_dd = np.mean(dds)
    max_m_dd = max(dds)
    dd_ratio = sum(1 for d in dds if d > 0) / len(dds) * 100
    print(f"  {month:<10} {avg_dd:>9.2f}% {max_m_dd:>9.1f}% {dd_ratio:>11.0f}%")

# 保存每日回撤数据
out_path = os.path.join(OUT_DIR, 'drawdown_analysis.json')
with open(out_path, 'w') as f:
    json.dump({
        'daily_drawdown': [{'date': d, 'equity': eq, 'drawdown_pct': dd} for d, eq, dd in daily_dd],
        'drawdown_periods': [
            {'peak_date': pk, 'trough_date': tr, 'peak_value': pv, 'trough_value': tv, 'drawdown_pct': dp, 'duration': dur}
            for pk, tr, pv, tv, dp, dur in drawdown_periods
        ],
        'summary': {
            'max_drawdown_pct': max(d[2] for d in daily_dd),
            'avg_drawdown_pct': float(np.mean([d[2] for d in daily_dd if d[2] > 0])),
            'median_drawdown_pct': float(np.median([d[2] for d in daily_dd if d[2] > 0])),
            'days_in_drawdown': len([d for d in daily_dd if d[2] > 0]),
            'total_trading_days': len(daily_dd),
        }
    }, f, ensure_ascii=False, indent=2)

print(f"\n  详细数据: {out_path}")
