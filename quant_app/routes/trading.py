"""
实盘交易 API 路由 — 通过 RemoteTraderExecutor 调用 Windows VM QMT服务
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from quant_app.trading.modes.remote_executor import RemoteTraderExecutor
from quant_app.trading.trade_recorder import get_trades, record_trade

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
async def api_connect():
    """连接到远程QMT交易服务"""
    try:
        e = get_executor()
        result = e.prepare()
        return {"status": "ok", "message": f"已连接: {result.get('window', '')}"}
    except Exception as ex:
        logger.error("连接失败: %s", ex)
        raise HTTPException(status_code=503, detail=str(ex))


@router.get("/balance")
async def api_balance():
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
async def api_positions():
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
async def api_orders():
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
async def api_buy(req: OrderRequest):
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
async def api_sell(req: OrderRequest):
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
async def api_cancel(order_id: str):
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
async def api_cancel_all():
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
async def api_market_order(req: MarketOrderRequest):
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
async def api_batch_order(req: BatchOrderRequest):
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
async def api_status():
    """查询远程交易服务状态"""
    try:
        e = get_executor()
        connected = e._connected
        detail = "已连接" if connected else "未连接"
        return {"status": "ok", "connected": connected, "detail": detail}
    except Exception as ex:
        return {"status": "error", "connected": False, "detail": str(ex)}


@router.get("/trades")
async def api_trades(limit: int = 50, offset: int = 0, ts_code: str = ""):
    """查询 QMT 实盘成交记录

    数据来源优先级:
    1. QMT 实时成交数据（从 get_trade_detail_data TRADE 读取）
    2. MySQL qmt_trades 表（本地持久化，降级方案）
    """
    try:
        e = get_executor()
        if e._connected:
            qmt_trades = e.get_qmt_trades()
            if qmt_trades:
                # 对数据做归一化
                trades = []
                for t in qmt_trades:
                    code = t.get("code", "")
                    if "." not in code:
                        code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                    bs = t.get("bs_flag", 0)
                    action = "SELL" if bs == 1 else "BUY"
                    trade_time = t.get("time", "")
                    if trade_time and len(trade_time) == 6:
                        trade_time = f"{trade_time[:2]}:{trade_time[2:4]}:{trade_time[4:]}"
                    trades.append({
                        "ts_code": code,
                        "stock_name": t.get("name", ""),
                        "action": action,
                        "price": float(t.get("price", 0)),
                        "quantity": int(t.get("volume", 0)),
                        "amount": float(t.get("amount", 0)),
                        "order_id": t.get("order_id", ""),
                        "trade_time": trade_time,
                        "source": "qmt_live",
                    })
                return {"trades": trades, "total": len(trades), "source": "qmt_live"}
    except Exception:
        logger.warning("QMT 实时成交数据不可用，降级到 MySQL")

    # 降级到本地 qmt_trades 表
    return get_trades(limit=limit, offset=offset, ts_code=ts_code)
