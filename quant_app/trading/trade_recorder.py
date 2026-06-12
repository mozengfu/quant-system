"""
实盘成交记录器 — 将 QMT 每笔成交写入 qmt_trades 表
"""
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


def record_trade(
    ts_code: str,
    stock_name: str,
    action: str,
    price: float,
    quantity: int,
    order_id: str = "",
    signal_id: str = "",
    reason: str = "",
    trade_date: date = None,
):
    """记录一笔 QMT 实盘成交到 qmt_trades 表

    在每次 buy/sell 成功返回后调用。
    """
    try:
        import pymysql

        from quant_app.utils.config import get_db_config

        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()

        amount = round(price * quantity, 2)
        trade_date = trade_date or date.today()
        now = datetime.now()

        cur.execute(
            """INSERT INTO qmt_trades
               (ts_code, stock_name, action, price, quantity, amount,
                order_id, signal_id, trade_date, trade_time, reason, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'filled', %s)""",
            (
                ts_code,
                stock_name or "",
                action,
                price,
                quantity,
                amount,
                order_id or "",
                signal_id or "",
                trade_date,
                now,
                reason or "",
                now,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ 实盘交易已记录: %s %s %s %d@%.2f", action, ts_code, stock_name, quantity, price)
        return True
    except Exception as e:
        logger.error("❌ 记录实盘交易失败: %s", e)
        return False


def get_trades(limit: int = 50, offset: int = 0, ts_code: str = ""):
    """查询 qmt_trades 记录"""
    try:
        import pymysql

        from quant_app.utils.config import get_db_config

        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()

        where = ""
        params = []
        if ts_code:
            where = "WHERE ts_code = %s"
            params.append(ts_code)

        cur.execute(
            f"SELECT COUNT(*) FROM qmt_trades {where}", params
        )
        total = cur.fetchone()[0] or 0

        cur.execute(
            f"""SELECT id, ts_code, stock_name, action, price, quantity, amount,
                       order_id, signal_id, trade_date, trade_time, commission,
                       status, reason
                FROM qmt_trades {where}
                ORDER BY trade_time DESC LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        trades = []
        for r in rows:
            reason = r[13] or ""
            trades.append({
                "id": r[0],
                "ts_code": r[1],
                "stock_name": r[2],
                "action": r[3],
                "price": float(r[4]),
                "quantity": r[5],
                "amount": float(r[6]),
                "order_id": r[7],
                "signal_id": r[8],
                "trade_date": str(r[9]),
                "trade_time": str(r[10]),
                "commission": float(r[11]) if r[11] else 0,
                "status": r[12],
                "reason": reason,
                "source": "qmt_live" if reason == "QMT策略" else "mysql",
            })
        return {"trades": trades, "total": total}
    except Exception as e:
        logger.error("查询实盘交易失败: %s", e)
        return {"trades": [], "total": 0}


def backfill_from_signals():
    """从 sim_signals 回填历史已执行交易到 qmt_trades"""
    try:
        import pymysql

        from quant_app.utils.config import get_db_config

        db = get_db_config()
        conn = pymysql.connect(**db)
        cur = conn.cursor()

        # 获取还未导入的已执行信号
        cur.execute("""
            SELECT s.ts_code, s.stock_name, s.signal_type, s.price, COALESCE(s.shares, s.qty, 0),
                   s.reason, s.signal_date, s.id, s.status
            FROM sim_signals s
            LEFT JOIN qmt_trades t ON t.signal_id = CAST(s.id AS CHAR)
            WHERE s.status IN ('已提交', '已执行')
              AND t.id IS NULL
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        imported = 0
        for r in rows:
            ts_code, name, stype, price, shares, reason, sig_date, sid, status = r
            action = "BUY" if stype in ("买入候选", "买入", "BUY") else "SELL"
            record_trade(
                ts_code=ts_code or "",
                stock_name=name or "",
                action=action,
                price=float(price or 0),
                quantity=int(shares or 0),
                signal_id=str(sid),
                reason=reason or f"回填-{status}",
                trade_date=sig_date,
            )
            imported += 1

        if imported:
            logger.info("回填完成: %d 条历史交易", imported)
        return imported
    except Exception as e:
        logger.error("回填历史交易失败: %s", e)
        return 0
