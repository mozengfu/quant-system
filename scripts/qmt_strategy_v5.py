#coding:gbk
"""
QMT 统一策略 v5 — 行情(3s) + 交易 + 缓存(30s)
股票池: stockpool.json (净流入比Top200) | 账号: 170000758981
"""
import json
import os
import time

# 全部正斜杠，彻底消除 \U unicode 转义
BASE = "C:/Users/Public"
CMD = BASE + "/qmt_cmd.json"
BAL = BASE + "/qmt_balance.json"
POS = BASE + "/qmt_position.json"
ORD = BASE + "/qmt_order.json"
TRD = BASE + "/qmt_trades.json"
MARKET_FILE = BASE + "/qmt_market.json"
INDEX_FILE = BASE + "/qmt_index.json"
STOCKPOOL_FILE = BASE + "/stockpool.json"
ACCOUNT = "170000758981"

INDICES = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]
POOL_SIZE = 200
_imported_trade_ids = set()

def _q(sql, p=None):
    try:
        import pymysql
        c = pymysql.connect(host="192.168.10.30", port=3306, user="root",
                             password="root123", database="quant_db",
                             charset="utf8mb4", connect_timeout=5)
        cur = c.cursor()
        cur.execute(sql, p or ())
        c.commit()
        cur.close()
        c.close()
    except Exception as e:
        print("MySQL:", e)

def init(ContextInfo):
    codes = []
    if os.path.exists(STOCKPOOL_FILE):
        try:
            with open(STOCKPOOL_FILE, encoding="utf-8") as f:
                pool = json.load(f)
            if isinstance(pool, dict) and "codes" in pool:
                # 新格式: {"ts":..., "n":..., "codes":["002214","300085",...]}
                codes = [str(c) for c in pool["codes"] if len(str(c)) >= 6]
                codes = codes[:POOL_SIZE]
                print("v5 股票池: stockpool.json(v2) -> %d只" % len(codes))
            elif isinstance(pool, list):
                # 旧格式: [{"code":"600837.SH",...},...]
                for s in pool:
                    c = s.get("code", "")
                    if c and len(c) >= 6:
                        codes.append(c)
                    if len(codes) >= POOL_SIZE:
                        break
                print("v5 股票池: stockpool.json(v1) -> %d只" % len(codes))
            else:
                print("v5 stockpool格式未知: %s" % type(pool).__name__)
                codes = []
        except Exception as e:
            print("v5 stockpool读取失败: %s, 回退沪深300" % e)
            codes = []
    if not codes:
        all_stocks = ContextInfo.get_stock_list_in_sector("沪深300")
        codes = all_stocks[:POOL_SIZE]
        print("v5 股票池: 沪深300 前%d只" % len(codes))

    # 统一格式 + 过滤无效代码
    valid_codes = []
    for c in codes:
        try:
            cstr = str(c).strip()
            # 去掉可能的后缀再统一加上
            cstr = cstr.replace(".SH","").replace(".SZ","").replace(".sh","").replace(".sz","")
            if len(cstr) == 6 and cstr.isdigit():
                suffix = ".SH" if cstr.startswith("6") else ".SZ"
                valid_codes.append(cstr + suffix)
            else:
                print("v5 跳过: %s" % c)
        except:
            pass
    codes = valid_codes
    ContextInfo._codes = codes
    ContextInfo._last_cache_sync = 0
    ContextInfo._last_market_sync = 0
    ContextInfo._first_run = True
    ContextInfo.set_universe(codes + INDICES)
    print("v5 启动: %d股+%d指数 账号=%s" % (len(codes), len(INDICES), ACCOUNT))

def handlebar(ContextInfo):
    try:
        _handlebar_impl(ContextInfo)
    except Exception as e:
        print("handlebar crash:", str(e))

def _handlebar_impl(ContextInfo):
    now = time.time()
    codes = ContextInfo._codes

    # ====== 1. 交易命令 ======
    try:
        if os.path.exists(CMD):
            with open(CMD, encoding="utf-8") as f:
                cmd = json.load(f)
            if cmd.get("status") == "pending":
                action = cmd.get("action", "")
                code = cmd["code"].strip()
                price = float(cmd.get("price", 0))
                amount = int(cmd.get("amount", 0))
                if "." not in code:
                    code = code + (".SH" if code.startswith("6") else ".SZ")

                # ---- 撤单 ----
                if action == "CANCEL":
                    oid = cmd.get("order_id", "")
                    if oid:
                        cancel(oid, ACCOUNT, ContextInfo)
                        cmd["status"] = "canceled"
                        cmd["msg"] = "撤单已提交"
                        print("cancel:", oid)
                    else:
                        cmd["status"] = "failed"
                        cmd["error"] = "缺少order_id"
                    with open(CMD, "w", encoding="utf-8") as f:
                        json.dump(cmd, f, ensure_ascii=False)
                    return

                # ---- 买卖 ----
                if action not in ("BUY", "SELL", "BUY_TARGET"):
                    cmd["status"] = "failed"
                    cmd["error"] = "未知动作: " + action
                    with open(CMD, "w", encoding="utf-8") as f:
                        json.dump(cmd, f, ensure_ascii=False)
                    return

                accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
                aid = str(accts[0].m_strAccountID) if accts else ACCOUNT
                pt = int(cmd.get("priceType", 11))

                if action in ("BUY", "BUY_TARGET"):
                    op = 23
                else:
                    op = 24

                oid = passorder(op, 1101, aid, code, pt, price, amount, "qmt_v5", 2, ContextInfo)
                print("passorder: %s %s %d@%.2f -> %s" % (action, code, amount, price, oid))

                if oid and oid > 0:
                    cmd["status"] = "submitted"
                    cmd["order_id"] = str(oid)
                    # 轮询等待成交状态(最多15秒)
                    for _ in range(15):
                        time.sleep(1)
                        try:
                            orders = get_trade_detail_data(ACCOUNT, "STOCK", "ORDER")
                            if orders:
                                for o in orders:
                                    if str(o.m_strOrderSysID) == str(oid):
                                        rs = int(str(o.m_nOrderStatus))
                                        tv = int(str(o.m_nVolumeTraded))
                                        if rs == 50:
                                            cmd["status"] = "filled"
                                            cmd["filled_volume"] = tv
                                            break
                                        elif rs == 52:
                                            cmd["status"] = "partial"
                                            cmd["filled_volume"] = tv
                                        elif rs == 53:
                                            cmd["status"] = "rejected"
                                            break
                                        elif rs == 51:
                                            cmd["status"] = "canceled"
                                            break
                        except:
                            pass
                        if cmd.get("status") != "submitted":
                            break
                else:
                    cmd["status"] = "failed"
                    cmd["error"] = "passorder返回0"

                with open(CMD, "w", encoding="utf-8") as f:
                    json.dump(cmd, f, ensure_ascii=False)
    except Exception as e:
        print("trade err:", e)
        try:
            cmd["status"] = "failed"
            cmd["error"] = str(e)
            with open(CMD, "w", encoding="utf-8") as f:
                json.dump(cmd, f, ensure_ascii=False)
        except:
            pass

    # ====== 2. 缓存(30秒): 余额/持仓/委托/成交 ======
    if now - ContextInfo._last_cache_sync >= 30:
        ContextInfo._last_cache_sync = now

        # 余额
        try:
            accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
            if accts:
                a = accts[0]
                available = float(str(a.m_dAvailable))
                total_asset = float(str(a.m_dBalance))
                market_value = float(str(a.m_dMarketValue))
                frozen = float(str(a.m_dFrozenCash))
                if not (available == 0 and total_asset == 0 and market_value == 0):
                    with open(BAL, "w", encoding="utf-8") as f:
                        json.dump({
                            "available": available,
                            "total_asset": total_asset,
                            "market_value": market_value,
                            "frozen": frozen,
                            "ts": now
                        }, f, ensure_ascii=False)
        except Exception as e:
            print("bal err:", e)

        # 持仓
        try:
            positions = get_trade_detail_data(ACCOUNT, "STOCK", "POSITION")
            if positions is not None:
                pl = []
                for p in positions:
                    vol = int(str(p.m_nVolume)) if hasattr(p, "m_nVolume") else 0
                    mv = float(str(p.m_dMarketValue)) if hasattr(p, "m_dMarketValue") else 0
                    pl.append({
                        "code": str(p.m_strInstrumentID),
                        "name": str(p.m_strInstrumentName) if hasattr(p, "m_strInstrumentName") else "",
                        "volume": vol,
                        "price": float(str(p.m_dOpenPrice)) if hasattr(p, "m_dOpenPrice") else 0,
                        "market_value": mv,
                        "profit": float(str(p.m_dFloatProfit)) if hasattr(p, "m_dFloatProfit") else 0
                    })
                with open(POS, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "positions": pl}, f, ensure_ascii=False)
        except Exception as e:
            print("pos err:", e)

        # 委托
        try:
            orders = get_trade_detail_data(ACCOUNT, "STOCK", "ORDER")
            if orders is not None:
                ol = []
                for o in orders:
                    rs = int(str(o.m_nOrderStatus)) if hasattr(o, "m_nOrderStatus") else 0
                    sm = {48: "pending", 50: "filled", 51: "canceled", 52: "partial", 53: "rejected"}
                    ol.append({
                        "code": str(o.m_strInstrumentID),
                        "id": str(o.m_strOrderSysID),
                        "status": sm.get(rs, str(rs)),
                        "price": float(str(o.m_dLimitPrice)),
                        "volume": int(str(o.m_nVolumeTotalOriginal)),
                        "traded": int(str(o.m_nVolumeTraded))
                    })
                with open(ORD, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "orders": ol}, f, ensure_ascii=False)
        except Exception as e:
            print("ord err:", e)

        # 成交
        try:
            trades = get_trade_detail_data(ACCOUNT, "STOCK", "TRADE")
            if trades is not None:
                tl = []
                for t in trades:
                    tid = str(t.m_strTradeID) if hasattr(t, "m_strTradeID") else ""
                    tl.append({
                        "code": str(t.m_strInstrumentID),
                        "name": str(t.m_strInstrumentName) if hasattr(t, "m_strInstrumentName") else "",
                        "price": float(str(t.m_dTradePrice)) if hasattr(t, "m_dTradePrice") else 0,
                        "volume": int(str(t.m_nTradeVolume)) if hasattr(t, "m_nTradeVolume") else 0,
                        "amount": float(str(t.m_dTradeAmount)) if hasattr(t, "m_dTradeAmount") else 0,
                        "time": str(t.m_strTradeTime) if hasattr(t, "m_strTradeTime") else "",
                        "id": tid,
                        "order_id": str(t.m_strOrderSysID) if hasattr(t, "m_strOrderSysID") else ""
                    })
                    # MySQL写入 (iQuant内置Python无pymysql, 静默失败)
                    if tid and tid not in _imported_trade_ids:
                        code = str(t.m_strInstrumentID)
                        if "." not in code:
                            code = code + (".SH" if code.startswith("6") else ".SZ")
                        # m_nBSFlag: 1=买入, 2=卖出
                        if hasattr(t, "m_nBSFlag"):
                            action = "BUY" if int(str(t.m_nBSFlag)) == 1 else "SELL"
                        else:
                            action = "BUY"
                        name = str(t.m_strInstrumentName) if hasattr(t, "m_strInstrumentName") else ""
                        pr = float(str(t.m_dTradePrice)) if hasattr(t, "m_dTradePrice") else 0
                        vol = int(str(t.m_nTradeVolume)) if hasattr(t, "m_nTradeVolume") else 0
                        amt = float(str(t.m_dTradeAmount)) if hasattr(t, "m_dTradeAmount") else 0
                        oid = str(t.m_strOrderSysID) if hasattr(t, "m_strOrderSysID") else ""
                        _q("INSERT INTO qmt_trades(ts_code,stock_name,action,price,quantity,amount,order_id,trade_time,reason,status) VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),'QMT','filled')",
                           (code, name, action, pr, vol, amt, oid))
                        _imported_trade_ids.add(tid)
                with open(TRD, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "trades": tl}, f, ensure_ascii=False)
        except Exception as e:
            print("trade rec err:", e)

    # ====== 3. 行情(3秒): 个股 + 指数 ======
    if now - ContextInfo._last_market_sync >= 3:
        ContextInfo._last_market_sync = now

        # 个股
        try:
            ticks = ContextInfo.get_full_tick(codes)
            data = {"ts": now, "stocks": []}
            for code in codes:
                last = vol = pct = bid1 = ask1 = amt = bidVol1 = askVol1 = 0.0
                if ticks and code in ticks:
                    t = ticks[code]
                    last = float(t.get("lastPrice", 0) or 0)
                    vol = float(t.get("volume", 0) or 0)
                    pct = float(t.get("pctChg", 0) or 0)
                    bid1 = float(t.get("bidPrice1", 0) or 0)
                    ask1 = float(t.get("askPrice1", 0) or 0)
                    amt = float(t.get("amount", 0) or 0)
                    bidVol1 = float(t.get("bidVol1", 0) or 0)
                    askVol1 = float(t.get("askVol1", 0) or 0)
                data["stocks"].append({
                    "code": code, "last": last, "volume": vol,
                    "pctChg": pct, "bid1": bid1, "ask1": ask1,
                    "amount": amt, "bidVol1": bidVol1, "askVol1": askVol1
                })
            with open(MARKET_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print("mkt err:", e)

        # 指数
        try:
            ticks = ContextInfo.get_full_tick(INDICES)
            idx = {"ts": now, "indices": []}
            for code in INDICES:
                last = pct = 0.0
                if ticks and code in ticks:
                    t = ticks[code]
                    last = float(t.get("lastPrice", 0) or 0)
                    pct = float(t.get("pctChg", 0) or 0)
                idx["indices"].append({"code": code, "last": last, "pctChg": pct})
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(idx, f, ensure_ascii=False)
        except Exception as e:
            print("idx err:", e)
