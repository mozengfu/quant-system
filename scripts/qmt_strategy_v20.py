#coding:gbk
import json
import os
import time

CMD = r"C:\Users\Public\qmt_cmd.json"
BAL = r"C:\Users\Public\qmt_balance.json"
PRC = r"C:\Users\Public\qmt_prices.json"
ACCOUNT = "620000221031"

def init(ContextInfo):
    ContextInfo.set_universe(["000001.SZ"])
    print("v21启动-缓存余额+行情")
    ContextInfo._last_bal = 0
    ContextInfo._last_prc = 0

def handlebar(ContextInfo):
    now = time.time()

    # 每30秒缓存余额
    if now - ContextInfo._last_bal > 30:
        ContextInfo._last_bal = now
        try:
            accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
            if accts:
                a = accts[0]
                with open(BAL, "w") as f:
                    json.dump({
                        "available": float(str(a.m_dAvailable)),
                        "total_asset": float(str(a.m_dBalance)),
                        "market_value": float(str(a.m_dStockValue)),
                        "frozen": float(str(a.m_dFrozenCash)),
                        "position_profit": float(str(a.m_dPositionProfit)),
                        "account_id": str(a.m_strAccountID),
                        "ts": now
                    }, f)
        except Exception as e:
            print("余额缓存异常:", e)

    # 每10秒缓存行情（读取监视列表）
    if now - ContextInfo._last_prc > 10:
        ContextInfo._last_prc = now
        try:
            watchlist = _load_watchlist()
            prices = {}
            bar_tag = ContextInfo.get_bar_timetag(ContextInfo.barpos)
            for code in watchlist:
                try:
                    p = ContextInfo.get_close_price(code, "", bar_tag)
                    if p:
                        prices[code] = float(str(p))
                except:
                    pass
            if prices:
                with open(PRC, "w") as f:
                    json.dump({"ts": now, "prices": prices}, f)
        except Exception as e:
            print("行情缓存异常:", e)

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

    if action == "WATCH":
        # 更新监视列表
        try:
            codes = cmd.get("codes", [])
            _save_watchlist(codes)
            cmd["status"] = "done"
            print("监视列表更新:", codes)
        except Exception as e:
            cmd["status"] = "failed"
            cmd["error"] = str(e)
        with open(CMD, "w") as f:
            json.dump(cmd, f)
        return

    if action == "QUOTE":
        try:
            code = cmd.get("code", "000001.SZ").strip()
            if "." not in code:
                code = code + (".SH" if code.startswith("6") else ".SZ")
            close = ContextInfo.get_close_price(code, "", ContextInfo.get_bar_timetag(ContextInfo.barpos))
            cmd["status"] = "done"
            cmd["quote"] = {"code": code, "price": float(str(close)) if close else 0}
            print("行情:", code, close)
        except Exception as e:
            cmd["status"] = "failed"
            cmd["error"] = str(e)
        with open(CMD, "w") as f:
            json.dump(cmd, f)
        return

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
        oid = passorder(op, 1101, ACCOUNT, raw, 11, price, amount, "qmt_v21", 2, ContextInfo)
        print("passorder返回:", oid)
        cmd["status"] = "done"
        cmd["order_id"] = str(oid)
    except Exception as e:
        print("异常:", str(e))
        cmd["status"] = "failed"
        cmd["error"] = str(e)

    with open(CMD, "w") as f:
        json.dump(cmd, f)

def _load_watchlist():
    try:
        with open(r"C:\Users\Public\qmt_watchlist.json") as f:
            return json.load(f)
    except:
        return []

def _save_watchlist(codes):
    try:
        with open(r"C:\Users\Public\qmt_watchlist.json", "w") as f:
            json.dump(codes, f)
    except:
        pass
