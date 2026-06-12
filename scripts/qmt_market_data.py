#coding:gbk
"""
QMT 实时行情采集策略 v2
- 采集个股行情（可配置股票池）→ qmt_market.json
- 采集指数行情 → qmt_index.json
- 供 Mac 端 realtime_scanner / market_service 使用

部署：在 iQuant 策略研究里粘贴运行，或作为独立策略加载
"""
import json
import time

BASE = r"C:\Users\Public"
MARKET_FILE = BASE + r"\qmt_market.json"
INDEX_FILE = BASE + r"\qmt_index.json"
WATCHLIST_FILE = BASE + r"\qmt_watchlist.json"

# 默认监控指数
INDICES = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]

# 默认股票池（沪深300前50只 + 可通过 WATCHLIST 动态扩展）
POOL_SIZE = 50
POOL_SECTOR = "沪深300"


def init(ContextInfo):
    """初始化：加载股票池"""
    all_stocks = ContextInfo.get_stock_list_in_sector(POOL_SECTOR)
    ContextInfo._codes = all_stocks[:POOL_SIZE]
    # 尝试从 watchlist 扩展股票池
    try:
        if __import__('os').path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE) as f:
                extra = json.load(f)
            if isinstance(extra, list):
                for c in extra:
                    if c not in ContextInfo._codes:
                        ContextInfo._codes.append(c)
    except:
        pass
    ContextInfo.set_universe(ContextInfo._codes + INDICES)
    print("QMT行情策略v2启动: %d只个股 + %d个指数" % (len(ContextInfo._codes), len(INDICES)))


def handlebar(ContextInfo):
    now = time.time()
    codes = ContextInfo._codes

    # ---- 个股行情快照 ----
    try:
        ticks = ContextInfo.get_full_tick(codes)
        data = {"ts": now, "stocks": []}
        for code in codes:
            last = vol = pct = bid1 = ask1 = amt = 0.0
            if ticks and code in ticks:
                t = ticks[code]
                last = float(t.get("lastPrice", 0) or 0)
                vol = float(t.get("volume", 0) or 0)
                pct = float(t.get("pctChg", 0) or 0)
                bid1 = float(t.get("bidPrice1", 0) or 0)
                ask1 = float(t.get("askPrice1", 0) or 0)
                amt = float(t.get("amount", 0) or 0)
            data["stocks"].append({
                "code": code, "last": last, "volume": vol,
                "pctChg": pct, "bid1": bid1, "ask1": ask1, "amount": amt,
            })
        with open(MARKET_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("个股行情异常:", e)

    # ---- 指数行情 ----
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
        with open(INDEX_FILE, "w") as f:
            json.dump(idx, f)
    except Exception as e:
        print("指数行情异常:", e)
