#!/usr/bin/env python3
"""
飞书预警系统
- 盘前推送（9:00）：V4组合策略候选股 Top 5
- 止盈止损预警：实时监控持仓触发飞书提醒
- 收盘推送（15:05）：持仓日报 + 明日关注
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pymysql

# 添加父目录到路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
sys.path.insert(0, os.path.dirname(_script_dir))
from alicloud_api import get_stock_realtime

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ========== 配置 ==========
BASE_DIR = Path(__file__).parent.parent
POSITIONS_FILE = BASE_DIR / "data" / "positions.json"

from quant_app.services.notification_service import send_feishu


# ========== 1. 盘前推送 ==========
def send_morning_alert(top_n=5):
    """
    盘前推送（9:00）：模拟盘持仓 + 近期买入信号
    """
    logger.info("开始盘前推送...")

    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        # 1. 当前模拟持仓
        cursor.execute("""
            SELECT ts_code, stock_name, shares, cost_price, current_price,
                   profit_loss, profit_pct, stop_loss
            FROM sim_positions WHERE status = 'HOLD'
        """)
        holdings = cursor.fetchall()

        # 2. 最近买入信号（3 天内）
        cursor.execute("""
            SELECT ts_code, stock_name, price, shares, strategy, signal_date
            FROM sim_signals
            WHERE signal_type = '买入' AND signal_date >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)
            ORDER BY signal_date DESC
        """)
        signals = cursor.fetchall()

        # 3. 账户概况
        cursor.execute("""
            SELECT total_value, profit_loss, profit_pct, trade_count, win_count
            FROM sim_account ORDER BY id DESC LIMIT 1
        """)
        account = cursor.fetchone()
        cursor.close()
        conn.close()

        today = datetime.now().strftime("%Y-%m-%d")
        msg = f"📊 盘前简报 · {today}\n"
        msg += "━" * 30 + "\n"

        # 账户概览
        if account:
            total_value = float(account[0])
            profit_loss = float(account[1])
            profit_pct = float(account[2]) * 100 if account[2] else 0
            trade_count = account[3]
            win_count = account[4] or 0
            msg += f"💰 模拟账户: {total_value:.0f} 元 ({profit_pct:+.2f}%)\n"
            msg += f"   交易 {trade_count} 次, 胜率 {win_count/max(trade_count,1)*100:.0f}%\n"

        # 当前持仓
        if holdings:
            msg += "\n📋 当前持仓:\n"
            for r in holdings:
                name = r[1]
                shares = int(r[2])
                cost = float(r[3])
                cur = float(r[4]) if r[4] else cost
                pnl_pct = (cur - cost) / cost * 100
                stop = float(r[7])
                marker = "🟢" if pnl_pct >= 0 else "🔴"
                msg += f"{marker} {name} {shares}股 成本{cost:.2f} 现价{cur:.2f} ({pnl_pct:+.1f}%) 止损{stop:.2f}\n"
        else:
            msg += "\n📋 当前持仓: 空仓\n"

        # 近期买入信号
        if signals:
            msg += "\n🆕 最近买入信号:\n"
            seen = set()
            for r in signals:
                ts_code = r[0]
                if ts_code in seen:
                    continue
                seen.add(ts_code)
                name = r[1]
                price = float(r[2])
                signal_date = str(r[5]) if r[5] else ""
                msg += f"  {name} ({ts_code}) 买入价{price:.2f} {signal_date}\n"

        # 市场状态
        try:
            from market_state import get_market_state
            ms = get_market_state() or {}
            state = ms.get('state', 'unknown')
            state_names = {"trend_up": "上涨", "trend_down": "下跌", "range": "震荡", "panic": "恐慌", "overheated": "过热"}
            msg += f"\n📌 市场状态: {state_names.get(state, state)}\n"
        except Exception:
            pass

        msg += "━" * 30 + "\n"
        msg += "📋 跟单建议在系统 → 跟单建议面板查看"

        send_feishu(msg)
        logger.info("盘前简报推送完成")

    except Exception as e:
        logger.error(f"盘前推送失败: {e}")
        send_feishu(f"⚠️ 盘前推送异常: {e}")


# ========== 2. 止盈止损预警 ==========
def check_position_alerts(alerted_state_file=None):
    """
    实时监控持仓，触发止盈止损时飞书提醒
    
    触发规则：
    - 触及止损（-5%）→ 飞书紧急提醒"建议止损"
    - 触及止盈第一档（+6%）→ 飞书提醒"建议卖1/3"
    - 触及止盈第二档（+10%）→ 飞书提醒"建议再卖1/3"
    - 触及止盈第三档（+18%）→ 飞书提醒"建议清仓"
    """
    if alerted_state_file is None:
        alerted_state_file = BASE_DIR / "data" / "alert_state.json"

    # 加载已提醒状态（防止重复通知）
    alerted = {}
    if os.path.exists(alerted_state_file):
        try:
            with open(alerted_state_file) as f:
                alerted = json.load(f)
        except Exception:
            alerted = {}

    # 实时监控心跳检查: 检测 cmd_monitor 是否在正常运行
    # 除了处理正常持仓的止盈止损, 也要确保监控进程本身健康
    # 仅工作日盘中检查 (9:15-15:00), 其他时段不报警
    from datetime import date as _date
    _today = _date.today()
    _now = datetime.now()
    if _today.weekday() < 5 and 915 <= _now.hour * 100 + _now.minute <= 1500:
        _hb_file = BASE_DIR / "data" / "monitor_heartbeat.txt"
        if _hb_file.exists():
            try:
                _hb_time_str = _hb_file.read_text().strip()
                _hb_time = datetime.fromisoformat(_hb_time_str)
                _elapsed = (_now - _hb_time).total_seconds()
                if _elapsed > 600:  # 超过 10 分钟没有心跳 → 报警
                    _hb_alert_key = "monitor_heartbeat"
                    if alerted.get(_hb_alert_key) != str(_today):  # 每日去重
                        send_feishu(
                            "⚠️ 监控心跳异常\n\n"
                            f"cmd_monitor 最后心跳: {_hb_time_str}\n"
                            f"当前时间: {_now.isoformat()}\n"
                            f"已停止运行 {_elapsed//60:.0f} 分钟\n"
                            f"请检查: crontab / live_trading_scheduler.py 状态"
                        )
                        alerted[_hb_alert_key] = str(_today)
                        logger.warning("监控心跳异常告警已发送, 最后心跳: %s", _hb_time_str)
            except Exception as e:
                logger.debug("心跳检查失败: %s", e)

    # 读取持仓
    if not POSITIONS_FILE.exists():
        logger.warning("positions.json 不存在")
        return

    with open(POSITIONS_FILE) as f:
        data = json.load(f)
    positions = data.get("positions", [])

    if not positions:
        logger.info("无持仓，跳过预警")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    triggered = False

    for pos in positions:
        code = pos.get("code", "")
        market = pos.get("market", "sz")
        name = pos.get("name", "")
        cost = float(pos.get("cost", 0))
        stop_loss = float(pos.get("stop_loss", 0))
        take_profit = float(pos.get("take_profit", 0))
        shares = int(pos.get("shares", 0))

        if cost <= 0:
            continue

        # 获取实时价格
        quote = get_stock_realtime(code, market)
        if not quote:
            continue

        price = quote["现价"]
        pct = (price - cost) / cost * 100

        # 判断触发条件
        alert_type = None
        alert_msg = None

        if price <= stop_loss:
            # 止损触发
            alert_key = f"{code}_stop_loss"
            if alerted.get(alert_key) == now[:10]:  # 当日已提醒
                continue
            alert_type = "🔴 止损预警"
            alert_msg = (
                f"{alert_type}\n\n"
                f"{name}（{market.upper()}{code}）\n"
                f"现价: {price:.2f}  成本: {cost:.2f}\n"
                f"跌幅: {pct:.2f}%\n"
                f"止损价: {stop_loss:.2f}\n"
                f"⚡ 建议：立即止损卖出"
            )
            alerted[alert_key] = now[:10]

        elif take_profit > 0:
            # 止盈三档判断
            tp1 = cost * 1.06   # +6%
            tp2 = cost * 1.10   # +10%
            tp3 = cost * 1.18   # +18%

            if price >= tp3:
                alert_key = f"{code}_tp3"
                if alerted.get(alert_key) == now[:10]:
                    continue
                alert_type = "🟢 止盈第三档"
                alert_msg = (
                    f"{alert_type}\n\n"
                    f"{name}（{market.upper()}{code}）\n"
                    f"现价: {price:.2f}  成本: {cost:.2f}\n"
                    f"涨幅: {pct:.2f}%\n"
                    f"⚡ 建议：清仓获利了结"
                )
                alerted[alert_key] = now[:10]

            elif price >= tp2:
                alert_key = f"{code}_tp2"
                if alerted.get(alert_key) == now[:10]:
                    continue
                alert_type = "🟢 止盈第二档"
                alert_msg = (
                    f"{alert_type}\n\n"
                    f"{name}（{market.upper()}{code}）\n"
                    f"现价: {price:.2f}  成本: {cost:.2f}\n"
                    f"涨幅: {pct:.2f}%\n"
                    f"⚡ 建议：再卖1/3锁定利润"
                )
                alerted[alert_key] = now[:10]

            elif price >= tp1:
                alert_key = f"{code}_tp1"
                if alerted.get(alert_key) == now[:10]:
                    continue
                alert_type = "🟢 止盈第一档"
                alert_msg = (
                    f"{alert_type}\n\n"
                    f"{name}（{market.upper()}{code}）\n"
                    f"现价: {price:.2f}  成本: {cost:.2f}\n"
                    f"涨幅: {pct:.2f}%\n"
                    f"⚡ 建议：卖1/3锁定利润"
                )
                alerted[alert_key] = now[:10]

        if alert_msg:
            send_feishu(alert_msg)
            logger.info("预警触发: %s %s %.2f%%", alert_type, name, pct)
            triggered = True

    # 保存提醒状态
    try:
        with open(alerted_state_file, 'w') as f:
            json.dump(alerted, f, indent=2)
    except Exception as e:
        logger.warning(f"保存提醒状态失败: {e}")

    if not triggered:
        logger.info("持仓监控完成，无预警触发")


# ========== 3. 收盘推送 ==========
def send_daily_report():
    """
    收盘推送（15:05）：持仓日报 + 明日关注
    """
    logger.info("开始收盘推送...")

    # 读取持仓
    if not POSITIONS_FILE.exists():
        send_feishu("📋 收盘日报：无持仓数据")
        return

    with open(POSITIONS_FILE) as f:
        data = json.load(f)
    positions = data.get("positions", [])

    if not positions:
        send_feishu("📋 收盘日报（%s）\n\n当前无持仓，空仓状态。\n" % datetime.now().strftime("%Y-%m-%d"))
        return

    # 获取每只持仓的收盘价（用当天最后的实时价代替）
    report_lines = []
    total_pnl = 0

    for pos in positions:
        code = pos.get("code", "")
        market = pos.get("market", "sz")
        name = pos.get("name", "")
        cost = float(pos.get("cost", 0))
        shares = int(pos.get("shares", 0))
        buy_date = pos.get("buy_date", "")

        quote = get_stock_realtime(code, market)
        if not quote:
            report_lines.append(f"  {name}（{market.upper()}{code}）：行情获取失败")
            continue

        price = quote["现价"]
        day_pct = quote.get("涨跌幅", 0)
        pnl = (price - cost) * shares
        pnl_pct = (price - cost) / cost * 100
        total_pnl += pnl

        direction = "🔴" if pnl < 0 else "🟢"
        report_lines.append(
            f"  {direction} {name}（{market.upper()}{code}）\n"
            f"     成本: {cost:.2f}  收盘: {price:.2f}\n"
            f"     当日: {day_pct:+.2f}%  累计: {pnl_pct:+.2f}%\n"
            f"     盈亏: {pnl:+.2f}元"
        )

    # 构建消息
    date_str = datetime.now().strftime("%Y-%m-%d")
    msg = f"📋 收盘日报（{date_str}）\n"
    msg += "━" * 28 + "\n"
    msg += "\n".join(report_lines)
    msg += "\n" + "━" * 28 + "\n"

    total_direction = "🔴 亏损" if total_pnl < 0 else "🟢 盈利"
    msg += f"\n💰 当日盈亏汇总: {total_direction} {total_pnl:+.2f}元"

    # ========== 净值追踪 ==========
    nav_file = BASE_DIR / "data" / "nav_history.json"
    if nav_file.exists():
        try:
            with open(nav_file) as f:
                nav_data = json.load(f)
            if nav_data:
                current = nav_data[-1]
                cur_val = float(current["total_value"])
                cur_profit = current["profit_pct"]
                cur_mdd = current["max_drawdown"]
                msg += "\n\n📊 净值追踪"
                msg += f"\n当前净值: {cur_val:,.0f} 元 ({cur_profit:+.2f}%)"
                if len(nav_data) >= 2:
                    yesterday = nav_data[-2]
                    yest_val = yesterday["total_value"]
                    day_diff = cur_val - yest_val
                    day_pct = (cur_val / yest_val - 1) * 100
                    msg += f"\n较昨日: {day_diff:+,.0f} 元 ({day_pct:+.2f}%)"
                # 5日前
                current_date = datetime.strptime(current["date"], "%Y-%m-%d")
                target_date = current_date - timedelta(days=5)
                target_str = target_date.strftime("%Y-%m-%d")
                five_day_ago = next((e for e in nav_data if e["date"] == target_str), None)
                if five_day_ago is None:
                    candidates = [e for e in nav_data if e["date"] <= target_str]
                    if candidates:
                        five_day_ago = candidates[-1]
                if five_day_ago:
                    fv = five_day_ago["total_value"]
                    fd_diff = cur_val - fv
                    fd_pct = (cur_val / fv - 1) * 100
                    msg += f"\n较5日前: {fd_diff:+,.0f} 元 ({fd_pct:+.2f}%)"
                msg += f"\n最大回撤: {cur_mdd:.2f}%"
                # ========== 月度收益 ==========
                monthly = {}
                for entry in nav_data:
                    month = entry["date"][:7]
                    if month not in monthly:
                        monthly[month] = []
                    monthly[month].append(entry)
                msg += "\n\n📅 月度收益"
                for month in sorted(monthly.keys(), reverse=True):
                    entries = monthly[month]
                    first_val = entries[0]["total_value"]
                    last_val = entries[-1]["total_value"]
                    month_return = (last_val / first_val - 1) * 100
                    msg += f"\n{month}: {month_return:+.2f}%"
                # ========== 回撤预警 ==========
                if cur_mdd > 10:
                    msg += f"\n\n⚠️ 回撤预警: 当前回撤 {cur_mdd:.2f}% > 10%，建议关注风控"
                elif cur_mdd > 5:
                    msg += f"\n\n📌 回撤注意: 当前回撤 {cur_mdd:.2f}% > 5%"
        except Exception as e:
            logger.warning(f"读取净值数据失败: {e}")

    # 明日关注（取 V4 扫描前 3）
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]

        if latest_date:
            today_str = str(latest_date)
            sql = """
                SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume_ratio
                FROM quant_db.daily_price d
                JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
                WHERE d.trade_date = %s
                  AND d.close > 5
                  AND d.pct_chg > 1
                  AND d.pct_chg < 9.5
                  AND d.volume_ratio > 1.5
                  AND s.is_st = 0
                  AND d.ts_code NOT LIKE '688%%'
                ORDER BY d.pct_chg DESC
                LIMIT 3
            """
            cursor.execute(sql, (today_str,))
            watch = cursor.fetchall()
            cursor.close()
            conn.close()

            if watch:
                msg += "\n\n👀 明日关注：\n"
                for w in watch:
                    code_raw = w[0].split(".")[0]
                    mkt = "sz" if w[0].endswith(".SZ") else "sh"
                    msg += f"  • {w[1]}（{mkt.upper()}{code_raw}） 涨幅 {w[3]:+.2f}%\n"
    except Exception as e:
        logger.warning(f"获取明日关注失败: {e}")

    msg += "\n⚡ 祝投资顺利！"

    send_feishu(msg)
    logger.info("收盘推送完成")


# ========== 入口 ==========
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="飞书预警系统")
    parser.add_argument("action", choices=["morning", "alert", "daily"],
                        help="morning=盘前推送, alert=止盈止损检查, daily=收盘日报")
    args = parser.parse_args()

    if args.action == "morning":
        send_morning_alert()
    elif args.action == "alert":
        check_position_alerts()
    elif args.action == "daily":
        send_daily_report()
