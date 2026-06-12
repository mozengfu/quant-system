#!/usr/bin/env python3
"""
TopDown V1 回测脚本

对比三层自上而下模型 vs V11.0 纯ML 基线

用法:
  python scripts/backtest_topdown.py
  python scripts/backtest_topdown.py --baseline    # 同时跑V11.0对比
  python scripts/backtest_topdown.py --start 2025-10-01 --end 2026-05-15
"""

import argparse
import logging
import os
import sys
import warnings

import numpy as np
import pymysql

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)  # noqa: E402

from quant_app.utils.config import get_db_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ── 回测参数 ──
START_DATE = "2025-10-01"
END_DATE = "2026-05-15"
SAMPLE_INTERVAL = 5   # 每5个交易日采样一次
TOP_N = 5              # 每次选5只
HOLD_DAYS = 5          # 持有5天
TOP_VOL_N = 300        # 候选池: 成交额Top300


def get_trade_dates(conn, start, end):
    """获取交易日列表"""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT trade_date FROM daily_price "
        "WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        (start, end)
    )
    return sorted([r[0] for r in cur.fetchall()])


def get_top_vol_stocks(conn, date_str, n=TOP_VOL_N):
    """获取成交额TopN股票 (排除ST/科创/北交)"""
    cur = conn.cursor()
    cur.execute(
        "SELECT ts_code FROM daily_price "
        "WHERE trade_date=%s "
        "AND LEFT(ts_code,1) NOT IN ('8','4','9') "
        "AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '43%%' "
        "AND close<=200 "
        "ORDER BY amount DESC LIMIT %s",
        (date_str, n)
    )
    return [r[0] for r in cur.fetchall()]


def forward_return(conn, code, date_str, hold_days=HOLD_DAYS):
    """计算个股未来持有期收益率（含交易成本）"""
    cur = conn.cursor()
    cur.execute(
        "SELECT pct_chg FROM daily_price "
        "WHERE ts_code=%s AND trade_date>=%s ORDER BY trade_date LIMIT %s",
        (code, date_str, hold_days + 1)
    )
    rows = cur.fetchall()
    if len(rows) < 2:
        return None
    rets = [float(r[0]) / 100.0 for r in rows[1:hold_days + 1] if r[0] is not None]
    rets = [r for r in rets if not np.isnan(r)]
    if len(rets) == 0:
        return None
    # 成本: 0.03%双向佣金 + 0.1%印花税(卖出)
    cost = 0.0003 * 2 + 0.001
    return float((1 + np.array(rets)).prod() - 1 - cost) * 100


def compute_metrics(returns):
    """计算回测指标"""
    returns = np.array(returns)
    if len(returns) == 0:
        return {}

    cumulative = float(np.prod(1 + np.array(returns) / 100) - 1) * 100
    win_rate = float(np.mean([1 if r > 0 else 0 for r in returns])) * 100
    avg_return = float(np.mean(returns))
    std_return = float(np.std(returns))

    # 夏普比率 (年化，无风险利率=2%)
    if std_return > 0:
        sharpe = (avg_return / 100 * 252 - 0.02) / (std_return / 100 * np.sqrt(252))
    else:
        sharpe = 0

    # 最大回撤
    cum_returns = np.cumprod(1 + np.array(returns) / 100)
    peak = np.maximum.accumulate(cum_returns)
    drawdown = (cum_returns - peak) / peak * 100
    max_dd = float(drawdown.min())

    # 盈亏比
    win_rets = [r for r in returns if r > 0]
    loss_rets = [r for r in returns if r < 0]
    if loss_rets:
        profit_loss_ratio = abs(np.mean(win_rets) / np.mean(loss_rets)) if win_rets else 0
    else:
        profit_loss_ratio = float('inf')

    return {
        'cumulative_return': round(cumulative, 2),
        'win_rate': round(win_rate, 1),
        'avg_return': round(avg_return, 2),
        'std_return': round(std_return, 2),
        'sharpe': round(sharpe, 2),
        'max_drawdown': round(max_dd, 2),
        'profit_loss_ratio': round(profit_loss_ratio, 2),
        'n_trades': len(returns),
    }


def backtest_topdown(conn, start_date, end_date, sample_interval=5, top_n=5, hold_days=5):
    """回测 TopDown V1 三层模型"""
    from quant_app.models.topdown_pipeline import predict_topdown

    trade_dates = get_trade_dates(conn, start_date, end_date)
    logger.info(f"Trading days: {len(trade_dates)}")

    # 采样点
    sample_dates = trade_dates[::sample_interval]
    logger.info(f"Sample points: {len(sample_dates)}")

    all_returns = []
    trade_records = []

    for i, date_str in enumerate(sample_dates):
        date_str = str(date_str)[:10]

        # 确保有足够未来数据
        future_dates = [d for d in trade_dates if str(d) > date_str]
        if len(future_dates) < hold_days:
            continue

        try:
            result = predict_topdown(conn, date_str, top_n=top_n)
        except Exception as e:
            logger.warning(f"  {date_str}: prediction failed - {e}")
            continue

        recs = result.get('recommendations', [])
        if not recs:
            logger.info(f"  {date_str}: no recommendations")
            continue

        selected = [r['ts_code'] for r in recs]
        regime = result['market_regime']['direction']
        pos_mul = result['market_regime']['position_multiplier']

        # 计算每只股票的持有期收益
        stock_returns = []
        for code in selected:
            ret = forward_return(conn, code, date_str, hold_days)
            if ret is not None:
                stock_returns.append(ret)
                trade_records.append({
                    'date': date_str,
                    'ts_code': code,
                    'return': round(ret, 2),
                    'regime': regime,
                })

        if stock_returns:
            # 等权组合收益
            portfolio_return = np.mean(stock_returns)
            all_returns.append(portfolio_return)

            logger.info(
                f"  {date_str}: regime={regime} pos={pos_mul:.2f} "
                f"stocks={selected[:3]}... ret={portfolio_return:+.2f}%"
            )
        else:
            logger.info(f"  {date_str}: no valid returns")

    return all_returns, trade_records


def backtest_v11_baseline(conn, start_date, end_date, sample_interval=5, top_n=5, hold_days=5):
    """回测 V11.0 纯ML 作为基线"""
    from ml_predict import predict_batch

    trade_dates = get_trade_dates(conn, start_date, end_date)
    sample_dates = trade_dates[::sample_interval]

    all_returns = []

    for i, date_str in enumerate(sample_dates):
        date_str = str(date_str)[:10]

        future_dates = [d for d in trade_dates if str(d) > date_str]
        if len(future_dates) < hold_days:
            continue

        # Top300成交额候选
        candidates = get_top_vol_stocks(conn, date_str, TOP_VOL_N)
        if len(candidates) < 50:
            continue

        try:
            preds = predict_batch(candidates, db_conn=None, as_of_date=date_str)
        except Exception as e:
            logger.warning(f"  {date_str}: V11 predict failed - {e}")
            continue

        # 按 probability 排序取TopN
        sorted_preds = sorted(preds.items(), key=lambda x: x[1].get('probability', 0), reverse=True)
        selected = [c for c, _ in sorted_preds[:top_n]]

        stock_returns = []
        for code in selected:
            ret = forward_return(conn, code, date_str, hold_days)
            if ret is not None:
                stock_returns.append(ret)

        if stock_returns:
            all_returns.append(np.mean(stock_returns))

    return all_returns


def main():
    parser = argparse.ArgumentParser(description='TopDown V1 Backtest')
    parser.add_argument('--start', default=START_DATE, help='Start date')
    parser.add_argument('--end', default=END_DATE, help='End date')
    parser.add_argument('--baseline', action='store_true', help='Run V11.0 baseline comparison')
    parser.add_argument('--interval', type=int, default=None)
    parser.add_argument('--top_n', type=int, default=None)
    parser.add_argument('--hold', type=int, default=None)
    args = parser.parse_args()

    # 用命令行参数覆盖模块级默认值
    interval = args.interval if args.interval is not None else SAMPLE_INTERVAL
    top_n = args.top_n if args.top_n is not None else TOP_N
    hold_days = args.hold if args.hold is not None else HOLD_DAYS

    conn = pymysql.connect(**get_db_config())
    logger.info(f"Backtest: {args.start} → {args.end}")
    logger.info(f"  Interval={interval}d, TopN={top_n}, Hold={hold_days}d")

    try:
        # ── TopDown V1 回测 ──
        logger.info("\n" + "=" * 60)
        logger.info("TopDown V1 Backtest")
        logger.info("=" * 60)
        td_returns, td_trades = backtest_topdown(conn, args.start, args.end, interval, top_n, hold_days)
        td_metrics = compute_metrics(td_returns)

        print("\n── TopDown V1 Results ──")
        for k, v in td_metrics.items():
            print(f"  {k}: {v}")

        # ── V11.0 基线对比 ──
        if args.baseline:
            logger.info("\n" + "=" * 60)
            logger.info("V11.0 Baseline Backtest")
            logger.info("=" * 60)
            v11_returns = backtest_v11_baseline(conn, args.start, args.end, interval, top_n, hold_days)
            v11_metrics = compute_metrics(v11_returns)

            print("\n── V11.0 Baseline Results ──")
            for k, v in v11_metrics.items():
                print(f"  {k}: {v}")

            # ── 对比 ──
            print("\n── Comparison ──")
            print(f"  {'Metric':<25} {'TopDown V1':>12} {'V11.0':>12} {'Diff':>10}")
            print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*10}")
            for k in ['cumulative_return', 'win_rate', 'sharpe', 'max_drawdown', 'n_trades']:
                td_v = td_metrics.get(k, 0)
                v11_v = v11_metrics.get(k, 0)
                if isinstance(td_v, (int, float)):
                    diff = td_v - v11_v
                    direction = '↑' if diff > 0 else '↓' if diff < 0 else '='
                    print(f"  {k:<25} {td_v:>+12.2f} {v11_v:>+12.2f} {diff:>+9.2f} {direction}")
                else:
                    print(f"  {k:<25} {str(td_v):>12} {str(v11_v):>12}")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
