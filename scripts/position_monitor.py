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

# 盘中自动执行交易（止损/止盈/超时）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from sim_trading import execute_sell, execute_partial_sell
from quant_app.utils.config import get_db_config

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

# ========== 风控参数（由 sim_trading.get_market_params 动态获取，此处仅留参考）==========
# 实际止损止盈由市场状态动态决定

def calc_trailing_stop(cost, current_price, original_stop_loss):
    """级联策略使用固定止损，无移动止损"""
    return original_stop_loss, False


def scan_positions():
    """
    扫描所有持仓，获取实时价格，计算浮动盈亏，检查止盈止损
    """
    # 获取市场状态参数（动态止盈止损、最大持仓等）
    from sim_trading import get_market_params
    mp = get_market_params()
    logger.info("当前市场状态: %s 止损%.0f%% 止盈%.0f%% 最大持仓%d",
                mp['state'], mp['stop_loss_pct'] * -100, mp['take_profit_pct'] * 100, mp['max_positions'])

    if not POSITIONS_FILE.exists():
        logger.warning("positions.json 不存在")
        return {"positions": [], "alerts": []}

    with open(POSITIONS_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # 兼容两种格式：中文字段列表 / 英文字段 {"positions": [...]}
    if isinstance(data, list):
        positions = data
    elif isinstance(data, dict):
        positions = data.get("positions", [])
    else:
        positions = []
    
    if not positions:
        logger.info("无持仓，跳过监控")
        return {"positions": [], "alerts": []}

    alerts = []
    now = datetime.now()
    updated = False

    for pos in positions:
        # 兼容中英文字段
        code = pos.get("code") or pos.get("代码", "")
        market = pos.get("market") or pos.get("市场", "sz")
        name = pos.get("name") or pos.get("名称", "")
        cost = float(pos.get("cost") or pos.get("成本", 0))
        shares = int(pos.get("shares") or pos.get("数量", 0))
        stop_loss = float(pos.get("stop_loss") or pos.get("止损", 0))
        take_profit = float(pos.get("take_profit") or pos.get("止盈", 0))
        buy_date = pos.get("buy_date") or pos.get("买入日期", "")
        position_id = int(pos.get("position_id") or 0)

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

        # 止盈止损判断 + 自动执行
        alert_type = None
        if price <= trailing_stop and position_id > 0:
            execute_sell(position_id, price, reason="盘中自动止损")
            alert_type = "STOP_LOSS"
            logger.warning("🔴 自动止损执行: %s 现价 %.2f 移动止损 %.2f", name, price, trailing_stop)
            # 已卖出，不继续检查止盈

        elif (take_profit > 0 or cost > 0) and position_id > 0:
            # 三档止盈（与 sim_trading 对齐）
            tp1_price = cost * 1.06   # +6%
            tp2_price = cost * 1.10   # +10%
            tp3_price = cost * 1.18   # +18%

            if price >= tp3_price:
                execute_sell(position_id, price, reason="盘中自动止盈清仓(+18%)")
                alert_type = "TAKE_PROFIT_3"
                logger.warning("🟢 自动止盈清仓: %s 现价 %.2f 涨幅 %.2f%%", name, price, pnl_pct)

            elif price >= tp2_price:
                sell_shares = max(100, shares // 3)
                execute_partial_sell(position_id, sell_shares, price, reason="盘中自动止盈减仓(+10%)")
                alert_type = "TAKE_PROFIT_2"
                logger.warning("🟡 自动止盈减仓: %s 现价 %.2f 涨幅 %.2f%% 卖出%d股", name, price, pnl_pct, sell_shares)

            elif price >= tp1_price:
                sell_shares = max(100, shares // 3)
                execute_partial_sell(position_id, sell_shares, price, reason="盘中自动止盈减仓(+6%)")
                alert_type = "TAKE_PROFIT_1"
                logger.info("🟡 自动止盈减仓: %s 现价 %.2f 涨幅 %.2f%% 卖出%d股", name, price, pnl_pct, sell_shares)

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
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        import sim_trading as _st
        mp = _st.get_market_params()
        print(json.dumps({
            "market_state": mp['state'],
            "effective_params": {
                "stop_loss_pct": mp['stop_loss_pct'],
                "take_profit_pct": mp['take_profit_pct'],
                "max_positions": mp['max_positions'],
                "ml_threshold": mp['ml_threshold'],
            },
        }, ensure_ascii=False, indent=2))
    else:
        result = scan_positions()
        print(json.dumps(result, ensure_ascii=False, indent=2))
