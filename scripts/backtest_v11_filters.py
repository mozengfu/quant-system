"""
V11.1 ML推荐 + L1/L2 过滤器 回测

回测逻辑：
  1. 读取 ai_sim_recommendations 历史推荐记录（涵盖25个交易日）
  2. 对每条推荐，计算推荐后5日实际收益
  3. 应用 L1 过滤（动量衰竭/连涨/RSI超买）+ L2 过滤（大盘/板块）
  4. 对比过滤前后的平均收益、胜率、最大回撤
"""

import logging

import pymysql

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")

DB_CONFIG = {
    "host": "127.0.0.1", "port": 3306, "user": "root",
    "password": os.environ.get("MYSQL_PASSWORD", ""), "database": "quant_db", "charset": "utf8mb4",
}

def get_db():
    return pymysql.connect(**DB_CONFIG)

def load_recommendations():
    """从 ai_sim_recommendations 读取所有历史推荐"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, recommend_date, rec_rank, ts_code, name, price, ml_score, market_regime
        FROM ai_sim_recommendations
        WHERE recommend_date >= '2026-05-01'
        ORDER BY recommend_date, rec_rank
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()

    recs = []
    for r in rows:
        recs.append({
            'id': r[0], 'date': str(r[1]), 'rank': r[2],
            'ts_code': r[3], 'name': r[4] or r[3],
            'price': float(r[5]) if r[5] else 0,
            'ml_score': float(r[6]) if r[6] else 0,
            'market_regime': r[7] or 'unknown',
        })
    return recs

def get_price_data(conn, ts_code, start_date, days=10):
    """获取推荐日前后的价格数据"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close, pct_chg, high, low, amount, turnover_rate
        FROM daily_price WHERE ts_code=%s AND trade_date >= %s
        ORDER BY trade_date LIMIT %s
    """, (ts_code, start_date, days + 5))
    rows = cur.fetchall()
    cur.close()
    return rows

def get_prev_10d(conn, ts_code, before_date):
    """获取推荐前10日数据，用于计算过滤条件"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close, pct_chg, high
        FROM daily_price WHERE ts_code=%s AND trade_date < %s
        ORDER BY trade_date DESC LIMIT 15
    """, (ts_code, before_date))
    rows = cur.fetchall()
    cur.close()
    return list(reversed(rows))

def get_market_index(conn, trade_date):
    """获取大盘当日数据（从 daily_price 计算 MA20）"""
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_price 
        WHERE ts_code='000001.SH' AND trade_date=%s
    """, (trade_date,))
    r = cur.fetchone()
    if not r:
        cur.close()
        return None
    mkt_close = float(r[0])
    # 计算 MA20
    cur.execute("""
        SELECT AVG(close) FROM (
            SELECT close FROM daily_price 
            WHERE ts_code='000001.SH' AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 20
        ) t
    """, (trade_date,))
    ma20 = cur.fetchone()[0]
    cur.close()
    return (mkt_close, ma20)

def calc_rsi(prices, period=14):
    """简易 RSI 计算"""
    if len(prices) < period + 1:
        return 50
    gains = losses = 0
    for i in range(-period, 0):
        chg = prices[i] - prices[i-1]
        if chg > 0: gains += chg
        else: losses += abs(chg)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def apply_l1_filter(prev_10d, entry_price):
    """L1 过滤器：动量衰竭/连涨/RSI"""
    reasons = []

    if len(prev_10d) < 5:
        return True, reasons  # 数据不足，不拦截

    closes = [float(r[1]) for r in prev_10d]
    pct_chgs = [float(r[2]) for r in prev_10d]

    # 1. 5日涨幅 >15% 且 当日已跌 >-3% → 动量衰竭
    chg_5d = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] > 0 else 0
    if chg_5d > 15 and pct_chgs[-1] < -3:
        reasons.append(f"动量衰竭: 5日涨{chg_5d:.0f}%且当日跌{pct_chgs[-1]:.1f}%")
        return False, reasons

    # 2. 连涨≥5日后首次跌 → 等回调
    consecutive_up = 0
    for p in reversed(pct_chgs):
        if p > 0: consecutive_up += 1
        else: break
    if consecutive_up >= 5 and pct_chgs[-1] < 0:
        reasons.append(f"连涨{consecutive_up}日后首次回调")
        return False, reasons

    # 3. RSI(14) > 75 → 超买
    if len(closes) >= 15:
        rsi_14 = calc_rsi(closes, 14)
        if rsi_14 > 75:
            reasons.append(f"RSI超买: {rsi_14:.0f}")
            return False, reasons

    return True, reasons

def apply_l2_filter(conn, ts_code, trade_date):
    """L2 过滤器：大盘/板块"""
    reasons = []

    # 4. 大盘在20日均线下方 → 停止建仓
    mkt = get_market_index(conn, trade_date)
    if mkt:
        mkt_close, mkt_ma20 = float(mkt[0]), float(mkt[1]) if mkt[1] else 0
        if mkt_ma20 > 0 and mkt_close < mkt_ma20:
            reasons.append(f"大盘破20日线: {mkt_close:.0f}<{mkt_ma20:.0f}")
            return False, reasons

    # 5. 板块强度（简化：查同板块其他股票平均表现）
    cur = conn.cursor()
    cur.execute("SELECT industry FROM stock_info WHERE ts_code=%s", (ts_code,))
    ri = cur.fetchone()
    industry = ri[0] if ri else ''
    if industry:
        cur.execute("""
            SELECT AVG(d.pct_chg) FROM daily_price d
            JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE s.industry=%s COLLATE utf8mb4_unicode_ci AND d.trade_date=%s
        """, (industry, trade_date))
        avg_sector = cur.fetchone()[0]
        if avg_sector is not None and float(avg_sector) < -2:
            reasons.append(f"板块弱势: {industry}均{float(avg_sector):.1f}%")
            return False, reasons
    cur.close()

    return True, reasons


def calc_future_return(conn, ts_code, entry_date, entry_price, hold_days=5):
    """计算推荐后 hold_days 日的实际收益"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close, pct_chg, high, low
        FROM daily_price WHERE ts_code=%s AND trade_date >= %s
        ORDER BY trade_date LIMIT %s
    """, (ts_code, entry_date, hold_days + 1))
    rows = cur.fetchall()
    cur.close()

    if len(rows) < 2:
        return None, None, None

    # 以 hold_days 日后收盘价计算收益
    exit_idx = min(hold_days, len(rows) - 1)
    exit_price = float(rows[exit_idx][1])

    # 期间最大回撤
    max_drawdown = 0
    peak = entry_price
    for r in rows[1:exit_idx+1]:
        h = float(r[3])
        l = float(r[4])
        if h > peak: peak = h
        dd = (peak - l) / peak * 100
        max_drawdown = max(max_drawdown, dd)

    ret = (exit_price - entry_price) / entry_price * 100 if entry_price > 0 else 0
    return ret, max_drawdown, exit_price


def main():
    recs = load_recommendations()
    logger.info(f"加载 {len(recs)} 条历史推荐记录")

    results = {
        'unfiltered': {'returns': [], 'wins': 0, 'total': 0, 'max_dd': 0},
        'filtered': {'returns': [], 'wins': 0, 'total': 0, 'max_dd': 0},
    }

    conn = get_db()
    filtered_count = 0
    filter_reasons = {}

    for rec in recs:
        entry_date = rec['date']
        ts_code = rec['ts_code']
        entry_price = rec['price']

        if entry_price <= 0:
            continue

        # 获取推荐前数据（用于L1过滤）
        prev_10d = get_prev_10d(conn, ts_code, entry_date)

        # L1 过滤
        l1_pass, l1_reasons = apply_l1_filter(prev_10d, entry_price)

        # L2 过滤
        l2_pass, l2_reasons = apply_l2_filter(conn, ts_code, entry_date)

        # 计算未来收益
        ret, mdd, exit_price = calc_future_return(conn, ts_code, entry_date, entry_price, hold_days=5)

        if ret is None:
            continue

        l1_label = 'PASS' if l1_pass else 'L1:' + ','.join(l1_reasons)
        l2_label = 'PASS' if l2_pass else 'L2:' + ','.join(l2_reasons)
        filtered = l1_pass and l2_pass

        if not filtered:
            filtered_count += 1
            for r in l1_reasons + l2_reasons:
                key = r[:20]
                filter_reasons[key] = filter_reasons.get(key, 0) + 1

        label = '✅' if filtered else '❌'
        logger.info(f"{label} {rec['date']} #{rec['rank']} {rec['name']:8s} {rec['ts_code']:10s} "
                    f"入={entry_price:6.2f} 出={exit_price or 0:6.2f} 收益={ret:+.2f}% "
                    f"MDD={mdd:.1f}% | {l1_label} | {l2_label}")

        # unfiltered（全部推荐）
        results['unfiltered']['returns'].append(ret)
        results['unfiltered']['total'] += 1
        if ret > 0: results['unfiltered']['wins'] += 1
        results['unfiltered']['max_dd'] = max(results['unfiltered']['max_dd'], mdd)

        # filtered
        if filtered:
            results['filtered']['returns'].append(ret)
            results['filtered']['total'] += 1
            if ret > 0: results['filtered']['wins'] += 1
            results['filtered']['max_dd'] = max(results['filtered']['max_dd'], mdd)

    conn.close()

    # ===== 报告 =====
    print()
    print("=" * 60)
    print("  V11.1 推荐 + L1+L2 过滤 回测报告")
    print("=" * 60)
    print()

    for mode, key in [('全部推荐(无过滤)', 'unfiltered'), ('L1+L2过滤后', 'filtered')]:
        r = results[key]
        n = r['total']
        if n == 0:
            print(f"  {mode}: 无数据")
            continue
        avg_ret = sum(r['returns']) / n
        win_rate = r['wins'] / n * 100
        max_ret = max(r['returns'])
        min_ret = min(r['returns'])
        cum_ret = sum(r['returns'])
        std = (sum((x - avg_ret)**2 for x in r['returns']) / n) ** 0.5

        print(f"  {mode}:")
        print(f"    样本数:      {n}")
        print(f"    平均收益:     {avg_ret:+.2f}%")
        print(f"    胜率:        {win_rate:.1f}%")
        print(f"    累计收益:     {cum_ret:+.2f}%")
        print(f"    最大收益:     {max_ret:+.2f}%")
        print(f"    最小收益:     {min_ret:+.2f}%")
        print(f"    标准差:       {std:.2f}%")
        print(f"    最大回撤:     {r['max_dd']:.1f}%")
        print()

    print(f"  L1+L2 过滤剔除: {filtered_count}/{results['unfiltered']['total']} "
          f"({filtered_count/results['unfiltered']['total']*100:.0f}%)")
    print()
    print("  过滤原因统计:")
    for reason, count in sorted(filter_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason:20s}: {count}次")

if __name__ == '__main__':
    main()
