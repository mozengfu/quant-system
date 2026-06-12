"""
IPC 桥接策略 v5 - handlebar轮询版
"""
import json
import os
import time

CMD_FILE = r"C:\Users\18978\qmt_cmd.json"
RESULT_FILE = r"C:\Users\18978\qmt_result.json"
HEARTBEAT_FILE = r"C:\Users\18978\qmt_heartbeat.txt"
count = 0
last_check = 0

def init(ContextInfo):
    global count, last_check
    print("=== QMT IPC 策略 v5 启动 ===")
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write("init ok at " + str(time.time()))
        print("心跳文件写入成功: " + HEARTBEAT_FILE)
    except Exception as e:
        print("心跳文件写入失败: " + str(e))
    print("=== v5 就绪，通过handlebar轮询cmd文件 ===")

def handlebar(ContextInfo):
    global count, last_check
    # 每秒最多检查一次
    now = time.time()
    if now - last_check < 1:
        return
    last_check = now
    count += 1
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write("handlebar " + str(count) + " at " + str(time.time()))
    except:
        pass
    try:
        if not os.path.exists(CMD_FILE):
            return
        with open(CMD_FILE, encoding="utf-8") as f:
            cmd = json.load(f)
        if cmd.get("status") != "pending":
            return
        print("检测到命令: " + str(cmd))
        _exec(cmd, ContextInfo)
    except Exception as e:
        print("handlebar Error: " + str(e))

def _exec(cmd, ctx):
    cid = cmd["id"]
    action = cmd["action"]
    code = cmd["code"]
    price = cmd["price"]
    amount = cmd["amount"]

    print("执行: " + str(action) + " " + str(code) + " " + str(amount) + "@" + str(price))

    raw = code.strip()
    if "." not in raw:
        raw = raw + (".SH" if raw.startswith("6") else ".SZ")

    try:
        accts = get_trade_detail_data("", "STOCK", "ACCOUNT")
        if not accts:
            print("无账号")
            _wr(cid, {"error":"无账号"})
            return
        aid = accts[0].m_strAccountID
        print("账号: " + aid)

        otype = 23 if action == "BUY" else 24
        oid = passorder(otype, 1101, aid, raw, 11, float(price), int(amount), ctx)

        if oid and oid > 0:
            print("OK id=" + str(oid))
            _wr(cid, {"order_id":oid, "status":"ok"})
        else:
            print("返回: " + str(oid))
            _wr(cid, {"order_id":oid, "status":"submitted" if oid==0 else "failed"})

        os.remove(CMD_FILE)
    except Exception as e:
        print("Exec error: " + str(e))
        _wr(cid, {"error":str(e)})
        try: os.remove(CMD_FILE)
        except: pass

def _wr(cid, data):
    data["cmd_id"] = cid
    data["ts"] = time.time()
    try:
        print("写入结果: " + str(data))
        with open(RESULT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except: pass
