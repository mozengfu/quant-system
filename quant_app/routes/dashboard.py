# -*- coding: utf-8 -*-
"""
持仓、回测、追踪相关 API 路由
"""
import os, json, time, logging, sys, math
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Cookie, Request as FastAPIRequest, HTTPException
from fastapi.responses import JSONResponse
from app_core import (
    get_stock_realtime, get_tushare_pro, get_recent_trade_dates,
    get_stock_history_from_db, get_technical_buy_sell_signals,
    backtest_stock_enhanced, backtest_stock, backtest_stock_v4,
    get_current_user, require_auth, get_db_config,
    load_track_data, save_track_data, record_recommendation, update_stock_results,
    calculate_rps, get_latest_rps_from_db,
    send_feishu, save_access_log, get_client_ip,
)
from quant_app.utils.authz import require_admin, is_admin

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["dashboard"])

# ========== 绩效看板 ==========

@router.get("/api/sim/nav_history")
async def get_nav_history(request: FastAPIRequest, token: str = Cookie(None)):
    """返回净值历史数据"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    nav_path = os.path.join(DATA_DIR, "nav_history.json")
    if os.path.exists(nav_path):
        with open(nav_path, 'r') as f:
            return json.load(f)
    return []


@router.get("/api/sim/performance_summary")
async def get_performance_summary(request: FastAPIRequest, token: str = Cookie(None)):
    """返回绩效汇总指标"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    nav_path = os.path.join(DATA_DIR, "nav_history.json")
    if not os.path.exists(nav_path):
        return {"error": "No NAV history"}

    with open(nav_path, 'r') as f:
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
    daily_returns = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
    avg_ret = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns) if daily_returns else 0
    std = var ** 0.5
    sharpe = (avg_ret / std) * (252 ** 0.5) if std > 0 else 0

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
        "annual_return": round(annual_return, 2),
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
async def get_today_signals(token: str = Cookie(None)):
    """返回今日模拟盘买入信号，供实盘跟单参考"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()
        # 查询今日买入信号
        cur.execute("""
            SELECT s.ts_code, s.stock_name, s.price, s.shares, s.strategy,
                   s.ml_prob, s.reason, s.signal_time,
                   t.price as current_price
            FROM sim_signals s
            LEFT JOIN (
                SELECT ts_code, current_price FROM sim_positions WHERE status='HOLD'
            ) t ON s.ts_code = t.ts_code
            WHERE s.signal_type='买入' AND s.signal_date = %s
            ORDER BY s.signal_time DESC
        """, (today,))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        signals = []
        for r in rows:
            buy_price = float(r[2])
            current = float(r[7]) if r[7] else buy_price
            signals.append({
                "ts_code": r[0],
                "name": r[1],
                "buy_price": buy_price,
                "shares": int(r[3]),
                "strategy": r[4] or "",
                "ml_prob": float(r[5]) if r[5] else 0,
                "reason": r[6] or "",
                "signal_time": str(r[7]) if r[7] else "",
                "current_price": current,
                "suggest_buy_upper": round(buy_price * 1.02, 2),  # 建议买入上限（+2%）
                "suggest_stop_loss": round(buy_price * 0.97, 2),  # 止损参考
                "suggest_take_profit_1": round(buy_price * 1.06, 2),  # 止盈一档
                "suggest_take_profit_3": round(buy_price * 1.18, 2),  # 止盈三档
            })
        return {"signals": signals, "date": today}
    except Exception as e:
        logger.warning(f"获取今日信号失败: {e}")
        return {"signals": [], "date": today, "error": str(e)}

POSITION_ALERT_STATE = {}
POSITION_ALERT_LOCK = __import__('threading').Lock()


# ========== 持仓数据（本地辅助函数）==========

def get_positions_data():
    """从MySQL数据库读取持仓数据，返回API期望的中文字段格式
    如果MySQL无数据或失败，则从positions.json读取作为fallback"""
    try:
        import pymysql
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT ts_code, code, name, market, quantity, cost,
                   stop_loss, take_profit, buy_date
            FROM positions
            ORDER BY buy_date DESC
        ''')
        positions = cursor.fetchall()
        conn.close()

        if positions:
            mapped = []
            for pos in positions:
                ts_code, code, name, market, quantity, cost, stop_loss, take_profit, buy_date = pos
                mapped.append({
                    "代码": code,
                    "市场": market,
                    "ts_code": ts_code,
                    "名称": name,
                    "成本": float(cost),
                    "数量": int(quantity),
                    "止盈": float(take_profit),
                    "止损": float(stop_loss),
                    "买日": str(buy_date),
                })
            return mapped

    except Exception as e:
        logging.getLogger().warning(f"读取MySQL持仓失败: {e}")

    # Fallback: 从positions.json读取
    try:
        positions_file = DATA_DIR / "positions.json"
        if positions_file.exists():
            with open(positions_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            positions_list = data.get("positions", [])
            mapped = []
            for pos in positions_list:
                code = pos.get("code", "")
                market = pos.get("market", "sz")
                mapped.append({
                    "代码": code,
                    "市场": market,
                    "ts_code": f"{code}.{'SZ' if market == 'sz' else 'SH'}",
                    "名称": pos.get("name", ""),
                    "成本": float(pos.get("cost", 0)),
                    "数量": int(pos.get("shares", 0)),
                    "止盈": float(pos.get("take_profit", 0)),
                    "止损": float(pos.get("stop_loss", 0)),
                    "买日": pos.get("buy_date", ""),
                })
            if mapped:
                logging.getLogger().info(f"从positions.json读取到{len(mapped)}条持仓")
                return mapped
    except Exception as e:
        logging.getLogger().warning(f"读取positions.json失败: {e}")

    return []


def calculate_atr_for_stock(ts_code):
    """Calculate ATR stop-loss reference for a stock"""
    try:
        import pymysql
        from app_core import calculate_atr
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()
        cur.execute("""
            SELECT high, low, close FROM daily_price
            WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 20
        """, (ts_code,))
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


def _get_latest_close_from_db(code, market='SZ'):
    """当东财实时API不可用时，从MySQL daily_price表取最近交易日收盘价"""
    try:
        import pymysql
        mkt = market.upper() if market else 'SZ'
        ts_code = f"{code}.{mkt}"
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT close, pct_chg FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
            (ts_code,)
        )
        row = cursor.fetchone()
        cursor.close(); conn.close()
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
            logging.getLogger().warning(f"获取 {pos.get('代码','?')} 实时数据失败: {e}")
            return None, pos

    current_positions = get_positions_data()
    results = await asyncio.gather(*[fetch_one(pos) for pos in current_positions])

    result = []
    alert_msgs = []

    today = datetime.now().strftime('%Y-%m-%d')
    with POSITION_ALERT_LOCK:
        if POSITION_ALERT_STATE.get('_last_reset_date') != today:
            POSITION_ALERT_STATE.clear()
            POSITION_ALERT_STATE['_last_reset_date'] = today

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

        result.append({
            "代码": f"{pos['市场'].upper()}{pos['代码']}", "名称": pos.get("名称") or pos.get("股票名称", ""),
            "数量": qty, "成本": cost, "现价": price,
            "浮动盈亏": round(float_pnl, 2), "盈亏比例": round(pnl_pct, 2),
            "止损价": pos["止损"], "止盈价": pos["止盈"] if pos["止盈"] > 0 else None,
            "信号": signal,
            "atr_stop_loss": atr_stop_loss, "atr_val": round(atr_val, 3) if atr_val else None
        })

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

    resp = {"持仓": result, "汇总": {"总成本": round(total_cost, 2), "总市值": round(total_value, 2), "总盈亏": round(total_pnl, 2), "盈亏比例": round(total_pnl_pct, 2)}}
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
async def backtest(request: FastAPIRequest, code: str = "", market: str = "sz", start: str = "", end: str = "", token: str = Cookie(None)):
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
async def backtest_bottom_api(request: FastAPIRequest, code: str = "", market: str = "sz", start: str = "", end: str = "", token: str = Cookie(None)):
    """底部起步条件回测"""
    user = get_current_user(token)
    if not user: raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"底部起步回测 {code}")
    from quant_app.services.backtest_service import backtest_bottom
    return backtest_bottom(code, market, start.replace("-",""), end.replace("-",""))


@router.get("/api/backtest_strong")
async def backtest_strong_api(request: FastAPIRequest, code: str = "", market: str = "sz", start: str = "", end: str = "", token: str = Cookie(None)):
    """强势活跃条件回测"""
    user = get_current_user(token)
    if not user: raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"强势活跃回测 {code}")
    from quant_app.services.backtest_service import backtest_strong
    return backtest_strong(code, market, start.replace("-",""), end.replace("-",""))


@router.get("/api/backtest_combo")
async def backtest_combo_api(request: FastAPIRequest, code: str = "", market: str = "sz", start: str = "", end: str = "", token: str = Cookie(None)):
    """组合策略条件回测"""
    user = get_current_user(token)
    if not user: raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"组合策略回测 {code}")
    from quant_app.services.backtest_service import backtest_combo
    return backtest_combo(code, market, start.replace("-",""), end.replace("-",""))


@router.get("/api/backtest_enhanced")
async def backtest_enhanced(request: FastAPIRequest, code: str = "", market: str = "sz", start: str = "", end: str = "", token: str = Cookie(None)):
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
async def technical_signals(request: FastAPIRequest, code: str = "", market: str = "sz", token: str = Cookie(None)):
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
    """获取胜率统计（异步更新，不阻塞响应）"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    data = load_track_data()
    stats = data.get("stats", {})

    import asyncio
    asyncio.create_task(_async_update_track_results())

    return stats


async def _async_update_track_results():
    """异步更新追踪结果"""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_stock_results)
    except Exception as e:
        logger.warning(f"异步更新追踪结果失败：{e}")


@router.get("/api/track/history")
async def get_track_history(token: str = Cookie(None)):
    """获取追踪历史"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    data = load_track_data()
    return {"recommendations": data["recommendations"][-30:]}


# ========== 模拟交易 ==========

@router.get("/api/sim_account")
async def get_sim_account(request: FastAPIRequest, token: str = Cookie(None)):
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
