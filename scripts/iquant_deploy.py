#!/usr/bin/env python3
"""
iQuant 自动交易部署检查工具

检查 macOS → VM → MySQL 链路是否就绪，输出部署状态报告。

用法:
    python3 scripts/iquant_deploy.py
    python3 scripts/iquant_deploy.py --verbose    # 详细输出
"""

import argparse
import json
import os
import sys
import urllib.request

# 添加项目根到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VM_HOST = "192.168.10.25"
VM_PORT = 1430
MYSQL_HOST = "192.168.10.30"
MYSQL_PORT = 3306

PASS = "\033[92m[✓]\033[0m"
FAIL = "\033[91m[✗]\033[0m"
SKIP = "\033[93m[~]\033[0m"


def check_label(status, detail=""):
    icon = PASS if status == "ok" else (FAIL if status == "fail" else SKIP)
    print("  %s %s" % (icon, detail))


def http_get(path, timeout=5):
    url = "http://%s:%d%s" % (VM_HOST, VM_PORT, path)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "iquant-deploy"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        data = resp.read().decode()
        return json.loads(data)
    except Exception as e:
        return {"error": str(e)}


def check_vm():
    print("\n[1] Windows VM 状态")
    print("    %s:%d" % (VM_HOST, VM_PORT))

    # Ping
    ping = http_get("/ping")
    if "error" in ping:
        check_label("fail", "VM 无响应: %s" % ping["error"])
        return False
    check_label("ok", "VM 响应: service=%s" % ping.get("service", "unknown"))

    # 检查服务类型
    service = ping.get("service", "")
    if service == "iquant-http":
        check_label("ok", "新 iQuant HTTP 服务")
    elif "connected" in ping:
        check_label("ok", "旧 QMT 服务 (connected=%s)" % ping.get("connected"))
    else:
        check_label("ok", "未知服务类型")

    # 余额
    bal = http_get("/balance")
    if "error" not in bal:
        total = bal.get("总资产", bal.get("total_asset", 0))
        avail = bal.get("可用金额", bal.get("available", 0))
        check_label("ok", "余额可读: 总资产=%.2f, 可用=%.2f" % (total, avail))
    else:
        check_label("warn", "余额不可读: %s" % bal.get("error", ""))

    # Keepalive
    ka = http_get("/keepalive")
    if ka.get("ok"):
        check_label("ok", "保活接口正常")
    else:
        check_label("warn", "保活接口异常")

    return True


def check_mysql():
    print("\n[2] MySQL 连通性")
    print("    %s:%d" % (MYSQL_HOST, MYSQL_PORT))

    try:
        import pymysql

        from quant_app.utils.config import config, get_db_config

        # 测试 192.168.10.30（VM 视角）
        cfg = get_db_config()
        check_label("warn", "本地配置 host=%s（VM 需用 %s）" % (cfg.get("host", ""), MYSQL_HOST))

        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", "quant_db"),
            charset="utf8mb4", connect_timeout=5
        )
        cur = conn.cursor()

        # 检查关键表
        for table in ["sim_signals", "sim_positions", "sim_account", "sim_trades"]:
            try:
                cur.execute("SELECT COUNT(*) FROM %s" % table)
                cnt = cur.fetchone()[0]
                check_label("ok", "表 %s 存在 (%d 条)" % (table, cnt))
            except Exception:
                check_label("warn", "表 %s 不存在（可能未初始化）" % table)

        # 检查 pending 信号
        cur.execute("SELECT COUNT(*) FROM sim_signals WHERE status='待执行'")
        pending = cur.fetchone()[0]
        if pending > 0:
            check_label("ok", "%d 条待执行信号" % pending)
        else:
            check_label("ok", "无待执行信号")

        cur.close()
        conn.close()
        return True

    except ImportError:
        check_label("fail", "本地未安装 pymysql")
        return False
    except Exception as e:
        check_label("fail", "连接失败: %s" % e)
        return False


def check_iquant_strategy():
    print("\n[3] iQuant 内部策略状态")

    try:
        import pymysql
        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT,
            user="root", password="root123",
            database="quant_db", charset="utf8mb4", connect_timeout=5
        )
        cur = conn.cursor()

        # 检查最近是否有信号被执行（证明 iQuant 策略在工作）
        cur.execute(
            "SELECT COUNT(*) FROM sim_signals "
            "WHERE status='已执行' AND close_date >= CURDATE()"
        )
        recent = cur.fetchone()[0]

        if recent > 0:
            check_label("ok", "今日已有 %d 条信号被执行" % recent)
        else:
            # 检查是否有已执行的信号
            cur.execute("SELECT COUNT(*) FROM sim_signals WHERE status='已执行'")
            total_executed = cur.fetchone()[0]
            if total_executed > 0:
                check_label("warn", "%d 条历史已执行信号，今日无新执行" % total_executed)
            else:
                check_label("warn", "无已执行信号（iQuant 策略可能未启动）")

        # 检查是否有失败信号
        cur.execute("SELECT COUNT(*) FROM sim_signals WHERE status='失败'")
        failed = cur.fetchone()[0]
        if failed > 0:
            check_label("warn", "%d 条执行失败的信号（请检查 iQuant 日志）" % failed)
            cur.execute(
                "SELECT ts_code, signal_type, reason FROM sim_signals "
                "WHERE status='失败' ORDER BY created_at DESC LIMIT 3"
            )
            for r in cur.fetchall():
                reason = (r[2] or "")[:80]
                check_label("warn", "  失败: %s %s - %s" % (r[0], r[1], reason))

        cur.close()
        conn.close()

    except Exception as e:
        check_label("warn", "无法查询信号状态: %s" % e)


def print_summary(vm_ok, mysql_ok):
    print("\n" + "=" * 50)
    print("部署检查报告")
    print("=" * 50)

    if not vm_ok:
        print("\n%s VM 无响应，请先确认：" % FAIL)
        print("  1. Windows VM 是否开机")
        print("  2. iQuant HTTP 服务是否运行（C:\\iquant_http_service.py）")
        print("  3. 防火墙是否放行 1430 端口")
        print("\n  启动命令: C:\\Python312-32\\python.exe C:\\iquant_http_service.py")

    if not mysql_ok:
        print("\n%s MySQL 不可达，请确认：" % FAIL)
        print("  1. macOS MySQL 是否运行（brew services list）")
        print("  2. MySQL bind_address=* 是否配置")
        print("  3. root 是否有远程登录权限")

    if vm_ok and mysql_ok:
        print("\n%s 链路正常，可以进行 RDP 配置 iQuant 策略" % PASS)

    print("\n一键 RDP 配置清单：")
    print("  1. RDP 到 192.168.10.25 (mozf / 782500)")
    print("  2. 打开 国信iQuant，登录 18978253999")
    print("  3. 安装 pymysql==1.0.2 到 iQuant Python 3.6.8")
    print("  4. 部署 bridge 策略到模型交易（实盘模式）")
    print("  5. 启动 HTTP 服务")
    print("  6. 在模型交易中启动策略运行")


def main():
    parser = argparse.ArgumentParser(description="iQuant 部署检查")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    print("=" * 50)
    print("iQuant 自动交易部署检查")
    print("=" * 50)
    print("时间: %s" % __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    vm_ok = check_vm()
    mysql_ok = check_mysql()

    if vm_ok and mysql_ok:
        check_iquant_strategy()

    print_summary(vm_ok, mysql_ok)

    return 0 if (vm_ok and mysql_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
