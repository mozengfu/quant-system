# -*- coding: utf-8 -*-
"""
QMT 交易 HTTP 服务 - 直接使用 QMT Python 3.6 环境
通过策略 API (passorder) 下单
"""
import json, os, time
from flask import Flask, jsonify, request

CMD_FILE = "C:\\qmt_cmd.json"
RESULT_FILE = "C:\\qmt_result.json"
app = Flask(__name__)

LAST_CMD_ID = 0

def write_cmd(action, code, price, amount):
    global LAST_CMD_ID
    LAST_CMD_ID += 1
    cmd = {"id": LAST_CMD_ID, "action": action, "code": code,
           "price": price, "amount": amount, "status": "pending", "ts": time.time()}
    with open(CMD_FILE, "w", encoding="utf-8") as f:
        json.dump(cmd, f)
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)
    print("CMD_WRITTEN: id=%d %s %s %d@%s" % (LAST_CMD_ID, action, code, amount, str(price)))
    return LAST_CMD_ID

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "mode": "qmt-service", "time": time.time()})

@app.route("/balance", methods=["GET"])
def get_balance():
    try:
        import pymysql
        conn = pymysql.connect(host="192.168.10.30", port=3306, user="root",
                               password="root123", database="quant_db", charset="utf8mb4")
        cur = conn.cursor()
        cur.execute("SELECT SUM(market_value), SUM(total_cost), SUM(profit_loss) FROM sim_positions")
        row = cur.fetchone()
        cur.close(); conn.close()
        mkv = row[0] or 0; cost = row[1] or 0; profit = row[2] or 0
        return jsonify({"可用金额": 100000, "股票市值": mkv, "总资产": 100000 + mkv, "冻结资金": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/buy", methods=["POST"])
def post_buy():
    try:
        d = request.get_json(force=True)
        code = d["code"]; price = float(d["price"]); amount = int(d["amount"])
        raw = code.strip()
        if "." not in raw:
            raw = raw + (".SH" if raw.startswith("6") else ".SZ")
        # 尝试用 passorder 下单（如果运行在 QMT 策略环境）
        try:
            accts = get_trade_detail_data("", "STOCK", "ACCOUNT")
            if accts:
                aid = accts[0].m_strAccountID
                oid = passorder(23, 1101, aid, raw, 11, price, amount, None)
                print("PASSORDER: %s %d@%s id=%s" % (raw, amount, str(price), str(oid)))
                if oid and oid > 0:
                    return jsonify({"order_id": oid, "code": code, "price": price, "amount": amount}), 201
        except Exception as e:
            print("PASSORDER_FAIL: %s" % str(e))
        # 降级到 IPC 模式
        cid = write_cmd("BUY", code, price, amount)
        return jsonify({"cmd_id": cid, "code": code, "msg": "IPC mode"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/sell", methods=["POST"])
def post_sell():
    try:
        d = request.get_json(force=True)
        code = d["code"]; price = float(d["price"]); amount = int(d["amount"])
        raw = code.strip()
        if "." not in raw:
            raw = raw + (".SH" if raw.startswith("6") else ".SZ")
        try:
            accts = get_trade_detail_data("", "STOCK", "ACCOUNT")
            if accts:
                aid = accts[0].m_strAccountID
                oid = passorder(24, 1101, aid, raw, 11, price, amount, None)
                if oid and oid > 0:
                    return jsonify({"order_id": oid, "code": code, "price": price, "amount": amount}), 201
        except:
            pass
        cid = write_cmd("SELL", code, price, amount)
        return jsonify({"cmd_id": cid, "code": code, "msg": "IPC mode"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/unlock", methods=["POST"])
def unlock(): return jsonify({"ok": True, "msg": "QMT无需解锁"})
@app.route("/keepalive", methods=["GET"])
def keepalive(): return jsonify({"ok": True, "time": time.time()})

if __name__ == "__main__":
    print("=" * 50)
    print(" QMT 交易服务启动")
    print(" 端口: 1430")
    print("=" * 50)
    app.run(host="0.0.0.0", port=1430, debug=False)
