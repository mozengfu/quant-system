"""
交易市场归因分析 — 统计每一笔已平仓交易在买入时的市场状态，
判断亏损交易是否集中在下跌市中。
"""
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

# 市场状态分类阈值（SH指数5日涨跌幅）
THRESHOLD_UP = 1.0   # > +1% = 上涨市
THRESHOLD_DOWN = -1.0  # < -1% = 下跌市
# ±1%之间 = 震荡市


def get_trade_market_attribution(mode="live"):
    """
    分析所有已平仓交易与买入时市场状态的关系。

    返回:
        dict: {
            "total_trades": int,          # 总交易数
            "overall_win_rate": float,    # 总胜率 %
            "overall_avg_pnl": float,     # 总平均盈亏 %
            "market_states": [            # 按市场状态分组的统计
                {
                    "market_state": str,   # 上涨市/震荡市/下跌市
                    "total_trades": int,
                    "wins": int,
                    "losses": int,
                    "win_rate": float,
                    "avg_pnl_pct": float,
                    "avg_win_pct": float,
                    "avg_loss_pct": float,
                    "max_win_pct": float,
                    "max_loss_pct": float,
                    "total_pnl_pct": float,
                    "trades": [...]        # 该状态下的交易明细
                },
                ...
            ],
            "recent_trades": [             # 近期交易（含当前持仓）
                {...}
            ]
        }
    """
    import pymysql

    from quant_app.utils.config import get_db_config

    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()

    # ── 1. 从 qmt_trades 取实盘已卖出交易, 匹配买入价计算盈亏 ──
    # ── 1. 从 qmt_trades 按 mode 取交易配对 ──
    # 配对策略：同一只股票按时间顺序 FIFO 匹配 BUY→SELL
    cur.execute("""
        SELECT
          s.sell_date, s.ts_code, s.sell_price, b.buy_date, b.buy_price
        FROM (
          SELECT ts_code, trade_date AS sell_date, price AS sell_price,
                 ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date) AS rn
          FROM qmt_trades
          WHERE mode = %s AND action = 'SELL' AND status = 'filled'
        ) s
        JOIN (
          SELECT ts_code, trade_date AS buy_date, price AS buy_price,
                 ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date) AS rn
          FROM qmt_trades
          WHERE mode = %s AND action = 'BUY' AND status = 'filled'
        ) b ON s.ts_code = b.ts_code AND s.rn = b.rn
        ORDER BY s.sell_date
    """, (mode, mode))
    sell_trades_raw = cur.fetchall()

    # ── 2. 补充：从 sim_positions 获取那些没有 buy_date 的交易的买入日期 ──
    missing_buy = set()
    for r in sell_trades_raw:
        if r[3] is None:  # buy_date is None (字段: sell_date,ts_code,sell_price,buy_date,buy_price)
            missing_buy.add(r[1])

    if missing_buy:
        fmt = ",".join(f"'{c}'" for c in missing_buy)
        cur.execute(f"""
            SELECT ts_code, MIN(buy_date) AS buy_date
            FROM sim_positions
            WHERE ts_code IN ({fmt})
              AND buy_date IS NOT NULL
            GROUP BY ts_code
        """)
        pos_buy_dates = dict(cur.fetchall())
    else:
        pos_buy_dates = {}

    # ── 3. 用买入价计算 profit_pct, 构建完整交易数据 ──
    raw_trades = []
    for r in sell_trades_raw:
        sell_date, ts_code, sell_price, buy_date, buy_price = r
        if buy_price is None or float(buy_price) <= 0:
            continue
        # 补填 buy_date
        if buy_date is None and ts_code in pos_buy_dates:
            buy_date = pos_buy_dates[ts_code]
        if buy_date is None:
            continue
        profit_pct = (float(sell_price) - float(buy_price)) / float(buy_price)
        if profit_pct is None:
            continue
        raw_trades.append({
            "ts_code": ts_code,
            "buy_date": buy_date,
            "sell_date": sell_date,
            "profit_pct": profit_pct,
        })

    # ── 4. 对每笔交易获取买入时的市场状态 ──
    results = []
    for t in raw_trades:
        market_state, sh_5d_change = _classify_market_state(cur, t["buy_date"])
        results.append({
            "ts_code": t["ts_code"],
            "buy_date": str(t["buy_date"]),
            "sell_date": str(t["sell_date"]),
            "profit_pct": round(t["profit_pct"] * 100, 2),
            "market_state": market_state,
            "sh_5d_change": sh_5d_change,
        })

    # ── 5. 按市场状态分组统计 ──
    groups = defaultdict(list)
    for r in results:
        groups[r["market_state"]].append(r)

    market_states = []
    # 排序：上涨市 → 震荡市 → 下跌市
    state_order = {"上涨市": 0, "震荡市": 1, "下跌市": 2, "未知": 3}
    for state in sorted(groups.keys(), key=lambda s: state_order.get(s, 9)):
        trades_in = groups[state]
        total = len(trades_in)
        wins = [t for t in trades_in if t["profit_pct"] > 0]
        losses = [t for t in trades_in if t["profit_pct"] <= 0]
        n_win = len(wins)
        n_loss = len(losses)
        win_rate = n_win / total * 100 if total > 0 else 0
        avg_pnl = sum(t["profit_pct"] for t in trades_in) / total
        avg_win = sum(t["profit_pct"] for t in wins) / n_win if n_win else 0
        avg_loss = sum(t["profit_pct"] for t in losses) / n_loss if n_loss else 0
        total_pnl = sum(t["profit_pct"] for t in trades_in)

        market_states.append({
            "market_state": state,
            "total_trades": total,
            "wins": n_win,
            "losses": n_loss,
            "win_rate": round(win_rate, 1),
            "avg_pnl_pct": round(avg_pnl, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "total_pnl_pct": round(total_pnl, 2),
            "max_win_pct": round(max(t["profit_pct"] for t in trades_in), 2),
            "max_loss_pct": round(min(t["profit_pct"] for t in trades_in), 2),
        })

    total_trades = len(results)
    overall_win = sum(1 for r in results if r["profit_pct"] > 0)
    overall_avg = sum(r["profit_pct"] for r in results) / total_trades if total_trades else 0

    conn.close()

    return {
        "total_trades": total_trades,
        "overall_win_rate": round(overall_win / total_trades * 100, 1) if total_trades else 0,
        "overall_avg_pnl": round(overall_avg, 2),
        "market_states": market_states,
        "trades": results,
    }


def _classify_market_state(cur, buy_date):
    """
    根据买入日期前的 SH 指数表现，判断买入时的市场状态。

    规则:
        SH指数5日涨幅 > +1%  → 上涨市
        SH指数5日涨幅 < -1%  → 下跌市
        否则                   → 震荡市

    Returns:
        (market_state: str, sh_5d_change: float|None)
    """
    cur.execute("""
        SELECT close_price, trade_date
        FROM market_index_daily
        WHERE index_code = '000001.SH'
          AND trade_date <= %s
        ORDER BY trade_date DESC
        LIMIT 6
    """, (buy_date,))
    rows = cur.fetchall()

    if len(rows) >= 6:
        # rows[0] = buy_date or last trading day before buy
        # rows[5] = 5 trading days before
        current_close = float(rows[0][0])
        five_days_ago_close = float(rows[5][0])
        change_5d = (current_close - five_days_ago_close) / five_days_ago_close * 100

        if change_5d > THRESHOLD_UP:
            return "上涨市", round(change_5d, 2)
        elif change_5d < THRESHOLD_DOWN:
            return "下跌市", round(change_5d, 2)
        else:
            return "震荡市", round(change_5d, 2)

    return "未知", None
