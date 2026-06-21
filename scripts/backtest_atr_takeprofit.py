"""
ATR 移动止盈回测 v2 — 基于已平仓持仓，模拟不同参数组合的止盈效果

止盈规则:
  trigger_price = max(peak_price - ATR_MULT × ATR(20), cost_price × (1 + MIN_PROFIT_PCT))

参数网格:
  ATR multipliers: 1.0 ~ 3.5
  Min profit floor: 3% ~ 12%

评估指标: 触发时的盈利, 与实际卖出对比
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import numpy as np
from datetime import timedelta
from collections import defaultdict
from quant_app.utils.config import get_db_config

ATR_MULTS = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
MIN_PROFITS = [0.03, 0.05, 0.06, 0.08, 0.10, 0.12]


def get_atr(cur, ts_code, date, period=20):
    """获取指定日期之前 N 天的 ATR"""
    cur.execute("""
        SELECT high, low, close
        FROM daily_price
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC
        LIMIT %s
    """, (ts_code, date, period + 1))
    rows = cur.fetchall()
    if len(rows) < period + 1:
        return None
    trs = []
    for i in range(period):
        h, l, pc = float(rows[i][0]), float(rows[i][1]), float(rows[i + 1][2])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(trs))


def simulate(positions, atr_mult, min_profit_pct, db_cur):
    """对每一笔完整持仓模拟 ATR 移动止盈"""
    results = []
    for pos in positions:
        ts_code = pos['ts_code']
        buy_date = pos['buy_date']
        sell_date = pos['sell_date']
        cost = pos['cost_price']
        actual_pnl = pos['final_pnl_pct'] * 100
        path = pos['price_path']

        peak = cost
        triggered = False
        trigger_profit = 0

        for d, close in path:
            if d <= buy_date:
                continue
            peak = max(peak, close)
            atr = get_atr(db_cur, ts_code, d)
            if atr is None or atr <= 0:
                continue
            tp = max(peak - atr_mult * atr, cost * (1 + min_profit_pct))
            if close <= tp and close > cost:
                triggered = True
                trigger_profit = (close - cost) / cost * 100
                break

        results.append({
            'ts_code': ts_code,
            'buy_date': str(buy_date),
            'sell_date': str(sell_date),
            'cost_price': round(cost, 2),
            'triggered': triggered,
            'trigger_profit_pct': round(trigger_profit, 2),
            'actual_profit_pct': round(actual_pnl, 2),
            'delta': round(trigger_profit - actual_pnl, 2) if triggered else 0,
        })
    return results


# ─── 主流程 ───
conn = pymysql.connect(**get_db_config())
cur = conn.cursor()

# 1. 取所有已平仓且有盈亏的持仓
cur.execute("""
    SELECT ts_code, buy_date, sell_date, cost_price, final_pnl_pct
    FROM sim_positions
    WHERE status = 'SOLD'
      AND final_pnl_pct IS NOT NULL
      AND buy_date IS NOT NULL
      AND sell_date IS NOT NULL
      AND buy_date <= sell_date
    ORDER BY buy_date
""")
rows = cur.fetchall()
print(f"已平仓记录: {len(rows)} 条")

# 2. 加载价格序列，跳过数据不足的
positions = []
for ts_code, buy_date, sell_date, cost_price, fp in rows:
    cost = float(cost_price)
    cur.execute("""
        SELECT trade_date, `close`
        FROM daily_price
        WHERE ts_code = %s AND trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (ts_code, buy_date - timedelta(days=35), sell_date))
    pr = cur.fetchall()
    if len(pr) < 22:  # 至少需要 1 天买入 + 21 天 ATR buffer
        continue
    path = [(p[0], float(p[1])) for p in pr]
    positions.append(dict(ts_code=ts_code, buy_date=buy_date, sell_date=sell_date,
                          cost_price=cost, final_pnl_pct=float(fp), price_path=path))

print(f"有完整价格数据的: {len(positions)} 条")
if len(positions) < 5:
    print("样本不足")
    conn.close()
    sys.exit(1)

# 3. 跑网格
print(f"\n{'ATR乘数':>8} {'最低止盈':>8} {'触发率':>8} {'触发均':>8} {'实际均':>8} {'总收益Δ':>9} {'触发胜率':>8}")
print("-" * 70)

all_stats = []
for mult in ATR_MULTS:
    for mp in MIN_PROFITS:
        results = simulate(positions, mult, mp, cur)
        trig = [r for r in results if r['triggered']]
        notrig = [r for r in results if not r['triggered']]
        if not trig:
            continue

        avg_t = float(np.mean([r['trigger_profit_pct'] for r in trig]))
        avg_a = float(np.mean([r['actual_profit_pct'] for r in results]))
        total_new = sum(r['trigger_profit_pct'] if r['triggered'] else r['actual_profit_pct'] for r in results)
        total_old = sum(r['actual_profit_pct'] for r in results)
        delta = total_new - total_old
        tr_win = sum(1 for r in trig if r['trigger_profit_pct'] > 0) / len(trig) * 100
        better = sum(1 for r in trig if r['trigger_profit_pct'] > r['actual_profit_pct'])

        score = avg_t * 0.4 + delta * 0.3 + tr_win * 0.3

        all_stats.append(dict(mult=mult, minp=round(mp*100,1), n_total=len(results),
                              n_trig=len(trig), trig_rate=round(len(trig)/len(results)*100,1),
                              avg_t=round(avg_t,2), avg_a=round(avg_a,2),
                              total_new=round(total_new,2), total_old=round(total_old,2),
                              delta=round(delta,2), tr_win=round(tr_win,1), better=better,
                              score=round(score,1)))

        print(f"{mult:>8.1f} {round(mp*100,1):>7.1f}% {all_stats[-1]['trig_rate']:>7.1f}% "
              f"{avg_t:>7.2f}% {avg_a:>7.2f}% {delta:>+8.2f}% {tr_win:>7.1f}%")

# 4. Top 10
sorted_s = sorted(all_stats, key=lambda x: x['score'], reverse=True)
print(f"\n===== Top 10 =====")
print(f"{'排名':>4} {'ATR':>6} {'止盈':>7} {'总分':>7} {'触发率':>7} {'触发均':>8} {'总收益Δ':>8} {'触发胜率':>8}")
print("-" * 65)
for i, s in enumerate(sorted_s[:10]):
    print(f"{i+1:>4} {s['mult']:>5.1f} {s['minp']:>5.1f}% {s['score']:>6.1f} "
          f"{s['trig_rate']:>6.1f}% {s['avg_t']:>7.2f}% {s['delta']:>+7.2f}% {s['tr_win']:>7.1f}%")

# 5. 最优参数详情
best = sorted_s[0]
print(f"\n===== 最优参数: ATR {best['mult']}×, 最低止盈 {best['minp']}% =====")
best_res = simulate(positions, best['mult'], best['minp'] / 100, cur)
for r in sorted(best_res, key=lambda x: x['trigger_profit_pct'] - x['actual_profit_pct'], reverse=True)[:10]:
    flag = "✓改善" if r['delta'] > 0 else "✗劣化" if r['delta'] < 0 else "=持平"
    print(f"  {r['ts_code']:12s} 买入{r['buy_date']} 成本{r['cost_price']:>7.2f} "
          f"触发{r['trigger_profit_pct']:>6.2f}% vs 实际{r['actual_profit_pct']:>6.2f}% ({flag})")

conn.close()
