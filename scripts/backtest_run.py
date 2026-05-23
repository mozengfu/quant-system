#!/usr/bin/env python3
"""统一回测入口 — 使用 quant_app/backtest/engine.py 统一引擎。

用法:
    python scripts/backtest_run.py --start 2024-11-01 --end 2026-05-08
    python scripts/backtest_run.py --start 2024-11-01 --end 2026-05-08 --top-n 5 --hold-days 5
"""
import argparse
import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.backtest.engine import BacktestEngine, BacktestResult
from quant_app.utils.config import get_db_config


def main():
    parser = argparse.ArgumentParser(description='统一回测')
    parser.add_argument('--start', default='2024-11-01')
    parser.add_argument('--end', default='2026-05-08')
    parser.add_argument('--pool', type=int, default=300, help='候选池大小')
    parser.add_argument('--top-n', type=int, default=3, help='每次买入数量')
    parser.add_argument('--hold-days', type=int, default=5, help='持有天数')
    parser.add_argument('--interval', type=int, default=5, help='采样间隔')
    parser.add_argument('--output', default='data/backtest_result.json')
    args = parser.parse_args()

    # 这里演示一个简单信号函数：选候选池中 ML 预测 Top N 的股票
    # 实际使用时，替换为自己的信号生成逻辑
    def my_signal_fn(trade_date: str) -> list[str]:
        """示例信号函数 — 实际应调用 ML 预测 + 风控管线。"""
        import pymysql
        conn = pymysql.connect(**get_db_config())
        try:
            engine = BacktestEngine(top_candidates=args.pool)
            pool = engine.get_top_pool(conn, trade_date)
            # TODO: 在这里接入 ML 预测、风控过滤等逻辑
            # 示例：直接返回前 N 只（仅验证引擎框架）
            return pool[:args.top_n]
        finally:
            conn.close()

    engine = BacktestEngine(
        top_candidates=args.pool,
        top_n=args.top_n,
        hold_days=args.hold_days,
        sample_interval=args.interval,
        use_prev_amount=True,  # 用前一日成交额，防止数据泄漏
    )

    result = engine.run(args.start, args.end, signal_fn=my_signal_fn)
    logger.info(result.summary())

    # 保存结果
    import json
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, 'w') as f:
        json.dump({
            "total_return": round(result.total_return, 2),
            "win_rate": round(result.win_rate, 1),
            "profit_loss_ratio": round(result.profit_loss_ratio, 2),
            "sharpe": round(result.sharpe, 2),
            "max_drawdown": round(result.max_drawdown, 2),
            "n_trades": result.n_trades,
            "n_wins": result.n_wins,
            "nav_values": result.nav_values,
        }, f, indent=2)
    logger.info(f"结果已保存: {args.output}")


if __name__ == '__main__':
    main()
