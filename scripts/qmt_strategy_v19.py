#coding:gbk
import json
import os

CMD = r"C:\Users\Public\qmt_cmd.json"
ACCOUNT = "170000758981"

def init(ContextInfo):
    ContextInfo.set_universe(["000001.SZ"])
    print("v19启动-实盘交易")

def handlebar(ContextInfo):
    if not os.path.exists(CMD):
        return
    try:
        with open(CMD) as f:
            cmd = json.load(f)
    except:
        return
    if cmd.get("status") != "pending":
        return

    action = cmd["action"]

    # 余额查询不需要 is_last_bar 守卫
    if action == "BALANCE":
        try:
            accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
            if accts:
                a = accts[0]
                info = {}
                for attr in dir(a):
                    if attr.startswith("m_"):
                        try:
                            info[attr] = str(getattr(a, attr))
                        except:
                            pass
                cmd["status"] = "done"
                cmd["balance"] = info
            else:
                cmd["status"] = "failed"
        except Exception as e:
            cmd["status"] = "failed"
            cmd["error"] = str(e)
        with open(CMD, "w") as f:
            json.dump(cmd, f)
        return

    # 交易只在当前实盘K线执行
    if not ContextInfo.is_last_bar():
        return

    code = cmd["code"].strip()
    price = float(cmd["price"])
    amount = int(cmd["amount"])
    raw = code
    if "." not in raw:
        raw = raw + (".SH" if raw.startswith("6") else ".SZ")

    print("执行: %s %s %d@%.2f" % (action, raw, amount, price))

    try:
        op = 23 if action == "BUY" else 24
        oid = passorder(op, 1101, ACCOUNT, raw, 11, price, amount, "qmt_v19", 2, ContextInfo)
        print("passorder返回:", oid)
        cmd["status"] = "done"
        cmd["order_id"] = str(oid)
    except Exception as e:
        print("异常:", str(e))
        cmd["status"] = "failed"
        cmd["error"] = str(e)

    with open(CMD, "w") as f:
        json.dump(cmd, f)
