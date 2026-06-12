#!/usr/bin/env python3
"""
晨间自动汇报脚本 - 每天早上 6:30 自动运行
1. 检查模拟交易状态
2. 检查真实持仓状态
3. 推送飞书晨间简报
"""
import json
import os
import sys
from datetime import datetime

import pymysql

_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
sys.path.insert(0, os.path.dirname(_script_dir))
from feishu_alerts import send_feishu

from quant_app.utils.config import get_db_config

QUANT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(QUANT_DIR, "data")

DB_CONFIG = get_db_config()

def get_db_conn():
    return pymysql.connect(**DB_CONFIG)

def get_sim_status():
    """获取模拟交易状态"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sim_account ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return None
        cols = [d[0] for d in cur.description]
        acc = dict(zip(cols, row))

        # 持仓
        cur.execute("""SELECT ts_code, stock_name, shares, cost_price, current_price, profit_pct, stop_loss, take_profit
                       FROM sim_positions WHERE status = 'HOLD'""")
        positions = cur.fetchall()
        pos_cols = [d[0] for d in cur.description]
        pos_list = [dict(zip(pos_cols, p)) for p in positions]

        cur.close()
        conn.close()

        return {
            'total_value': float(acc['total_value']),
            'profit_loss': float(acc['profit_loss']),
            'profit_pct': round(float(acc['profit_pct']) * 100, 2),
            'max_drawdown': round(float(acc['max_drawdown']) * 100, 2),
            'cash': float(acc['cash']),
            'trade_count': acc['trade_count'],
            'win_rate': round(float(acc['win_rate']) * 100, 2),
            'positions': pos_list,
        }
    except Exception as e:
        return {'error': str(e)}

def get_real_positions():
    """获取真实持仓状态"""
    try:
        positions_file = os.path.join(DATA_DIR, "positions.json")
        if not os.path.exists(positions_file):
            return []
        with open(positions_file) as f:
            data = json.load(f)
        return data.get('positions', [])
    except Exception:
        return []

def morning_briefing():
    """生成并发送晨间简报"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    weekday = now.strftime("%A")

    # 检查是否交易日（周一到周五）
    is_trading_day = now.weekday() < 5

    lines = []
    lines.append(f"☀️ 早安！{today_str} {weekday}")
    lines.append("")

    # === 模拟交易 ===
    lines.append("🧪 **模拟交易状态**")
    sim = get_sim_status()
    if sim and 'error' not in sim:
        sign = '+' if sim['profit_loss'] >= 0 else ''
        lines.append(f"  账户净值: ¥{sim['total_value']:,.2f}")
        lines.append(f"  累计盈亏: {sign}{sim['profit_loss']:.2f}元 ({sign}{sim['profit_pct']}%)")
        lines.append(f"  最大回撤: {sim['max_drawdown']}%")
        lines.append(f"  可用资金: ¥{sim['cash']:,.2f}")
        lines.append(f"  交易次数: {sim['trade_count']}笔 | 胜率: {sim['win_rate']}%")

        if sim['positions']:
            lines.append(f"  持仓: {len(sim['positions'])}只")
            for p in sim['positions']:
                code = p['ts_code'].split('.')[0]
                pnl_sign = '+' if p['profit_pct'] >= 0 else ''
                lines.append(f"    {p['stock_name']}({code}) {p['shares']}股 盈亏{pnl_sign}{p['profit_pct']:.2f}%")
    else:
        lines.append("  暂无数据（模拟交易尚未启动）")

    lines.append("")

    # === 真实持仓 ===
    if is_trading_day:
        lines.append("📊 **真实持仓状态**")
        real_pos = get_real_positions()
        if real_pos:
            total_cost = 0
            total_value = 0
            for p in real_pos:
                name = p.get('名称', p.get('股票名称', '未知'))
                code = p.get('代码', '')
                cost = p.get('成本', p.get('成本价', 0))
                qty = p.get('数量', 0)
                total_cost += cost * qty
                # 收盘价需要用 API 获取，这里简化显示
                lines.append(f"  {name}({code}) {qty}股 成本{cost:.3f}")
                stop = p.get('止损', 0)
                take = p.get('止盈', 0)
                if stop > 0:
                    lines.append(f"    止损:{stop:.2f} 止盈:{take:.2f}")
        else:
            lines.append("  当前无持仓")

        lines.append("")
        lines.append("📌 **今日操作提醒**")
        lines.append("  ⏰ 9:00 盘前推送 V4 候选股")
        lines.append("  🔔 盘中止盈止损预警（每5分钟）")
        lines.append("  📊 15:05 收盘日报")
        lines.append("  🧪 15:10 模拟交易扫描")
        lines.append("")
        lines.append("⚡ 请确认清仓/建仓计划是否执行！")

    msg = "\n".join(lines)
    send_feishu(msg)
    print(f"晨间简报已发送: {today_str}")

if __name__ == "__main__":
    morning_briefing()
