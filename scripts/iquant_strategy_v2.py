#coding:gbk
"""
iQuant 增强行情策略 v2
新增: 换手率、量比、振幅、流通市值、涨速、五档深度
输出到 C:\Users\Public\qmt_market_v2.json
"""
import json, os, time

OUTPUT = r"C:\Users\Public\qmt_market_v2.json"
INDEX_OUT = r"C:\Users\Public\qmt_index_v2.json"
INTERVAL = 3  # 3秒更新一次

# 指数列表
INDICES = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH", "000300.SH"]

# 沪深300成分股(流动性靠前的80只) —— 替代 get_stock_list_in_sector("沪深300")
HS300_STOCKS = [
    # 金融
    "600036.SH", "601318.SH", "600030.SH", "601398.SH", "601288.SH",
    "601939.SH", "601988.SH", "601328.SH", "600016.SH", "000001.SZ",
    "002142.SZ", "601166.SH", "600000.SH", "601818.SH", "601688.SH",
    "601601.SH", "600837.SH", "000166.SZ", "002736.SZ", "601211.SH",
    # 消费/白酒/医药
    "600519.SH", "000858.SZ", "000568.SZ", "002304.SZ", "600809.SH",
    "000333.SZ", "600276.SH", "300760.SZ", "300015.SZ", "603259.SH",
    "000651.SZ", "002415.SZ", "600690.SH", "000895.SZ", "002714.SZ",
    # 新能源/制造
    "300750.SZ", "002594.SZ", "601012.SH", "600438.SH", "002129.SZ",
    "300274.SZ", "601899.SH", "600104.SH", "000625.SZ", "601238.SH",
    # 科技/半导体
    "002371.SZ", "603501.SH", "688981.SH", "300782.SZ", "688012.SH",
    "002049.SZ", "300408.SZ", "603986.SH", "688396.SH", "002916.SZ",
    # TMT/互联网
    "300059.SZ", "002230.SZ", "300033.SZ", "688111.SH", "300124.SZ",
    "002236.SZ", "688036.SH", "300413.SZ", "002555.SZ", "300418.SZ",
    # 基建/周期/公用
    "601668.SH", "600585.SH", "600031.SH", "601390.SH", "601766.SH",
    "600900.SH", "601857.SH", "600028.SH", "601088.SH", "600150.SH",
    # 其他
    "002475.SZ", "300498.SZ", "601888.SH", "300014.SZ", "002050.SZ",
]

def init(ContextInfo):
    ContextInfo.stock_list = HS300_STOCKS[:80]
    ContextInfo._last_update = 0
    print(f"行情v2启动: {len(ContextInfo.stock_list)}只股票")

def handlebar(ContextInfo):
    now = time.time()
    if now - ContextInfo._last_update < INTERVAL:
        return
    ContextInfo._last_update = now

    stocks = []
    for code in ContextInfo.stock_list:
        try:
            tick = ContextInfo.get_full_tick([code])
            if not tick or code not in tick:
                continue
            t = tick[code]
            
            # 基础字段
            last = float(str(t.get("lastPrice", 0)))
            if last <= 0:
                continue
                
            # 计算买卖盘总挂单量(bidVol1~bidVol5求和)
            bid_vol_sum = sum(float(str(t.get("bidVol%d" % i, 0))) for i in range(1, 6))
            ask_vol_sum = sum(float(str(t.get("askVol%d" % i, 0))) for i in range(1, 6))
                
            stock = {
                "code": code,
                "last": last,
                "open": float(str(t.get("open", 0))),
                "high": float(str(t.get("high", 0))),
                "low": float(str(t.get("low", 0))),
                "volume": float(str(t.get("volume", 0))),
                "amount": float(str(t.get("amount", 0))),
                "pctChg": float(str(t.get("pctChg", 0))),
                
                # v2 新增字段
                "turnoverRate": float(str(t.get("turnoverRate", 0))),
                "volRatio": float(str(t.get("volRatio", 0))),
                "amplitude": float(str(t.get("amplitude", 0))),
                "circulationValue": float(str(t.get("circulationValue", 0))),
                "totalMarketCap": float(str(t.get("totalMarketCap", 0))),
                "speed1m": float(str(t.get("speed1m", 0))),
                "speed5m": float(str(t.get("speed5m", 0))),
                
                # 五档买卖价格
                "bid1": float(str(t.get("bid1", 0))),
                "bid2": float(str(t.get("bid2", 0))),
                "bid3": float(str(t.get("bid3", 0))),
                "bid4": float(str(t.get("bid4", 0))),
                "bid5": float(str(t.get("bid5", 0))),
                "ask1": float(str(t.get("ask1", 0))),
                "ask2": float(str(t.get("ask2", 0))),
                "ask3": float(str(t.get("ask3", 0))),
                "ask4": float(str(t.get("ask4", 0))),
                "ask5": float(str(t.get("ask5", 0))),
                
                # 五档挂单量(逐档)
                "bidVol1": float(str(t.get("bidVol1", 0))),
                "bidVol2": float(str(t.get("bidVol2", 0))),
                "bidVol3": float(str(t.get("bidVol3", 0))),
                "bidVol4": float(str(t.get("bidVol4", 0))),
                "bidVol5": float(str(t.get("bidVol5", 0))),
                "askVol1": float(str(t.get("askVol1", 0))),
                "askVol2": float(str(t.get("askVol2", 0))),
                "askVol3": float(str(t.get("askVol3", 0))),
                "askVol4": float(str(t.get("askVol4", 0))),
                "askVol5": float(str(t.get("askVol5", 0))),
                
                # 买卖盘总挂单
                "bidVolSum": bid_vol_sum,
                "askVolSum": ask_vol_sum,
            }
            stocks.append(stock)
        except Exception as e:
            pass

    # 指数行情
    indices = []
    for idx_code in INDICES:
        try:
            tick = ContextInfo.get_full_tick([idx_code])
            if tick and idx_code in tick:
                t = tick[idx_code]
                indices.append({
                    "code": idx_code,
                    "last": float(str(t.get("lastPrice", 0))),
                    "open": float(str(t.get("open", 0))),
                    "high": float(str(t.get("high", 0))),
                    "low": float(str(t.get("low", 0))),
                    "pctChg": float(str(t.get("pctChg", 0))),
                    "amount": float(str(t.get("amount", 0))),
                    "volume": float(str(t.get("volume", 0))),
                })
        except:
            pass

    # 写入文件
    output = {"ts": now, "stocks": stocks}
    try:
        with open(OUTPUT, "w") as f:
            json.dump(output, f)
    except:
        pass

    idx_output = {"ts": now, "indices": indices}
    try:
        with open(INDEX_OUT, "w") as f:
            json.dump(idx_output, f)
    except:
        pass
