#coding:gbk
"""
QMT 策略 v23 — 增强版
- v22 基础: JSON 文件 IPC 缓存(余额/持仓/委托/成交) + passorder 下单
- v23 新增: 环境变量 DB 配置、市价单支持、部分成交状态、成交回调实时写入
"""
import json
import os
import time

CMD = r"C:\Users\Public\qmt_cmd.json"
BAL = r"C:\Users\Public\qmt_balance.json"
POS = r"C:\Users\Public\qmt_position.json"
ORD = r"C:\Users\Public\qmt_order.json"
TRD = r"C:\Users\Public\qmt_trades.json"    # 成交记录
ACCOUNT = "170000758981"  # 实盘账号

# MySQL 连接（写入成交记录到 Mac 的 qmt_trades 表）
def _get_db_config():
    return {
        "host": os.environ.get("DB_HOST", "192.168.10.30"),
        "port": int(os.environ.get("DB_PORT", 3306)),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_DATABASE", "quant_db"),
        "charset": "utf8mb4",
    }

_imported_trade_ids = set()


def _q(sql, p=None):
    try:
        import pymysql
        c = pymysql.connect(**_get_db_config(), connect_timeout=5)
        cur = c.cursor()
        cur.execute(sql, p or ())
        c.commit()
        cur.close()
        c.close()
    except Exception as e:
        print("MySQL异常:", e)


def init(ContextInfo):
    ContextInfo.set_universe(["000001.SZ"])
    print("v23启动-全量缓存")
    ContextInfo._last_sync = 0


def handlebar(ContextInfo):
    now = time.time()
    if now - ContextInfo._last_sync < 30:
        # 检查命令
        if os.path.exists(CMD):
            try:
                with open(CMD) as f:
                    cmd = json.load(f)
                if cmd.get("status") == "pending":
                    _handle_cmd(cmd, ContextInfo)
            except:
                pass
        return

    ContextInfo._last_sync = now

    # ---- 缓存余额 ----
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

    # ---- 缓存持仓（独立try，崩溃不影响） ----
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

    # ---- 缓存委托（独立try） ----
    try:
        orders = get_trade_detail_data(ACCOUNT, "STOCK", "ORDER")
        if orders is not None:
            ord_list = []
            for o in orders:
                # 状态映射: 48=已报, 50=已成交, 51=已撤, 52=部分成交, 53=废单
                raw_status = int(str(o.m_nOrderStatus)) if hasattr(o, "m_nOrderStatus") else 0
                status_map = {48: "pending", 50: "filled", 51: "canceled", 52: "partial", 53: "rejected"}
                ord_list.append({
                    "code": str(o.m_strInstrumentID),
                    "id": str(o.m_strOrderSysID),
                    "status": status_map.get(raw_status, str(raw_status)),
                    "raw_status": raw_status,
                    "price": float(str(o.m_dLimitPrice)),
                    "volume": int(str(o.m_nVolumeTotalOriginal)),
                    "traded": int(str(o.m_nVolumeTraded)),
                })
            with open(ORD, "w") as f:
                json.dump({"ts": now, "orders": ord_list}, f)
    except Exception as e:
        print("委托异常:", e)

    # ---- 缓存成交记录 + 写入 MySQL（独立try） ----
    try:
        trades = get_trade_detail_data(ACCOUNT, "STOCK", "TRADE")
        if trades is not None:
            trd_list = []
            for t in trades:
                trade_id = str(t.m_strTradeID) if hasattr(t, "m_strTradeID") else ""
                trd_list.append({
                    "code": str(t.m_strInstrumentID),
                    "name": str(t.m_strInstrumentName) if hasattr(t, "m_strInstrumentName") else "",
                    "price": float(str(t.m_dTradePrice)) if hasattr(t, "m_dTradePrice") else 0,
                    "volume": int(str(t.m_nTradeVolume)) if hasattr(t, "m_nTradeVolume") else 0,
                    "amount": float(str(t.m_dTradeAmount)) if hasattr(t, "m_dTradeAmount") else 0,
                    "time": str(t.m_strTradeTime) if hasattr(t, "m_strTradeTime") else "",
                    "id": trade_id,
                    "order_id": str(t.m_strOrderSysID) if hasattr(t, "m_strOrderSysID") else "",
                    "bs_flag": int(str(t.m_nBSFlag)) if hasattr(t, "m_nBSFlag") else 0,  # 0=买入 1=卖出
                })
                # 未导入过的成交记录写入 MySQL
                if trade_id and trade_id not in _imported_trade_ids:
                    code = str(t.m_strInstrumentID)
                    if "." not in code:
                        code = code + (".SH" if code.startswith("6") else ".SZ")
                    bs_flag = int(str(t.m_nBSFlag)) if hasattr(t, "m_nBSFlag") else 0
                    action = "SELL" if bs_flag == 1 else "BUY"
                    stock_name = str(t.m_strInstrumentName) if hasattr(t, "m_strInstrumentName") else ""
                    price = float(str(t.m_dTradePrice)) if hasattr(t, "m_dTradePrice") else 0
                    volume = int(str(t.m_nTradeVolume)) if hasattr(t, "m_nTradeVolume") else 0
                    amount = float(str(t.m_dTradeAmount)) if hasattr(t, "m_dTradeAmount") else 0
                    trade_time = str(t.m_strTradeTime) if hasattr(t, "m_strTradeTime") else ""
                    order_id = str(t.m_strOrderSysID) if hasattr(t, "m_strOrderSysID") else ""
                    # QMT 成交时间格式 HHMMSS → 拼接日期
                    if trade_time and len(trade_time) == 6:
                        trade_time = trade_time[:2] + ":" + trade_time[2:4] + ":" + trade_time[4:]
                        trade_dt = "CONCAT(CURDATE(), ' ', '%s')" % trade_time
                    else:
                        trade_dt = "NOW()"
                    _q("""INSERT INTO qmt_trades
                          (ts_code, stock_name, action, price, quantity, amount, order_id, trade_time, reason, status, mode)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,""" + trade_dt + """,'QMT策略','filled','live')""",
                       (code, stock_name, action, price, volume, amount, order_id))
                    _imported_trade_ids.add(trade_id)
            with open(TRD, "w") as f:
                json.dump({"ts": now, "trades": trd_list}, f)
    except Exception as e:
        print("成交异常:", e)


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
                cmd["error"] = "no account"
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

    if action == "TICK":
        # 获取实时行情快照
        try:
            code = cmd["code"].strip()
            if "." not in code:
                code = code + (".SH" if code.startswith("6") else ".SZ")
            tick = get_full_tick([code])
            if tick:
                cmd["status"] = "done"
                cmd["tick"] = {
                    "last": float(str(tick[code].m_dLastPrice)) if hasattr(tick[code], "m_dLastPrice") else 0,
                    "bid1": float(str(tick[code].m_dBidPrice1)) if hasattr(tick[code], "m_dBidPrice1") else 0,
                    "ask1": float(str(tick[code].m_dAskPrice1)) if hasattr(tick[code], "m_dAskPrice1") else 0,
                    "volume": int(str(tick[code].m_nVolume)) if hasattr(tick[code], "m_nVolume") else 0,
                    "amount": float(str(tick[code].m_dTradeAmount)) if hasattr(tick[code], "m_dTradeAmount") else 0,
                }
            else:
                cmd["status"] = "failed"
                cmd["error"] = "no tick data"
        except Exception as e:
            cmd["status"] = "failed"
            cmd["error"] = str(e)
        with open(CMD, "w") as f:
            json.dump(cmd, f)
        return

    # 模拟盘非交易时段 is_last_bar() 可能返回 False，跳过此检查确保命令可执行
    if not ContextInfo.is_last_bar():
        print("警告: is_last_bar=False, 仍尝试执行命令")

    code = cmd["code"].strip()
    price = float(cmd["price"])
    amount = int(cmd["amount"])
    raw = code
    if "." not in raw:
        raw = raw + (".SH" if raw.startswith("6") else ".SZ")

    # 动态获取账户ID（不能硬编码，iQuant里m_strAccountID可能不同）
    try:
        accts = get_trade_detail_data(ACCOUNT, "STOCK", "ACCOUNT")
        if accts:
            real_aid = str(accts[0].m_strAccountID)
            print("动态获取账户ID: %s (配置值: %s)" % (real_aid, ACCOUNT))
        else:
            real_aid = ACCOUNT
    except:
        real_aid = ACCOUNT

    # 价格类型: 支持市价单 (priceType=0/-1) 和限价单 (priceType=11)
    price_type = int(cmd.get("priceType", 11))

    print("执行: %s %s %d@%.2f type=%d aid=%s" % (action, raw, amount, price, price_type, real_aid))

    try:
        if action == "BUY":
            op = 23
        elif action == "SELL":
            op = 24
        elif action == "BUY_TARGET":
            # order_target_value: 按目标金额买入
            oid = passorder(35, 1101, real_aid, raw, 11, amount, price, "qmt_v23", 2, ContextInfo)
            print("passorder target_value:", oid)
            cmd["status"] = "done" if oid and oid > 0 else "failed"
            cmd["order_id"] = str(oid) if oid else ""
            with open(CMD, "w") as f:
                json.dump(cmd, f)
            return
        else:
            cmd["status"] = "failed"
            cmd["error"] = "unknown action: " + action
            with open(CMD, "w") as f:
                json.dump(cmd, f)
            return

        oid = passorder(op, 1101, real_aid, raw, price_type, price, amount, "qmt_v23", 2, ContextInfo)
        print("passorder:", oid)
        if oid and oid > 0:
            cmd["status"] = "submitted"
            cmd["order_id"] = str(oid)
            # 轮询等待成交结果（最多15秒）
            for _ in range(15):
                time.sleep(1)
                try:
                    orders = get_trade_detail_data(ACCOUNT, "STOCK", "ORDER")
                    if orders:
                        for o in orders:
                            if str(o.m_strOrderSysID) == str(oid):
                                raw_status = int(str(o.m_nOrderStatus))
                                traded_vol = int(str(o.m_nVolumeTraded))
                                if raw_status == 50:  # 已成交
                                    cmd["status"] = "filled"
                                    cmd["filled_volume"] = traded_vol
                                    break
                                elif raw_status == 52:  # 部分成交
                                    cmd["status"] = "partial"
                                    cmd["filled_volume"] = traded_vol
                                    # 部分成交继续等，但标记状态
                                elif raw_status == 53:  # 废单
                                    cmd["status"] = "rejected"
                                    break
                                elif raw_status == 51:  # 已撤
                                    cmd["status"] = "canceled"
                                    break
                except:
                    pass
                if cmd.get("status") != "submitted":
                    break
        else:
            cmd["status"] = "failed"
            cmd["error"] = "passorder returned 0"
    except Exception as e:
        print("异常:", e)
        cmd["status"] = "failed"
        cmd["error"] = str(e)

    with open(CMD, "w") as f:
        json.dump(cmd, f)
