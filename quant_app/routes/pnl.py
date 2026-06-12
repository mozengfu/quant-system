"""
实盘盈亏统计 API — 基于 QMT 实时数据 + qmt_trades 历史
"""
import logging
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, HTTPException

from quant_app.trading.modes.remote_executor import RemoteTraderExecutor
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pnl", tags=["pnl"])

_executor: RemoteTraderExecutor = None


def _get_executor() -> RemoteTraderExecutor:
    global _executor
    if _executor is None:
        _executor = RemoteTraderExecutor()
    return _executor


@router.get("/summary")
async def pnl_summary():
    """实盘盈亏汇总：余额 + 持仓浮动盈亏 + 历史已实现盈亏 + 净值曲线"""
    try:
        import pymysql

        e = _get_executor()
        if not e._connected:
            e.prepare()

        # ---- 1. QMT 实时余额 ----
        balance = e.get_balance()
        total_asset = balance.total_asset if balance else 0
        available = balance.available if balance else 0
        market_value = balance.market_value if balance else 0

        # ---- 2. QMT 实时持仓（未实现盈亏） ----
        positions = e.get_positions() or []
        unrealized_pnl = 0.0
        pos_list = []
        for p in positions:
            pnl = getattr(p, 'pnl', 0) or 0
            pnl_pct = getattr(p, 'pnl_pct', 0) or 0
            unrealized_pnl += float(pnl)
            pos_list.append({
                "ts_code": getattr(p, 'ts_code', ''),
                "name": getattr(p, 'name', ''),
                "quantity": getattr(p, 'quantity', 0),
                "cost_price": round(float(getattr(p, 'cost_price', 0) or 0), 3),
                "current_price": round(float(getattr(p, 'current_price', 0) or 0), 3),
                "market_value": round(float(getattr(p, 'market_value', 0) or 0), 2),
                "pnl": round(float(pnl), 2),
                "pnl_pct": round(float(pnl_pct), 2),
            })

        # ---- 3. 历史已实现盈亏（MySQL qmt_trades） ----
        realized_pnl = 0.0
        daily_pnl = defaultdict(float)
        trade_count = 0
        win_count = 0

        try:
            conn = pymysql.connect(**get_db_config())
            cur = conn.cursor()

            # 查询所有已成交的买卖对，计算已实现盈亏
            cur.execute("""
                SELECT ts_code, action, price, quantity, amount, trade_date
                FROM qmt_trades
                WHERE status = 'filled'
                ORDER BY trade_date, trade_time
            """)
            trades = cur.fetchall()

            # 按股票分组计算已实现盈亏（FIFO 匹配）
            holdings = defaultdict(list)
            for t in trades:
                code, action, price, qty, amount, td = t
                price = float(price)
                qty = int(qty)
                td_str = str(td) if td else ""
                if action == "BUY":
                    holdings[code].append({"qty": qty, "cost": price, "date": td_str})
                elif action == "SELL":
                    sell_qty = qty
                    sell_amount = price * qty
                    cost_amount = 0
                    while sell_qty > 0 and holdings.get(code):
                        lot = holdings[code][0]
                        match_qty = min(sell_qty, lot["qty"])
                        cost_amount += match_qty * lot["cost"]
                        lot["qty"] -= match_qty
                        sell_qty -= match_qty
                        if lot["qty"] <= 0:
                            holdings[code].pop(0)
                    if sell_qty == 0:
                        pnl = round(sell_amount - cost_amount, 2)
                        realized_pnl += pnl
                        daily_pnl[td_str] += pnl
                        trade_count += 1
                        if pnl > 0:
                            win_count += 1

            cur.close()
            conn.close()
        except Exception as ex:
            logger.warning("读取 qmt_trades 失败: %s", ex)

        # ---- 4. 计算总盈亏 & 初始资金 ----
        total_pnl = round(realized_pnl + unrealized_pnl, 2)
        # 反推初始资金：当前总资产 - 累计盈亏 = 净投入本金
        initial_capital = round(total_asset - total_pnl, 2)
        total_pnl_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0
        win_rate = round(win_count / trade_count * 100, 1) if trade_count > 0 else 0

        # ---- 5. 每日净值曲线 ----
        sorted_dates = sorted(daily_pnl.keys())
        curve = []
        cum_pnl = 0.0
        cum_nav = initial_capital
        max_nav = initial_capital
        max_drawdown = 0.0

        for d in sorted_dates:
            cum_pnl += daily_pnl[d]
            cum_nav = initial_capital + cum_pnl
            if cum_nav > max_nav:
                max_nav = cum_nav
            dd = (max_nav - cum_nav) / max_nav * 100 if max_nav > 0 else 0
            if dd > max_drawdown:
                max_drawdown = round(dd, 2)
            curve.append({
                "date": d,
                "daily_pnl": round(daily_pnl[d], 2),
                "cum_nav": round(cum_nav, 2),
                "cum_pnl_pct": round((cum_nav / initial_capital - 1) * 100, 2) if initial_capital else 0,
            })

        # 加上当前净值点
        today_str = date.today().isoformat()
        if not curve or curve[-1]["date"] != today_str:
            current_nav = initial_capital + total_pnl
            if current_nav > max_nav:
                max_nav = current_nav
            dd = (max_nav - current_nav) / max_nav * 100 if max_nav > 0 else 0
            if dd > max_drawdown:
                max_drawdown = round(dd, 2)
            curve.append({
                "date": today_str,
                "daily_pnl": round(unrealized_pnl, 2),
                "cum_nav": round(current_nav, 2),
                "cum_pnl_pct": round(total_pnl_pct, 2),
            })

        return {
            "status": "ok",
            "summary": {
                "total_asset": round(total_asset, 2),
                "available": round(available, 2),
                "market_value": round(market_value, 2),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "total_pnl": total_pnl,
                "total_pnl_pct": total_pnl_pct,
                "initial_capital": initial_capital,
                "trade_count": trade_count,
                "win_count": win_count,
                "win_rate": win_rate,
                "max_drawdown": max_drawdown,
            },
            "positions": pos_list,
            "curve": curve,
        }
    except Exception as ex:
        logger.error("盈亏统计异常: %s", ex, exc_info=True)
        raise HTTPException(status_code=500, detail=str(ex))
