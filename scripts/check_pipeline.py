"""
每日流水线健康检查

在 sim_trading scan 之后运行，验证模拟交易执行结果。

用法: python3 scripts/check_pipeline.py
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

import pymysql

from quant_app.utils.config import get_db_config


def get_db_conn():
    return pymysql.connect(**get_db_config())


def check_signals_today(conn, today):
    """检查当天是否有信号记录"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM sim_signals WHERE signal_date = %s", (today,))
    count = cursor.fetchone()[0]
    cursor.close()
    return count


def check_trades_today(conn, today):
    """检查当天是否有交易"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM sim_trades WHERE trade_date = %s", (today,))
    count = cursor.fetchone()[0]
    cursor.close()
    return count


def check_account(conn):
    """检查账户状态"""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT total_value, profit_loss, profit_pct, trade_count, updated_at FROM sim_account ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    return row


def check_positions_json():
    """检查 positions.json 是否存在"""
    path = os.path.join(BASE_DIR, "data", "positions.json")
    if not os.path.exists(path):
        return False, 0
    with open(path) as f:
        data = json.load(f)
    return True, len(data.get("positions", []))


def run_check():
    today = datetime.now().strftime("%Y-%m-%d")
    results = []
    all_ok = True

    # 1. 检查数据库连接
    try:
        conn = get_db_conn()
        results.append(("[OK] 数据库连接成功", "info"))
    except Exception as e:
        results.append((f"[FAIL] 数据库连接失败: {e}", "fail"))
        return results, False

    # 2. 检查信号记录
    try:
        signal_count = check_signals_today(conn, today)
        if signal_count > 0:
            results.append((f"[OK] 今日信号记录: {signal_count} 条", "ok"))
        else:
            results.append(("[WARN] 今日无信号记录（可能是非交易日或扫描尚未执行）", "warn"))
    except Exception as e:
        results.append((f"[FAIL] 信号表查询失败: {e}", "fail"))
        all_ok = False

    # 3. 检查交易记录
    try:
        trade_count = check_trades_today(conn, today)
        results.append((f"[OK] 今日交易记录: {trade_count} 笔", "ok" if trade_count > 0 else "warn"))
    except Exception as e:
        results.append((f"[FAIL] 交易表查询失败: {e}", "fail"))
        all_ok = False

    # 4. 检查账户状态
    try:
        account = check_account(conn)
        if account:
            total_value = float(account[0])
            profit_loss = float(account[1])
            profit_pct = float(account[2]) * 100
            trade_count = account[3]
            results.append((
                f"[OK] 账户总资产: {total_value:.2f}, 盈亏: {profit_loss:.2f} ({profit_pct:.2f}%), 累计交易: {trade_count} 笔",
                "ok"))
        else:
            results.append(("[WARN] 账户未初始化", "warn"))
    except Exception as e:
        results.append((f"[FAIL] 账户查询失败: {e}", "fail"))
        all_ok = False

    conn.close()

    # 5. 检查 positions.json
    try:
        exists, pos_count = check_positions_json()
        if exists:
            results.append((f"[OK] positions.json 存在，持仓 {pos_count} 只", "ok"))
        else:
            results.append(("[WARN] positions.json 不存在（盘后扫描尚未执行）", "warn"))
    except Exception as e:
        results.append((f"[FAIL] positions.json 检查失败: {e}", "fail"))
        all_ok = False

    return results, all_ok


def main():
    results, all_ok = run_check()

    # 打印结果
    print("=" * 60)
    print(f"模拟交易流水线健康检查 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 60)
    for msg, level in results:
        print(f"  {msg}")

    print("-" * 60)
    if all_ok:
        print("✅ 整体状态: 正常")
    else:
        # 检查是否有 FAIL
        fails = [m for m, l in results if l == "fail"]
        if fails:
            print(f"❌ 整体状态: 异常（{len(fails)} 项失败）")
        else:
            print("⚠️ 整体状态: 基本正常（存在警告）")
    print("=" * 60)

    # 写日志
    log_file = os.path.join(LOG_DIR, "pipeline_check.log")
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {'OK' if all_ok else 'WARN'}\n")
        for msg, level in results:
            f.write(f"  {msg}\n")
        f.write("-" * 40 + "\n")


if __name__ == "__main__":
    main()
