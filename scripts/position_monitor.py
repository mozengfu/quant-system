#!/usr/bin/env python3
"""
盘中监控系统
- 定时扫描（每 5 分钟）持仓和候选股
- 止盈止损判断（V4 移动止损规则）
- 触发预警时记录日志并更新 positions.json 浮动盈亏字段
"""
import os, sys, json, logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from alicloud_api import get_stock_realtime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "position_monitor.log"))
    ]
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
POSITIONS_FILE = BASE_DIR / "data" / "positions.json"

# ========== V4.1→V6.5 级联策略风控 ==========
# 固定止损 -3%，一级止盈 +6%
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.06

def calc_trailing_stop(cost, current_price, original_stop_loss):
    """级联策略使用固定止损，无移动止损"""
    return original_stop_loss, False


def scan_positions():
    """
    扫描所有持仓，获取实时价格，计算浮动盈亏，检查止盈止损
    """
    if not POSITIONS_FILE.exists():
        logger.warning("positions.json 不存在")
        return {"positions": [], "alerts": []}

    with open(POSITIONS_FILE, 'r') as f:
        data = json.load(f)
    
    positions = data.get("positions", [])
    if not positions:
        logger.info("无持仓，跳过监控")
        return {"positions": [], "alerts": []}

    alerts = []
    now = datetime.now()
    updated = False

    for pos in positions:
        code = pos.get("code", "")
        market = pos.get("market", "sz")
        name = pos.get("name", "")
        cost = float(pos.get("cost", 0))
        shares = int(pos.get("shares", 0))
        stop_loss = float(pos.get("stop_loss", 0))
        take_profit = float(pos.get("take_profit", 0))
        buy_date = pos.get("buy_date", "")

        if cost <= 0:
            continue

        # 获取实时价格
        quote = get_stock_realtime(code, market)
        if not quote:
            logger.warning(f"获取 {name}({code}) 行情失败")
            continue

        price = quote["现价"]
        day_pct = quote.get("涨跌幅", 0)
        
        # 计算浮动盈亏
        pnl = (price - cost) * shares
        pnl_pct = (price - cost) / cost * 100
        high_since_buy = quote.get("最高", price)
        low_since_buy = quote.get("最低", price)

        # 更新 positions.json 中的浮动盈亏字段
        pos["current_price"] = price
        pos["float_pnl"] = round(pnl, 2)
        pos["float_pnl_pct"] = round(pnl_pct, 2)
        pos["day_pct"] = round(day_pct, 2)
        pos["last_update"] = now.strftime("%Y-%m-%d %H:%M:%S")
        updated = True

        # V4 移动止损计算
        trailing_stop, _ = calc_trailing_stop(cost, price, stop_loss)
        pos["trailing_stop"] = trailing_stop

        # 止盈止损判断
        alert_type = None
        if price <= trailing_stop:
            alert_type = "STOP_LOSS"
            alert_detail = {
                "code": code,
                "name": name,
                "type": "止损",
                "price": price,
                "cost": cost,
                "pnl_pct": round(pnl_pct, 2),
                "trailing_stop": trailing_stop,
                "time": now.strftime("%Y-%m-%d %H:%M:%S")
            }
            alerts.append(alert_detail)
            logger.warning("🔴 止损触发: %s 现价 %.2f 移动止损 %.2f", name, price, trailing_stop)

        elif take_profit > 0 or cost > 0:
            # 三档止盈（与 sim_trading 对齐）
            tp1_price = cost * 1.06   # +6%
            tp2_price = cost * 1.10   # +10%
            tp3_price = cost * 1.18   # +18%

            if price >= tp3_price:
                alert_type = "TAKE_PROFIT_3"
                alert_detail = {
                    "code": code,
                    "name": name,
                    "type": "止盈第三档(+18%)",
                    "price": price,
                    "cost": cost,
                    "pnl_pct": round(pnl_pct, 2),
                    "time": now.strftime("%Y-%m-%d %H:%M:%S")
                }
                alerts.append(alert_detail)
                logger.warning("🟢 止盈第三档: %s 现价 %.2f 涨幅 %.2f%%", name, price, pnl_pct)

            elif price >= tp2_price:
                alert_type = "TAKE_PROFIT_2"
                alert_detail = {
                    "code": code,
                    "name": name,
                    "type": "止盈第二档(+10%)",
                    "price": price,
                    "cost": cost,
                    "pnl_pct": round(pnl_pct, 2),
                    "time": now.strftime("%Y-%m-%d %H:%M:%S")
                }
                alerts.append(alert_detail)
                logger.warning("🟢 止盈第二档: %s 现价 %.2f 涨幅 %.2f%%", name, price, pnl_pct)

            elif price >= tp1_price:
                alert_type = "TAKE_PROFIT_1"
                alert_detail = {
                    "code": code,
                    "name": name,
                    "type": "止盈第一档(+6%)",
                    "price": price,
                    "cost": cost,
                    "pnl_pct": round(pnl_pct, 2),
                    "time": now.strftime("%Y-%m-%d %H:%M:%S")
                }
                alerts.append(alert_detail)
                logger.info("🟢 止盈第一档: %s 现价 %.2f 涨幅 %.2f%%", name, price, pnl_pct)

        # 打印监控摘要
        status = "⚠️" if alert_type else "✅"
        logger.info(
            f"{status} {name}({market.upper()}{code}) 现价:{price:.2f} 成本:{cost:.2f} "
            f"浮动:{pnl_pct:+.2f}% 当日:{day_pct:+.2f}% "
            f"移动止损:{trailing_stop:.2f}"
        )

    # 保存更新后的 positions.json
    if updated:
        try:
            with open(POSITIONS_FILE, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info("positions.json 已更新")
        except Exception as e:
            logger.error(f"保存 positions.json 失败: {e}")

    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "positions": positions,
        "alerts": alerts
    }


if __name__ == "__main__":
    result = scan_positions()
    print(json.dumps(result, ensure_ascii=False, indent=2))
