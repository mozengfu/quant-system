#!/usr/bin/env python3
"""
板RPS止盈止损参数优化 — ATR动态规则版

高效: 先预计算所有交易的日频高开低收数据, 再批量应用ATR规则。
"""
import sys, os, json, logging
import numpy as np
import pandas as pd
import pymysql
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config
from quant_app.services.board_rps_scanner import get_top_board_stocks

DB_CONFIG = get_db_config()
START_DATE = "2026-02-01"
END_DATE = "2026-06-09"
MAX_HOLD = 20


def calc_atr(high, low, close, period=14):
    """计算ATR序列"""
    tr = np.maximum(high[1:] - low[1:],
                    np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:] - close[:-1]))
    atr = np.zeros(len(close))
    if len(tr) < period:
        return atr
    atr[period] = tr[:period].mean()
    for i in range(period + 1, len(close)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i-1]) / period
    return atr


def precompute_trades():
    """预计算所有候选交易的日频OHLC序列"""
    logger.info("预计算候选交易 (含OHLC)...")
    conn = pymysql.connect(**DB_CONFIG)

    dates = sorted(pd.read_sql("""SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date""",
        conn, params=(START_DATE, END_DATE))['trade_date'].tolist())
    sample_dates = [d for d in dates[5:] if d > dates[5]]

    all_trades  = []  # [{open, high, low, close}, ...]

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
            bd_str = str(buy_date)[:10]
            # 取买入后 MAX_HOLD 个交易日的高开低收
            cur.execute("""SELECT open, high, low, close, pct_chg FROM daily_price
                WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s""",
                (tc, bd_str, MAX_HOLD))
            rows = cur.fetchall()
            cur.close()
            if len(rows) >= 2:
                ohlc = [{'open':float(r[0]), 'high':float(r[1]), 'low':float(r[2]),
                         'close':float(r[3]), 'pct':float(r[4])} for r in rows if r[3] is not None]
                if len(ohlc) >= 2:
                    all_trades.append(ohlc)

    conn.close()
    logger.info(f"  共 {len(all_trades)} 笔候选交易")
    return all_trades


def evaluate(trades, sl_fixed, tp_fixed, atr_mult=2.0, trail_peak=5.0, trail_atr=2.0):
    """应用 ATR 风控规则模拟交易

    Args:
        sl_fixed: 固定止损比例 (如-0.07)
        tp_fixed: 固定止盈比例 (如0.05, 0=不使用)
        atr_mult: ATR止损倍数
        trail_peak: 启动移动止盈的盈利阈值(%)
        trail_atr: ATR移动止盈回落倍数
    """
    results = []
    for ohlc in trades:
        entry = ohlc[0]['close']  # 以第一根收盘价作为买入价
        closes = np.array([d['close'] for d in ohlc])
        highs = np.array([d['high'] for d in ohlc])
        lows = np.array([d['low'] for d in ohlc])
        opens = np.array([d['open'] for d in ohlc])

        atr = calc_atr(highs, lows, closes, period=14)
        cum = 1.0
        exited = False
        peak_nav = 1.0
        peak_pct = 0.0

        for i in range(len(closes)):
            day_ret = closes[i] / entry - 1  # 相对买入价的收益
            cum *= (1 + ohlc[i]['pct'] / 100)  # 累积收益

            # 盘中高点对应的收益
            high_pct = highs[i] / entry - 1

            # ATR止损 (每日收盘检查)
            if i >= 14 and atr[i] > 0 and entry > 0:
                atr_stop_pct = -atr_mult * atr[i] / entry
                if day_ret < atr_stop_pct:
                    results.append(day_ret * 100)
                    exited = True
                    break

            # 固定止损
            if sl_fixed < 0 and day_ret < sl_fixed:
                results.append(day_ret * 100)
                exited = True
                break

            # 更新峰值
            if high_pct > peak_pct:
                peak_pct = high_pct

            # ATR移动止盈: 峰值盈利>trail_peak后，回落trail_atr×ATR即卖
            if peak_pct >= trail_peak / 100 and i >= 14 and atr[i] > 0 and entry > 0:
                drawdown = peak_pct - day_ret  # 从峰值回撤
                if drawdown >= trail_atr * atr[i] / entry:
                    results.append(day_ret * 100)
                    exited = True
                    break

            # 固定止盈
            if tp_fixed > 0 and day_ret >= tp_fixed:
                results.append(day_ret * 100)
                exited = True
                break

        if not exited:
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
    score = sharpe * 0.35 + (wins / n * 100) * 0.3 + pl * 0.2 + (1 - max_dd / 100) * 0.15

    return {
        'n': n, 'win_rate': round(wins / n * 100, 1),
        'cum_ret': round(cum_ret, 2), 'avg_ret': round(avg, 2),
        'sharpe': round(sharpe, 2), 'max_dd': round(max_dd, 2),
        'pl_ratio': round(pl, 2), 'score': round(score, 2),
    }


def main():
    # Step 1: 预计算（只做一次）
    trades = precompute_trades()

    # Step 2: 参数网格
    configs = []

    # A) 纯ATR动态（无固定止盈止损）
    for atr_mult in [1.5, 2.0, 2.5, 3.0]:
        configs.append((f"ATR止损{atr_mult}x", {'sl_fixed': -0.07, 'tp_fixed': 0,
                      'atr_mult': atr_mult, 'trail_peak': 99, 'trail_atr': 99}))

    # B) ATR移动止盈组合
    for peak in [3, 5, 8, 10]:
        for tatr in [1.0, 1.5, 2.0, 3.0]:
            configs.append((f"移动止盈:峰值>{peak}%回落{tatr}xATR",
                          {'sl_fixed': -0.07, 'tp_fixed': 0,
                           'atr_mult': 2.0, 'trail_peak': peak, 'trail_atr': tatr}))

    # C) ATR + 固定止盈组合
    for tp in [5, 8, 10]:
        configs.append((f"ATR2x+固定止盈{tp}%",
                      {'sl_fixed': -0.07, 'tp_fixed': tp/100,
                       'atr_mult': 2.0, 'trail_peak': 99, 'trail_atr': 99}))

    # D) 混合: ATR移动止盈 + 固定止盈兜底
    for tp in [8, 10, 15]:
        for peak in [5, 8]:
            configs.append((f"移动止盈峰值{peak}%+固定{tp}%",
                          {'sl_fixed': -0.07, 'tp_fixed': tp/100,
                           'atr_mult': 2.0, 'trail_peak': peak, 'trail_atr': 2.0}))

    # E) 纯固定（基准）
    configs.append(("纯固定-7%止损+5%止盈(基准)",
                  {'sl_fixed': -0.07, 'tp_fixed': 0.05,
                   'atr_mult': 99, 'trail_peak': 99, 'trail_atr': 99}))

    results = []
    for label, p in configs:
        r = evaluate(trades, **p)
        if r:
            results.append({'label': label, **r})
            logger.info(f"  {label}: 胜率={r['win_rate']}% 夏普={r['sharpe']} 盈亏比={r['pl_ratio']}")

    # 输出
    print(f"\n{'='*100}")
    print("板RPS ATR风控参数扫描结果")
    print(f"{'='*100}")
    print(f"{'配置':<38} {'笔数':>5} {'胜率':>7} {'累积':>10} {'均收益':>8} {'夏普':>7} {'回撤':>8} {'盈亏比':>7} {'综合':>6}")
    print(f"{'-'*38} {'-'*5} {'-'*7} {'-'*10} {'-'*8} {'-'*7} {'-'*8} {'-'*7} {'-'*6}")

    for r in sorted(results, key=lambda x: -x['score']):
        print(f"{r['label']:<38} {r['n']:>5} {r['win_rate']:>6.1f}% "
              f"{r['cum_ret']:>+9.2f}% {r['avg_ret']:>+7.2f}% {r['sharpe']:>6.2f} "
              f"{r['max_dd']:>7.1f}% {r['pl_ratio']:>6.2f} {r['score']:>5.1f}")

    json.dump(results, open(os.path.join(os.path.dirname(__file__), '..',
                                          'data', 'backtest_board_rps_atr.json'), 'w'),
              indent=2, default=str)
    logger.info("已保存: data/backtest_board_rps_atr.json")


if __name__ == '__main__':
    main()
