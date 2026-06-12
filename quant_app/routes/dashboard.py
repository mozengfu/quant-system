"""
持仓、回测、追踪相关 API 路由
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException
from fastapi import Request as FastAPIRequest

from quant_app.routes.auth import get_current_user
from quant_app.services.backtest_service import (
    backtest_stock_enhanced,
    backtest_stock_v4,
)
from quant_app.services.market_service import (
    get_stock_realtime,
    get_technical_buy_sell_signals,
)
from quant_app.services.market_state import get_market_state
from quant_app.services.notification_service import send_feishu
from quant_app.utils.authz import require_admin
from quant_app.utils.config import get_db_config
from quant_app.utils.persistence import (
    get_client_ip,
    get_positions_data,
    load_track_data,
    save_access_log,
    update_stock_results,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["dashboard"])

# ========== 绩效看板 ==========


@router.get("/api/sim/nav_history")
def get_nav_history(request: FastAPIRequest, token: str = Cookie(None)):
    """返回净值历史数据"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    nav_path = os.path.join(DATA_DIR, "nav_history.json")
    if os.path.exists(nav_path):
        with open(nav_path) as f:
            return json.load(f)
    return []


@router.get("/api/sim/performance_summary")
def get_performance_summary(request: FastAPIRequest, token: str = Cookie(None)):
    """返回绩效汇总指标"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    nav_path = os.path.join(DATA_DIR, "nav_history.json")
    if not os.path.exists(nav_path):
        return {"error": "No NAV history"}

    with open(nav_path) as f:
        history = json.load(f)

    if not history:
        return {"error": "Empty NAV history"}

    first = history[0]
    last = history[-1]
    total_return = last["profit_pct"]
    trade_count = last.get("trade_count", 0)

    # 年化收益：至少 5 个交易日数据才计算，否则显示 None
    days = (datetime.strptime(last["date"], "%Y-%m-%d") - datetime.strptime(first["date"], "%Y-%m-%d")).days
    if days >= 5 and total_return != 0:
        annual_return = round(((1 + total_return / 100) ** (365 / days) - 1) * 100, 2)
    else:
        annual_return = None

    values = [h["total_value"] for h in history]
    daily_returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))]
    avg_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns) if daily_returns else 0
    std = var**0.5
    sharpe = (avg_ret / std) * (252**0.5) if std > 0 else 0

    monthly = {}
    for h in history:
        month = h["date"][:7]
        if month not in monthly:
            monthly[month] = {"first_value": h["total_value"]}
        monthly[month]["last_value"] = h["total_value"]
    monthly_returns = {}
    for m, v in monthly.items():
        monthly_returns[m] = round((v["last_value"] - v["first_value"]) / v["first_value"] * 100, 2)

    return {
        "total_return": total_return,
        "annual_return": round(annual_return or 0, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown": last.get("max_drawdown", 0),
        "trade_count": trade_count,
        "current_value": last["total_value"],
        "current_cash": last.get("cash", 0),
        "monthly_returns": monthly_returns,
        "nav_dates": [h["date"] for h in history],
        "nav_values": [round(h["total_value"], 2) for h in history],
    }


# ========== 跟单建议 ==========


@router.get("/api/sim/today_signals")
def get_today_signals(token: str = Cookie(None)):
    """返回今日模拟交易实际操作（买入/卖出），供实盘跟单参考"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        import pymysql

        from quant_app.utils.config import get_db_config

        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()

        # 获取市场状态，拿 max_positions
        try:
            ms = get_market_state(db_conn=conn)
            params = ms.get("params", {})
            max_positions = params.get("max_positions", 3)
            market_state_name = ms.get("state_name", "未知")
        except Exception:
            max_positions = 3
            market_state_name = "未知"

        # 查询当前持仓数
        cur.execute("SELECT COUNT(*) FROM sim_positions WHERE status='HOLD'")
        position_count = cur.fetchone()[0]

        # 查询今日模拟交易实际买卖操作
        cur.execute(
            """
            SELECT ts_code, stock_name, action, price, shares,
                   profit_loss, profit_pct, reason, trade_time
            FROM sim_trades
            WHERE trade_date = %s
            ORDER BY trade_time ASC
        """,
            (today,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        actions = []
        for r in rows:
            actions.append(
                {
                    "ts_code": r[0],
                    "name": r[1],
                    "action": r[2],  # BUY / SELL
                    "price": float(r[3]) if r[3] else 0,
                    "shares": int(r[4]) if r[4] else 0,
                    "profit_loss": float(r[5]) if r[5] else 0,
                    "profit_pct": float(r[6]) if r[6] else 0,
                    "reason": r[7] or "",
                    "trade_time": str(r[8]) if r[8] else "",
                }
            )
        return {
            "actions": actions,
            "date": today,
            "position_count": position_count,
            "max_positions": max_positions,
            "market_state_name": market_state_name,
        }
    except Exception as e:
        logger.warning(f"获取今日操作失败: {e}")
        return {"actions": [], "date": today, "error": str(e)}


POSITION_ALERT_STATE = {}
POSITION_ALERT_LOCK = __import__("threading").Lock()


def calculate_atr_for_stock(ts_code):
    """Calculate ATR stop-loss reference for a stock"""
    try:
        import pymysql

        from quant_app.utils.indicators import calculate_atr

        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()
        cur.execute(
            """
            SELECT high, low, close FROM daily_price
            WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 20
        """,
            (ts_code,),
        )
        rows = cur.fetchall()
        conn.close()

        if len(rows) < 15:
            return None, None

        highs = [r[0] for r in rows][::-1]
        lows = [r[1] for r in rows][::-1]
        closes = [r[2] for r in rows][::-1]

        atr_val = calculate_atr(highs, lows, closes, period=14)
        return atr_val, atr_val * 2
    except Exception:
        return None, None


def _get_latest_close_from_db(code, market="SZ"):
    """当东财实时API不可用时，从MySQL daily_price表取最近交易日收盘价"""
    try:
        import pymysql

        mkt = market.upper() if market else "SZ"
        ts_code = f"{code}.{mkt}"
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT close, pct_chg FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1", (ts_code,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return {"现价": float(row[0]), "涨跌幅": float(row[1] or 0)}
    except Exception:
        pass
    return None


# ========== 持仓 ==========


@router.get("/api/positions")
async def get_positions(request: FastAPIRequest, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    require_admin(user)

    import asyncio

    async def fetch_one(pos):
        try:
            loop = asyncio.get_event_loop()
            rt = await loop.run_in_executor(None, get_stock_realtime, pos["代码"], pos["市场"])
            return rt, pos
        except Exception as e:
            logging.getLogger().warning(f"获取 {pos.get('代码', '?')} 实时数据失败: {e}")
            return None, pos

    current_positions = get_positions_data()
    results = await asyncio.gather(*[fetch_one(pos) for pos in current_positions])

    result = []
    alert_msgs = []

    today = datetime.now().strftime("%Y-%m-%d")
    with POSITION_ALERT_LOCK:
        if POSITION_ALERT_STATE.get("_last_reset_date") != today:
            POSITION_ALERT_STATE.clear()
            POSITION_ALERT_STATE["_last_reset_date"] = today

    realtime_failed_count = 0
    db_fallback_count = 0
    for rt, pos in results:
        code = f"{pos['市场'].upper()}{pos['代码']}"
        cost = pos["成本"]
        qty = pos["数量"]

        if rt:
            price = rt["现价"]
        else:
            db_fallback = _get_latest_close_from_db(pos["代码"], pos.get("市场", "SZ"))
            if db_fallback and db_fallback["现价"] > 0:
                price = db_fallback["现价"]
                db_fallback_count += 1
            else:
                price = cost
                realtime_failed_count += 1

        float_pnl = (price - cost) * qty
        pnl_pct = (price - cost) / cost * 100 if cost else 0
        signal = ""

        if rt:
            with POSITION_ALERT_LOCK:
                if code not in POSITION_ALERT_STATE:
                    POSITION_ALERT_STATE[code] = {"stop_loss_notified": False, "stop_profit_notified": False}
                state = POSITION_ALERT_STATE[code]

            if price >= pos["止盈"] and pos["止盈"] > 0:
                signal = "⚠️ 触发止盈"
                if not state["stop_profit_notified"]:
                    alert_msgs.append(f"🎯 {pos.get('名称', pos['代码'])} 触发止盈！现价{price} ≥ 止盈价{pos['止盈']}")
                    state["stop_profit_notified"] = True
            elif price <= pos["止损"]:
                signal = "🚨 触发止损"
                if not state["stop_loss_notified"]:
                    alert_msgs.append(f"🚨 {pos.get('名称', pos['代码'])} 触发止损！现价{price} ≤ 止损价{pos['止损']}")
                    state["stop_loss_notified"] = True
            elif pnl_pct >= 5:
                signal = "✅ 浮动盈利≥5%"
        else:
            signal = "数据异常"

        atr_val, double_atr = None, None
        if rt:
            ts_code = f"{code}.SZ" if code.startswith(("SZ", "sz", "0", "3")) else f"{code}.SH"
            if code.startswith(("SZ", "sz")):
                ts_code = f"{pos['代码']}.SZ"
            elif code.startswith(("SH", "sh")):
                ts_code = f"{pos['代码']}.SH"
            elif code.startswith("0") or code.startswith("3"):
                ts_code = f"{pos['代码']}.SZ"
            elif code.startswith("6"):
                ts_code = f"{pos['代码']}.SH"
            try:
                atr_val, double_atr = calculate_atr_for_stock(ts_code)
            except Exception:
                pass
        atr_stop_loss = round(price - float(double_atr), 2) if atr_val else None

        result.append(
            {
                "代码": f"{pos['市场'].upper()}{pos['代码']}",
                "名称": pos.get("名称") or pos.get("股票名称", ""),
                "数量": qty,
                "成本": cost,
                "现价": price,
                "浮动盈亏": round(float_pnl, 2),
                "盈亏比例": round(pnl_pct, 2),
                "止损价": pos["止损"],
                "止盈价": pos["止盈"] if pos["止盈"] > 0 else None,
                "信号": signal,
                "atr_stop_loss": atr_stop_loss,
                "atr_val": round(atr_val, 3) if atr_val else None,
            }
        )

    if alert_msgs:
        alert_text = "\n".join(alert_msgs)
        try:
            send_feishu(f"⏰ 持仓预警 - {datetime.now().strftime('%H:%M')}\n\n{alert_text}\n\n请登录交易APP及时处理！")
        except Exception:
            pass

    total_cost = sum(pos["成本"] * pos["数量"] for pos in current_positions)
    total_value = sum(r["现价"] * r["数量"] for i, r in enumerate(result))
    total_pnl = total_value - total_cost
    total_pnl_pct = total_pnl / total_cost * 100 if total_cost else 0

    resp = {
        "持仓": result,
        "汇总": {
            "总成本": round(total_cost, 2),
            "总市值": round(total_value, 2),
            "总盈亏": round(total_pnl, 2),
            "盈亏比例": round(total_pnl_pct, 2),
        },
    }
    if db_fallback_count > 0 or realtime_failed_count > 0:
        parts = []
        if db_fallback_count > 0:
            parts.append(f"{db_fallback_count}只使用昨日收盘价（非实时，东财API不可用）")
        if realtime_failed_count > 0:
            parts.append(f"{realtime_failed_count}只无行情数据")
        resp["_note"] = "⚠️ " + "；".join(parts)
    return resp


# ========== 回测 ==========


@router.get("/api/backtest")
def backtest(
    request: FastAPIRequest,
    code: str = "",
    market: str = "sz",
    start: str = "",
    end: str = "",
    token: str = Cookie(None),
):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not code:
        return {"error": "请提供股票代码"}
    start_fmt = start.replace("-", "") if start else ""
    end_fmt = end.replace("-", "") if end else ""
    save_access_log(user, get_client_ip(request), f"回测 {market.upper()}{code}")
    return backtest_stock_v4(code, market, start_fmt, end_fmt)


@router.get("/api/backtest_bottom")
def backtest_bottom_api(
    request: FastAPIRequest,
    code: str = "",
    market: str = "sz",
    start: str = "",
    end: str = "",
    token: str = Cookie(None),
):
    """底部起步条件回测"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"底部起步回测 {code}")
    from quant_app.services.backtest_service import backtest_bottom

    return backtest_bottom(code, market, start.replace("-", ""), end.replace("-", ""))


@router.get("/api/backtest_strong")
def backtest_strong_api(
    request: FastAPIRequest,
    code: str = "",
    market: str = "sz",
    start: str = "",
    end: str = "",
    token: str = Cookie(None),
):
    """强势活跃条件回测"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"强势活跃回测 {code}")
    from quant_app.services.backtest_service import backtest_strong

    return backtest_strong(code, market, start.replace("-", ""), end.replace("-", ""))


@router.get("/api/backtest_combo")
def backtest_combo_api(
    request: FastAPIRequest,
    code: str = "",
    market: str = "sz",
    start: str = "",
    end: str = "",
    token: str = Cookie(None),
):
    """组合策略条件回测"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"组合策略回测 {code}")
    from quant_app.services.backtest_service import backtest_combo

    return backtest_combo(code, market, start.replace("-", ""), end.replace("-", ""))


@router.get("/api/backtest_enhanced")
def backtest_enhanced(
    request: FastAPIRequest,
    code: str = "",
    market: str = "sz",
    start: str = "",
    end: str = "",
    token: str = Cookie(None),
):
    """增强版回测 - MACD+KDJ+布林带+止盈止损+风控指标"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not code:
        return {"error": "请提供股票代码"}
    start_fmt = start.replace("-", "") if start else ""
    end_fmt = end.replace("-", "") if end else ""
    save_access_log(user, get_client_ip(request), f"增强回测 {market.upper()}{code}")
    return backtest_stock_enhanced(code, market, start_fmt, end_fmt)


@router.get("/api/technical_signals")
def technical_signals(request: FastAPIRequest, code: str = "", market: str = "sz", token: str = Cookie(None)):
    """技术面买卖点建议分析"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not code:
        return {"error": "请提供股票代码"}
    save_access_log(user, get_client_ip(request), f"技术面分析 {market.upper()}{code}")
    return get_technical_buy_sell_signals(code, market)


# ========== 追踪统计 ==========


@router.get("/api/track/stats")
async def get_track_stats(token: str = Cookie(None)):
    """获取胜率统计（同步更新后返回最新数据）"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    # 同步更新（update_stock_results 内部有 300 秒冷却，冷却期内只重算不查库）
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_stock_results)
    except Exception as e:
        logger.warning(f"更新追踪结果失败：{e}")

    data = load_track_data()
    return data.get("stats", {})


@router.get("/api/track/history")
def get_track_history(token: str = Cookie(None)):
    """获取追踪历史"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    data = load_track_data()
    return {"recommendations": data["recommendations"][-30:][::-1]}


# ========== 模拟交易 ==========


@router.get("/api/sim_account")
def get_sim_account(request: FastAPIRequest, token: str = Cookie(None)):
    """获取模拟账户状态（收益率、持仓、交易记录）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "模拟账户查询")

    try:
        import decimal
        from datetime import date, datetime

        scripts_dir = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from sim_trading import get_sim_account_info

        data = get_sim_account_info()

        def convert(obj):
            if isinstance(obj, decimal.Decimal):
                return float(obj)
            if isinstance(obj, (date, datetime)):
                return str(obj)
            if isinstance(obj, dict):
                return {k: convert(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [convert(i) for i in obj]
            return obj

        return convert(data)
    except Exception as e:
        import traceback

        logger.error(f"sim_account error: {traceback.format_exc()}")
        return {"error": str(e)}


@router.get("/api/track/curve")
def get_track_curve(token: str = Cookie(None)):
    """获取累计收益曲线 — 基于模拟建仓每日NAV快照"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    import json
    import os
    from pathlib import Path

    nav_path = Path(__file__).resolve().parent.parent.parent / "data" / "nav_history.json"
    fallback_path = (
        Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent.parent / "data"))) / "nav_history.json"
    )

    if not nav_path.exists():
        nav_path = fallback_path

    if not nav_path.exists():
        logger.warning(f"nav_history.json not found at {nav_path} or {fallback_path}")
        return {"curve": [], "summary": {"cum_ret": 0, "win_rate": 0, "total_trades": 0, "max_drawdown": 0}}

    try:
        with open(nav_path) as f:
            snapshots = json.load(f)
    except Exception as e:
        logger.error(f"nav_history.json parse error: {e}")
        return {"curve": [], "summary": {"cum_ret": 0, "win_rate": 0, "total_trades": 0, "max_drawdown": 0}}

    if not snapshots:
        logger.warning(f"nav_history.json is empty array: {nav_path}")
        return {"curve": [], "summary": {"cum_ret": 0, "win_rate": 0, "total_trades": 0, "max_drawdown": 0}}

    curve = []
    initial_capital = 100000.0
    for snap in snapshots:
        total_value = float(snap.get("total_value", 0))
        cum_ret = (total_value / initial_capital - 1) * 100
        curve.append(
            {
                "date": snap.get("date", ""),
                "total_value": round(total_value, 2),
                "cash": round(float(snap.get("cash", 0)), 2),
                "holdings": round(float(snap.get("holdings_value", 0)), 2),
                "cum_ret": round(cum_ret, 2),
            }
        )

    total_value = float(snapshots[-1].get("total_value", 0))
    cum_ret = (total_value / initial_capital - 1) * 100
    trade_count = snapshots[-1].get("trade_count", 0)
    max_dd = float(max(s.get("max_drawdown", 0) for s in snapshots))

    return {
        "source": "sim_account",
        "curve": curve,
        "summary": {
            "cum_ret": round(cum_ret, 2),
            "initial_capital": round(initial_capital, 2),
            "total_trades": trade_count,
            "max_drawdown": round(max_dd, 2),
        },
    }


def _compute_max_drawdown(curve_values):
    """计算最大回撤（%）"""
    if not curve_values:
        return 0
    peak = curve_values[0]
    max_dd = 0
    for v in curve_values:
        if v > peak:
            peak = v
        dd = (peak - v) / (peak + 0.01)  # 避免除零
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)
