#!/usr/bin/env python3
r"""
QMT 下单守护进程 — 独立运行，不依赖iQuant策略
运行: C:\Python312\python.exe C:\Users\18978\qmt_daemon.py

前提: iQuant必须打开并登录，否则xtquant无法连接
"""
import json
import os
import time

from xtquant import xtconstant, xttrader
from xtquant.xttype import StockAccount

CMD_FILE = r"C:\Users\18978\qmt_cmd.json"
RESULT_FILE = r"C:\Users\18978\qmt_result.json"
QMT_PATH = r"C:\国信iQuant策略交易平台\userdata\users\18978253999"
SESSION_ID = 1
ACCOUNT_ID = "170000758981"

def main():
    print("=== QMT Daemon v1 ===")

    print("连接iQuant...")
    trader = xttrader.XtQuantTrader(QMT_PATH, SESSION_ID)
    trader.start()
    connect_result = trader.connect()
    print(f"connect返回: {connect_result}")
    time.sleep(2)

    account = StockAccount(ACCOUNT_ID)
    trader.subscribe(account)
    print(f"已订阅账号: {ACCOUNT_ID}")
    print("开始轮询 cmd 文件...")

    while True:
        try:
            if not os.path.exists(CMD_FILE):
                time.sleep(0.5)
                continue

            with open(CMD_FILE, encoding="utf-8") as f:
                cmd = json.load(f)

            if cmd.get("status") != "pending":
                time.sleep(0.5)
                continue

            cid = cmd["id"]
            action = cmd["action"]
            code = cmd["code"].strip()
            price = float(cmd["price"])
            amount = int(cmd["amount"])
            print(f"[{cid}] {action} {code} {amount}@{price}")

            if "." not in code:
                code = code + (".SH" if code.startswith("6") else ".SZ")

            otype = xtconstant.STOCK_BUY if action == "BUY" else xtconstant.STOCK_SELL
            oid = trader.order_stock(account, code, otype, amount, xtconstant.FIX_PRICE, price)

            result = {"cmd_id": cid, "ts": time.time()}
            if oid and oid > 0:
                result["order_id"] = oid
                result["status"] = "ok"
                print(f"[{cid}] 下单成功 order_id={oid}")
            else:
                result["order_id"] = oid
                result["status"] = "failed" if oid is None else "submitted"
                print(f"[{cid}] 返回: {oid}")

            with open(RESULT_FILE, "w", encoding="utf-8") as f:
                json.dump(result, f)

            os.remove(CMD_FILE)

        except Exception as e:
            print(f"错误: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()
