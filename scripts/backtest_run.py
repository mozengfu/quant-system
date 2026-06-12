#!/usr/bin/env python3
"""
统一回测入口 — ML预测 + 市场状态判断 + 分级threshold

用法:
    python scripts/backtest_run.py --start 2024-11-01 --end 2026-05-08
    python scripts/backtest_run.py --start 2024-11-01 --end 2026-05-08 --top-n 5 --hold-days 5 --pool 300
"""
import argparse
import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

import pymysql
import tushare as ts

from quant_app.backtest.engine import BacktestEngine
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()


def _load_index_history(start: str, end: str) -> dict:
    """加载上证指数历史 (Tushare)，返回 {trade_date: pct_chg}"""
    pro = ts.pro_api()
    df = pro.index_daily(ts_code='000001.SH', start_date=start.replace('-',''),
                         end_date=end.replace('-',''))
    result = {}
    for _, row in df.iterrows():
        result[str(row['trade_date'])] = float(row['pct_chg'])
    logger.info("上证指数加载: %d 个交易日", len(result))
    return result


def _load_breadth(conn, start: str, end: str) -> dict:
    """加载每日涨跌比，返回 {trade_date: {'up':n, 'down':n, 'ratio':f}}"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date,
               COUNT(CASE WHEN pct_chg > 0 THEN 1 END),
               COUNT(CASE WHEN pct_chg < 0 THEN 1 END)
        FROM daily_price
        WHERE trade_date BETWEEN %s AND %s
        GROUP BY trade_date ORDER BY trade_date
    """, (start.replace('-',''), end.replace('-','')))
    result = {}
    for d, up, down in cur.fetchall():
        d_str = str(d)
        up, down = int(up or 0), int(down or 0)
        result[d_str] = {"up": up, "down": down, "ratio": round(up / max(down, 1), 2)}
    cur.close()
    logger.info("涨跌比加载: %d 个交易日", len(result))
    return result


def _get_market_state_backtest(trade_date: str, sh_idx: dict, breadth: dict) -> dict:
    """回测版市场状态判断（纯历史数据，不依赖实时API）"""
    sh_pct = sh_idx.get(trade_date, 0)
    br = breadth.get(trade_date)

    is_bear = False

    # 1. 单日大跌 > 1.5%
    if sh_pct < -1.5:
        is_bear = True
    # 2. 跌 > 0.7% + 涨跌比 < 1.0
    elif sh_pct < -0.7 and br and br["ratio"] < 1.0:
        is_bear = True
    # 3. 连续两天阴跌
    elif sh_pct < 0:
        # 找前一个交易日
        prev_dates = sorted([d for d in sh_idx if d < trade_date])
        if prev_dates:
            prev = sh_idx.get(prev_dates[-1], 0)
            if prev < 0:
                is_bear = True
    # 4. 涨跌比 < 0.5
    if not is_bear and br and br["ratio"] < 0.5:
        is_bear = True

    # threshold
    if is_bear:
        threshold = 0.40
    elif br and br["ratio"] < 0.8:
        threshold = 0.45
    else:
        threshold = 0.55

    return {"is_bear": is_bear, "threshold": threshold, "mkt_chg": sh_pct}


def _load_ml_predictor():
    """加载 v11.2 模型预测函数"""
    v11_2_path = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_2.pkl')
    if os.path.exists(v11_2_path):
        from scripts.predict_v11_2 import load_v11_2_model, predict_v11_2
        load_v11_2_model()
        return predict_v11_2
    # Fallback v11.0
    from ml_predict import _ensemble_predict
    from scripts.predict_v11 import load_v11_model
    bundle = load_v11_model(os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_0.pkl'))
    return lambda conn, codes, date: (None, _ensemble_predict(bundle, conn, codes, date))



def _filter_trend_bt(conn, ts_codes, trade_date):
    """回测版趋势过滤：ma5<ma20 且近3日累计下跌>3%"""
    if not ts_codes:
        return []
    cur = conn.cursor()
    deep_bear = set()
    for code in ts_codes:
        cur.execute(
            "SELECT ma5, ma20 FROM daily_price WHERE ts_code=%s AND trade_date=%s",
            (code, trade_date))
        row = cur.fetchone()
        if not row or not row[0] or not row[1] or float(row[0]) >= float(row[1]):
            continue
        cur.execute(
            "SELECT pct_chg FROM daily_price WHERE ts_code=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 3",
            (code, trade_date))
        rows = cur.fetchall()
        if len(rows) == 3 and sum(float(r[0] or 0) for r in rows) < -3:
            deep_bear.add(code)
    cur.close()
    if deep_bear:
        logger.info("趋势过滤排除 %d 只: %s", len(deep_bear), sorted(deep_bear))
    return [c for c in ts_codes if c not in deep_bear]


def main():
    parser = argparse.ArgumentParser(description='统一回测 (ML+市场状态)')
    parser.add_argument('--start', default='2024-11-01')
    parser.add_argument('--end', default='2026-05-08')
    parser.add_argument('--pool', type=int, default=300, help='候选池大小')
    parser.add_argument('--top-n', type=int, default=3, help='每次买入数量')
    parser.add_argument('--hold-days', type=int, default=5, help='持有天数')
    parser.add_argument('--interval', type=int, default=5, help='采样间隔')
    parser.add_argument('--stop-loss', type=float, default=-0.07, help='止损线(如-0.07=7%)')
    parser.add_argument('--output', default='data/backtest_result.json')
    args = parser.parse_args()

    # 预加载市场数据
    logger.info("加载市场数据...")
    sh_idx = _load_index_history(args.start, args.end)
    conn = pymysql.connect(**DB_CONFIG)
    breadth = _load_breadth(conn, args.start, args.end)
    conn.close()

    # 加载 ML 模型
    logger.info("加载 ML 模型...")
    predict_fn = _load_ml_predictor()

    def my_signal_fn(trade_date: str) -> list[str]:
        """ML 预测 + 市场状态过滤"""
        conn2 = pymysql.connect(**DB_CONFIG)
        try:
            engine = BacktestEngine(top_candidates=args.pool)
            pool = engine.get_top_pool(conn2, trade_date)
            if not pool:
                return []

            # ML 预测
            feat, preds = predict_fn(conn2, pool, as_of_date=trade_date)
            if feat is None or preds is None:
                return []

            codes = feat['ts_code'].tolist() if hasattr(feat, 'tolist') else list(feat['ts_code'])
            ranked = sorted(zip(codes, preds), key=lambda x: -x[1])

            # 市场状态判断
            mkt = _get_market_state_backtest(trade_date, sh_idx, breadth)
            min_score = mkt["threshold"]

            # 过滤 + 取 top
            filtered = [tc for tc, sc in ranked if sc >= min_score]
            # 趋势过滤
            filtered = _filter_trend_bt(conn2, filtered, trade_date)
            result = filtered[:args.top_n]

            if result:
                logger.debug("%s %s threshold=%.2f -> %d只",
                           trade_date, mkt["is_bear"] and "逆市" or "常态",
                           min_score, len(result))
            return result
        finally:
            conn2.close()

    engine = BacktestEngine(
        top_candidates=args.pool,
        top_n=args.top_n,
        hold_days=args.hold_days,
        sample_interval=args.interval,
        use_prev_amount=True,
        stop_loss=args.stop_loss,
    )

    result = engine.run(args.start, args.end, signal_fn=my_signal_fn)
    logger.info(result.summary())

    # 保存结果
    import json
    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else '.', exist_ok=True)
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
