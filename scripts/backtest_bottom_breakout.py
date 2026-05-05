#!/usr/bin/env python3
# DEPRECATED: 底部起步策略已下线，使用 scripts/run_backtest.py v4 替代
"""
底部起步策略回测（更新版：MACD软评分 + 缩量确认 + 基本面加分）
回测区间: 2025-10-01 ~ 2026-04-24
止损: -5%, 止盈: +8%, 持仓上限: 5天, 最大持仓: 5只
"""
import sys, os, json, pymysql, logging, math
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
from quant_app.services.strategy_service import calculate_ema, detect_macd_crossover

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

def get_db():
    return pymysql.connect(**get_db_config())

def get_trade_dates(start, end):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (start, end))
    dates = [str(r[0]) for r in cur.fetchall()]
    conn.close()
    return dates

def get_candidates(trade_date):
    """获取当日+历史数据做完整评分"""
    conn = get_db()
    cur = conn.cursor()
    # 粗筛SQL（同strategy_service.py，不含stock_info join以提升速度）
    cur.execute("""
        SELECT d.ts_code, d.close, d.pct_chg, d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20, d.rps_20
        FROM daily_price d
        WHERE d.trade_date = %s
          AND d.close > 3
          AND d.pct_chg BETWEEN -5 AND 10
          AND d.turnover_rate > 1.5
          AND d.volume_ratio > 0.7
          AND d.ts_code NOT LIKE '688%%'
          AND d.ts_code NOT LIKE '68%%'
          AND d.ts_code NOT LIKE '83%%'
          AND d.ts_code NOT LIKE '87%%'
          AND d.ts_code NOT LIKE '43%%'
          AND d.ts_code NOT LIKE '8%%'
          AND d.ts_code NOT LIKE '4%%'
          AND d.ts_code NOT LIKE '9%%'
    """, (trade_date,))
    rows = cur.fetchall()

    # 批量查询52周位置和主力资金
    codes = [r[0] for r in rows]
    high52 = {}; low52 = {}; main_net = {}
    if codes:
        p = ','.join(['%s'] * len(codes))
        cur.execute(f"SELECT ts_code, high_52w, low_52w FROM daily_price WHERE trade_date=%s AND ts_code IN ({p})", (trade_date, *codes))
        for r in cur.fetchall():
            high52[r[0]] = float(r[1]) if r[1] else 0
            low52[r[0]] = float(r[2]) if r[2] else 0
        cur.execute(f"SELECT ts_code, main_net FROM moneyflow_daily WHERE trade_date=%s AND ts_code IN ({p})", (trade_date, *codes))
        for r in cur.fetchall():
            main_net[r[0]] = float(r[1]) if r[1] else 0

    # 批量查询基本面
    fina = {}
    try:
        cur.execute(f"SELECT f.ts_code, f.roe FROM fina_indicator f INNER JOIN (SELECT ts_code, MAX(end_date) md FROM fina_indicator WHERE ts_code IN ({p}) GROUP BY ts_code) l ON f.ts_code=l.ts_code AND f.end_date=l.md")
        for r in cur.fetchall():
            fina[r[0]] = float(r[1]) if r[1] else None
    except Exception:
        pass

    conn.close()

    candidates = []
    for r in rows:
        ts_code, close, pct_chg = r[0], float(r[1]) if r[1] else 0, float(r[2]) if r[2] else 0
        turnover, vol_ratio = float(r[3]) if r[3] else 0, float(r[4]) if r[4] else 0
        ma5, ma10, ma20 = float(r[5]) if r[5] else 0, float(r[6]) if r[6] else 0, float(r[7]) if r[7] else 0
        rps = float(r[8]) if r[8] else 0
        if close <= 0: continue

        # 条件1-5：均线多头、价>MA5、涨幅、量比、换手率
        if not (ma5 > ma10 > ma20 and ma20 > 0): continue
        if close < ma5: continue
        if vol_ratio > 5: continue
        if turnover > 20: continue
        if rps < 20: continue

        # 条件7：52周位置<85%
        h52 = high52.get(ts_code, 0)
        l52 = low52.get(ts_code, 0)
        if h52 > l52 and close > l52 + (h52 - l52) * 0.85: continue

        # 条件8：MACD检测（软评分）
        hist = get_hist_data(ts_code, trade_date)
        if hist is None or len(hist['closes']) < 30: continue
        closes = hist['closes']; volumes = hist['volumes']

        has_cross, days_ago = detect_macd_crossover(closes, lookback=10)
        is_approach = False
        if not has_cross:
            fe = calculate_ema(closes, 12); se = calculate_ema(closes, 26)
            dv = [(f - s) for f, s in zip(fe, se) if f is not None and s is not None]
            if len(dv) >= 10:
                k = 2.0 / (9 + 1); dea = sum(dv[:9]) / 9.0
                for v in dv[9:]: dea = v * k + dea * (1 - k)
                if dv[-1] < dea and len(dv) >= 2:
                    is_approach = (dea - dv[-2]) > (dea - dv[-1]) > 0

        # 评分
        score = 0; reasons = []
        # MACD
        if has_cross:
            macd_pts = 25 if days_ago <= 2 else (20 if days_ago <= 5 else 15)
            score += macd_pts
        elif is_approach:
            score += 10

        # 缩量确认
        vol_shrink = False
        if len(volumes) >= 25:
            avg20 = sum(volumes[-25:-5]) / 20.0
            vol_shrink = any(v < avg20 * 0.8 for v in volumes[-5:])
        if vol_shrink: score += 10

        # ROE加分
        roe = fina.get(ts_code)
        if roe is not None and roe > 5: score += 8

        # 均线多头
        score += 25
        # RPS
        if rps >= 80: score += 20
        elif rps >= 60: score += 15
        elif rps >= 40: score += 12
        elif rps >= 20: score += 8
        # 涨幅
        if -5 <= pct_chg < 0: score += 10
        elif 0 <= pct_chg <= 3: score += 10
        elif 3 < pct_chg <= 5: score += 13
        else: score += 5
        # 量比
        score += 10 if 1.0 <= vol_ratio <= 3 else 5
        # 换手率
        if 3 <= turnover <= 10: score += 10
        elif 1.5 <= turnover < 3: score += 5
        else: score += 3
        # 主力
        mn = main_net.get(ts_code, 0)
        if mn > 5000: score += 12
        elif mn > 1000: score += 8
        elif mn > 0: score += 4

        candidates.append({'ts_code': ts_code, 'score': score, 'close': close})

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates

def get_hist_data(ts_code, trade_date):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT close, vol FROM daily_price WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date DESC LIMIT 80", (ts_code, trade_date))
    rows = cur.fetchall()
    conn.close()
    if len(rows) < 30: return None
    closes = [float(r[0]) for r in reversed(rows)]
    volumes = [float(r[1]) for r in reversed(rows)]
    return {'closes': closes, 'volumes': volumes}

def get_future_prices(ts_code, after_date, days=5):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT trade_date, close FROM daily_price WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date ASC LIMIT %s", (ts_code, after_date, days))
    rows = [(str(r[0]), float(r[1])) for r in cur.fetchall()]
    conn.close()
    return rows

def run():
    start, end = '2025-10-01', '2026-04-24'
    logger.info(f"底部起步策略回测: {start} ~ {end}")

    dates = get_trade_dates(start, end)
    logger.info(f"交易日: {len(dates)}天")

    balance = 100000.0; positions = []; trades = []

    for idx, trade_date in enumerate(dates):
        # 卖出检查
        new_positions = []
        for pos in positions:
            pos['hold_days'] += 1
            # 获取当天价格
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (pos['ts_code'], trade_date))
            row = cur.fetchone()
            conn.close()
            if not row: continue
            price = float(row[0])
            ret = (price - pos['buy_price']) / pos['buy_price'] * 100

            if ret <= pos['stop_loss']:  # 止损
                loss = pos['cost'] * (1 + ret / 100) - pos['cost']
                balance += pos['cost'] + loss
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'stop_loss'})
                continue
            if ret >= pos['take_profit']:  # 止盈
                gain = pos['cost'] * (1 + ret / 100) - pos['cost']
                balance += pos['cost'] + gain
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'take_profit'})
                continue
            if pos['hold_days'] >= pos['max_days']:  # 到期
                pnl = pos['cost'] * (1 + ret / 100) - pos['cost']
                balance += pos['cost'] + pnl
                trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': trade_date, 'ret': ret, 'reason': 'expiry'})
                continue
            new_positions.append(pos)
        positions = new_positions

        # 买入
        can_buy = 5 - len(positions)
        if can_buy > 0:
            candidates = get_candidates(trade_date)
            buys = [c for c in candidates if c['ts_code'] not in [p['ts_code'] for p in positions]][:can_buy]
            for b in buys:
                cost = balance * 0.2  # 等仓
                if cost < 10000: continue
                positions.append({
                    'ts_code': b['ts_code'], 'buy_date': trade_date, 'buy_price': b['close'],
                    'cost': cost, 'hold_days': 0, 'max_days': 5,
                    'stop_loss': -5, 'take_profit': 8
                })
                balance -= cost

        if (idx + 1) % 20 == 0:
            logger.info(f"进度 {idx+1}/{len(dates)} 持仓{len(positions)}只 市值{balance+sum(p['cost'] for p in positions):.0f}")

    # 到期平仓
    for pos in positions:
        prices = get_future_prices(pos['ts_code'], pos['buy_date'], 5)
        if prices:
            final_price = prices[-1][1]
            ret = (final_price - pos['buy_price']) / pos['buy_price'] * 100
            pnl = pos['cost'] * (1 + ret / 100) - pos['cost']
            balance += pos['cost'] + pnl
            trades.append({'ts_code': pos['ts_code'], 'buy_date': pos['buy_date'], 'sell_date': prices[-1][0], 'ret': ret, 'reason': 'force_close'})

    # 统计
    total_value = balance
    total_ret = (total_value / 100000 - 1) * 100
    wins = [t for t in trades if t['ret'] > 0]
    losses = [t for t in trades if t['ret'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['ret'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['ret'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['ret'] for t in wins) / sum(t['ret'] for t in losses)) if losses and sum(t['ret'] for t in losses) != 0 else 0

    # 最大回撤
    peak = 100000; mdd = 0
    cum_ret = 100000
    for t in trades:
        cum_ret += (t['ret'] / 100) * (cum_ret * 0.2)  # simplified drawdown calc
        # Actually let me compute properly
    # Simplified: track balance after each trade
    eq = 100000
    for t in trades:
        eq += t['ret'] / 100 * 100000 * 0.2  # approx
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > mdd: mdd = dd

    # 夏普（简化）
    avg_ret = sum(t['ret'] for t in trades) / len(trades) if trades else 0
    var = sum((t['ret'] - avg_ret)**2 for t in trades) / len(trades) if trades else 1
    sharpe = (avg_ret / (var**0.5 + 0.001)) * (252**0.5) / 100 if var > 0 else 0

    print(f"\n{'='*60}")
    print(f"底部起步策略回测结果")
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
        'strategy': '底部起步', 'total_return': round(total_ret, 2), 'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2), 'sharpe': round(sharpe, 2), 'mdd': round(mdd, 2),
        'trades': len(trades), 'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2)
    }
    with open(os.path.join(DATA_DIR, 'backtest_bottom_breakout.json'), 'w') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result

if __name__ == '__main__':
    run()
