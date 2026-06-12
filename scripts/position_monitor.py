#!/usr/bin/env python3
"""持仓风控监控 - 每个交易日 9:50/14:50 运行"""
import json
import ssl
import sys
import urllib.request
from datetime import datetime

# Add project root to path
sys.path.insert(0, '.')

from quant_app.utils.config import FEISHU_WEBHOOK, get_db_config


def get_positions():
    """从MySQL获取持仓列表"""
    import pymysql
    config = get_db_config()
    conn = pymysql.connect(**config)
    cur = conn.cursor()
    cur.execute(
        "SELECT ts_code, name, quantity, cost, stop_loss, take_profit, buy_date "
        "FROM positions"
    )
    positions = []
    for r in cur.fetchall():
        ts_code = r[0]  # e.g. "000559.SZ"
        code_num = ts_code.split(".")[0]  # "000559"
        positions.append({
            "ts_code": ts_code,
            "code": code_num,
            "name": r[1],
            "shares": r[2],
            "cost": float(r[3]),
            "stop_loss": float(r[4]) if r[4] else None,
            "take_profit": float(r[5]) if r[5] else None,
            "buy_date": str(r[6]) if r[6] else "",
        })
    conn.close()
    return positions


def tencent_symbol(code):
    """代码 -> 腾讯前缀: 00/30/60开头 -> sz/sh"""
    if code.startswith(('00', '30')):
        return f"sz{code}"
    elif code.startswith('60'):
        return f"sh{code}"
    elif code.startswith('8') or code.startswith('4'):
        return f"bj{code}"  # 北交所
    else:
        return f"sz{code}"


def get_quotes(positions):
    """批量获取腾讯实时行情"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    symbols = [tencent_symbol(p["code"]) for p in positions]
    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"

    req = urllib.request.Request(url)
    req.add_header('Referer', 'https://finance.qq.com')
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        raw = resp.read().decode('gbk')

    quotes = {}
    for line in raw.strip().split('\n'):
        if '=' not in line:
            continue
        parts = line.split('~')
        if len(parts) < 45:
            continue
        # parts[2] = code (without prefix)
        code = parts[2]
        price = float(parts[3]) if parts[3] else 0  # 当前价
        prev_close = float(parts[4]) if parts[4] else 0  # 昨收
        open_price = float(parts[5]) if parts[5] else 0  # 开盘
        high = float(parts[33]) if parts[33] else 0  # 最高
        low = float(parts[34]) if parts[34] else 0  # 最低
        volume = float(parts[36]) if parts[36] else 0  # 成交量(手)
        turnover = float(parts[37]) if parts[37] else 0  # 成交额(万)

        quotes[code] = {
            "price": price,
            "prev_close": prev_close,
            "open": open_price,
            "high": high,
            "low": low,
            "volume": volume,
            "turnover": turnover,
            "change_pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        }
    return quotes


def get_hs300_quote():
    """获取沪深300指数行情"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        url = "https://qt.gtimg.cn/q=sh000300"
        req = urllib.request.Request(url)
        req.add_header('Referer', 'https://finance.qq.com')
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read().decode('gbk')
        parts = raw.strip().split('~')
        if len(parts) >= 45:
            price = float(parts[3]) if parts[3] else 0
            prev_close = float(parts[4]) if parts[4] else 0
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else 0
            return {"price": price, "change_pct": change_pct}
    except Exception:
        pass
    return None


def analyze(positions, quotes):
    """分析持仓状态"""
    results = []
    triggers = []

    for p in positions:
        q = quotes.get(p["code"])
        if not q or q["price"] == 0:
            results.append({
                "name": p["name"],
                "code": p["code"],
                "status": "无行情",
                "price": 0,
                "cost": p["cost"],
                "pnl_pct": 0,
                "pnl_amount": 0,
            })
            continue

        price = q["price"]
        cost = p["cost"]
        pnl_pct = round((price - cost) / cost * 100, 2)
        pnl_amount = round((price - cost) * p["shares"], 2)

        status = "正常"
        action = None

        # 止损判断
        if p["stop_loss"] and price <= p["stop_loss"]:
            status = "⚠️ 触及止损"
            action = f"建议止损卖出（止损价 {p['stop_loss']}元，现价 {price}元，浮亏 {pnl_pct}%）"
            triggers.append({"name": p["name"], "code": p["code"], "action": action})

        # 止盈判断
        if p["take_profit"] and price >= p["take_profit"]:
            status = "🎯 触及止盈"
            action = f"建议分批止盈（止盈价 {p['take_profit']}元，现价 {price}元，浮盈 {pnl_pct}%）"
            triggers.append({"name": p["name"], "code": p["code"], "action": action})

        # 浮亏>5%但未到止损价 → 预警
        if pnl_pct < -5 and p["stop_loss"] and price > p["stop_loss"]:
            status = "🔶 浮亏预警"
            action = f"浮亏 {pnl_pct}%，距止损价还有 {round(price - p['stop_loss'], 2)}元"
            triggers.append({"name": p["name"], "code": p["code"], "action": action})

        results.append({
            "name": p["name"],
            "code": p["code"],
            "status": status,
            "price": price,
            "cost": cost,
            "pnl_pct": pnl_pct,
            "pnl_amount": pnl_amount,
            "action": action,
        })

    return results, triggers


def build_report(results, triggers, hs300):
    """生成报告文本"""
    now = datetime.now()
    title = f"【持仓监控】{now.strftime('%m月%d日 %H:%M')}"

    lines = [title, "---", "持仓状态：", ""]

    for r in results:
        emoji = "🔴" if r["pnl_pct"] < 0 else "🟢"
        if r["status"] == "无行情":
            lines.append(f"• {r['name']} ({r['code']})：无实时行情")
        else:
            sign = "+" if r["pnl_pct"] > 0 else ""
            lines.append(
                f"• {r['name']} ({r['code']})：现价{r['price']}元，"
                f"成本{r['cost']}元，浮盈{sign}{r['pnl_pct']}%（{sign}{r['pnl_amount']}元）"
            )
            lines.append(f"  → {r['status']}")
        if r.get("action"):
            lines.append(f"  💡 {r['action']}")
        lines.append("")

    # 市场风险提示
    if hs300 and hs300["change_pct"] < -2:
        lines.append(f"⚠️ 市场风控：沪深300下跌 {hs300['change_pct']}%，注意整体风险敞口")
        lines.append("")

    # 汇总
    total_pnl = sum(r.get("pnl_amount", 0) for r in results)
    sign = "+" if total_pnl > 0 else ""
    lines.append(f"合计浮盈：{sign}{round(total_pnl, 2)}元")

    if triggers:
        lines.append("")
        lines.append("⚡ 触发信号：")
        for t in triggers:
            lines.append(f"• {t['name']} ({t['code']}): {t['action']}")

    return "\n".join(lines)


def send_feishu(message):
    """发送飞书消息"""
    if not FEISHU_WEBHOOK:
        print("FEISHU_WEBHOOK 未配置，跳过发送")
        print("\n报告内容：\n" + message)
        return False
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps({"msg_type": "text", "content": {"text": message}}).encode('utf-8')
    req = urllib.request.Request(FEISHU_WEBHOOK, data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=5, context=ctx)
    result = json.loads(resp.read().decode())
    if result.get("StatusCode") == 0 or result.get("code") == 0:
        print("飞书消息已发送")
        return True
    else:
        print(f"飞书发送返回异常: {result}")
        return False


def main():
    print(f"=== 持仓风控监控 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    # 1. 获取持仓
    positions = get_positions()
    print(f"持仓数：{len(positions)}")
    if not positions:
        send_feishu("【持仓监控】当前无持仓，空仓状态。")
        return

    # 2. 获取行情
    quotes = get_quotes(positions)
    print(f"获取行情：{len(quotes)} 只")

    # 3. 沪深300
    hs300 = get_hs300_quote()
    if hs300:
        print(f"沪深300: {hs300['price']} ({hs300['change_pct']}%)")

    # 4. 分析
    results, triggers = analyze(positions, quotes)

    # 5. 报告
    report = build_report(results, triggers, hs300)
    print("\n" + report)

    # 6. 发送飞书
    send_feishu(report)


if __name__ == "__main__":
    main()
