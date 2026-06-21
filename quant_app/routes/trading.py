"""
实盘交易 API 路由 — 通过 RemoteTraderExecutor 调用 Windows VM QMT服务
"""
import logging

from fastapi import APIRouter, Cookie, HTTPException
from fastapi import Request as FastAPIRequest
from pydantic import BaseModel
from quant_app.routes.auth import get_current_user

from quant_app.trading.modes.remote_executor import RemoteTraderExecutor
from quant_app.trading.trade_recorder import get_trades, record_trade

UNAUTHORIZED = HTTPException(status_code=401, detail="未登录或会话已过期")


def _auth_guard(token: str) -> str:
    """返回当前登录用户名；未登录抛 401"""
    user = get_current_user(token)
    if not user:
        raise UNAUTHORIZED
    return user


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trading", tags=["trading"])

# 单例执行器（应用启动后首次调用时创建）
_executor: RemoteTraderExecutor = None


def get_executor() -> RemoteTraderExecutor:
    global _executor
    if _executor is None:
        _executor = RemoteTraderExecutor()
    return _executor


class OrderRequest(BaseModel):
    code: str
    price: float
    amount: int
    name: str = ""


class MarketOrderRequest(BaseModel):
    code: str
    price: float = 0
    amount: int
    name: str = ""
    side: str = "BUY"  # BUY / SELL


class BatchOrderItem(BaseModel):
    code: str
    price: float
    amount: int
    name: str = ""
    side: str = "BUY"


class BatchOrderRequest(BaseModel):
    orders: list[BatchOrderItem]


@router.post("/connect")
async def api_connect(token: str = Cookie(None)):
    user = _auth_guard(token)
    """连接到远程QMT交易服务"""
    try:
        e = get_executor()
        result = e.prepare()
        return {"status": "ok", "message": f"已连接: {result.get('window', '')}"}
    except Exception as ex:
        logger.error("连接失败: %s", ex)
        raise HTTPException(status_code=503, detail=str(ex))


@router.get("/balance")
async def api_balance(token: str = Cookie(None)):
    user = _auth_guard(token)
    """获取账户余额"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        b = e.get_balance()
        if b is None:
            return {"status": "error", "detail": "获取余额失败：远程QMT服务未连接"}
        return {
            "status": "ok",
            "total_asset": b.total_asset,
            "available": b.available,
            "market_value": b.market_value,
        }
    except Exception as ex:
        logger.error("获取余额失败: %s", ex)
        raise HTTPException(status_code=503, detail=str(ex))


@router.get("/positions")
async def api_positions(token: str = Cookie(None)):
    user = _auth_guard(token)
    """获取持仓列表"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        positions = e.get_positions()
        return {
            "status": "ok",
            "count": len(positions),
            "positions": [
                {
                    "ts_code": p.ts_code,
                    "name": p.name,
                    "quantity": p.quantity,
                    "cost_price": p.cost_price,
                    "current_price": p.current_price,
                    "market_value": p.market_value,
                    "pnl": round(getattr(p, 'pnl', 0) or 0, 2),
                    "pnl_pct": round(getattr(p, 'pnl_pct', 0) or 0, 2),
                }
                for p in positions
            ],
        }
    except Exception as ex:
        logger.error("获取持仓失败: %s", ex)
        return {"status": "error", "detail": str(ex), "positions": []}


@router.get("/orders")
async def api_orders(token: str = Cookie(None)):
    user = _auth_guard(token)
    """获取委托列表"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        orders = e.get_orders()
        return {
            "status": "ok",
            "count": len(orders),
            "orders": [
                {
                    "order_id": o.order_id,
                    "ts_code": o.ts_code,
                    "name": o.name,
                    "action": o.action,
                    "price": o.price,
                    "quantity": o.quantity,
                    "amount": o.amount,
                    "status": o.status,
                    "reason": o.reason,
                    "created_at": o.created_at,
                }
                for o in orders
            ],
        }
    except Exception as ex:
        logger.error("获取委托列表失败: %s", ex)
        return {"status": "error", "detail": str(ex), "orders": []}


@router.post("/buy")
async def api_buy(req: OrderRequest, token: str = Cookie(None)):
    user = _auth_guard(token)
    """执行买入"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        order = e.buy(
            ts_code=req.code,
            name=req.name,
            market="",
            price=req.price,
            quantity=req.amount,
        )
        # 记录成交
        if order:
            record_trade(
                ts_code=req.code,
                stock_name=req.name,
                action="BUY",
                price=req.price,
                quantity=req.amount,
                order_id=str(order.order_id) if hasattr(order, 'order_id') else "",
                reason="API买入",
            )
        return {"status": "ok", "order": str(order)}
    except Exception as ex:
        logger.error("买入失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/sell")
async def api_sell(req: OrderRequest, token: str = Cookie(None)):
    user = _auth_guard(token)
    """执行卖出"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        order = e.sell(
            position_id=0,
            ts_code=req.code,
            price=req.price,
            quantity=req.amount,
        )
        # 记录成交
        if order:
            record_trade(
                ts_code=req.code,
                stock_name=req.name,
                action="SELL",
                price=req.price,
                quantity=req.amount,
                order_id=str(order.order_id) if hasattr(order, 'order_id') else "",
                reason="API卖出",
            )
        return {"status": "ok", "order": str(order)}
    except Exception as ex:
        logger.error("卖出失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/cancel/{order_id}")
async def api_cancel(order_id: str, token: str = Cookie(None)):
    user = _auth_guard(token)
    """撤单"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        result = e.cancel(order_id)
        return {"status": "ok" if result else "error", "order_id": order_id}
    except Exception as ex:
        logger.error("撤单失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/cancel-all")
async def api_cancel_all(token: str = Cookie(None)):
    user = _auth_guard(token)
    """批量撤单"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        orders = e.get_orders()
        results = []
        for o in orders:
            if o.status in ("pending", "已报单"):
                ok = e.cancel(o.order_id)
                results.append({"order_id": o.order_id, "ok": ok})
        return {"status": "ok", "count": len(results), "results": results}
    except Exception as ex:
        logger.error("批量撤单失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/market-order")
async def api_market_order(req: MarketOrderRequest, token: str = Cookie(None)):
    user = _auth_guard(token)
    """市价单 — 卖出用 sell_market 确保立即成交，买入用 buy_target_value"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        if req.side.upper() == "BUY":
            # 市价买入：用目标金额模式，让 QMT 自动以对手价成交
            if hasattr(e, "buy_target_value") and req.price and req.price > 0:
                target_amount = req.price * req.amount
                order = e.buy_target_value(ts_code=req.code, target_amount=target_amount, reason="市价买入")
            else:
                order = e.buy(
                    ts_code=req.code, name=req.name or "", market="",
                    price=req.price or 0, quantity=req.amount,
                )
        else:
            # 市价卖出：用 sell_market，QMT 以最优对手价立即成交
            if hasattr(e, "sell_market"):
                order = e.sell_market(
                    position_id=0, ts_code=req.code,
                    price=req.price or 0, quantity=req.amount, reason="市价卖出",
                )
            else:
                order = e.sell(
                    position_id=0, ts_code=req.code,
                    price=req.price or 0, quantity=req.amount,
                )
        return {"status": "ok", "order": str(order)}
    except Exception as ex:
        logger.error("市价单失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.post("/batch-order")
async def api_batch_order(req: BatchOrderRequest, token: str = Cookie(None)):
    user = _auth_guard(token)
    """批量下单"""
    try:
        e = get_executor()
        if not e._connected:
            e.prepare()
        results = []
        for item in req.orders:
            try:
                if item.side.upper() == "BUY":
                    order = e.buy(
                        ts_code=item.code, name=item.name, market="",
                        price=item.price, quantity=item.amount,
                    )
                else:
                    order = e.sell(
                        position_id=0, ts_code=item.code,
                        price=item.price, quantity=item.amount,
                    )
                results.append({"code": item.code, "status": "ok", "order": str(order)})
            except Exception as e:
                results.append({"code": item.code, "status": "error", "detail": str(e)})
        return {"status": "ok", "count": len(results), "results": results}
    except Exception as ex:
        logger.error("批量下单失败: %s", ex)
        raise HTTPException(status_code=500, detail=str(ex))


@router.get("/status")
async def api_status(token: str = Cookie(None)):
    user = _auth_guard(token)
    """查询远程交易服务状态"""
    try:
        e = get_executor()
        connected = e._connected
        detail = "已连接" if connected else "未连接"
        return {"status": "ok", "connected": connected, "detail": detail}
    except Exception as ex:
        return {"status": "error", "connected": False, "detail": str(ex)}


@router.get("/trades")
async def api_trades(limit: int = 50, offset: int = 0, ts_code: str = "", token: str = Cookie(None)):
    user = _auth_guard(token)
    """查询实盘成交记录

    数据来源: MySQL qmt_trades 表 (source of truth)
    QMT daemon 运行时实时写入 MySQL, 收盘后 backfill_from_signals 补全。
    """
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()

        where_parts = ["mode = 'live'", "status = 'filled'"]
        params = []
        if ts_code:
            where_parts.append("ts_code = %s")
            params.append(ts_code)
        where = " AND ".join(where_parts)

        cur.execute(f"SELECT COUNT(*) FROM qmt_trades WHERE {where}", params)
        total = cur.fetchone()[0] or 0

        cur.execute(
            f"""SELECT ts_code, stock_name, action, price, quantity, amount,
                       order_id, trade_time, reason, trade_date
                FROM qmt_trades WHERE {where}
                ORDER BY trade_date DESC, trade_time DESC
                LIMIT %s OFFSET %s""",
            params + [limit, offset]
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        trades = []
        for r in rows:
            action = "卖出" if r[2] == "SELL" else "买入"
            trades.append({
                "ts_code": r[0],
                "stock_name": r[1] or "",
                "action": action,
                "price": float(r[3]) if r[3] else 0,
                "quantity": int(r[4]) if r[4] else 0,
                "amount": float(r[5]) if r[5] else float(r[3] or 0) * int(r[4] or 0),
                "order_id": r[6] or "",
                "trade_time": str(r[7]) if r[7] else "",
                "reason": r[8] or "",
                "trade_date": str(r[9]) if r[9] else "",
                "source": "qmt_live",
            })
        return {"trades": trades, "total": total, "source": "qmt_trades"}
    except Exception as e:
        logger.warning("查询 qmt_trades 失败: %s", e)

    # 最后一层兜底：qmt_trades 表
    return get_trades(limit=limit, offset=offset, ts_code=ts_code)

