#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国信iQuant (QMT) 量化交易服务
取代同花顺+pywinauto方案，通过xtquant直接对接国信柜台

启动:
  1. 打开国信iQuant策略交易平台并登录
  2. 运行: pythonw C:\start_trader_qmt.py
     (或 pythonw C:\start_trader_qmt.py --sim 使用模拟账号)
"""

import logging
import os
import sys
import time as _time
from functools import wraps
from threading import Lock

from flask import Flask, jsonify, request

from xtquant import xttrader, xtconstant
from xtquant.xttype import StockAccount

# ── 配置 ──
QMT_USERDATA = r"C:\国信iQuant策略交易平台\userdata\users\18978253999"
SESSION_ID = 1  # 会话ID，整数
ACCOUNT_REAL = "620000221031"      # 实盘账号
ACCOUNT_SIM = "100010027115"       # 模拟账号

PORT = 1430
LOG_FILE = r"C:\qmt_trader.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── 全局状态 ──
trader = None
account = None
connected = False
lock = Lock()


# ========== QMT 回调 ==========
class QmtCallback(xttrader.XtQuantTraderCallback):
    def on_connected(self):
        logger.info("✅ QMT 连接成功")

    def on_disconnected(self):
        logger.info("❌ QMT 连接断开")

    def on_account_status(self, status):
        logger.info(f"账户状态: {status}")

    def on_stock_asset(self, asset):
        logger.info(f"资产: 可用{asset.cash:.2f} 市值{asset.market_value:.2f} 总{asset.total_asset:.2f}")

    def on_stock_order(self, order):
        logger.info(f"委托: {order.stock_code} {'买' if order.order_type==23 else '卖'} {order.order_volume}股 @ {order.price}")

    def on_stock_trade(self, trade):
        logger.info(f"成交: {trade.stock_code} {trade.traded_volume}股 @ {trade.traded_price}")

    def on_order_error(self, error):
        logger.error(f"下单错误: {error}")


# ========== 初始化 ==========
def init_qmt(use_sim=False):
    global trader, account, connected
    with lock:
        try:
            aid = ACCOUNT_SIM if use_sim else ACCOUNT_REAL
            trader = xttrader.XtQuantTrader(QMT_USERDATA, SESSION_ID, QmtCallback())
            trader.start()
            trader.connect()
            _time.sleep(2)  # 等待连接

            account = StockAccount(aid)
            trader.subscribe(account)
            connected = True
            logger.info(f"✅ QMT 就绪 (账号={aid})")
            return True
        except Exception as e:
            logger.error(f"❌ QMT 初始化失败: {e}")
            connected = False
            return False


# ========== 装饰器 ==========
def need_qmt(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not connected or trader is None:
            return jsonify({"error": "QMT未连接"}), 400
        return f(*args, **kwargs)
    return wrapper


def code_to_qmt(code):
    """300438 -> 300438.SZ, 600000 -> 600000.SH"""
    code = code.strip()
    if "." in code:
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"


# ========== API 端点 ==========
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "connected": connected, "time": _time.time()})


@app.route("/balance", methods=["GET"])
@need_qmt
def get_balance():
    try:
        asset = trader.query_stock_asset(account)
        if not asset:
            return jsonify({"error": "查询资产失败"}), 400
        return jsonify({
            "可用金额": asset.cash,
            "股票市值": asset.market_value,
            "总资产": asset.total_asset,
            "冻结资金": asset.frozen_cash,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/position", methods=["GET"])
@need_qmt
def get_position():
    try:
        positions = trader.query_stock_positions(account)
        result = []
        for p in positions:
            result.append({
                "code": p.stock_code,
                "volume": p.volume,
                "usable": p.usable_volume,
                "cost": p.cost_price,
                "price": p.market_value / p.volume if p.volume else 0,
                "profit": p.float_pnl,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/buy", methods=["POST"])
@need_qmt
def post_buy():
    try:
        data = request.get_json(force=True)
        code = code_to_qmt(data["code"])
        price = float(data["price"])
        amount = int(data["amount"])

        logger.info(f"买入: {code} {amount}股 @ {price}")
        oid = trader.order_stock(account, code, xtconstant.STOCK_BUY, amount, xtconstant.FIX_PRICE, price)
        if oid <= 0:
            return jsonify({"error": f"下单失败", "order_id": oid}), 400
        return jsonify({"order_id": oid, "code": code, "price": price, "amount": amount}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/sell", methods=["POST"])
@need_qmt
def post_sell():
    try:
        data = request.get_json(force=True)
        code = code_to_qmt(data["code"])
        price = float(data["price"])
        amount = int(data["amount"])

        logger.info(f"卖出: {code} {amount}股 @ {price}")
        oid = trader.order_stock(account, code, xtconstant.STOCK_SELL, amount, xtconstant.FIX_PRICE, price)
        if oid <= 0:
            return jsonify({"error": f"卖出失败", "order_id": oid}), 400
        return jsonify({"order_id": oid, "code": code, "price": price, "amount": amount}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/orders", methods=["GET"])
@need_qmt
def get_orders():
    try:
        orders = trader.query_stock_orders(account)
        result = []
        for o in orders:
            result.append({
                "id": o.order_id,
                "code": o.stock_code,
                "type": o.order_type,
                "volume": o.order_volume,
                "price": o.price,
                "status": o.order_status,
                "traded": o.traded_volume,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/unlock", methods=["POST"])
def unlock():
    return jsonify({"ok": True, "msg": "QMT无需解锁"})


@app.route("/keepalive", methods=["GET"])
def keepalive():
    return jsonify({"ok": connected, "time": _time.time()})


# ========== 主函数 ==========
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sim", action="store_true", help="使用模拟账号")
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("国信 QMT 量化交易服务")
    logger.info(f"端口: {PORT}")
    logger.info("=" * 50)

    if init_qmt(use_sim=args.sim):
        logger.info("✅ 启动成功")
        app.run(host="0.0.0.0", port=PORT, debug=False)
    else:
        logger.error("❌ 启动失败，确认iQuant/QMT客户端已打开并登录")
        sys.exit(1)
