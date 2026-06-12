#coding:gbk
"""
iQuant 行情数据写入策略
运行在 iQuant Python 策略中，每5秒将实时行情写入 JSON 文件
供 HTTP 服务读取

iQuant 中新建 Python 策略，粘贴此代码，选择「1秒」周期运行
"""
import json as _json
import time as _time

MARKET_FILE = r"C:\Users\Public\qmt_market.json"
# 监听的股票池
STOCKS = None  # None = 用沪深300


def init(ContextInfo):
    global STOCKS
    print("=" * 40)
    print("iQuant 行情写入策略启动")
    try:
        from xtquant import xtdata
        sectors = xtdata.get_sector_list()
        print(f"板块数: {len(sectors)}")
        if "沪深300" in sectors:
            from qmt_stock_lists import get_stocks_in_sector as _gsis; STOCKS = _gsis("沪深300")
            print(f"沪深300成分股: {len(STOCKS)}只")
    except Exception as e:
        print(f"xtdata加载失败: {e}")
        STOCKS = ["000001.SZ", "000002.SZ", "600000.SH", "600036.SH", "000858.SZ"]
    ContextInfo.set_universe(["000001.SZ"])
    print(f"监控 {len(STOCKS)} 只股票")
    print("=" * 40)


def handlebar(ContextInfo):
    try:
        from xtquant import xtdata

        # 取前20只做快照
        codes = STOCKS[:20] if len(STOCKS) > 20 else STOCKS
        ticks = xtdata.get_full_tick(codes)

        data = {
            "ts": _time.time(),
            "count": len(codes),
            "stocks": []
        }

        for c in codes:
            if c in ticks:
                t = ticks[c]
                data["stocks"].append({
                    "code": c,
                    "last": _safe(t.get("lastPrice")),
                    "open": _safe(t.get("open")),
                    "high": _safe(t.get("high")),
                    "low": _safe(t.get("low")),
                    "preClose": _safe(t.get("preClose")),
                    "pctChg": _safe(t.get("pctChg")),
                    "volume": _safe(t.get("volume")),
                    "amount": _safe(t.get("amount")),
                    "bid1": _safe(t.get("bidPrice1")),
                    "ask1": _safe(t.get("askPrice1")),
                    "time": str(t.get("time", "")),
                })

        with open(MARKET_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass  # 静默跳过，不影响交易


def _safe(v):
    try: return float(v) if v == v else None
    except: return None
