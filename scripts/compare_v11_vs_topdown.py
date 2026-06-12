#!/usr/bin/env python3
"""
策略级 A/B 对比: V11.0 baseline vs 新三段模型

用法:
  python3 scripts/compare_v11_vs_topdown.py --start 2025-01-01 --end 2025-06-30
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.backtest.strategy_engine import Signal, StrategyBacktest, StrategyConfig
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


def get_daily_prices(conn, start, end, ts_codes=None):
    """加载所有日线 (前复权用 close 近似)"""
    if ts_codes:
        placeholders = ','.join(['%s'] * len(ts_codes))
        sql = f"SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg FROM daily_price WHERE trade_date BETWEEN %s AND %s AND ts_code IN ({placeholders})"
        df = pd.read_sql(sql, conn, params=(start, end, *ts_codes), parse_dates=['trade_date'])
    else:
        sql = "SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg FROM daily_price WHERE trade_date BETWEEN %s AND %s"
        df = pd.read_sql(sql, conn, params=(start, end), parse_dates=['trade_date'])
    return df


def build_v11_signals(conn, start, end, top_n=5):
    """用 V11.0 模型生成每日 Top N 信号"""
    from ml_predict import predict_batch
    df_price = get_daily_prices(conn, start, end)
    all_dates = sorted(df_price['trade_date'].dt.strftime('%Y-%m-%d').unique())
    signals = {}
    for d in all_dates:
        try:
            # 全部股票预测 (会慢, 但 V11 是生产模型)
            preds = predict_batch(df_price['ts_code'].unique().tolist(), db_conn=conn, as_of_date=d)
            if preds is None or preds.empty:
                continue
            preds = preds.sort_values('pred_score' if 'pred_score' in preds.columns else preds.columns[-1], ascending=False).head(top_n)
            sig_list = []
            for _, r in preds.iterrows():
                score = float(r.get('pred_score', r.get('score', 0.5)))
                sig_list.append(Signal(
                    date=d,
                    ts_code=r['ts_code'],
                    score=score,
                    confidence='high' if score >= 0.7 else ('mid' if score >= 0.5 else 'low'),
                    expected_return=score,
                ))
            signals[d] = sig_list
        except Exception as e:
            logger.error(f"V11 predict {d}: {e}")
            continue
    return signals


def build_topdown_signals(conn, start, end, top_n=5):
    """用新三段模型生成每日 Top N 信号"""
    from quant_app.pipeline.topdown_predictor import TopDownPredictor
    df_price = get_daily_prices(conn, start, end)
    all_dates = sorted(df_price['trade_date'].dt.strftime('%Y-%m-%d').unique())
    # 用月末/每 5 日跑一次, 避免重复
    sample_dates = all_dates[::5] + [all_dates[-1]]  # 每 5 日 + 最后一日
    sample_dates = sorted(set(sample_dates))

    # 先跑全 sample_dates 拿到候选
    logger.info(f"Running top-down on {len(sample_dates)} dates...")
    p = TopDownPredictor(conn=conn)
    all_results = {}
    for d in sample_dates:
        try:
            r = p.run_predict(d)
            all_results[d] = r
            logger.info(f"  {d}: gate={r['gate']}, market={r['market']['direction']}, "
                       f"sectors={len(r['hot_sectors'])}, candidates={len(r['candidates'])}")
        except Exception as e:
            logger.error(f"  {d}: {e}")
            continue

    # 把每个 d 的 Top N 信号扩展到 d 之后的 5 天 (盘后信号, 5 日内有效)
    signals = {d: [] for d in all_dates}
    for d, r in all_results.items():
        d_dt = pd.Timestamp(d)
        for cand in r['candidates'][:top_n]:
            sig = Signal(
                date=d,
                ts_code=cand['ts_code'],
                score=float(cand.get('final_score', 50)),
                confidence=str(cand.get('confidence', 'mid')),
                expected_return=float(cand.get('ml_v11', 0)),
            )
            # 5 日内每天都有这个信号 (实际上应该只在 d+1 触发, 这里简化)
            for offset in range(1, 6):
                target = (d_dt + pd.tseries.offsets.BDay(offset)).strftime('%Y-%m-%d')
                if target in signals:
                    signals[target].append(sig)
    return signals


def run_comparison(start, end, initial_capital=1_000_000):
    conn = pymysql.connect(**get_db_config())
    cfg = StrategyConfig(start=start, end=end, initial_capital=initial_capital)

    logger.info("=== V11 baseline ===")
    v11_signals = build_v11_signals(conn, start, end, top_n=5)
    bt1 = StrategyBacktest(cfg)
    r1 = bt1.run(v11_signals, conn=conn)
    logger.info(bt1.report())

    logger.info("\n=== Top-down 新模型 ===")
    td_signals = build_topdown_signals(conn, start, end, top_n=5)
    bt2 = StrategyBacktest(cfg)
    r2 = bt2.run(td_signals, conn=conn)
    logger.info(bt2.report())

    # 对比
    m1, m2 = r1['metrics'], r2['metrics']
    logger.info("\n" + "=" * 60)
    logger.info("  对比摘要")
    logger.info("=" * 60)
    logger.info(f"  {'指标':<22} {'V11':>12} {'TopDown':>12} {'差异':>10}")
    logger.info(f"  {'年化收益':<22} {m1.get('annual_return_pct',0):>11.2f}% {m2.get('annual_return_pct',0):>11.2f}% "
                f"{m2.get('annual_return_pct',0)-m1.get('annual_return_pct',0):>+9.2f}%")
    logger.info(f"  {'最大回撤':<22} {m1.get('max_drawdown_pct',0):>11.2f}% {m2.get('max_drawdown_pct',0):>11.2f}% "
                f"{m2.get('max_drawdown_pct',0)-m1.get('max_drawdown_pct',0):>+9.2f}%")
    logger.info(f"  {'夏普比率':<22} {m1.get('sharpe',0):>12.2f} {m2.get('sharpe',0):>12.2f} "
                f"{m2.get('sharpe',0)-m1.get('sharpe',0):>+10.2f}")
    logger.info(f"  {'胜率':<22} {m1.get('win_rate_pct',0):>11.2f}% {m2.get('win_rate_pct',0):>11.2f}% "
                f"{m2.get('win_rate_pct',0)-m1.get('win_rate_pct',0):>+9.2f}%")
    logger.info(f"  {'盈亏比':<22} {m1.get('pl_ratio',0):>12.2f} {m2.get('pl_ratio',0):>12.2f} "
                f"{m2.get('pl_ratio',0)-m1.get('pl_ratio',0):>+10.2f}")
    logger.info("=" * 60)

    # 保存
    out_dir = Path('data/backtest_compare')
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f'compare_{start}_{end}.json', 'w') as f:
        json.dump({
            'start': start, 'end': end,
            'v11': m1, 'topdown': m2,
        }, f, indent=2, ensure_ascii=False)
    logger.info(f"\nResults saved → {out_dir}/compare_{start}_{end}.json")


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--start', default='2025-01-01')
    p.add_argument('--end', default='2025-06-30')
    p.add_argument('--capital', type=float, default=1_000_000)
    args = p.parse_args()
    run_comparison(args.start, args.end, args.capital)
