"""
模拟盘执行器 — 包装 sim_trading.py 的 MySQL 逻辑，实现 AbstractTradeExecutor
"""

import logging
from datetime import datetime

import pymysql

from quant_app.trading.executor import AbstractTradeExecutor
from quant_app.trading.orders import Balance, Order, Position
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()


def _get_db_conn():
    return pymysql.connect(**DB_CONFIG)


def _count_trading_days_since(buy_date):
    """统计买入后经过了多少个交易日"""
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_price "
            "WHERE trade_date > %s AND trade_date <= %s",
            (
                buy_date.strftime("%Y%m%d") if hasattr(buy_date, "strftime") else str(buy_date),
                datetime.now().strftime("%Y%m%d"),
            ),
        )
        count = cursor.fetchone()[0] or 0
        cursor.close()
        conn.close()
        return count
    except Exception:
        return (datetime.now().date() - buy_date).days if buy_date else 0


def _get_market_params():
    """获取市场状态参数（动态止盈止损）"""
    try:
        from quant_app.services.market_state import get_market_state

        ms = get_market_state() or {}
        p = ms.get("params", {})
        return {
            "state": ms.get("state", "range"),
            "stop_loss_pct": p.get("stop_loss_pct", -3) / 100,
            "take_profit_pct": p.get("take_profit_pct", 6) / 100,
            "max_positions": p.get("max_positions", 3),
            "ml_threshold": p.get("ml_threshold", 0.55),
        }
    except Exception:
        return {
            "state": "range",
            "stop_loss_pct": -0.03,
            "take_profit_pct": 0.06,
            "max_positions": 3,
            "ml_threshold": 0.55,
        }


def _get_stock_realtime(code: str, market: str = "sz"):
    """用腾讯财经获取实时行情"""
    import urllib.request

    symbol = f"{market}{code}"
    url = "http://qt.gtimg.cn/q=" + symbol
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
        if "~" not in data:
            return None
        parts = data.strip().rstrip(";").split("~")
        if len(parts) < 50:
            return None
        return {
            "名称": parts[1],
            "代码": parts[2],
            "现价": float(parts[3]),
            "昨收": float(parts[4]),
            "涨跌幅": float(parts[32]),
            "成交量": float(parts[6]),
            "成交额": float(parts[37]),
            "换手率": float(parts[38]) if len(parts) > 38 else 0,
        }
    except Exception:
        return None


def _record_signal(
    signal_type, ts_code, stock_name, price, shares, strategy,
    ml_prob, enhanced_score, market_state, reason, status="已执行",
):
    """记录信号到 sim_signals 表"""
    try:
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO sim_signals
               (signal_type, ts_code, stock_name, price, shares, strategy,
                ml_prob, enhanced_score, market_state, reason,
                signal_date, signal_time, status, created_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                signal_type, ts_code, stock_name, price, shares, strategy,
                ml_prob, enhanced_score, market_state, reason,
                datetime.now().strftime("%Y-%m-%d"), datetime.now(), status, datetime.now(),
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.warning("记录信号失败: %s", e)


def _generate_order_id():
    """生成唯一订单ID"""
    import uuid
    return datetime.now().strftime("%Y%m%d%H%M%S") + str(uuid.uuid4())[:4]


class SimExecutor(AbstractTradeExecutor):
    """模拟盘执行器 — 读写 MySQL sim_* 表"""

    def buy(
        self,
        ts_code: str,
        name: str,
        market: str,
        price: float,
        quantity: int,
        strategy: str = None,
        ml_prob: float = None,
        enhanced_score: float = None,
        market_state: str = None,
        reason: str = None,
    ) -> Order | None:
        """执行模拟买入"""
        trade_date = datetime.now().strftime("%Y-%m-%d")
        amount = round(price * quantity, 2)
        commission = max(5.0, amount * 0.00025)

        conn = _get_db_conn()
        cursor = conn.cursor()

        # 获取账户
        cursor.execute("SELECT * FROM sim_account ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            logger.error("模拟账户不存在，请先运行 init")
            cursor.close()
            conn.close()
            return None
        cols = [d[0] for d in cursor.description]
        account = dict(zip(cols, row))

        if float(account["cash"]) < amount + commission:
            logger.warning("资金不足: 需要 %.2f, 可用 %.2f", amount + commission, float(account["cash"]))
            cursor.close()
            conn.close()
            return None

        # 扣减资金
        new_cash = float(account["cash"]) - amount - commission
        cursor.execute("UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s",
                       (new_cash, datetime.now(), account["id"]))

        # 记录交易
        cursor.execute(
            """INSERT INTO sim_trades
               (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
                trade_date, trade_time, reason, created_at)
               VALUES (%s, %s, %s, 'BUY', %s, %s, %s, %s, 0, %s, %s, %s, %s)""",
            (ts_code, name, market, price, quantity, amount, commission,
             trade_date, datetime.now(), reason or "ML策略买入", datetime.now()),
        )

        # 计算止盈止损（动态市场状态参数）
        mp = _get_market_params()
        stop_loss = round(price * (1 + mp["stop_loss_pct"]), 3)
        take_profit = round(price * (1 + mp["take_profit_pct"]), 3)
        total_cost = amount + commission

        cursor.execute(
            """INSERT INTO sim_positions
               (ts_code, stock_name, market, shares, cost_price, total_cost,
                stop_loss, take_profit, buy_date, buy_time, status, updated_at,
                ml_prob, strategy, market_state)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HOLD', %s, %s, %s, %s)""",
            (ts_code, name, market, quantity, price, total_cost,
             stop_loss, take_profit, trade_date, datetime.now(), datetime.now(),
             ml_prob, strategy, market_state),
        )

        # 更新交易计数
        cursor.execute("UPDATE sim_account SET trade_count = trade_count + 1, updated_at = %s WHERE id = %s",
                       (datetime.now(), account["id"]))
        conn.commit()
        cursor.close()
        conn.close()

        # 记录信号
        _record_signal(
            "买入", ts_code, name, price, quantity, strategy or "ML策略",
            ml_prob, enhanced_score, market_state, reason or "ML策略买入", "持仓中",
        )

        order_id = _generate_order_id()
        order = Order(
            order_id=order_id,
            ts_code=ts_code,
            name=name,
            action="BUY",
            price=price,
            quantity=quantity,
            amount=amount,
            status="filled",
            filled_quantity=quantity,
            filled_amount=amount,
            reason=reason or "ML策略买入",
        )
        logger.info("✅ 模拟买入: %s %d股 @ %.2f (金额: %.2f) [ML=%.2f, 策略=%s]",
                    name, quantity, price, amount, ml_prob or 0, strategy or "ML")
        return order

    def sell(
        self,
        position_id: int,
        ts_code: str,
        price: float,
        quantity: int,
        reason: str = None,
    ) -> Order | None:
        """执行模拟卖出（清仓）"""
        trade_date = datetime.now().strftime("%Y-%m-%d")
        conn = _get_db_conn()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT p.*, a.id as account_id, a.cash
               FROM sim_positions p
               JOIN sim_account a ON a.id = (SELECT MAX(id) FROM sim_account)
               WHERE p.id = %s AND p.status = 'HOLD'""",
            (position_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            logger.warning("持仓 %d 不存在或已卖出", position_id)
            return None

        cols = [d[0] for d in cursor.description]
        pos = dict(zip(cols, row))

        shares = int(pos["shares"])
        amount = round(price * shares, 2)
        commission = max(5.0, amount * 0.00025)
        stamp_tax = amount * 0.001
        total_fees = commission + stamp_tax

        sell_amount = amount - total_fees
        pnl = sell_amount - float(pos["total_cost"])
        pnl_pct = pnl / float(pos["total_cost"]) if float(pos["total_cost"]) > 0 else 0

        # 更新账户资金
        new_cash = float(pos["cash"]) + sell_amount
        cursor.execute("UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s",
                       (new_cash, datetime.now(), pos["account_id"]))

        # 记录交易
        cursor.execute(
            """INSERT INTO sim_trades
               (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
                trade_date, trade_time, profit_loss, profit_pct, reason, created_at)
               VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (pos["ts_code"], pos["stock_name"], pos["market"], price, shares, amount,
             commission, stamp_tax, trade_date, datetime.now(), pnl, pnl_pct,
             reason or "止盈/止损", datetime.now()),
        )

        # 更新持仓状态
        cursor.execute(
            """UPDATE sim_positions
               SET status = 'SOLD', sell_date = %s, sell_price = %s,
                   final_pnl = %s, final_pnl_pct = %s, updated_at = %s
               WHERE id = %s""",
            (trade_date, price, pnl, pnl_pct, datetime.now(), position_id),
        )

        # 更新账户统计
        cursor.execute(
            """UPDATE sim_account
               SET trade_count = trade_count + 1,
                   win_count = win_count + CASE WHEN %s > 0 THEN 1 ELSE 0 END,
                   win_rate = CASE WHEN trade_count + 1 > 0
                       THEN (win_count + CASE WHEN %s > 0 THEN 1 ELSE 0 END) / (trade_count + 1)
                       ELSE 0 END,
                   updated_at = %s
               WHERE id = %s""",
            (pnl, pnl, datetime.now(), pos["account_id"]),
        )
        conn.commit()

        # 更新信号记录
        cursor.execute(
            """UPDATE sim_signals SET status='已平仓', close_price=%s, close_date=%s, pnl=%s, pnl_pct=%s
               WHERE ts_code=%s AND status='持仓中' ORDER BY id DESC LIMIT 1""",
            (price, trade_date, pnl, pnl_pct, pos["ts_code"]),
        )
        conn.commit()
        cursor.close()
        conn.close()

        order = Order(
            order_id=_generate_order_id(),
            ts_code=pos["ts_code"],
            name=pos["stock_name"],
            action="SELL",
            price=price,
            quantity=shares,
            amount=amount,
            status="filled",
            filled_quantity=shares,
            filled_amount=amount,
            reason=reason or "止盈/止损",
        )
        logger.info("💰 模拟卖出: %s %d股 @ %.2f 盈亏: %.2f (%.2f%%)",
                    pos["stock_name"], shares, price, pnl, pnl_pct * 100)
        return order

    def partial_sell(
        self,
        position_id: int,
        ts_code: str,
        price: float,
        quantity: int,
        reason: str = None,
    ) -> Order | None:
        """执行模拟部分卖出"""
        trade_date = datetime.now().strftime("%Y-%m-%d")
        conn = _get_db_conn()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT p.*, a.id as account_id, a.cash
               FROM sim_positions p
               JOIN sim_account a ON a.id = (SELECT MAX(id) FROM sim_account)
               WHERE p.id = %s AND p.status = 'HOLD'""",
            (position_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            logger.warning("持仓 %d 不存在或已卖出", position_id)
            return None

        cols = [d[0] for d in cursor.description]
        pos = dict(zip(cols, row))

        total_shares = int(pos["shares"])
        shares_to_sell = min(quantity, total_shares)
        shares_remaining = total_shares - shares_to_sell

        if shares_remaining <= 0:
            cursor.close()
            conn.close()
            return self.sell(position_id, ts_code, price, total_shares, reason)

        amount = round(price * shares_to_sell, 2)
        commission = max(5.0, amount * 0.00025)
        stamp_tax = amount * 0.001
        total_fees = commission + stamp_tax

        sell_amount = amount - total_fees
        cost_of_sold = float(pos["total_cost"]) * (shares_to_sell / total_shares)
        pnl = sell_amount - cost_of_sold

        # 更新账户资金
        new_cash = float(pos["cash"]) + sell_amount
        cursor.execute("UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s",
                       (new_cash, datetime.now(), pos["account_id"]))

        # 更新持仓
        new_total_cost = float(pos["total_cost"]) * (shares_remaining / total_shares)
        cursor.execute("UPDATE sim_positions SET shares = %s, total_cost = %s, updated_at = %s WHERE id = %s",
                       (shares_remaining, new_total_cost, datetime.now(), position_id))

        # 记录交易
        cursor.execute(
            """INSERT INTO sim_trades
               (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
                trade_date, trade_time, profit_loss, profit_pct, reason, created_at)
               VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (pos["ts_code"], pos["stock_name"], pos["market"], price, shares_to_sell, amount,
             commission, stamp_tax, trade_date, datetime.now(),
             pnl, pnl / cost_of_sold if cost_of_sold > 0 else 0,
             reason or "止盈减仓", datetime.now()),
        )
        conn.commit()
        cursor.close()
        conn.close()

        order = Order(
            order_id=_generate_order_id(),
            ts_code=pos["ts_code"],
            name=pos["stock_name"],
            action="SELL",
            price=price,
            quantity=shares_to_sell,
            amount=amount,
            status="filled",
            filled_quantity=shares_to_sell,
            filled_amount=amount,
            reason=reason or "止盈减仓",
        )
        logger.info("💰 模拟减仓: %s %d→%d股 @ %.2f (%s)",
                    pos["stock_name"], total_shares, shares_remaining, price, reason or "止盈减仓")
        return order

    def get_positions(self) -> list[Position]:
        """获取当前模拟持仓"""
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT id, ts_code, stock_name, market, shares, cost_price, total_cost,
                      current_price, market_value, profit_loss, profit_pct,
                      stop_loss, take_profit, buy_date, ml_prob, strategy
               FROM sim_positions
               WHERE status = 'HOLD'""",
        )
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        cursor.close()
        conn.close()

        positions = []
        for row in rows:
            d = dict(zip(cols, row))
            positions.append(Position(
                position_id=int(d["id"]),
                ts_code=d["ts_code"],
                name=d["stock_name"],
                market=d["market"],
                quantity=int(d["shares"]),
                cost_price=float(d["cost_price"]),
                total_cost=float(d["total_cost"]),
                current_price=float(d["current_price"]) if d["current_price"] else 0,
                market_value=float(d["market_value"]) if d["market_value"] else 0,
                pnl=float(d["profit_loss"]) if d["profit_loss"] else 0,
                pnl_pct=float(d["profit_pct"]) if d["profit_pct"] else 0,
                stop_loss=float(d["stop_loss"]) if d["stop_loss"] else 0,
                take_profit=float(d["take_profit"]) if d["take_profit"] else 0,
                buy_date=str(d["buy_date"]) if d["buy_date"] else "",
                ml_prob=float(d["ml_prob"]) if d["ml_prob"] else None,
                strategy=d["strategy"],
            ))
        return positions

    def get_balance(self) -> Balance | None:
        """获取模拟账户资金"""
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sim_account ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        d = dict(zip(cols, row))
        return Balance(
            total_asset=float(d["total_value"]),
            available=float(d["cash"]),
            market_value=float(d["total_value"]) - float(d["cash"]),
            initial_capital=float(d["initial_capital"]),
            profit_loss=float(d["profit_loss"]),
            profit_pct=float(d["profit_pct"]),
            max_drawdown=float(d["max_drawdown"]),
            trade_count=int(d["trade_count"]),
            win_count=int(d["win_count"]),
            win_rate=float(d["win_rate"]) if d["win_rate"] else 0,
        )

    def get_orders(self, status: str = None) -> list[Order]:
        """获取模拟交易记录"""
        conn = _get_db_conn()
        cursor = conn.cursor()
        if status:
            cursor.execute(
                "SELECT * FROM sim_trades WHERE action = %s ORDER BY trade_time DESC LIMIT 50",
                (status,),
            )
        else:
            cursor.execute("SELECT * FROM sim_trades ORDER BY trade_time DESC LIMIT 50")
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        cursor.close()
        conn.close()

        orders = []
        for row in rows:
            d = dict(zip(cols, row))
            orders.append(Order(
                order_id=str(d["id"]),
                ts_code=d["ts_code"],
                name=d["stock_name"],
                action=d["action"],
                price=float(d["price"]),
                quantity=int(d["shares"]),
                amount=float(d["amount"]),
                status="filled",
                filled_quantity=int(d["shares"]),
                filled_amount=float(d["amount"]),
                reason=d["reason"] or "",
            ))
        return orders

    def cancel(self, order_id: str) -> bool:
        """模拟盘不支持撤单"""
        logger.warning("模拟盘不支持撤单操作")
        return False

    def update_account_value(self):
        """更新账户总价值和最大回撤（供外部调用）"""
        account = self.get_balance()
        if not account:
            return
        positions = self.get_positions()
        holding_value = 0

        conn = _get_db_conn()
        cursor = conn.cursor()

        for pos in positions:
            code = pos.ts_code.split(".")[0]
            market = pos.market
            quote = _get_stock_realtime(code, market)
            if quote:
                current_price = quote["现价"]
                market_value = round(current_price * pos.quantity, 2)
                pnl = round(market_value - pos.total_cost, 2)
                pnl_pct = round(pnl / pos.total_cost, 4) if pos.total_cost > 0 else 0
                cursor.execute(
                    """UPDATE sim_positions
                       SET current_price = %s, market_value = %s,
                           profit_loss = %s, profit_pct = %s, updated_at = %s
                       WHERE id = %s""",
                    (current_price, market_value, pnl, pnl_pct, datetime.now(), pos.position_id),
                )
                holding_value += market_value

        total_value = account.available + holding_value
        profit_loss = total_value - account.initial_capital
        profit_pct = profit_loss / account.initial_capital if account.initial_capital > 0 else 0
        peak = max(account.total_asset if hasattr(account, "total_asset") else account.initial_capital, total_value)
        drawdown = (peak - total_value) / peak if peak > 0 else 0

        cursor.execute(
            """UPDATE sim_account
               SET total_value = %s, profit_loss = %s, profit_pct = %s,
                   peak_value = %s, max_drawdown = %s, updated_at = %s
               WHERE id = (SELECT id FROM (SELECT MAX(id) as id FROM sim_account) t)""",
            (total_value, profit_loss, profit_pct, peak, drawdown, datetime.now()),
        )
        conn.commit()
        cursor.close()
        conn.close()

        logger.info("📊 账户净值: %.2f 盈亏: %.2f (%.2f%%) 最大回撤: %.2f%%",
                    total_value, profit_loss, profit_pct * 100, drawdown * 100)

    def refresh_positions_prices(self):
        """刷新所有持仓的现价（不触发交易）"""
        conn = _get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, ts_code, market, shares, total_cost FROM sim_positions WHERE status = 'HOLD'"
        )
        rows = cursor.fetchall()
        if not rows:
            cursor.close()
            conn.close()
            return
        for row in rows:
            pos_id, ts_code, market, shares, total_cost = row
            code = ts_code.split(".")[0]
            quote = _get_stock_realtime(code, market)
            if not quote:
                continue
            price = quote["现价"]
            market_value = round(price * int(shares), 2)
            pnl = round(market_value - float(total_cost), 2)
            pnl_pct = pnl / float(total_cost) if float(total_cost) > 0 else 0
            cursor.execute(
                """UPDATE sim_positions
                   SET current_price = %s, market_value = %s,
                       profit_loss = %s, profit_pct = %s, updated_at = %s
                   WHERE id = %s""",
                (price, market_value, pnl, pnl_pct, datetime.now(), pos_id),
            )
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("✅ 已刷新 %d 只持仓的现价与盈亏数据", len(rows))
