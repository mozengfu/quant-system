#!/usr/bin/env python3
"""每日开盘健康检查 — 9:36 跑一次, 验证 P1/P2/对齐都正常

检查项:
1. live_trading_monitor.log 最近 5min 有新条目 (说明 monitor 还在跑)
2. sim_signals 今日 (ts_code, date) 没有重复 (P1 唯一约束生效)
3. sim_positions 持仓股数 = QMT /position 股数 (数据对齐)
4. 总资产 sane (>= initial_capital/2)
5. 飞书汇总报告
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
from datetime import datetime

import pymysql
import requests

from quant_app.services.notification_service import send_feishu
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

LOG_FILE = "/Users/mozengfu/workspace/quant-system/logs/live_trading_monitor.log"
QMT_BASE = "http://192.168.10.25:1430"


def check_monitor_fresh():
    """检查 monitor 日志是否最近 5min 有新内容"""
    try:
        mtime = os.path.getmtime(LOG_FILE)
        age_min = (datetime.now().timestamp() - mtime) / 60
        if age_min > 10:
            return False, f"log {age_min:.0f}min 未更新"
        return True, f"log {age_min:.1f}min 前更新"
    except Exception as e:
        return False, f"读 log 失败: {e}"


def check_sim_signals_dedup():
    """检查今日 sim_signals (ts_code, date) 唯一"""
    conn = pymysql.connect(**get_db_config(connect_timeout=5))
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) - COUNT(DISTINCT ts_code, DATE(created_at)) AS dup_count
        FROM sim_signals
        WHERE status='已执行' AND DATE(created_at)=CURDATE()
    """)
    dup = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM sim_signals
        WHERE status='已执行' AND DATE(created_at)=CURDATE()
    """)
    total = cur.fetchone()[0]
    cur.close(); conn.close()
    if dup > 0:
        return False, f"今日 {total} 条已执行, 其中 {dup} 条 (ts_code, date) 重复"
    return True, f"今日 {total} 条已执行, 全唯一"


def check_qmt_alignment():
    """QMT 持仓 vs sim_positions 持仓股数对齐"""
    try:
        r = requests.get(f"{QMT_BASE}/position", timeout=5)
        qmt_positions = r.json()
    except Exception as e:
        return False, f"QMT /position 不可达: {e}"

    if isinstance(qmt_positions, dict):
        qmt_positions = qmt_positions.get("positions", [])

    qmt_map = {}
    for p in qmt_positions:
        code = p.get("code", "") if isinstance(p, dict) else ""
        if "." not in code:
            code = code + (".SH" if code.startswith("6") else ".SZ")
        qmt_map[code] = int(p.get("volume", 0) or 0)

    conn = pymysql.connect(**get_db_config(connect_timeout=5))
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code, SUM(shares) FROM sim_positions
        WHERE status='HOLD' GROUP BY ts_code
    """)
    sim_map = dict(cur.fetchall())
    cur.close(); conn.close()

    diffs = []
    for code, qmt_shares in qmt_map.items():
        sim_shares = sim_map.get(code, 0)
        if abs(qmt_shares - sim_shares) > 0:
            diffs.append(f"{code}: QMT={qmt_shares} sim={sim_shares}")
    extra_sim = set(sim_map) - set(qmt_map)
    if extra_sim:
        for code in extra_sim:
            diffs.append(f"{code}: sim={sim_map[code]} (QMT 无)")

    if diffs:
        return False, "持仓不一致: " + "; ".join(diffs[:3])
    return True, f"{len(qmt_map)} 只持仓, QMT 与 sim 完全一致"


def check_balance_sane():
    """总资产 sane 检查"""
    try:
        r = requests.get(f"{QMT_BASE}/balance", timeout=5)
        bal = r.json()
    except Exception as e:
        return False, f"QMT /balance 不可达: {e}"

    total = float(bal.get("total_asset", 0))
    available = float(bal.get("available", 0))
    if total <= 0:
        return False, f"total_asset={total} 异常"
    if total < 30000:
        return False, f"总资产 {total:.0f} 跌破 3w, 关注"
    return True, f"总资产 {total:.0f}, 可用 {available:.0f}"


def main():
    print("=== 每日开盘健康检查 ===")
    print()

    results = []
    for name, fn in [
        ("monitor 活跃", check_monitor_fresh),
        ("P1 dedup 唯一", check_sim_signals_dedup),
        ("QMT/sim 对齐", check_qmt_alignment),
        ("资产健康", check_balance_sane),
    ]:
        ok, msg = fn()
        results.append((name, ok, msg))
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name}: {msg}")

    n_ok = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    overall = "✅ 全部通过" if n_ok == n_total else f"⚠️ {n_total - n_ok} 项异常"

    msg_lines = [f"📊 每日健康检查 ({datetime.now().strftime('%H:%M')}) {overall}"]
    for name, ok, m in results:
        marker = "✓" if ok else "✗"
        msg_lines.append(f"  {marker} {name}: {m}")
    send_feishu("\n".join(msg_lines))

    print()
    print(f"飞书: 已发送 {overall}")
    return 0 if n_ok == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
