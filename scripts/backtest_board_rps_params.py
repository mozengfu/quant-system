#!/usr/bin/env python3
"""
板RPS止盈止损参数优化

高效版：先一次性算出所有候选交易的全量日频收益序列，
再对每个止盈止损参数组合应用规则，避免重复的板RPS计算。
"""
import sys, os, json, logging
import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config
from quant_app.services.board_rps_scanner import get_top_board_stocks

DB_CONFIG = get_db_config()
START_DATE = "2026-02-01"
END_DATE = "2026-06-09"
MAX_HOLD = 20  # 最大持仓天数


def precompute_trades():
    """预计算所有候选交易的每日收益序列"""
    logger.info("预计算候选交易...")
    conn = pymysql.connect(**DB_CONFIG)

    dates = sorted(pd.read_sql("""SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date""",
        conn, params=(START_DATE, END_DATE))['trade_date'].tolist())
    sample_dates = [d for d in dates[5:] if d > dates[5]]

    all_trades = []  # [{date, daily_rets:[pct_chg...]}, ...]

    for buy_date in sample_dates:
        future = [d for d in dates if d > buy_date]
        if len(future) < 3:
            continue

        try:
            cand = get_top_board_stocks(as_of_date=buy_date)
        except:
            continue
        if not cand['ts_codes'] or len(cand['ts_codes']) < 3:
            continue

        cur = conn.cursor()
        ph = ','.join(['%s'] * len(cand['ts_codes']))
        cur.execute(f"""SELECT ts_code FROM daily_price
            WHERE ts_code IN ({ph}) AND trade_date=%s
            ORDER BY amount DESC LIMIT 3""", (*cand['ts_codes'], buy_date))
        top3 = [r[0] for r in cur.fetchall()]
        cur.close()

        for tc in top3:
            cur = conn.cursor()
            cur.execute("""SELECT pct_chg FROM daily_price
                WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s""",
                (tc, buy_date, MAX_HOLD))
            vals = [float(r[0]) for r in cur.fetchall() if r[0] is not None]
            cur.close()
            if len(vals) >= 1:
                all_trades.append({'daily_rets': vals})

    conn.close()
    logger.info(f"  共 {len(all_trades)} 笔候选交易")
    return all_trades


def evaluate(trades, sl, tp):
    """对一组交易应用止盈止损规则"""
    results = []
    for t in trades:
        cum = 1.0
        for v in t['daily_rets']:
            cum *= (1 + v / 100)
            if tp > 0 and cum - 1 >= tp:
                break
            if sl < 0 and cum - 1 <= sl:
                break
        results.append((cum - 1) * 100)

    r = np.array(results)
    n = len(r)
    if n < 5:
        return None

    wins = int((r > 0).sum())
    cum_ret = float(np.prod(1 + r / 100) - 1) * 100
    avg = float(r.mean())
    std = float(r.std())
    sharpe = float(avg / std * np.sqrt(252 / 5)) if std > 0 else 0
    max_dd = 0.0
    nav = 100.0
    peak = 100.0
    for ret in r:
        nav *= (1 + ret / 100)
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak * 100
        if dd > max_dd:
            max_dd = dd
    pl = abs(r[r > 0].mean() / r[r < 0].mean()) if (r[r > 0].size > 0 and r[r < 0].size > 0) else 0
    score = sharpe * 0.4 + (wins / n * 100) * 0.3 + pl * 0.3

    return {
        'n': n, 'win_rate': round(wins / n * 100, 1),
        'cum_ret': round(cum_ret, 2), 'avg_ret': round(avg, 2),
        'sharpe': round(sharpe, 2), 'max_dd': round(max_dd, 2),
        'pl_ratio': round(pl, 2), 'score': round(score, 2),
    }


def main():
    import pandas as pd
    # Step 1: 预计算（只做一次）
    trades = precompute_trades()

    # Step 2: 参数网格
    sl_values = [0, -0.03, -0.05, -0.07, -0.10]
    tp_values = [0, 0.03, 0.05, 0.08, 0.10, 0.15]

    results = []
    for sl in sl_values:
        for tp in tp_values:
            label = f"SL={sl*100 if sl<0 else 0:.0f}% TP={tp*100:.0f}%"
            r = evaluate(trades, sl, tp)
            if r:
                results.append({
                    'stop_loss': f"{sl*100:.0f}%" if sl < 0 else "无",
                    'take_profit': f"{tp*100:.0f}%" if tp > 0 else "无",
                    **r
                })
                logger.info(f"  {label}: 胜率={r['win_rate']}% 夏普={r['sharpe']} 累积={r['cum_ret']}%")

    # Step 3: 输出
    print(f"\n{'='*100}")
    print("板RPS 止盈止损参数扫描结果")
    print(f"{'='*100}")
    print(f"{'止损':>6} {'止盈':>6} {'笔数':>5} {'胜率':>7} {'累积收益':>10} {'均收益':>8} {'夏普':>7} {'最大回撤':>8} {'盈亏比':>7} {'综合分':>6}")
    print(f"{'-'*6} {'-'*6} {'-'*5} {'-'*7} {'-'*10} {'-'*8} {'-'*7} {'-'*8} {'-'*7} {'-'*6}")

    for r in sorted(results, key=lambda x: -x['score']):
        print(f"{r['stop_loss']:>6} {r['take_profit']:>6} {r['n']:>5} {r['win_rate']:>6.1f}% "
              f"{r['cum_ret']:>+9.2f}% {r['avg_ret']:>+7.2f}% {r['sharpe']:>6.2f} "
              f"{r['max_dd']:>7.1f}% {r['pl_ratio']:>6.2f} {r['score']:>5.1f}")

    json.dump(results, open(os.path.join(os.path.dirname(__file__), '..', 'data',
                                          'backtest_board_rps_params.json'), 'w'),
              indent=2, default=str)
    logger.info("已保存: data/backtest_board_rps_params.json")


if __name__ == '__main__':
    main()
