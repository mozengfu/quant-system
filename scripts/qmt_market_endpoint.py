"""
QMT 行情 HTTP 接口 - 独立服务
运行在 QMT Windows 机器上 (iQuant 需已打开)
启动: python qmt_market_endpoint.py --port 58610
"""
import logging
import time

from flask import Flask, jsonify, request
from qmt_stock_lists import get_stocks_in_sector
from xtquant import xtdata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qmt_market")
app = Flask(__name__)

# ── 辅助函数 ──
def _fix_code(code):
    code = code.strip()
    if "." not in code:
        return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    return code

def _safe_num(v):
    """NaN -> None"""
    try:
        return float(v) if v == v else None
    except:
        return None


# ── 接口 ──
@app.route("/ping")
def ping():
    return jsonify({"service": "qmt-market", "ok": True, "time": time.time()})

@app.route("/sectors")
def sectors():
    return jsonify({"sectors": xtdata.get_sector_list()})

@app.route("/sector_stocks")
def sector_stocks():
    sector = request.args.get("sector", "沪深300")
    s = get_stocks_in_sector(sector)
    return jsonify({"sector": sector, "count": len(s), "stocks": s[:200]})

@app.route("/snapshot/<code>")
@app.route("/snapshot")
def snapshot(code=None):
    if not code:
        code = request.args.get("code", "")
        if not code:
            codes = request.args.get("codes", request.args.get("stocks", ""))
            if codes:
                code_list = [c.strip() for c in codes.split(",")]
            else:
                return jsonify({"error": "code or codes required"}), 400
        else:
            code_list = [_fix_code(code)]
    else:
        code_list = [_fix_code(code)]

    ticks = xtdata.get_full_tick(code_list)
    results = {}
    for c in code_list:
        if c in ticks:
            t = ticks[c]
            results[c] = {
                "last": _safe_num(t.get("lastPrice")),
                "open": _safe_num(t.get("open")),
                "high": _safe_num(t.get("high")),
                "low": _safe_num(t.get("low")),
                "preClose": _safe_num(t.get("preClose")),
                "pctChg": _safe_num(t.get("pctChg")),
                "volume": _safe_num(t.get("volume")),
                "amount": _safe_num(t.get("amount")),
                "bid1": _safe_num(t.get("bidPrice1")),
                "bidVol1": _safe_num(t.get("bidVol1")),
                "ask1": _safe_num(t.get("askPrice1")),
                "askVol1": _safe_num(t.get("askVol1")),
                "time": str(t.get("time", "")),
            }
    return jsonify(results)

@app.route("/kline")
def kline():
    code = _fix_code(request.args.get("code", "000001.SH"))
    period = request.args.get("period", "1d")
    count = int(request.args.get("count", 100))
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    fields_str = request.args.get("fields", "open,high,low,close,volume,amount,pctChg")
    fields = [f.strip() for f in fields_str.split(",")]

    data = xtdata.get_market_data_ex(
        field_list=fields, stock_list=[code], period=period,
        start_time=start, end_time=end, count=count,
        dividend_type="front", fill_data=True,
    )

    result = {"code": code, "period": period, "kline": []}
    # 按时间索引重组
    times = []
    tkey = "time" if "time" in data else ("date" if "date" in data else None)
    if tkey:
        times = [str(t) for t in data[tkey]]

    for i in range(len(times)):
        row = {"time": times[i]}
        for f in fields:
            if f in data and code in data[f]:
                row[f] = _safe_num(data[f][code][i])
        result["kline"].append(row)

    return jsonify(result)

@app.route("/batch_snapshot")
def batch_snapshot():
    sector = request.args.get("sector", "沪深300")
    limit = int(request.args.get("limit", 20))
    stocks = get_stocks_in_sector(sector)[:limit]
    ticks = xtdata.get_full_tick(stocks)

    results = []
    for c in stocks:
        if c in ticks:
            t = ticks[c]
            results.append({
                "code": c,
                "last": _safe_num(t.get("lastPrice")),
                "pctChg": _safe_num(t.get("pctChg")),
                "volume": _safe_num(t.get("volume")),
                "amount": _safe_num(t.get("amount")),
            })
    return jsonify({"sector": sector, "count": len(results), "stocks": results})


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=58610)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()

    logger.info(f"QMT 行情服务: http://{args.host}:{args.port}")
    logger.info("接口: /ping /sectors /snapshot?code=000001.SH /kline?code=000001.SH /batch_snapshot?sector=沪深300")
    app.run(host=args.host, port=args.port, debug=False)
