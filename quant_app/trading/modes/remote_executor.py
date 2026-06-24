"""
远程交易执行器 — 调用 Windows VM QMT 交易 API

数据源说明:
  - get_positions() → QMT 实时持仓 → 本地跟踪
  - get_balance()   → QMT 实时余额缓存 → MySQL 降级

使用方法:
  executor = RemoteTraderExecutor()
  executor.prepare()               # 连接 + 自动解锁
  executor.keepalive()             # 保活（可定时调用）
  executor.buy("000559.SZ", ...)  # 买入
  executor.get_balance()          # 查余额（实盘）
  executor.get_positions()        # 查持仓（优先实盘，降级本地跟踪）
"""

import logging
import time

import requests

from quant_app.trading.config import trading_config
from quant_app.trading.executor import AbstractTradeExecutor
from quant_app.trading.orders import Balance, Order, Position

logger = logging.getLogger(__name__)

_MARKET_PREFIX = {"sz": "0.", "sh": "1."}

def _ts_code_to_remote(code: str) -> str:
    """将 ts_code (如 '000559.SZ') 转为不带市场后缀的代码"""
    return code.split(".")[0]


def _get_tencent_quote(code: str, market: str = "sz") -> dict | None:
    """用腾讯财经获取实时行情（与 sim_executor 一致）"""
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
            "name": parts[1],
            "code": parts[2],
            "price": float(parts[3]),
            "close": float(parts[4]),
            "chg_pct": float(parts[32]),
            "volume": float(parts[6]),
            "amount": float(parts[37]),
        }
    except Exception:
        return None


def _resolve_market(ts_code: str) -> str:
    """根据 ts_code 判断市场 sz/sh"""
    if ts_code.endswith(".SH"):
        return "sh"
    return "sz"


def _ts_code_add_suffix(code: str) -> str:
    """给不带后缀的代码补上 .SH/.SZ"""
    if not code or "." in code:
        return code
    if code.startswith("6") or code.startswith("5"):
        return f"{code}.SH"
    return f"{code}.SZ"


def _remote_position_to_order(p: dict) -> Position | None:
    """将持仓 dict 转为 Position 对象"""
    try:
        # 兼容多种字段名格式
        ts_code = _ts_code_add_suffix(p.get("stock_code", "") or p.get("code", "") or p.get("ts_code", ""))
        if not ts_code:
            return None
        market = _resolve_market(ts_code)

        cost_price = float(p.get("cost_price", 0) or p.get("cost", 0) or 0)
        quantity = int(p.get("current_amount", 0) or p.get("amount", 0) or p.get("shares", 0) or p.get("total_volume", 0) or p.get("volume", 0) or 0)
        price = float(p.get("current_price", 0) or p.get("price", 0) or 0)
        name = p.get("stock_name", "") or p.get("name", "") or ""
        total_cost = round(cost_price * quantity, 2)

        # 优先用 API 返回的 market_value，缺省才计算
        raw_mv = p.get("market_value", p.get("profit_loss", 0))
        if raw_mv:
            market_value = round(float(raw_mv), 2)
            # 如果 current_price 为 0 但 market_value 有值，反推价格
            if price == 0 and quantity > 0:
                price = round(market_value / quantity, 2)
        else:
            market_value = round(price * quantity, 2)

        pnl = round(market_value - total_cost, 2)
        pnl_pct = round(pnl / total_cost * 100, 2) if total_cost > 0 else 0.0

        return Position(
            ts_code=ts_code,
            name=name,
            market=market,
            quantity=quantity,
            cost_price=cost_price,
            total_cost=total_cost,
            current_price=price,
            market_value=market_value,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )
    except Exception as e:
        logger.warning("解析持仓记录失败: %s, data=%s", e, p)
        return None


class RemoteTraderExecutor(AbstractTradeExecutor):
    """远程执行器 — 调用 Windows VM 上的 QMT 交易服务"""

    def __init__(self, host: str = None, port: int = None, auto_prepare: bool = True):
        host = host or trading_config.remote_trader_host or "192.168.10.25"
        port = port or trading_config.remote_trader_port or 1430
        self.base_url = f"http://{host}:{port}"
        self._session = requests.Session()
        self._session.timeout = (10, 30)
        self._connected = False
        self._password = trading_config.trade_password
        self._is_remote = True
        # 自动连接 + 解锁
        if auto_prepare:
            try:
                self.prepare()
            except Exception as e:
                logger.warning("自动连接/解锁失败: %s", e)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(method, url, json=kwargs.pop("json", None), **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError:
            logger.error("❌ 无法连接到远程交易服务 %s", self.base_url)
            raise
        except requests.Timeout:
            logger.error("⏱ 请求超时 %s %s", method, path)
            return {"e": "timeout"}
        except Exception as e:
            logger.error("请求 %s %s 失败: %s", method, path, e)
            return {"e": str(e)[:200]}

    def ping(self) -> dict:
        """健康检查 — 通过 /status 或降级到 /balance 检测连通性"""
        try:
            status = self._request("GET", "/status")
            if status.get("ok"):
                return {"status": "ok", "message": f"已连接: {status.get('title', '')}", "data": status}
            # /status 不存在时降级用 /balance 验证连通性
            if status.get("e"):
                balance = self._request("GET", "/balance")
                if not balance.get("e"):
                    return {"status": "ok", "message": "服务正常 (降级检测)", "data": balance}
            return {"status": "error", "message": "服务状态异常", "data": status}
        except requests.ConnectionError:
            return {"status": "error", "message": f"无法连接 {self.base_url}"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:200]}

    def prepare(self) -> dict:
        """连接服务 + 自动解锁

        状态检查降级策略:
        1. 先尝试 /status 端点
        2. 404/不可用时以 /unlock 和 /balance 结果判断连通性
        """
        result = {}
        self._connected = True

        # 检查状态 — 404 不影响连通性判定
        try:
            status = self._request("GET", "/status")
            if status.get("ok"):
                logger.info("✅ 已连接远程交易服务: %s", status.get("title", ""))
                result["status"] = "connected"
            elif status.get("e") and "404" in str(status.get("e", "")):
                logger.info("⚠️ /status 端点不存在，将通过解锁和余额验证连通性")
                result["status"] = "connected"  # 不因缺少 /status 报错
            else:
                logger.warning("远程服务状态异常: %s", status)
                result["status"] = "warning"
        except Exception as e:
            logger.warning("状态检查失败: %s", e)

        # 自动解锁 — 成功解锁即证明连通正常
        if self._password:
            try:
                unlock = self._request("POST", "/unlock", json={"password": self._password})
                if unlock.get("ok"):
                    logger.info("🔓 交易系统已自动解锁")
                    if result.get("status") != "connected":
                        result["status"] = "connected"
                elif unlock.get("still"):
                    logger.warning("🔒 交易系统仍锁定，解锁结果: %s", unlock)
                result["unlock"] = unlock
            except Exception as e:
                logger.warning("自动解锁失败: %s", e)
                result["unlock_error"] = str(e)[:100]
        else:
            logger.warning("未配置交易密码，无法自动解锁")

        return result

    def keepalive(self) -> bool:
        """保活 — 防止交易系统锁屏"""
        try:
            result = self._request("GET", "/keepalive")
            return result.get("ok", False)
        except Exception:
            return False

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
        code = _ts_code_to_remote(ts_code)
        result = self._request("POST", "/buy", json={
            "code": code, "price": price, "amount": quantity,
            "priceType": -1,   # 市价单标识（HTTP服务→IPC→QMT策略最终用11兜底）
        })
        logger.info("买入 %s(%s) %d股 @ %.2f: %s", name, code, quantity, price, result)

        if result.get("e"):
            logger.error("买入失败: %s", result["e"])
            return None

        return Order(
            order_id=result.get("order_id", f"remote_{int(time.time())}"),
            ts_code=ts_code, name=name, action="BUY",
            price=price, quantity=quantity, amount=round(price * quantity, 2),
            status="pending",
            filled_quantity=0, filled_amount=0.0,
            reason=reason or result.get("msg", ""),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _parse_order_status(self, result: dict, ts_code: str, action: str,
                            price: float, quantity: int, reason: str = "") -> Order | None:
        """解析 QMT v23 增强订单状态: submitted/filled/partial/rejected/canceled"""
        raw_status = result.get("status", "pending")
        status_map = {
            "submitted": "pending",   # 已提交，等待成交
            "filled": "filled",       # 全部成交
            "partial": "partial",     # 部分成交
            "rejected": "rejected",   # 废单/拒绝
            "canceled": "canceled",   # 已撤单
            "done": "filled",         # v22 兼容
            "failed": "rejected",     # v22 兼容
        }
        mapped_status = status_map.get(raw_status, raw_status)
        filled_qty = result.get("filled_volume", 0)
        if mapped_status == "filled":
            filled_qty = quantity
        elif mapped_status == "partial":
            filled_qty = result.get("filled_volume", 0)

        if mapped_status == "rejected":
            logger.error("%s 被拒绝: %s %s %d股", action, ts_code, result.get("error", ""), quantity)
            return None

        return Order(
            order_id=result.get("order_id", f"remote_{int(time.time())}"),
            ts_code=ts_code, name="", action=action,
            price=price, quantity=quantity, amount=round(price * quantity, 2),
            status=mapped_status,
            filled_quantity=filled_qty,
            filled_amount=round(price * filled_qty, 2),
            reason=result.get("error") or reason or result.get("msg", ""),
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def sell(
        self, position_id: int, ts_code: str, price: float,
        quantity: int, reason: str = None,
    ) -> Order | None:
        code = _ts_code_to_remote(ts_code)
        result = self._request("POST", "/sell", json={
            "code": code, "price": price, "amount": quantity,
            "priceType": -1,   # 市价单标识（HTTP服务转action → IPC → QMT策略用11兜底）
        })
        logger.info("卖出 %s %d股 @ %.2f: %s", code, quantity, price, result)

        if result.get("e"):
            logger.error("卖出失败: %s", result["e"])
            return None

        return self._parse_order_status(result, ts_code, "SELL", price, quantity, reason or result.get("msg", ""))

    def sell_market(
        self, position_id: int, ts_code: str, price: float,
        quantity: int, reason: str = None,
    ) -> Order | None:
        """市价卖出 — 止损/清仓场景，确保立即成交"""
        code = _ts_code_to_remote(ts_code)
        result = self._request("POST", "/sell", json={
            "code": code, "price": price, "amount": quantity,
            "priceType": -1,   # 市价单标识（HTTP服务转action → IPC → QMT策略用11兜底）
        })
        logger.info("市价卖出 %s %d股: %s", code, quantity, result)

        if result.get("e"):
            logger.error("市价卖出失败: %s", result["e"])
            return None

        return self._parse_order_status(result, ts_code, "SELL", price, quantity, reason or "市价卖出")

    def buy_target_value(
        self, ts_code: str, target_amount: float, reason: str = None,
    ) -> Order | None:
        """按目标金额买入 — QMT order_target_value，自动计算股数"""
        code = _ts_code_to_remote(ts_code)
        result = self._request("POST", "/buy", json={
            "code": code, "price": target_amount, "amount": 0,
            "action": "BUY_TARGET",
        })
        logger.info("目标金额买入 %s ￥%.0f: %s", code, target_amount, result)

        if result.get("e"):
            logger.error("目标金额买入失败: %s", result["e"])
            return None

        return self._parse_order_status(result, ts_code, "BUY", 0, 0, reason or "目标金额买入")

    def partial_sell(self, position_id: int, ts_code: str, price: float,
                     quantity: int, reason: str = None) -> Order | None:
        return self.sell(position_id, ts_code, price, quantity, reason or "减仓")

    def get_positions(self) -> list[Position]:
        """获取持仓 — 两级降级: 实盘 → 本地跟踪

        1. 先尝试 QMT /position（实时数据）
        2. 失败则降级到 /positions（MySQL sim_positions 本地跟踪）
        """
        real = self.get_real_positions()
        if real is not None:
            return real or []

        logger.info("/position 不可用，降级到本地跟踪数据 /positions")
        return self.get_tracked_positions()

    def get_real_positions(self) -> list[Position] | None:
        """获取实盘持仓 — 优先/position → 降级/positions(MySQL)"""
        for endpoint in ("/position", "/positions"):
            try:
                data = self._request("GET", endpoint)
                if isinstance(data, dict) and data.get("e"):
                    continue
                # 正确处理两种响应格式: {"positions": [...]} 或 [...]
                if isinstance(data, dict):
                    raw_list = data.get("positions", data)
                    # 防御: data.get("positions", data) 可能返回 dict 自身
                    if isinstance(raw_list, dict):
                        raw_list = raw_list.get("positions", [])
                elif isinstance(data, list):
                    raw_list = data
                else:
                    continue
                if not isinstance(raw_list, list):
                    continue
                positions = []
                for p in raw_list:
                    try:
                        pos = _remote_position_to_order(p)
                        if pos and pos.quantity > 0:
                            positions.append(pos)
                    except Exception as parse_err:
                        logger.warning("解析单条持仓记录失败: %s, data=%s", parse_err, p)
                        continue
                if positions or isinstance(data, list):
                    logger.info("实盘持仓 %d 只(来源:%s), 总市值 %.2f", len(positions),
                                endpoint, sum(p.market_value for p in positions))
                    return positions
            except requests.ConnectionError:
                continue
            except Exception as e:
                logger.warning("获取实盘持仓(%s)失败: %s", endpoint, e)
                continue
        logger.warning("所有持仓数据源均不可用")
        return None

    def get_tracked_positions(self) -> list[Position]:
        """从 MySQL sim_positions 获取本地跟踪持仓（HOLD 记录）"""
        try:
            import pymysql

            from quant_app.utils.config import get_db_config
            db = get_db_config()
            conn = pymysql.connect(**db)
            cur = conn.cursor()
            cur.execute("""SELECT id, ts_code, stock_name, market, shares, cost_price,
                                   current_price, stop_loss, take_profit, buy_date,
                                   ml_prob, strategy
                            FROM sim_positions WHERE status='HOLD'""")
            rows = cur.fetchall()
            cur.close()
            conn.close()

            from concurrent.futures import ThreadPoolExecutor, as_completed

            # 批量并行拉腾讯行情，避免 N 只持仓串行 HTTP
            quote_futures = {}
            with ThreadPoolExecutor(max_workers=8) as pool:
                for row in rows:
                    (pid, ts_code, name, market, qty, cost, cur_price,
                     stop_loss, take_profit, buy_date, ml_prob, strategy) = row

                    ts_code = _ts_code_add_suffix(ts_code) if '.' not in ts_code else ts_code
                    market = _resolve_market(ts_code)
                    code_only = ts_code.split('.')[0]

                    future = pool.submit(_get_tencent_quote, code_only, market)
                    quote_futures[future] = (pid, ts_code, name, market, qty, cost, cur_price,
                                              stop_loss, take_profit, buy_date, ml_prob, strategy, code_only)

            positions = []
            for future in as_completed(quote_futures):
                (pid, ts_code, name, market, qty, cost, cur_price,
                 stop_loss, take_profit, buy_date, ml_prob, strategy, code_only) = quote_futures[future]

                quote = future.result()
                price = quote["price"] if quote else float(cur_price or 0)
                name = name or (quote["name"] if quote else "")

                quantity = int(qty or 0)
                cost_price = float(cost or 0)
                total_cost = round(cost_price * quantity, 2)
                market_value = round(price * quantity, 2)
                pnl = round(market_value - total_cost, 2)
                pnl_pct = round(pnl / total_cost * 100, 2) if total_cost > 0 else 0.0

                positions.append(Position(
                    position_id=int(pid or 0),
                    ts_code=ts_code,
                    name=name,
                    market=market,
                    quantity=quantity,
                    cost_price=cost_price,
                    total_cost=total_cost,
                    current_price=price,
                    market_value=market_value,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    stop_loss=float(stop_loss or cost_price * 0.93),
                    take_profit=float(take_profit or 0),
                    buy_date=str(buy_date or ''),
                    ml_prob=float(ml_prob) if ml_prob else None,
                    strategy=str(strategy or '') if strategy else None,
                ))

            logger.info("本地跟踪持仓 %d 只, 总市值 %.2f", len(positions),
                        sum(p.market_value for p in positions))
            return positions
        except Exception as e:
            logger.error("获取本地跟踪持仓失败: %s", e)
            return []

    def get_balance(self) -> Balance | None:
        """从远程服务获取账户资金

        返回 None 表示完全无法获取；返回 Balance 但 available=0 时调用方需自行判断。
        """
        try:
            data = self._request("GET", "/balance")
            if data.get("e"):
                logger.error("获取余额失败: %s", data["e"])
                return None
            # 尝试多种字段名（兼容不同服务端格式）
            available = 0
            market_value = 0
            for key in ["可用金额", "可用", "available", "cash"]:
                if key in data:
                    try:
                        available = float(data[key])
                        break
                    except (ValueError, TypeError):
                        pass
            for key in ["股票市值", "市值", "market_value", "market"]:
                if key in data:
                    try:
                        market_value = float(data[key])
                        break
                    except (ValueError, TypeError):
                        pass
            # 优先从响应中读取总资产
            total_asset = 0
            for key in ["总资产", "total_asset", "total"]:
                if key in data:
                    try:
                        total_asset = float(data[key])
                        break
                    except (ValueError, TypeError):
                        pass
            if total_asset == 0:
                total_asset = available + market_value
            # 备选: 如果 available=0 但 total_asset>0, 用总资产减去市值估算
            if available == 0 and total_asset > 0:
                available = total_asset - market_value
                logger.info("余额解析: 可用金额推算为 %.2f (总资产%.2f - 市值%.2f)",
                           available, total_asset, market_value)
            # 如果余额仍为空，尝试用本地跟踪持仓估算市值
            if available == 0 and market_value == 0:
                logger.warning("余额接口返回空，尝试从本地持仓估算")
                try:
                    pos_data = self._request("GET", "/positions")
                    if isinstance(pos_data, dict):
                        positions = pos_data.get("positions", [])
                    elif isinstance(pos_data, list):
                        positions = pos_data
                    else:
                        positions = []
                    market_value = sum(
                        float(p.get("market_value", float(p.get("cost_price", 0)) * int(p.get("shares", 0))))
                        for p in positions if isinstance(p, dict)
                    )
                except Exception:
                    pass
            return Balance(
                total_asset=total_asset,
                available=available,
                market_value=market_value,
            )
        except Exception as e:
            logger.error("获取远程余额失败: %s", e)
            return None

    def get_qmt_trades(self) -> list[dict]:
        """获取 QMT 实际成交记录（从 TRADE 缓存读取）"""
        try:
            data = self._request("GET", "/trades")
            if isinstance(data, dict) and data.get("e"):
                return []
            return data.get("trades", []) if isinstance(data, dict) else []
        except Exception as e:
            logger.error("获取 QMT 成交记录失败: %s", e)
            return []

    def get_orders(self, status: str = None) -> list[Order]:
        """获取远程订单记录"""
        try:
            data = self._request("GET", "/orders")
            if isinstance(data, dict) and data.get("e"):
                return []
            orders_list = []
            if isinstance(data, dict):
                orders_list = data.get("orders", [])
            elif isinstance(data, list):
                orders_list = data
            orders = []
            for o in orders_list:
                ts_code = o.get("ts_code", "")
                ts_code = _ts_code_add_suffix(ts_code) if '.' not in ts_code else ts_code
                action = o.get("signal_type", o.get("action", "BUY"))
                raw_status = o.get("status", "pending")
                # 映射中文状态
                if raw_status in ("待执行", "pending"):
                    mapped_status = "pending"
                elif raw_status in ("已提交", "已成交", "filled"):
                    mapped_status = "filled"
                elif raw_status in ("已撤单", "canceled"):
                    mapped_status = "canceled"
                elif raw_status in ("失败", "rejected"):
                    mapped_status = "rejected"
                else:
                    mapped_status = raw_status
                orders.append(Order(
                    order_id=o.get("order_id", "") or o.get("id", ""),
                    ts_code=ts_code,
                    name=o.get("name", o.get("stock_name", "")),
                    action=action,
                    price=float(o.get("price", 0)),
                    quantity=int(o.get("shares", o.get("amount", 0))),
                    amount=float(o.get("price", 0)) * int(o.get("shares", o.get("amount", 0))),
                    status=mapped_status,
                    reason=o.get("reason", ""),
                    created_at=o.get("created_at", o.get("close_date", o.get("time", ""))),
                ))
            return orders
        except Exception as e:
            logger.error("获取远程订单失败: %s", e)
            return []

    def cancel(self, order_id: str) -> bool:
        """远程撤单 — 调用 QMT HTTP /cancel_order"""
        try:
            result = self._request("POST", "/cancel_order", json={"order_id": order_id})
            if result.get("ok"):
                logger.info("撤单成功: %s", order_id)
                return True
            if result.get("e"):
                logger.error("撤单失败 %s: %s", order_id, result["e"])
                return False
            logger.warning("撤单返回未知状态: %s", result)
            return False
        except Exception as e:
            logger.error("撤单异常 %s: %s", order_id, e)
            return False

    def cancel_all(self) -> list[dict]:
        """批量撤单 — 遍历当前委托逐笔撤单"""
        results = []
        try:
            orders = self.get_orders()
            for o in orders:
                if o.status in ("pending", "已报单"):
                    ok = self.cancel(o.order_id)
                    results.append({"order_id": o.order_id, "ok": ok})
            logger.info("批量撤单: %d 笔, 成功 %d 笔", len(results), sum(1 for r in results if r["ok"]))
        except Exception as e:
            logger.error("批量撤单异常: %s", e)
        return results

    def sync_positions(self, positions: list[dict]) -> bool:
        """同步初始持仓到远程服务"""
        try:
            result = self._request("POST", "/sync_positions", json={
                "clear": True, "positions": positions,
            })
            if result.get("ok"):
                logger.info("✅ 已同步 %d 条持仓到远程服务", result.get("count", 0))
                return True
            return False
        except Exception as e:
            logger.error("同步持仓失败: %s", e)
            return False
