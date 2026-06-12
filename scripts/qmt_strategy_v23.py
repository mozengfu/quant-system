#coding:gbk
"""
QMT v23 — 定时器驱动 + 全量数据缓存 + IPC交易
关键改进:
  1. ContextInfo.run_time() 定时器替代被动等handlebar，盘前盘后都运行
  2. 北向资金流向缓存 → qmt_north_flow.json
  3. 保留IPC交易机制不变
"""
import json
import os
import time

CMD = r"C:\Users\Public\qmt_cmd.json"
BAL = r"C:\Users\Public\qmt_balance.json"
POS = r"C:\Users\Public\qmt_position.json"
ORD = r"C:\Users\Public\qmt_order.json"
NFL = r"C:\Users\Public\qmt_north_flow.json"
ACCOUNT = "620000221031"

def init(ContextInfo):
    ContextInfo.set_universe(["000001.SZ"])
    print("v23启动-定时器驱动")
    # 核心：用定时器替代被动等handlebar，每30秒全量刷新
    ContextInfo.run_time("sync_all", 30, "nSecond", 5)
    # 首次立即执行
    ContextInfo.run_time("sync_all", 0, "nSecond", 0)

def handlebar(ContextInfo):
    """只处理IPC命令，缓存刷新由定时器负责"""
    if not os.path.exists(CMD):
        return
    try:
        with open(CMD) as f:
            cmd = json.load(f)
    except:
        return
    if cmd.get("status") != "pending":
        return
    _handle_cmd(cmd, ContextInfo)

def sync_all(ContextInfo):
    """定时器回调：全量刷新余额+持仓+委托+北向资金"""
    now = time.time()

    # ---- 余额 ----
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
                    "account_id": str(a.m_strAccountID),
                    "ts": now
                }, f)
    except Exception as e:
        print("余额异常:", e)

    # ---- 持仓 ----
    try:
        positions = get_trade_detail_data(ACCOUNT, "STOCK", "POSITION")
        if positions is not None:
            pos_list = []
            for p in positions:
                pos_list.append({
                    "code": str(p.m_strInstrumentID),
                    "name": str(p.m_strInstrumentName) if hasattr(p, "m_strInstrumentName") else "",
                    "volume": int(str(p.m_nVolume)) if hasattr(p, "m_nVolume") else 0,
                    "price": float(str(p.m_dOpenPrice)) if hasattr(p, "m_dOpenPrice") else 0,
                    "market_value": float(str(p.m_dMarketValue)) if hasattr(p, "m_dMarketValue") else 0,
                    "profit": float(str(p.m_dFloatProfit)) if hasattr(p, "m_dFloatProfit") else 0,
                })
            with open(POS, "w") as f:
                json.dump({"ts": now, "positions": pos_list}, f)
    except Exception as e:
        print("持仓异常:", e)

    # ---- 委托 ----
    try:
        orders = get_trade_detail_data(ACCOUNT, "STOCK", "ORDER")
        if orders is not None:
            ord_list = []
            for o in orders:
                ord_list.append({
                    "code": str(o.m_strInstrumentID),
                    "id": str(o.m_strOrderSysID),
                    "status": str(o.m_nOrderStatus),
                    "price": float(str(o.m_dLimitPrice)),
                    "volume": int(str(o.m_nVolumeTotalOriginal)),
                    "traded": int(str(o.m_nVolumeTraded)),
                })
            with open(ORD, "w") as f:
                json.dump({"ts": now, "orders": ord_list}, f)
    except Exception as e:
        print("委托异常:", e)

    # ---- 北向资金 ----
    try:
        nf = ContextInfo.get_north_finance_change("1d")
        if nf is not None and len(nf) > 0:
            latest = nf[-1] if isinstance(nf, list) else nf
            with open(NFL, "w") as f:
                json.dump({
                    "ts": now,
                    "net_flow": float(str(latest)) if latest else 0,
                }, f)
    except Exception:
        pass  # 北向资金获取失败不影响主流程

def _handle_cmd(cmd, ContextInfo):
    action = cmd.get("action", "")

    if action == "BALANCE":
        try:
            accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
            if accts:
                a = accts[0]
                info = {}
                for attr in dir(a):
                    if attr.startswith("m_"):
                        try: info[attr] = str(getattr(a, attr))
                        except: pass
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
        try:
            codes = cmd.get("codes", [])
            with open(r"C:\Users\Public\qmt_watchlist.json", "w") as f:
                json.dump(codes, f)
            cmd["status"] = "done"
        except:
            cmd["status"] = "failed"
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
        oid = passorder(op, 1101, ACCOUNT, raw, 11, price, amount, "qmt_v23", 2, ContextInfo)
        print("passorder:", oid)
        cmd["status"] = "done"
        cmd["order_id"] = str(oid)
    except Exception as e:
        print("异常:", e)
        cmd["status"] = "failed"
        cmd["error"] = str(e)

    with open(CMD, "w") as f:
        json.dump(cmd, f)
