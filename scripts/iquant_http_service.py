#!/usr/bin/env python3
"""
iQuant HTTP 状态服务 + IPC 命令写入
"""
import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, request

PORT = 1430

def _get_db_config():
    """获取数据库配置，优先环境变量，默认值用于开发环境"""
    return {
        "host": os.environ.get("DB_HOST", "192.168.10.30"),
        "port": int(os.environ.get("DB_PORT", 3306)),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_DATABASE", "quant_db"),
        "charset": "utf8mb4",
    }
IPC_CMD = "C:\\Users\\Public\\qmt_cmd.json"
IPC_RESULT = "C:\\Users\\Public\\qmt_result.json"
TRADES_FILE = "C:\\Users\\Public\\qmt_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("C:\\iquant_http.log", encoding="utf-8"), logging.StreamHandler()])
logger = logging.getLogger(__name__)
app = Flask(__name__)


def _q(sql, p=None):
    import pymysql
    c = pymysql.connect(**_get_db_config(), connect_timeout=5)
    try:
        cur = c.cursor()
        cur.execute(sql, p or ())
        # SELECT 返回结果，INSERT/UPDATE/DELETE 返回受影响行数
        if sql.strip().upper().startswith("SELECT"):
            r = cur.fetchall()
            cur.close()
            return r
        c.commit()
        cur.close()
    finally:
        c.close()


# ========== IPC 命令轮询 ==========
def _ipc_poller():
    logger.info("IPC poller started")
    done = set()
    while True:
        try:
            r = _q("SELECT id,ts_code,signal_type,price,shares FROM sim_signals WHERE status='待执行' ORDER BY id LIMIT 1")
            if r and r[0][0] not in done:
                sid, tc, st, pr, sh = r[0]
                st = (st or "").strip()
                if st in ("买入候选", "买入", "BUY"): a = "BUY"
                elif st == "BUY_TARGET": a = "BUY_TARGET"
                elif st in ("卖出", "止损", "止盈", "超时", "SELL"): a = "SELL"
                else: done.add(sid); continue
                c = tc.split(".")[0] if "." in tc else tc
                cmd = {"id": sid, "action": a, "code": c, "price": float(pr or 0), "amount": int(sh or 0), "status": "pending", "ts": time.time()}
                # 止损/恐慌清仓用市价单确保立即成交
                if st in ("止损", "恐慌清仓"):
                    cmd["priceType"] = -1
                    logger.info("IPC cmd: %s 使用市价单", st)
                # 清除旧结果文件
                if os.path.exists(IPC_RESULT):
                    try: os.remove(IPC_RESULT)
                    except: pass
                with open(IPC_CMD, "w", encoding="utf-8") as f:
                    json.dump(cmd, f, ensure_ascii=False)
                logger.info("IPC cmd: sig=%d %s %s %d@%.2f", sid, a, c, int(sh or 0), float(pr or 0))
                # 等待 v23 策略回写结果（CMD 文件状态从 pending 变为其他）
                for _ in range(30):
                    time.sleep(1)
                    if os.path.exists(IPC_CMD):
                        try:
                            with open(IPC_CMD, encoding="utf-8") as f:
                                res = json.load(f)
                            if res.get("id") == cmd["id"] and res.get("status") != "pending":
                                # v23 已回写结果
                                v23_status = res.get("status", "done")
                                if res.get("order_id") and str(res["order_id"]) != "0":
                                    if v23_status == "filled":
                                        _q("UPDATE sim_signals SET status='已执行',reason='成交:%s' WHERE id=%s", (str(res["order_id"]), sid))
                                    elif v23_status == "partial":
                                        filled_qty = res.get("filled_volume", 0)
                                        _q("UPDATE sim_signals SET status='部分成交',reason='部分成交:%s(%s股)' WHERE id=%s", (str(res["order_id"]), str(filled_qty), sid))
                                    elif v23_status == "rejected":
                                        _q("UPDATE sim_signals SET status='失败',reason='废单:%s' WHERE id=%s", (res.get("error", ""), sid))
                                    elif v23_status == "submitted":
                                        _q("UPDATE sim_signals SET status='已提交',reason='委托:%s' WHERE id=%s", (str(res["order_id"]), sid))
                                    else:
                                        _q("UPDATE sim_signals SET status='已提交',reason='委托:%s' WHERE id=%s", (str(res["order_id"]), sid))
                                    logger.info("IPC ok: sig=%d oid=%s status=%s", sid, res["order_id"], v23_status)
                                elif v23_status == "failed":
                                    _q("UPDATE sim_signals SET status='失败',reason='%s' WHERE id=%s", (res.get("error", "passorder返回0"), sid))
                                    logger.warning("IPC failed: sig=%d error=%s", sid, res.get("error", ""))
                                break
                        except: pass
                # 30秒超时，标记信号
                else:
                    _q("UPDATE sim_signals SET status='超时',reason='IPC等待超时(策略未响应)' WHERE id=%s", (sid,))
                    logger.warning("IPC timeout: sig=%d 策略30秒未响应", sid)
                done.add(sid)
        except Exception as e:
            logger.error("IPC: %s", e)
        time.sleep(5)
threading.Thread(target=_ipc_poller, daemon=True).start()


# ========== API ==========
@app.route("/ping", methods=["GET"])
def ping():
    try: _q("SELECT 1"); db = "ok"
    except: db = "error"
    return jsonify({"ok": True, "service": "iquant-http", "db": db, "time": time.time()})

@app.route("/balance", methods=["GET"])
def balance():
    try:
        r = _q("SELECT cash,total_value FROM sim_account ORDER BY updated_at DESC LIMIT 1")
        if r:
            cash, total = float(r[0][0] or 0), float(r[0][1] or 0)
            return jsonify({"可用金额": cash, "股票市值": max(0,total-cash), "总资产": total, "冻结资金": 0})
        return jsonify({"可用金额": 0, "股票市值": 0, "总资产": 0, "冻结资金": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/position", methods=["GET"])
def position():
    try:
        r = _q("SELECT ts_code,cost_price,current_price,shares,market_value,profit_loss,stock_name FROM sim_positions WHERE status='HOLD'")
        return jsonify([{"code":c,"cost_price":float(cp or 0),"current_price":float(pr or 0),"shares":int(sh or 0),
                        "market_value":float(mv or 0),"profit":float(pl or 0),"stock_name":sn or ""} for c,cp,pr,sh,mv,pl,sn in r])
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/positions", methods=["GET"])
def positions(): return position()

@app.route("/buy", methods=["POST"])
def buy():
    try:
        d = request.get_json(force=True)
        code = (d.get("code") or d.get("security") or "").strip()
        price = float(d.get("price", 0))
        shares = int(d.get("amount", d.get("shares", 0)))
        if not code or price <= 0 or shares <= 0: return jsonify({"error": "required"}), 400
        if "." not in code: code = ("%s.SH" % code) if code.startswith("6") else ("%s.SZ" % code)
        # 修复 P0: 之前忽略 action 字段，全部当 "买入候选" 处理
        # 现在尊重调用方传入的 action: BUY / BUY_TARGET
        # 兜底：缺省为"买入候选"
        action = (d.get("action") or "买入候选").strip() or "买入候选"
        if action not in ("买入候选", "买入", "BUY", "BUY_TARGET"):
            action = "买入候选"
        _q("INSERT INTO sim_signals(ts_code,price,shares,status,signal_type,reason,created_at,signal_date) VALUES(%s,%s,%s,'待执行',%s,'HTTP',NOW(),CURDATE())", (code,price,shares,action))
        return jsonify({"order_id": "sig_%d" % int(time.time()), "code": code, "price": price, "amount": shares, "action": action}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/sell", methods=["POST"])
def sell():
    try:
        d = request.get_json(force=True)
        code = (d.get("code") or d.get("security") or "").strip()
        price = float(d.get("price", 0))
        shares = int(d.get("amount", d.get("shares", 0)))
        if not code or price <= 0 or shares <= 0: return jsonify({"error": "required"}), 400
        if "." not in code: code = ("%s.SH" % code) if code.startswith("6") else ("%s.SZ" % code)
        # 修复 P0: 之前忽略 action 字段，全部当 "卖出" 处理 → IPC poller 不走市价单
        # 现在尊重调用方传入的 action: 卖出/止损/止盈/恐慌清仓/超时/RPS止损/SELL
        # priceType=-1 → 市价单 (v23 策略会用)
        action = (d.get("action") or "卖出").strip() or "卖出"
        valid_actions = ("卖出", "止损", "止盈", "恐慌清仓", "超时", "RPS止损", "SELL", "分批止盈", "兜底止盈")
        if action not in valid_actions:
            action = "卖出"
        price_type = d.get("priceType", 0)
        # 市价单时把 action 改成"止损"或"恐慌清仓"以触发 IPC poller 走 priceType=-1
        # 防止: 调用方传 "SELL" + priceType=-1 时被 IPC 当成普通限价
        if str(price_type) == "-1" and action in ("卖出", "SELL"):
            action = "止损"
        reason = "HTTP"
        if str(price_type) == "-1":
            reason = "HTTP(市价)"
        _q("INSERT INTO sim_signals(ts_code,price,shares,status,signal_type,reason,created_at,signal_date) VALUES(%s,%s,%s,'待执行',%s,%s,NOW(),CURDATE())", (code,price,shares,action,reason))
        return jsonify({"order_id": "sig_%d" % int(time.time()), "code": code, "price": price, "amount": shares, "action": action}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/orders", methods=["GET"])
def orders():
    try:
        r = _q("SELECT id,ts_code,signal_type,price,shares,status,close_date,reason,order_id FROM sim_signals WHERE created_at>=DATE_SUB(NOW(),INTERVAL 7 DAY) ORDER BY created_at DESC LIMIT 50")
        return jsonify({"orders": [{"id":str(i),"ts_code":c,"signal_type":s,"price":float(p or 0),"shares":int(sh or 0),
                                   "status":st,"close_date":str(cd or ""),"reason":str(rs or ""),"order_id":str(oid or "")}
                                   for i,c,s,p,sh,st,cd,rs,oid in r]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/cancel_order", methods=["POST"])
def cancel_order():
    """撤单 — 取消待执行的信号"""
    try:
        d = request.get_json(force=True) or {}
        order_id = d.get("order_id", "")
        if not order_id:
            return jsonify({"error": "order_id required"}), 400

        # 先尝试从 sim_signals 更新状态
        if order_id.startswith("sig_"):
            signal_id = order_id.replace("sig_", "")
            _q("UPDATE sim_signals SET status='已撤单',reason='HTTP cancel' WHERE id=%s AND status='待执行'", (signal_id,))
        else:
            _q("UPDATE sim_signals SET status='已撤单',reason='HTTP cancel' WHERE order_id=%s AND status='待执行'", (order_id,))

        # 写入取消命令到 IPC 文件
        try:
            cancel_cmd = {"action": "CANCEL", "order_id": order_id, "status": "pending", "ts": time.time()}
            with open(IPC_CMD, "w", encoding="utf-8") as f:
                json.dump(cancel_cmd, f, ensure_ascii=False)
            logger.info("Cancel cmd written for order: %s", order_id)
        except Exception as e:
            logger.warning("写入取消命令失败: %s", e)

        return jsonify({"ok": True, "order_id": order_id, "msg": "取消请求已提交"})
    except Exception as e:
        logger.error("撤单失败: %s", e)
        return jsonify({"error": str(e)}), 400

@app.route("/trades", methods=["GET"])
def trades():
    """返回 QMT 成交记录（从 qmt_trades.json 读取）"""
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({"ts": 0, "trades": []})
    except Exception as e:
        logger.error("读取成交记录失败: %s", e)
        return jsonify({"error": str(e)}), 400

# ========== 实时行情端点（读 QMT 行情策略写入的 JSON） ==========
MARKET_FILE = "C:\\Users\\Public\\qmt_market.json"
INDEX_FILE = "C:\\Users\\Public\\qmt_index.json"


@app.route("/market/snapshot", methods=["GET"])
def market_snapshot():
    """返回 QMT 实时行情快照（个股）"""
    try:
        if os.path.exists(MARKET_FILE):
            mtime = os.path.getmtime(MARKET_FILE)
            # 超过120秒没更新视为过期
            if time.time() - mtime > 120:
                return jsonify({"ts": mtime, "stocks": [], "stale": True})
            with open(MARKET_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({"ts": 0, "stocks": [], "stale": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/market/index", methods=["GET"])
def market_index():
    """返回 QMT 实时指数行情"""
    try:
        if os.path.exists(INDEX_FILE):
            mtime = os.path.getmtime(INDEX_FILE)
            if time.time() - mtime > 120:
                return jsonify({"ts": mtime, "indices": [], "stale": True})
            with open(INDEX_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return jsonify(data)
        return jsonify({"ts": 0, "indices": [], "stale": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/market/tick", methods=["GET"])
def market_tick():
    """获取单只股票实时tick（通过QMT策略IPC）"""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "code required"}), 400
    try:
        # 通过 IPC 命令获取实时tick
        cmd = {"id": int(time.time() * 1000), "action": "TICK", "code": code,
               "status": "pending", "ts": time.time()}
        with open(IPC_CMD, "w", encoding="utf-8") as f:
            json.dump(cmd, f, ensure_ascii=False)
        # 等待结果
        for _ in range(10):
            time.sleep(0.5)
            if os.path.exists(IPC_CMD):
                with open(IPC_CMD, encoding="utf-8") as f:
                    res = json.load(f)
                if res.get("id") == cmd["id"] and res.get("status") != "pending":
                    if res.get("tick"):
                        return jsonify(res["tick"])
                    return jsonify({"error": res.get("error", "no data")}), 400
        return jsonify({"error": "tick timeout"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/unlock", methods=["POST"])
def unlock(): return jsonify({"ok": True, "msg": "ok"})
@app.route("/keepalive", methods=["GET"])
def keepalive(): return jsonify({"ok": True, "time": time.time()})
@app.route("/prepare", methods=["POST"])
def prepare(): return jsonify({"ok": True, "msg": "ready"})

if __name__ == "__main__":
    logger.info("Starting on port %d", PORT)
    try: _q("SELECT 1"); logger.info("DB OK")
    except: logger.warning("DB unavailable")
    app.run(host="0.0.0.0", port=PORT, debug=False)
