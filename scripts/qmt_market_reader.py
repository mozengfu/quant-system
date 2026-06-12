#!/usr/bin/env python3
"""
QMT 行情数据读取模块
基于 xtquant.xtdata，运行在 iQuant 已打开的 Windows 环境中

用法:
  python qmt_market_reader.py                     # 独立测试
  python qmt_market_reader.py --http :58610       # 启动HTTP行情服务
"""

import logging
import time

from qmt_stock_lists import get_stocks_in_sector
from xtquant import xtdata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("qmt_market")


class QmtMarketReader:
    """QMT 行情读取器"""

    @staticmethod
    def download_history(stock_list, period="1d", start="", end="", count=100):
        """下载历史数据到本地缓存（必须先下载再读取）"""
        xtdata.download_history_data(stock_list, period, start, end)
        logger.info(f"历史数据下载完成: {len(stock_list)}只, period={period}")

    @staticmethod
    def get_kline(stock_list, period="1d", start="20250101", end="", count=100,
                  fields=None):
        """获取K线数据"""
        if fields is None:
            fields = ["open", "high", "low", "close", "volume", "amount", "pctChg"]
        data = xtdata.get_market_data_ex(
            field_list=fields,
            stock_list=stock_list,
            period=period,
            start_time=start,
            end_time=end,
            count=count,
            dividend_type="front",  # 前复权
            fill_data=True,
        )
        return data

    @staticmethod
    def get_full_tick(stock_list):
        """获取全推实时快照"""
        return xtdata.get_full_tick(stock_list)

    @staticmethod
    def get_snapshot(stock_code):
        """获取单只股票快照"""
        tick = xtdata.get_full_tick([stock_code])
        if tick and stock_code in tick:
            t = tick[stock_code]
            return {
                "code": stock_code,
                "lastPrice": t.get("lastPrice"),
                "open": t.get("open"),
                "high": t.get("high"),
                "low": t.get("low"),
                "volume": t.get("volume"),
                "amount": t.get("amount"),
                "pctChg": t.get("pctChg"),
                "bidPrice": [t.get(f"bidPrice{i}") for i in range(1, 6)],
                "bidVolume": [t.get(f"bidVol{i}") for i in range(1, 6)],
                "askPrice": [t.get(f"askPrice{i}") for i in range(1, 6)],
                "askVolume": [t.get(f"askVol{i}") for i in range(1, 6)],
                "time": t.get("time"),
                "preClose": t.get("preClose"),
            }
        return None

    @staticmethod
    def get_sector_list():
        """获取所有板块列表"""
        return xtdata.get_sector_list()

    @staticmethod
    def get_stocks_in_sector(sector):
        """获取板块成分股"""
        return get_stocks_in_sector(sector)

    @staticmethod
    def get_instrument_detail(stock_code):
        """获取股票基本信息"""
        return xtdata.get_instrument_detail(stock_code)

    @staticmethod
    def get_trading_calendar(market="SH", start="", end=""):
        """获取交易日历"""
        return xtdata.get_trading_dates(market, start, end)

    @staticmethod
    def get_dividend(stock_code):
        """获取分红送配"""
        return xtdata.get_divid_factors(stock_code)


# ========== HTTP 服务 ==========
def run_http_service(host="0.0.0.0", port=58610):
    """启动独立的 QMT 行情 HTTP 服务"""
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    reader = QmtMarketReader()

    @app.route("/ping", methods=["GET"])
    def ping():
        return jsonify({"ok": True, "service": "qmt-market", "time": time.time()})

    @app.route("/snapshot", methods=["GET"])
    def snapshot():
        code = request.args.get("code", "").strip()
        if not code:
            codes = request.args.get("codes", "").strip()
            if codes:
                code_list = [c.strip() for c in codes.split(",")]
            else:
                return jsonify({"error": "code or codes required"}), 400
        else:
            code_list = [code]

        results = {}
        for c in code_list:
            if "." not in c:
                c = f"{c}.SH" if c.startswith("6") else f"{c}.SZ"
            snap = reader.get_snapshot(c)
            if snap:
                # 转成可JSON序列化
                results[c] = {k: (float(v) if isinstance(v, (int, float)) and v == v else v)
                              for k, v in snap.items() if v is not None}
        return jsonify(results)

    @app.route("/kline", methods=["GET"])
    def kline():
        code = request.args.get("code", "000001.SH")
        period = request.args.get("period", "1d")
        start = request.args.get("start", "")
        end = request.args.get("end", "")
        count = int(request.args.get("count", 100))

        if "." not in code:
            code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        fields_str = request.args.get("fields", "close,open,high,low,volume,amount")
        fields = [f.strip() for f in fields_str.split(",")]

        data = reader.get_kline([code], period=period, start=start, end=end, count=count, fields=fields)

        result = {"code": code, "period": period, "fields": fields, "data": {}}
        for field in fields:
            if field in data and code in data[field]:
                arr = data[field][code]
                # 处理 NaN
                result["data"][field] = [float(v) if v == v else None for v in arr.tolist()]

        if "time" in data:
            result["time"] = [str(t) for t in data["time"]]
        elif "date" in data:
            result["time"] = [str(d) for d in data["date"]]

        return jsonify(result)

    @app.route("/sectors", methods=["GET"])
    def sectors():
        return jsonify({"sectors": reader.get_sector_list()})

    @app.route("/sector_stocks", methods=["GET"])
    def sector_stocks():
        sector = request.args.get("sector", "沪深300")
        stocks = reader.get_stocks_in_sector(sector)
        return jsonify({"sector": sector, "count": len(stocks), "stocks": stocks[:100]})

    @app.route("/tick_snapshot", methods=["GET"])
    def tick_snapshot():
        """批量获取板块全推行情"""
        sector = request.args.get("sector", "沪深300")
        limit = int(request.args.get("limit", 10))
        stocks = reader.get_stocks_in_sector(sector)[:limit] if hasattr(reader, "get_stocks_in_sector") else get_stocks_in_sector(sector, limit)

        ticks = reader.get_full_tick(stocks)
        results = []
        for code in stocks:
            if code in ticks:
                t = ticks[code]
                results.append({
                    "code": code,
                    "lastPrice": t.get("lastPrice"),
                    "pctChg": t.get("pctChg"),
                    "volume": t.get("volume"),
                    "amount": t.get("amount"),
                    "time": str(t.get("time", "")),
                })
        return jsonify({"sector": sector, "count": len(results), "stocks": results})

    logger.info(f"QMT 行情服务启动: http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="QMT行情读取器")
    parser.add_argument("--http", type=str, default="", help="启动HTTP服务，如 :58610")
    args = parser.parse_args()

    if args.http:
        port = int(args.http.replace(":", ""))
        run_http_service(port=port)
    else:
        # 独立测试模式
        print("=" * 50)
        print("QMT 行情数据读取测试")
        print("=" * 50)

        reader = QmtMarketReader()

        # 1. 板块列表
        sectors = reader.get_sector_list()
        print(f"\n板块总数: {len(sectors)}")
        print(f"前10个: {sectors[:10]}")

        # 2. 沪深300成分股
        hs300 = reader.get_stocks_in_sector("沪深300")
        print(f"\n沪深300成分股: {len(hs300)}只")
        print(f"前5只: {hs300[:5]}")

        # 3. 单股快照
        print("\n上证指数快照:")
        snap = reader.get_snapshot("000001.SH")
        if snap:
            print(f"  最新价: {snap.get('lastPrice')}")
            print(f"  涨跌幅: {snap.get('pctChg')}%")
            print(f"  成交量: {snap.get('volume')}")
            print(f"  时间: {snap.get('time')}")

        # 4. 批量全推行情
        print("\n沪深300前5只实时行情:")
        ticks = reader.get_full_tick(hs300[:5])
        for code in hs300[:5]:
            if code in ticks:
                t = ticks[code]
                print(f"  {code}: 最新={t.get('lastPrice')} 涨跌={t.get('pctChg')}% 量={t.get('volume')}")

        # 5. K线测试
        print("\n000001.SH 最近5天日K线:")
        kline = reader.get_kline(["000001.SH"], period="1d", count=5)
        if "close" in kline and "000001.SH" in kline["close"]:
            closes = kline["close"]["000001.SH"]
            print(f"  收盘价序列: {[round(float(v), 2) for v in closes.tolist() if v == v]}")

        print("\n✅ 测试完成")
