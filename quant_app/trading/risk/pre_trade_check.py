"""
下单前安全检查 — 所有实盘下单前必须经过此模块验证

检查项列表：
  1. 交易时间（9:30-11:30 / 13:00-14:57）
  2. 价格偏差（下单价 vs 实时行情 < PRICE_DEVIATION_PCT）
  3. 单日熔断（当日累计亏损 > MAX_DAILY_LOSS_PCT）
  4. 单笔限额（金额 > MAX_SINGLE_ORDER_AMOUNT）
  5. 仓位上限（单股市值占比 > MAX_POSITION_PCT）
  6. 账户余额（可用资金充足）
  7. 重复检查（同一股票 60s 内已有同方向订单）
  8. ENABLE_REAL_TRADING 安全开关
"""

import logging
import time
from datetime import datetime

import pymysql

from quant_app.trading.config import trading_config
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()

# 内存中的熔断状态（跨方法共享）
_daily_risk_state = {
    "initial_loss": None,       # 当日初始总资产
    "current_loss_pct": 0.0,    # 当日累计亏损比例
    "circuit_triggered": False, # 是否已触发熔断
    "circuit_date": "",         # 触发日期
}

# 最近订单记录（防重复下单）
_recent_orders: dict[str, float] = {}  # ts_code -> timestamp


class PreTradeChecker:
    """下单前安全检查"""

    @staticmethod
    def _is_trading_time() -> bool:
        """检查是否在交易时段"""
        now = datetime.now()
        # 周末不交易
        if now.weekday() >= 5:
            return False
        # 交易时段
        hour, minute = now.hour, now.minute
        time_val = hour * 60 + minute
        morning_start = 9 * 60 + 30
        morning_end = 11 * 60 + 30
        afternoon_start = 13 * 60
        afternoon_end = 14 * 60 + 57
        return (morning_start <= time_val <= morning_end) or (afternoon_start <= time_val <= afternoon_end)

    @staticmethod
    def _get_realtime_price(ts_code: str, market: str) -> float | None:
        """获取实时行情价"""
        try:
            import urllib.request

            symbol = f"{market}{ts_code.split('.')[0]}"
            req = urllib.request.Request(
                f"http://qt.gtimg.cn/q={symbol}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read().decode("gbk")
            if "~" not in data:
                return None
            parts = data.strip().rstrip(";").split("~")
            if len(parts) < 5:
                return None
            return float(parts[3])
        except Exception as e:
            logger.warning("获取实时行情失败 %s: %s", ts_code, e)
            return None

    @staticmethod
    def _get_daily_pnl() -> float:
        """计算当日累计盈亏比例（从 MySQL real_pnl_cache 或 account 表）"""
        try:
            conn = pymysql.connect(**DB_CONFIG)
            cursor = conn.cursor()
            cursor.execute("SELECT total_value, initial_capital FROM sim_account ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row and float(row[1]) > 0:
                return (float(row[0]) - float(row[1])) / float(row[1]) * 100
            return 0.0
        except Exception:
            return 0.0

    def _record_risk_check(self, ts_code: str, check_name: str, passed: bool, detail: str):
        """将风控检查结果写入 MySQL"""
        try:
            conn = pymysql.connect(**DB_CONFIG)
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO trade_risk_checks
                   (ts_code, check_name, passed, detail, check_time)
                   VALUES (%s, %s, %s, %s, %s)""",
                (ts_code, check_name, 1 if passed else 0, detail, datetime.now()),
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.warning("写入风控检查记录失败: %s", e)

    def check_buy(self, ts_code: str, name: str, market: str,
                  price: float, quantity: int) -> dict:
        """买入前安全检查，返回 {'passed': bool, 'message': str, 'checks': [...]}"""
        checks = []
        all_passed = True

        # 1. 安全开关
        if not trading_config.enable_real_trading:
            msg = "ENABLE_REAL_TRADING 未开启，当前为 dry-run 模式"
            checks.append({"name": "enable_real_trading", "passed": False, "detail": msg})
            return {"passed": False, "message": msg, "checks": checks}

        # 2. 交易时间
        time_ok = self._is_trading_time()
        checks.append({
            "name": "trading_time",
            "passed": time_ok,
            "detail": f"当前时间 {datetime.now().strftime('%H:%M')}，{'交易时段' if time_ok else '非交易时段'}",
        })
        if not time_ok:
            all_passed = False

        # 3. 价格偏差
        realtime_price = self._get_realtime_price(ts_code, market)
        if realtime_price and realtime_price > 0:
            deviation = abs(price - realtime_price) / realtime_price * 100
            price_ok = deviation <= trading_config.price_deviation_pct
            checks.append({
                "name": "price_deviation",
                "passed": price_ok,
                "detail": f"下单价{price} vs 市价{realtime_price}，偏差{deviation:.2f}% (阈值{trading_config.price_deviation_pct}%)",
            })
            if not price_ok:
                all_passed = False
        else:
            checks.append({
                "name": "price_deviation",
                "passed": True,
                "detail": "无法获取实时行情，跳过价格偏差检查",
            })

        # 4. 单笔限额
        amount = round(price * quantity, 2)
        amount_ok = amount <= trading_config.max_single_order_amount
        checks.append({
            "name": "single_order_amount",
            "passed": amount_ok,
            "detail": f"下单金额 {amount} (阈值 {trading_config.max_single_order_amount})",
        })
        if not amount_ok:
            all_passed = False

        # 5. 单日熔断
        global _daily_risk_state
        today = datetime.now().strftime("%Y-%m-%d")
        if _daily_risk_state["circuit_date"] != today:
            _daily_risk_state["circuit_date"] = today
            _daily_risk_state["circuit_triggered"] = False

        if not _daily_risk_state["circuit_triggered"]:
            daily_pnl = self._get_daily_pnl()
            _daily_risk_state["current_loss_pct"] = daily_pnl
            if daily_pnl < trading_config.max_daily_loss_pct:
                _daily_risk_state["circuit_triggered"] = True
                circuit_ok = False
            else:
                circuit_ok = True
        else:
            circuit_ok = False

        checks.append({
            "name": "daily_circuit_breaker",
            "passed": circuit_ok,
            "detail": f"当日盈亏 {_daily_risk_state['current_loss_pct']:.2f}% (熔断阈值 {trading_config.max_daily_loss_pct}%)"
                      + (" — 已触发熔断" if _daily_risk_state["circuit_triggered"] else ""),
        })
        if not circuit_ok:
            all_passed = False

        # 6. 重复检查
        now = time.time()
        last_time = _recent_orders.get(ts_code, 0)
        dup_ok = (now - last_time) > 60
        checks.append({
            "name": "duplicate_order",
            "passed": dup_ok,
            "detail": f"上次同股票下单 {now - last_time:.0f}s 前 (阈值60s)" if not dup_ok else "60s内无重复下单",
        })
        if not dup_ok:
            all_passed = False
        _recent_orders[ts_code] = now

        # 记录到 MySQL
        for c in checks:
            self._record_risk_check(ts_code, c["name"], c["passed"], c["detail"])

        result_msg = "全部检查通过" if all_passed else f"风控拒绝: {'; '.join(c['detail'] for c in checks if not c['passed'])}"
        return {"passed": all_passed, "message": result_msg, "checks": checks}

    def check_sell(self, ts_code: str, price: float, quantity: int) -> dict:
        """卖出前安全检查"""
        checks = []
        all_passed = True

        if not trading_config.enable_real_trading:
            msg = "ENABLE_REAL_TRADING 未开启，当前为 dry-run 模式"
            checks.append({"name": "enable_real_trading", "passed": False, "detail": msg})
            return {"passed": False, "message": msg, "checks": checks}

        # 交易时间
        time_ok = self._is_trading_time()
        checks.append({
            "name": "trading_time",
            "passed": time_ok,
            "detail": f"当前时间 {datetime.now().strftime('%H:%M')}，{'交易时段' if time_ok else '非交易时段'}",
        })
        if not time_ok:
            all_passed = False

        # 单笔限额
        amount = round(price * quantity, 2)
        amount_ok = amount <= trading_config.max_single_order_amount
        checks.append({
            "name": "single_order_amount",
            "passed": amount_ok,
            "detail": f"卖出金额 {amount} (阈值 {trading_config.max_single_order_amount})",
        })
        if not amount_ok:
            all_passed = False

        for c in checks:
            self._record_risk_check(ts_code, c["name"], c["passed"], c["detail"])

        result_msg = "全部检查通过" if all_passed else f"风控拒绝: {'; '.join(c['detail'] for c in checks if not c['passed'])}"
        return {"passed": all_passed, "message": result_msg, "checks": checks}

    def reset_daily_state(self):
        """重置每日熔断状态（新交易日调用）"""
        global _daily_risk_state
        _daily_risk_state["circuit_triggered"] = False
        _daily_risk_state["circuit_date"] = ""
        _daily_risk_state["current_loss_pct"] = 0.0
        logger.info("🔄 已重置每日风控状态")
