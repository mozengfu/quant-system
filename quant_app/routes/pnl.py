"""
实盘盈亏统计 API — 基于 QMT 实时数据 + 每日持仓盯市盈亏
"""
import logging
from collections import defaultdict
from datetime import date, timedelta

from fastapi import APIRouter, Cookie, HTTPException

from quant_app.routes.auth import get_current_user
from quant_app.services.market_attribution import get_trade_market_attribution
from quant_app.trading.config import trading_config
from quant_app.trading.modes.remote_executor import RemoteTraderExecutor
from quant_app.utils.config import get_db_config

UNAUTHORIZED = HTTPException(status_code=401, detail="未登录或会话已过期")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pnl", tags=["pnl"])

_executor: RemoteTraderExecutor = None


def _get_executor() -> RemoteTraderExecutor:
    global _executor
    if _executor is None:
        _executor = RemoteTraderExecutor()
    return _executor


def _auth_guard(token: str) -> str:
    user = get_current_user(token)
    if not user:
        raise UNAUTHORIZED
    return user


def _get_trading_dates(cur):
    """获取所有交易日（排序）"""
    cur.execute("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date")
    return [r[0] for r in cur.fetchall()]


def _get_close_price(cur, ts_code, trade_date):
    """获取某只股票某天的收盘价"""
    cur.execute(
        "SELECT `close` FROM daily_price WHERE ts_code=%s AND trade_date=%s",
        (ts_code, trade_date))
    r = cur.fetchone()
    return float(r[0]) if r else None


@router.get("/summary")
async def pnl_summary(mode: str = "live", token: str = Cookie(None)):
    """
    实盘盈亏汇总 — 每日盯市盈亏（mark-to-market）

    计算逻辑：
      对每一笔持仓（包含已平仓和当前持仓），从买入日到卖出日/今天，
      按每日收盘价计算盯市盈亏，叠加到每日 PnL 中。
      + QMT 实时余额/持仓作为最新锚点。
    """
    _auth_guard(token)
    try:
        import pymysql
        import numpy as np

        e = _get_executor()
        if not e._connected:
            e.prepare()

        # ---- 1. QMT 实时余额 ----
        balance = e.get_balance()
        total_asset = float(balance.total_asset) if balance else 0
        available = float(balance.available) if balance else 0
        market_value = float(balance.market_value) if balance else 0

        # ---- 2. QMT 实时持仓（含当日已清仓的） ----
        # QMT /position 返回全部持仓，vol=0 表示今天已卖出但仍显示盈亏
        positions = e.get_positions() or []
        # 也通过 HTTP 直接获取原始 QMT 持仓（含 profit 字段）
        try:
            import urllib.request, json
            resp = urllib.request.urlopen(
                f"http://{trading_config.remote_trader_host}:{trading_config.remote_trader_port}/position", timeout=5)
            qmt_raw = json.loads(resp.read())
        except Exception:
            qmt_raw = []

        # 当日总盈亏 = QMT 所有股票 profit 字段之和（含已清仓的当日已实现盈亏）
        today_pnl_from_qmt = sum(float(p.get('profit', 0) or 0) for p in qmt_raw)

        # 当日有交易（已清仓股票的 profit 即今日已实现盈亏）
        qmt_holdings = {}  # ts_code -> {profit, shares, cost, price}
        for p in qmt_raw:
            code = p.get('code', '')
            ts_code = f"{code}.{'SH' if code.startswith('6') else 'SZ'}"
            vol = int(p.get('total_volume', 0))
            cost = float(p.get('cost', 0) or 0)
            price = float(p.get('price', 0) or 0)
            # QMT 远程 profit 字段可能为 0, 这里用 (price-cost)*shares 兜底计算
            qmt_profit = float(p.get('profit', 0) or 0)
            computed_profit = (price - cost) * vol if vol > 0 and cost > 0 else 0
            # QMT 报的 profit 为 0 但有价差, 用兜底值
            profit = qmt_profit if qmt_profit != 0 else computed_profit
            qmt_holdings[ts_code] = {
                'profit': profit,
                'shares': vol,
                'cost': cost,
                'price': price,
                'name': p.get('name', ''),
            }

        unrealized_pnl = 0.0
        pos_list = []
        # 只显示有实际持仓的（volume>0）
        for ts_code, info in qmt_holdings.items():
            if info['shares'] <= 0:
                continue
            unrealized_pnl += info['profit']
            pos_list.append({
                "ts_code": ts_code,
                "name": info['name'],
                "quantity": info['shares'],
                "cost_price": round(info['cost'], 3),
                "current_price": round(info['price'], 3),
                "market_value": round(info['price'] * info['shares'], 2),
                "pnl": round(info['profit'], 2),
                "pnl_pct": round((info['price'] - info['cost']) / info['cost'] * 100, 2) if info['cost'] > 0 else 0,
            })

        # ---- 3. 从 MySQL 构建每日盯市盈亏曲线 ----
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()

        # 3a. 所有交易过的股票
        cur.execute("""
            SELECT DISTINCT ts_code FROM sim_signals WHERE status IN ('已执行','已平仓')
            UNION
            SELECT DISTINCT ts_code FROM sim_positions WHERE buy_date IS NOT NULL
        """)
        all_codes = [r[0] for r in cur.fetchall()]
        if not all_codes:
            cur.close(); conn.close()
            return {"status": "ok", "summary": {
                "total_asset": round(total_asset, 2), "available": round(available, 2),
                "market_value": round(market_value, 2), "realized_pnl": 0,
                "unrealized_pnl": round(unrealized_pnl, 2), "total_pnl": 0, "total_pnl_pct": 0,
                "initial_capital": round(total_asset - unrealized_pnl, 2),
                "trade_count": 0, "win_count": 0, "win_rate": 0, "max_drawdown": 0,
            }, "positions": pos_list, "curve": []}

        # 3b. 用 QMT 实时持仓校准当前日期的持仓数据
        qmt_shares = {k: v['shares'] for k, v in qmt_holdings.items() if v['shares'] > 0}

        # 从 sim_signals 取真实的买卖记录（只取已执行的）
        cur.execute("""
            SELECT ts_code, signal_date, signal_type, shares
            FROM sim_signals
            WHERE status IN ('已执行','已平仓')
              AND (signal_type IN ('买入','卖出','止损','止盈','峰值止盈','兜底止盈','超时','强制平仓','移动止盈'))
              AND shares > 0 AND signal_date IS NOT NULL
            ORDER BY signal_date, signal_time
        """)
        raw_signals = cur.fetchall()

        # 构建每日头寸变化
        position_changes = defaultdict(lambda: defaultdict(int))
        for r in raw_signals:
            code, d, st, shares = r
            d_str = str(d)
            if st in ('买入',):
                position_changes[d_str][code] += int(shares)
            elif st in ('卖出','止损','止盈','峰值止盈','兜底止盈','超时','强制平仓'):
                position_changes[d_str][code] -= int(shares)

        # 3c. 获取价格数据并逐日计算盯市盈亏
        code_list = ",".join(f"'{c}'" for c in all_codes)
        cur.execute(f"""
            SELECT ts_code, trade_date, `close`
            FROM daily_price WHERE ts_code IN ({code_list})
            ORDER BY trade_date
        """)
        prices = defaultdict(dict)
        for r in cur.fetchall():
            prices[r[0]][str(r[1])] = float(r[2])

        all_dates = sorted(set(
            list(position_changes.keys()) +
            [str(d) for cp in prices.values() for d in cp.keys()]
        ))
        if not all_dates:
            cur.close(); conn.close()
            return {"status": "ok", "summary": {}, "positions": pos_list, "curve": []}

        # 逐日盯市
        prev_shares = defaultdict(int)
        prev_prices = defaultdict(float)
        daily_pnl = {}
        earliest = min(position_changes.keys()) if position_changes else all_dates[0]
        try:
            si = max(0, all_dates.index(earliest) - 1)
        except ValueError:
            si = 0
        relevant_dates = all_dates[si:]

        holdings_by_date = defaultdict(lambda: defaultdict(int))

        for d in relevant_dates:
            ds = str(d)
            # 当日头寸变化
            if ds in position_changes:
                for code, delta in position_changes[ds].items():
                    prev_shares[code] += delta
                    if prev_shares[code] <= 0:
                        prev_shares[code] = 0
                        prev_prices.pop(code, None)
            # 确保持仓记录
            for code in list(prev_shares.keys()):
                holdings_by_date[ds][code] = prev_shares[code]
            # 最后一天用 QMT 实时数据校准
            if ds == str(date.today()) and qmt_shares:
                for code, qty in qmt_shares.items():
                    if qty > 0:
                        prev_shares[code] = qty
                        holdings_by_date[ds][code] = qty

            # 计算盯市
            day_pnl = 0.0
            for code, held in holdings_by_date[ds].items():
                close = prices.get(code, {}).get(ds)
                if close is None:
                    continue
                pc = prev_prices.get(code)
                if pc is not None and pc > 0:
                    day_pnl += held * (close - pc)
                prev_prices[code] = close

            if day_pnl != 0 or ds in position_changes:
                daily_pnl[ds] = round(day_pnl, 2)

            # QMT broker 当日盈亏
            if ds == str(date.today()) and today_pnl_from_qmt != 0:
                daily_pnl[ds] = round(today_pnl_from_qmt, 2)

        # 3d. 构建净值曲线：用 QMT total_asset 做锚点
        total_mtm_pnl = sum(daily_pnl.values())
        initial_capital = round(total_asset - total_mtm_pnl, 2)
        if initial_capital <= 0:
            initial_capital = total_asset - unrealized_pnl

        sorted_dates = sorted(daily_pnl.keys())
        curve = []
        cum_pnl = 0.0
        cum_nav = initial_capital
        max_nav = initial_capital
        max_drawdown = 0.0

        # 已实现盈亏统计
        realized_pnl = 0.0
        trade_count = 0
        win_count = 0

        # 从 qmt_trades 实盘交易计算已实现盈亏（FIFO）
        try:
            cur.execute("""
                SELECT ts_code, action, price, quantity, amount, trade_date
                FROM qmt_trades WHERE status = 'filled' AND mode = %s ORDER BY trade_date
            """, (mode,))
            db_trades = cur.fetchall()

            all_trades = []
            for t in db_trades:
                all_trades.append((t[0], t[1], float(t[2]), int(t[3]), float(t[4] or 0), str(t[5] or "")))

            all_trades.sort(key=lambda x: x[5])
            fifo = defaultdict(list)
            for code, act, price, qty, amt, td in all_trades:
                if act == "BUY":
                    fifo[code].append({"qty": qty, "cost": price, "date": td})
                elif act == "SELL":
                    sell_qty = qty
                    sell_amt = price * qty
                    cost_amt = 0
                    while sell_qty > 0 and fifo.get(code):
                        lot = fifo[code][0]
                        m = min(sell_qty, lot["qty"])
                        cost_amt += m * lot["cost"]
                        lot["qty"] -= m
                        sell_qty -= m
                        if lot["qty"] <= 0:
                            fifo[code].pop(0)
                    if sell_qty == 0:
                        pnl = round(sell_amt - cost_amt, 2)
                        realized_pnl += pnl
                        trade_count += 1
                        if pnl > 0: win_count += 1
        except Exception:
            pass

        for d_str in sorted_dates:
            cum_pnl += daily_pnl[d_str]
            cum_nav = initial_capital + cum_pnl
            if cum_nav > max_nav:
                max_nav = cum_nav
            dd = (max_nav - cum_nav) / max_nav * 100 if max_nav > 0 else 0
            if dd > max_drawdown:
                max_drawdown = round(dd, 2)
            curve.append({
                "date": d_str,
                "daily_pnl": daily_pnl[d_str],
                "cum_nav": round(cum_nav, 2),
                "cum_pnl_pct": round((cum_nav / initial_capital - 1) * 100, 2) if initial_capital else 0,
            })

        # 确保今天有数据点（可能被交易日起始偏移漏掉）
        today_str = date.today().isoformat()
        if not curve or (curve and curve[-1]["date"] != today_str):
            today_pnl = today_pnl_from_qmt if today_pnl_from_qmt != 0 else (daily_pnl.get(today_str, 0))
            cum_pnl += today_pnl
            current_nav = total_asset
            if current_nav > max_nav:
                max_nav = current_nav
            dd = (max_nav - current_nav) / max_nav * 100 if max_nav > 0 else 0
            if dd > max_drawdown:
                max_drawdown = round(dd, 2)
            curve.append({
                "date": today_str,
                "daily_pnl": round(today_pnl, 2),
                "cum_nav": round(current_nav, 2),
                "cum_pnl_pct": round((current_nav / initial_capital - 1) * 100, 2) if initial_capital else 0,
            })

        total_pnl = round(total_asset - initial_capital, 2)
        total_pnl_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0
        win_rate = round(win_count / trade_count * 100, 1) if trade_count > 0 else 0

        cur.close()
        conn.close()

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
                "initial_capital": round(initial_capital, 2),
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


@router.get("/market_attribution")
async def market_attribution(mode: str = "live", token: str = Cookie(None)):
    """交易市场归因分析 — 按上涨市/震荡市/下跌市统计交易表现"""
    _auth_guard(token)
    try:
        result = get_trade_market_attribution(mode=mode)
        return {"status": "ok", **result}
    except Exception as ex:
        logger.error("市场归因异常: %s", ex, exc_info=True)
        raise HTTPException(status_code=500, detail=str(ex))
