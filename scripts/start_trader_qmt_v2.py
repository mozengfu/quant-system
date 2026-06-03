#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国信iQuant QMT 交易 HTTP 服务 v3 (IPC模式)
- 交易通过 JSON 文件传给 QMT 内部策略执行 (passorder)
- 查询从 MySQL 读取
"""

import json, logging, os, subprocess, sys, time as _time
from functools import wraps
from flask import Flask, jsonify, request

PORT = 1430
CMD_FILE = r"C:\qmt_cmd.json"
RESULT_FILE = r"C:\qmt_result.json"
LOG_FILE = r"C:\qmt_trader.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)
app = Flask(__name__)

CMD_ID = 0

def write_cmd(action, code, price, amount):
    global CMD_ID
    CMD_ID += 1
    cmd = {"id": CMD_ID, "action": action, "code": code, "price": price,
           "amount": amount, "status": "pending", "ts": _time.time()}
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False)
    # 清除旧结果
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)
    logger.info(f"命令已写入: {action} {code} {amount}@{price} (id={CMD_ID})")
    return CMD_ID

def wait_result(timeout=30):
    """等待 QMT 策略执行结果"""
    for _ in range(timeout):
        _time.sleep(1)
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE, "r", encoding="utf-8") as f:
                    r = json.load(f)
                os.remove(RESULT_FILE)
                return r
            except: pass
    return {"error": "超时"}

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "mode": "qmt-ipc", "time": _time.time()})

@app.route("/balance", methods=["GET"])
def get_balance():
    try:
        import pymysql
        conn = pymysql.connect(host="192.168.10.30", port=3306, user="root",
                               password="root123", database="quant_db", charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("SELECT SUM(market_value), SUM(total_cost), SUM(profit_loss) FROM sim_positions ")
        row = cur.fetchone()
        cur.close(); conn.close()
        mkv = row[0] or 0; cost = row[1] or 0; profit = row[2] or 0
        return jsonify({"可用金额": 100000, "股票市值": mkv, "总资产": 100000 + mkv, "冻结资金": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/position", methods=["GET"])
def get_position():
    try:
        import pymysql
        conn = pymysql.connect(host="192.168.10.30", port=3306, user="root",
                               password="root123", database="quant_db", charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("SELECT ts_code, shares, cost_price, market_value, profit_loss FROM sim_positions ")
        result = [{"code":r[0],"shares":r[1],"cost":r[2],"market_value":r[3],"profit":r[4]} for r in cur.fetchall()]
        cur.close(); conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/buy", methods=["POST"])
def post_buy():
    d = request.get_json(force=True)
    code = d["code"]; price = float(d["price"]); amount = int(d["amount"])
    logger.info(f"买入: {code} {amount}股 @ {price}")
    cid = write_cmd("BUY", code, price, amount)
    result = wait_result()
    logger.info(f"结果: {result}")
    if "error" in result:
        return jsonify({"error": result["error"], "cmd_id": cid}), 400
    return jsonify({"order_id": result.get("order_id"), "cmd_id": cid, "code": code}), 201

@app.route("/sell", methods=["POST"])
def post_sell():
    d = request.get_json(force=True)
    code = d["code"]; price = float(d["price"]); amount = int(d["amount"])
    logger.info(f"卖出: {code} {amount}股 @ {price}")
    cid = write_cmd("SELL", code, price, amount)
    result = wait_result()
    if "error" in result:
        return jsonify({"error": result["error"], "cmd_id": cid}), 400
    return jsonify({"order_id": result.get("order_id"), "cmd_id": cid, "code": code}), 201

@app.route("/orders", methods=["GET"])
def get_orders():
    return jsonify([])

@app.route("/unlock", methods=["POST"])
def unlock(): return jsonify({"ok": True, "msg": "QMT无需解锁"})
@app.route("/keepalive", methods=["GET"])
def keepalive(): return jsonify({"ok": True, "time": _time.time()})

if __name__ == "__main__":
    logger.info("="*50)
    logger.info("国信 QMT v3 (IPC模式)"); logger.info(f"端口: {PORT}")
    logger.info("="*50)
    logger.info(f"命令文件: {CMD_FILE}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
