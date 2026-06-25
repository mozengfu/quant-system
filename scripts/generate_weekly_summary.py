#!/usr/bin/env python3
"""
每周投资总结生成器

用法:
  python3 scripts/generate_weekly_summary.py                           # 自动取最近周六
  python3 scripts/generate_weekly_summary.py --date 2026-06-20        # 指定周结束日期
  python3 scripts/generate_weekly_summary.py --mode live              # 仅实盘(默认)
  python3 scripts/generate_weekly_summary.py --mode simulation        # 仅模拟

输出: ~/Library/Mobile Documents/莫增富的笔记/投资总结/周投资总结_YYYY-MM-DD.md
"""

import argparse
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pymysql
from dotenv import load_dotenv

# ========== 路径 ==========
QUANT_ROOT = Path(__file__).resolve().parent.parent
OBSIDIAN_VAULT = Path(
    "/Users/mozengfu/Library/Mobile Documents/iCloud~md~obsidian/Documents/莫增富的笔记"
)
load_dotenv(QUANT_ROOT / ".env")

DB_CFG = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DATABASE", "quant_db"),
    "charset": "utf8mb4",
    "connect_timeout": 5,
}


def get_db():
    return pymysql.connect(**DB_CFG)


def fmt_price(v):
    return f"{v:.2f} 元" if v is not None else "—"


def fmt_pnl(v):
    return f"{v:+.2f} 元" if v is not None else "—"


def fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "—"


def get_market_summary(cur, week_start, week_end):
    """获取本周市场概况"""
    indices = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
    names = {
        "000001.SH": "上证指数",
        "399001.SZ": "深证成指",
        "399006.SZ": "创业板指",
        "000688.SH": "科创50",
    }
    rows = []
    for idx in indices:
        try:
            cur.execute(
                """SELECT trade_date, `close`, pct_chg FROM daily_price
                   WHERE ts_code=%s AND trade_date BETWEEN %s AND %s
                   ORDER BY trade_date""",
                (idx, week_start, week_end),
            )
            data = cur.fetchall()
            if len(data) >= 2:
                fc = float(data[0][1])
                lc = float(data[-1][1])
                chg = round((lc - fc) / fc * 100, 2)
                rows.append((names[idx], lc, chg))
            elif len(data) == 1:
                rows.append((names[idx], float(data[0][1]), 0.0))
            else:
                rows.append((names[idx], "—", "—"))
        except Exception:
            rows.append((names[idx], "—", "—"))
    return rows


def get_weekly_trades(cur, week_start, week_end, mode="live"):
    """获取本周交易记录"""
    cur.execute(
        """SELECT ts_code, stock_name, action, price, quantity, amount,
                  trade_date, trade_time, reason
           FROM qmt_trades
           WHERE trade_date BETWEEN %s AND %s AND mode=%s AND status='filled'
           ORDER BY trade_date, trade_time""",
        (week_start, week_end, mode),
    )
    return cur.fetchall()


def get_weekly_trade_summary(cur, week_start, week_end, mode="live"):
    """获取本周交易汇总"""
    cur.execute(
        """SELECT action, COUNT(*), COALESCE(SUM(amount),0)
           FROM qmt_trades
           WHERE trade_date BETWEEN %s AND %s AND mode=%s AND status='filled'
           GROUP BY action""",
        (week_start, week_end, mode),
    )
    summary = {"BUY": (0, 0.0), "SELL": (0, 0.0)}
    for r in cur.fetchall():
        summary[r[0]] = (r[1], float(r[2]))
    return summary


def get_weekly_realized_pnl(cur, week_start, week_end, mode="live"):
    """
   计算本周已实现盈亏

   找出本周在指定 mode 下卖出的股票，然后跨所有模式查这些股票的完整交易记录
   来计算真实的已实现盈亏（因为买和卖可能在不同 mode 下记录）。
   """
    cur.execute(
        """SELECT DISTINCT ts_code FROM qmt_trades
           WHERE trade_date BETWEEN %s AND %s AND mode=%s
             AND status='filled' AND action='SELL'""",
        (week_start, week_end, mode),
    )
    sold_codes = [r[0] for r in cur.fetchall()]
    if not sold_codes:
        return 0.0, []

    placeholders = ",".join(["%s"] * len(sold_codes))
    cur.execute(
        f"""SELECT ts_code, stock_name, action, price, quantity, amount, trade_date
            FROM qmt_trades
            WHERE status='filled' AND ts_code IN ({placeholders})
            ORDER BY ts_code, trade_date""",
        sold_codes,
    )
    all_trades = cur.fetchall()

    trades_by_stock = {}
    for t in all_trades:
        trades_by_stock.setdefault(t[0], []).append(t)

    items = []
    total = 0.0
    for code, trades in trades_by_stock.items():
        name = trades[0][1]
        buy_qty = 0
        buy_amt = 0.0
        sell_qty = 0
        sell_amt = 0.0
        sell_date = ""
        for t in trades:
            a, qty, amt = t[2], t[4], float(t[5])
            if a == "BUY":
                buy_qty += qty
                buy_amt += amt
            elif a == "SELL":
                sell_qty += qty
                sell_amt += amt
                sell_date = str(t[6])

        if buy_amt == 0:
            continue  # 数据不全，跳过 PnL 计算
        if sell_qty >= buy_qty:
            pnl = sell_amt - buy_amt * (sell_qty / buy_qty) if buy_qty > 0 else 0
            pnl_pct = round(pnl / buy_amt * 100, 2) if buy_amt > 0 else 0
            total += pnl
            items.append({
                "code": code, "name": name,
                "pnl": round(pnl, 2), "pnl_pct": pnl_pct,
                "sell_date": sell_date,
            })
    return round(total, 2), items


def get_all_trades(cur, mode="live"):
    """获取所有历史交易"""
    cur.execute(
        """SELECT ts_code, stock_name, action, price, quantity, amount, trade_date
           FROM qmt_trades WHERE status='filled' AND mode=%s
           ORDER BY trade_date""",
        (mode,),
    )
    return cur.fetchall()


def calculate_closed_trades(all_trades, mode="live"):
    """计算已清仓股票的盈亏"""
    trades_by_stock = {}
    for t in all_trades:
        trades_by_stock.setdefault(t[0], []).append(t)

    closed = []
    total = 0.0
    for code, trades in trades_by_stock.items():
        name = trades[0][1]
        buy_qty = 0
        buy_amt = 0.0
        sell_qty = 0
        sell_amt = 0.0
        last_date = ""
        for t in trades:
            a, qty, amt = t[2], t[4], float(t[5])
            if a == "BUY":
                buy_qty += qty
                buy_amt += amt
            elif a == "SELL":
                sell_qty += qty
                sell_amt += amt
            last_date = str(t[6])
        if buy_amt == 0:
            continue
        if sell_qty >= buy_qty:
            pnl = sell_amt - buy_amt * (sell_qty / buy_qty) if buy_qty > 0 else 0
            pct = round(pnl / buy_amt * 100, 2) if buy_amt > 0 else 0
            total += pnl
            closed.append({
                "code": code, "name": name,
                "pnl": round(pnl, 2), "pnl_pct": pct,
                "last_date": last_date,
            })
    closed.sort(key=lambda x: x["last_date"], reverse=True)
    return closed, round(total, 2)


def read_positions():
    """读取当前持仓"""
    pos_file = QUANT_ROOT / "data" / "positions.json"
    if not pos_file.exists():
        return []
    try:
        with open(pos_file) as f:
            data = json.load(f)
        return data.get("positions", [])
    except Exception:
        return []


def calculate_float_pnl(positions):
    """计算浮动盈亏汇总"""
    total_cost = 0.0
    total_mv = 0.0
    total_fpnl = 0.0
    for p in positions:
        cost = float(p.get("cost", 0)) * int(p.get("shares", 0))
        mv = float(p.get("current_price", 0)) * int(p.get("shares", 0))
        total_cost += cost
        total_mv += mv
        total_fpnl += float(p.get("float_pnl", 0))
    return round(total_cost, 2), round(total_mv, 2), round(total_fpnl, 2)


def generate_summary(week_end_date=None, mode="live"):
    """生成周投资总结"""
    if week_end_date is None:
        today = date.today()
        days_to_saturday = (today.weekday() - 5) % 7
        week_end_date = today - timedelta(days=days_to_saturday)
    elif isinstance(week_end_date, str):
        week_end_date = date.fromisoformat(week_end_date)

    week_start = week_end_date - timedelta(days=6)
    ws, we = week_start.isoformat(), week_end_date.isoformat()

    db = get_db()
    cur = db.cursor()

    market_data = get_market_summary(cur, ws, we)
    trades = get_weekly_trades(cur, ws, we, mode)
    trade_summary = get_weekly_trade_summary(cur, ws, we, mode)
    weekly_realized_pnl, weekly_pnl_items = get_weekly_realized_pnl(cur, ws, we, mode)

    db2 = get_db()
    cur2 = db2.cursor()
    all_closed, total_realized = calculate_closed_trades(get_all_trades(cur2, mode), mode)

    cur.close()
    db.close()
    cur2.close()
    db2.close()

    positions = read_positions()
    total_cost, total_market_value, total_float_pnl = calculate_float_pnl(positions)

    mode_label = "实盘" if mode == "live" else "模拟"
    week_range = f"{ws} ~ {we}"
    now_str = datetime.now().strftime("%Y 年 %m 月 %d 日 %H:%M")

    lines = []
    lines.append(f"# 周投资总结（{mode_label}）")
    lines.append(f"**生成时间**: {now_str}")
    lines.append(f"**数据范围**: {week_range}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 一、市场概况
    lines.append("## 一、市场概况")
    lines.append("")
    has_market = any(not isinstance(r[1], str) for r in market_data)
    if has_market:
        lines.append("| 指数 | 最新价 | 周涨跌幅 |")
        lines.append("|------|--------|---------|")
        for name, close, chg in market_data:
            if isinstance(chg, str):
                rows = f"| {name} | {close} | {chg} |"
            else:
                emoji = "📈" if chg > 0 else ("📉" if chg < 0 else "➡️")
                rows = f"| {name} | {fmt_price(close)} | {emoji} {chg:+.2f}% |"
            lines.append(rows)
    else:
        lines.append("*市场数据暂不可用*")
    lines.append("")

    # 二、本周交易
    lines.append("## 二、本周交易记录")
    lines.append("")
    buy_cnt, buy_amt = trade_summary.get("BUY", (0, 0.0))
    sell_cnt, sell_amt = trade_summary.get("SELL", (0, 0.0))
    lines.append(f"- **买入**: {buy_cnt} 笔, {fmt_price(buy_amt)}")
    lines.append(f"- **卖出**: {sell_cnt} 笔, {fmt_price(sell_amt)}")
    lines.append("")

    if trades:
        lines.append("| 日期 | 代码 | 名称 | 操作 | 价格 | 数量 | 金额 |")
        lines.append("|------|------|------|------|------|------|------|")
        for t in trades:
            code, name, action, price, qty, amt, td, tt, reason = t
            act_label = "买入" if action == "BUY" else "卖出"
            lines.append(
                f"| {td} | {code} | {name} | {act_label} | {fmt_price(price)} | {qty} | {fmt_price(amt)} |"
            )
        lines.append("")
    else:
        lines.append("*本周无交易记录*")
        lines.append("")

    # 三、本周已实现盈亏
    lines.append("## 三、本周已实现盈亏")
    lines.append("")
    if weekly_pnl_items:
        lines.append("| 股票 | 代码 | 盈亏 | 盈亏% | 卖出日期 |")
        lines.append("|------|------|------|--------|---------|")
        for item in weekly_pnl_items:
            lines.append(
                f"| {item['name']} | {item['code']} | {fmt_pnl(item['pnl'])} | "
                f"{fmt_pct(item['pnl_pct'])} | {item['sell_date']} |"
            )
        lines.append("")
        lines.append(f"**本周已实现盈亏合计**: {fmt_pnl(weekly_realized_pnl)}")
    else:
        lines.append("*本周无可计算已实现盈亏的股票（数据不足）*")
    lines.append("")

    # 四、当前持仓
    lines.append("## 四、当前持仓")
    lines.append("")
    if positions:
        lines.append("| 代码 | 名称 | 数量 | 成本价 | 最新价 | 浮动盈亏 | 盈亏% | 买入日期 |")
        lines.append("|------|------|------|--------|--------|---------|--------|---------|")
        for p in positions:
            lines.append(
                f"| {p['code']} | {p['name']} | {p['shares']} | {fmt_price(p['cost'])} | "
                f"{fmt_price(p['current_price'])} | {fmt_pnl(p['float_pnl'])} | "
                f"{fmt_pct(p['float_pnl_pct'])} | {p.get('buy_date', '—')} |"
            )
        lines.append("")
        lines.append(f"- **持仓市值**: {fmt_price(total_market_value)}")
        lines.append(f"- **持仓成本**: {fmt_price(total_cost)}")
        lines.append(f"- **浮动盈亏**: {fmt_pnl(total_float_pnl)}")
    else:
        lines.append("*当前空仓*")
    lines.append("")

    # 五、组合总览
    lines.append("## 五、组合总览")
    lines.append("")
    total_pnl = weekly_realized_pnl + total_float_pnl
    lines.append("| 项目 | 金额 |")
    lines.append("|------|------|")
    lines.append(f"| 本周已实现盈亏 | {fmt_pnl(weekly_realized_pnl)} |")
    lines.append(f"| 当前浮动盈亏 | {fmt_pnl(total_float_pnl)} |")
    lines.append(f"| **合计** | **{fmt_pnl(total_pnl)}** |")
    lines.append(f"| 持仓市值 | {fmt_price(total_market_value)} |")
    lines.append("")

    # 六、历史累计
    lines.append("## 六、历史累计")
    lines.append("")
    lines.append(f"- **累计已实现盈亏**: {fmt_pnl(total_realized)}")
    lines.append(f"- **累计已清仓股票**: {len(all_closed)} 只")
    if all_closed:
        wins = [t for t in all_closed if t["pnl"] > 0]
        losses = [t for t in all_closed if t["pnl"] <= 0]
        wr = round(len(wins) / len(all_closed) * 100, 1)
        lines.append(f"- **胜率**: {wr}%（{len(wins)} 赢 / {len(losses)} 亏）")
        if wins:
            lines.append(f"- **平均盈利**: {fmt_price(round(sum(t['pnl'] for t in wins) / len(wins), 2))}")
        if losses:
            lines.append(f"- **平均亏损**: {fmt_price(round(sum(t['pnl'] for t in losses) / len(losses), 2))}")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*本报告由莫莫自动生成，数据来源：QMT {mode_label}交易记录*")
    lines.append("*仅供参考，不构成投资建议*")

    content = "\n".join(lines)

    summary_dir = OBSIDIAN_VAULT / "投资总结"
    summary_dir.mkdir(parents=True, exist_ok=True)
    filename = f"周投资总结_{we}.md"
    filepath = summary_dir / filename

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"✅ 周投资总结已生成: {filepath}")
    print(f"   - 数据范围: {week_range}")
    print(f"   - 本周交易: {buy_cnt} 笔买入, {sell_cnt} 笔卖出")
    print(f"   - 已实现盈亏: {fmt_pnl(weekly_realized_pnl)}")
    print(f"   - 当前持仓: {len(positions)} 只")
    print(f"   - 浮动盈亏: {fmt_pnl(total_float_pnl)}")
    print(f"   - 累计已实现盈亏: {fmt_pnl(total_realized)} (基于 {len(all_closed)} 只已清仓)")
    return filepath


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成每周投资总结")
    parser.add_argument("--date", type=str, default=None,
                        help="周结束日期（YYYY-MM-DD），默认最近周六")
    parser.add_argument("--mode", type=str, default="live",
                        choices=["live", "simulation"], help="交易模式（默认实盘）")
    args = parser.parse_args()
    filepath = generate_summary(args.date, args.mode)
    print(f"\n完成: {filepath}")
