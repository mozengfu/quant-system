#!/usr/bin/env python3
# DEPRECATED: 强势活跃策略已下线，使用 scripts/run_backtest.py v4 替代
"""
强势活跃策略回测（更新版：RPS/主力资金来自DB + 52周评分翻转 + 概率下调）
回测区间: 2025-10-01 ~ 2026-04-24
止损: -5%, 止盈: +8%, 持仓上限: 5天, 最大持仓: 5只
"""
import sys, os, json, pymysql, logging, math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 强势活跃评分函数（与strong_active_strategy.py保持一致，纯函数）
def score_strong_active(row):
    score = 0
    change_pct = float(row.get("pct_chg", 0) or 0)
    if -3 <= change_pct <= 0:
        score += 30
    elif 0 < change_pct <= 5:
        score += 25
    elif change_pct > 5 or change_pct < -3:
        return 0

    volume_ratio = float(row.get("volume_ratio", 0) or 0)
    if 1.5 <= volume_ratio <= 3.0:
        score += 25
    elif 3.0 < volume_ratio <= 5.0:
        score += 15

    turnover = float(row.get("turnover_rate", 0) or 0)
    if 3 <= turnover <= 8:
        score += 20
    elif 8 < turnover <= 10:
        score += 15

    ma5, ma10, ma20 = float(row.get("ma5", 0) or 0), float(row.get("ma10", 0) or 0), float(row.get("ma20", 0) or 0)
    price = float(row.get("close", 0) or 0)
    if ma5 > ma10 > ma20 and price > ma5:
        score += 20
    elif price > ma5:
        score += 10

    rps = float(row.get("rps", 0) or 0)
    if rps >= 85:
        score += 25
    elif 70 <= rps < 85:
        score += 20
    elif 50 <= rps < 70:
        score += 10

    main_money = float(row.get("main_money", 0) or 0)
    if main_money >= 10000:
        score += 20
    elif 5000 <= main_money < 10000:
        score += 15
    elif 1000 <= main_money < 5000:
        score += 10
    elif main_money > 0:
        score += 5

    change_3d = float(row.get("change_3d", 0) or 0)
    if 0 <= change_3d <= 10:
        score += 10

    high_52w, low_52w = float(row.get("high_52w", 0) or 0), float(row.get("low_52w", 0) or 0)
    if high_52w > low_52w:
        pos_52w = (price - low_52w) / (high_52w - low_52w) * 100
        if 60 <= pos_52w < 85:
            score += 15
        elif pos_52w < 60:
            score += 5
        else:
            return 0

    return score

def get_db():
    return pymysql.connect(**get_db_config())

def get_trade_dates(start, end):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start, end))
    dates = [str(r[0]) for r in cur.fetchall()]
    conn.close(); return dates

def get_candidates(trade_date):
    """获取当日强势活跃候选股并评分"""
    conn = get_db(); cur = conn.cursor()
    # 粗筛：涨幅-3~5%, 换手率>3%, 价格>3
    cur.execute("""
        SELECT d.ts_code, d.close, d.pct_chg, d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20, d.rps_20,
               d.high_52w, d.low_52w, d.pre_close
        FROM daily_price d
        WHERE d.trade_date = %s
          AND d.close > 3
          AND d.pct_chg >= -3 AND d.pct_chg <= 5
          AND d.turnover_rate > 3
          AND d.ts_code NOT LIKE '688%%'
          AND d.ts_code NOT LIKE '92%%'
          AND d.ts_code NOT LIKE '8%%'
    """, (trade_date,))
    rows = cur.fetchall()
    if not rows: conn.close(); return []

    codes = [r[0] for r in rows]
    p = ','.join(['%s'] * len(codes))

    # 批量查询主力资金
    main_money = {}
    cur.execute(f"SELECT ts_code, main_net FROM moneyflow_daily WHERE trade_date=%s AND ts_code IN ({p})", (trade_date, *codes))
    for r in cur.fetchall():
        main_money[r[0]] = float(r[1]) if r[1] else 0

    # 批量查询3日涨幅（用前第3个交易日的close）
    cur.execute(f"SELECT ts_code, close FROM daily_price WHERE trade_date=%s AND ts_code IN ({p})", (trade_date, *codes))
    cur_close = {r[0]: float(r[1]) for r in cur.fetchall()}

    # 前一个交易日
    cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (trade_date,))
    prev_date = cur.fetchone()[0]
    cur.execute(f"SELECT ts_code, close FROM daily_price WHERE trade_date=%s AND ts_code IN ({p})", (prev_date, *codes))
    prev_close = {r[0]: float(r[1]) for r in cur.fetchall()}

    # 3日前
    cur.execute("SELECT trade_date FROM daily_price WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 3", (trade_date,))
    d3rows = cur.fetchall()
    if len(d3rows) >= 3:
        d3_date = d3rows[-1][0]  # 第3个
        cur.execute(f"SELECT ts_code, close FROM daily_price WHERE trade_date=%s AND ts_code IN ({p})", (d3_date, *codes))
        d3_close = {r[0]: float(r[1]) for r in cur.fetchall()}
    else:
        d3_close = {}

    conn.close()

    candidates = []
    for r in rows:
        ts_code, close, pct_chg = r[0], float(r[1]) if r[1] else 0, float(r[2]) if r[2] else 0
        turnover, vol_ratio = float(r[3]) if r[3] else 0, float(r[4]) if r[4] else 0
        ma5, ma10, ma20 = float(r[5]) if r[5] else 0, float(r[6]) if r[6] else 0, float(r[7]) if r[7] else 0
        rps = float(r[8]) if r[8] else 0
        h52, l52 = float(r[9]) if r[9] else 0, float(r[10]) if r[10] else 0
        pre_close = float(r[11]) if r[11] else 0

        if close <= 0: continue

        # 3日涨幅
        c3 = d3_close.get(ts_code, 0)
        chg_3d = (close / c3 - 1) * 100 if c3 > 0 else 0

        row = {
            'pct_chg': pct_chg, 'volume_ratio': vol_ratio, 'turnover_rate': turnover,
            'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'close': close,
            'rps': rps, 'main_money': main_money.get(ts_code, 0),
            'change_3d': chg_3d, 'high_52w': h52, 'low_52w': l52,
        }

        score = score_strong_active(row)
        if score >= 60:
            candidates.append({'ts_code': ts_code, 'score': score, 'close': close})

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates

def get_future_prices(ts_code, after_date, days=5):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT trade_date, close FROM daily_price WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date ASC LIMIT %s", (ts_code, after_date, days))
    rows = [(str(r[0]), float(r[1])) for r in cur.fetchall()]
    conn.close(); return rows

def run():
    start, end = '2025-10-01', '2026-04-24'
    logger.info(f"强势活跃策略回测: {start} ~ {end}")

    dates = get_trade_dates(start, end)
    logger.info(f"交易日: {len(dates)}天")

    balance = 100000.0; positions = []; trades = []

    for idx, trade_date in enumerate(dates):
        # 卖出检查
        new_positions = []
        for pos in positions:
            pos['hold_days'] += 1
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (pos['ts_code'], trade_date))
            row = cur.fetchone()
            conn.close()
            if not row: continue
            price = float(row[0])
            ret = (price - pos['buy_price']) / pos['buy_price'] * 100

            if ret <= pos['stop_loss']:
                bal_adj = pos['cost'] * (1 + ret / 100)
                balance += bal_adj
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'stop_loss'})
                continue
            if ret >= pos['take_profit']:
                bal_adj = pos['cost'] * (1 + ret / 100)
                balance += bal_adj
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'take_profit'})
                continue
            if pos['hold_days'] >= pos['max_days']:
                bal_adj = pos['cost'] * (1 + ret / 100)
                balance += bal_adj
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'expiry'})
                continue
            new_positions.append(pos)
        positions = new_positions

        # 买入
        can_buy = 5 - len(positions)
        if can_buy > 0:
            candidates = get_candidates(trade_date)
            existing = [p['ts_code'] for p in positions]
            buys = [c for c in candidates if c['ts_code'] not in existing][:can_buy]
            for b in buys:
                cost = balance * 0.2
                if cost < 10000: continue
                positions.append({
                    'ts_code': b['ts_code'], 'buy_date': trade_date, 'buy_price': b['close'],
                    'cost': cost, 'hold_days': 0, 'max_days': 5,
                    'stop_loss': -5, 'take_profit': 8
                })
                balance -= cost

        if (idx + 1) % 20 == 0:
            net = balance + sum(p['cost'] for p in positions)
            logger.info(f"进度 {idx+1}/{len(dates)} 持仓{len(positions)}只 市值{net:.0f}")

    # 到期平仓
    for pos in positions:
        prices = get_future_prices(pos['ts_code'], pos['buy_date'], 5)
        if prices:
            final_price = prices[-1][1]
            ret = (final_price - pos['buy_price']) / pos['buy_price'] * 100
            bal_adj = pos['cost'] * (1 + ret / 100)
            balance += bal_adj
            trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': prices[-1][0], 'ret': ret, 'reason': 'force_close'})

    # 统计
    total_ret = (balance / 100000 - 1) * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['ret'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['ret'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['ret'] for t in wins) / sum(t['ret'] for t in losses)) if losses and sum(t['ret'] for t in losses) != 0 else 0

    # 最大回撤（简化为持仓市值曲线）
    peak = 100000; mdd = 0; eq = 100000
    for t in trades:
        eq += t['ret'] / 100 * 100000 * 0.2
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd: mdd = dd

    # 夏普
    avg_r = sum(t['ret'] for t in trades) / len(trades) if trades else 0
    var_r = sum((t['ret'] - avg_r)**2 for t in trades) / len(trades) if trades else 1
    sharpe = (avg_r / (var_r**0.5 + 0.001)) * (252**0.5) / 100 if var_r > 0 else 0

    print(f"\n{'='*60}")
    print(f"强势活跃策略回测结果")
    print(f"{'='*60}")
    print(f"总收益率: {total_ret:.2f}%")
    print(f"胜率: {win_rate:.1f}% ({len(wins)}/{len(trades)})")
    print(f"盈亏比: {profit_factor:.2f}")
    print(f"夏普比率: {sharpe:.2f}")
    print(f"最大回撤: {mdd:.2f}%")
    print(f"交易次数: {len(trades)}")
    print(f"平均盈利: {avg_win:.2f}%")
    print(f"平均亏损: {avg_loss:.2f}%")
    print(f"{'='*60}\n")

    result = {
        'strategy': '强势活跃', 'total_return': round(total_ret, 2), 'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2), 'sharpe': round(sharpe, 2), 'mdd': round(mdd, 2),
        'trades': len(trades), 'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2)
    }
    with open(os.path.join(DATA_DIR, 'backtest_strong_active.json'), 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result

if __name__ == '__main__':
    run()
