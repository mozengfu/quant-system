#!/usr/bin/env python3
"""
统一实时行情服务 — 所有实时数据读取的统一入口

调用链: 内存缓存 → 腾讯(主) → 东财 → 阿里云(兜底)
"""

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

# 确保直接运行时能找到 quant_app
_pkg_dir = os.path.join(os.path.dirname(__file__), "..", "..")
if _pkg_dir not in sys.path:
    sys.path.insert(0, os.path.abspath(_pkg_dir))

from quant_app.utils.config import ALIYUN_CODE, ALIYUN_HOST, get_db_config  # noqa: E402

logger = logging.getLogger(__name__)

# ========== 常量 ==========
EASTMONEY_HOST = "http://push2.eastmoney.com"
_QUOTE_CACHE_TTL = 30  # 个股行情缓存 30s
_BREADTH_CACHE_TTL = 60  # 涨跌家数缓存 60s


def _retry_urlopen(req, max_retries=3, timeout=3):
    """带重试的 urlopen（指数退避）"""
    import time

    for attempt in range(max_retries):
        try:
            return urlopen(req, timeout=timeout)
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def _get_limit_pct(ts_code, stock_name=""):
    """根据股票代码判断涨跌停幅度"""
    if "ST" in stock_name or "*ST" in stock_name:
        return 0.05
    if ts_code.startswith("68"):
        return 0.20  # 科创板
    if ts_code.startswith("30"):
        return 0.20  # 创业板
    if ts_code.startswith("8"):
        return 0.30  # 北交所
    return 0.10  # 主板


# ========== 统一内存缓存 ==========
_cache = {}
_cache_lock = threading.Lock()


def _get_cache(key, ttl):
    with _cache_lock:
        v = _cache.get(key)
        if v and time.time() - v["ts"] < ttl:
            return v["data"]
        return None


def _set_cache(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
        if len(_cache) > 500:
            now = time.time()
            for k in list(_cache.keys()):
                if now - _cache[k]["ts"] > 120:
                    del _cache[k]


# ========== 内部辅助：代码转换 ==========


def _code_to_secid(code, market="sz"):
    """代码转东方财富secid格式"""
    code = code.upper().strip()
    if code.startswith("SH"):
        return f"1.{code[2:]}"
    elif code.startswith("SZ"):
        return f"0.{code[2:]}"
    elif code.startswith("6"):
        return f"1.{code}"
    else:
        return f"0.{code}"


# ========== 个股行情源（统一17字段格式）==========


def _try_tencent(code, market):
    """腾讯财经实时行情"""
    try:
        req = UrlRequest(f"http://qt.gtimg.cn/q={market.lower()}{code}", headers={"User-Agent": "Mozilla/5.0"})
        with _retry_urlopen(req) as resp:
            data = resp.read().decode("gbk")
        if "~" not in data:
            return None
        parts = data.strip().rstrip(";").split("~")
        if len(parts) < 45:
            return None
        price = float(parts[3])
        prev_close = float(parts[4])
        limit_pct = _get_limit_pct(code, parts[1])
        return {
            "名称": parts[1],
            "现价": price,
            "昨收": prev_close,
            "今开": float(parts[5]),
            "最高": float(parts[33]),
            "最低": float(parts[34]),
            "成交量": int(float(parts[6])),
            "成交额": float(parts[37]) * 10000 if len(parts) > 37 else 0,
            "换手率": float(parts[38]) if len(parts) > 38 else 0,
            "量比": float(parts[46]) if len(parts) > 46 else 0,
            "涨停价": round(prev_close * (1 + limit_pct), 2) if prev_close > 0 else 0,
            "跌停价": round(prev_close * (1 - limit_pct), 2) if prev_close > 0 else 0,
            "52周高": float(parts[44]) if len(parts) > 44 else 0,
            "52周低": float(parts[45]) if len(parts) > 45 else 0,
            "市值": 0,
            "涨跌幅": float(parts[32]),
            "涨跌额": round(price - prev_close, 2),
        }
    except Exception as e:
        logger.warning(f"腾讯行情失败 {market}{code}: {e}")
        return None


def _try_eastmoney(code, market):
    """东方财富实时行情"""
    try:
        secid = _code_to_secid(code, market)
        fields = "f43,f44,f45,f46,f47,f48,f50,f51,f52,f57,f58,f60,f116,f167,f168,f170,f184"
        req = UrlRequest(f"{EASTMONEY_HOST}/api/qt/stock/get?secid={secid}&fields={fields}")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        with _retry_urlopen(req) as resp:
            raw = json.loads(resp.read().decode())
        d = raw.get("data", {})
        if not d or not d.get("f58"):
            return None

        def _p(x):
            return round((x or 0) / 100, 2) if x else 0

        price = _p(d.get("f43"))
        prev_close = _p(d.get("f60"))
        pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
        return {
            "名称": d.get("f58", ""),
            "现价": price,
            "昨收": prev_close,
            "今开": _p(d.get("f46")),
            "最高": _p(d.get("f44")),
            "最低": _p(d.get("f45")),
            "成交量": int(d.get("f47", 0) or 0),
            "成交额": float(d.get("f48", 0) or 0),
            "换手率": float(d.get("f184", 0) or 0),
            "量比": float(d.get("f50", 0) or 0),
            "涨停价": _p(d.get("f51")),
            "跌停价": _p(d.get("f52")),
            "52周高": float(d.get("f167", 0) or 0) / 100,
            "52周低": float(d.get("f168", 0) or 0) / 100,
            "市值": float(d.get("f116", 0) or 0),
            "涨跌幅": pct,
            "涨跌额": round(price - prev_close, 2),
        }
    except Exception as e:
        logger.warning(f"东财行情失败 {market}{code}: {e}")
        return None


def _try_aliyun(code, market):
    """阿里云实时行情（兜底）"""
    if not ALIYUN_CODE:
        return None
    try:
        req = UrlRequest(
            f"{ALIYUN_HOST}/query/com?symbol={market.upper()}{code}",
            headers={"Authorization": f"APPCODE {ALIYUN_CODE}"},
        )
        with _retry_urlopen(req) as resp:
            raw = json.loads(resp.read().decode())
        if raw.get("Code") != 0:
            return None
        d = raw["Obj"]
        return {
            "名称": d.get("N", ""),
            "现价": float(d.get("P", 0) or 0),
            "昨收": float(d.get("YC", 0) or 0),
            "今开": float(d.get("O", 0) or 0),
            "最高": float(d.get("H", 0) or 0),
            "最低": float(d.get("L", 0) or 0),
            "成交量": int(float(d.get("V", 0) or 0)),
            "成交额": float(d.get("NV", 0) or 0),
            "换手率": float(d.get("HS", 0) or 0),
            "量比": float(d.get("VR", 0) or 0),
            "涨停价": float(d.get("ZT", 0) or 0),
            "跌停价": float(d.get("DT", 0) or 0),
            "涨跌幅": float(d.get("ZF", 0) or 0),
            "涨跌额": float(d.get("ZD", 0) or 0),
        }
    except Exception as e:
        logger.warning(f"阿里云行情失败 {market}{code}: {e}")
    return None


# ========== 指数行情源（统一格式）==========


def _try_tencent_index(symbol):
    """腾讯指数行情"""
    try:
        req = UrlRequest(f"http://qt.gtimg.cn/q={symbol}", headers={"User-Agent": "Mozilla/5.0"})
        with _retry_urlopen(req) as resp:
            raw = resp.read().decode("gbk")
        if "~" not in raw:
            return None
        p = raw.strip().rstrip(";").split("~")
        if len(p) < 33:
            return None
        price = float(p[3])
        prev_close = float(p[4])
        return {
            "最新价": round(price, 2),
            "涨跌幅": float(p[32]),
            "涨跌额": round(price - prev_close, 2),
            "昨日收盘": round(prev_close, 2),
        }
    except Exception as e:
        logger.warning(f"腾讯指数行情失败: {e}")
        return None


def _try_eastmoney_index(secid, fields):
    """东方财富指数行情"""
    try:
        req = UrlRequest(f"{EASTMONEY_HOST}/api/qt/stock/get?secid={secid}&fields={fields}")
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        with _retry_urlopen(req) as resp:
            raw = json.loads(resp.read().decode())
        d = raw.get("data", {})
        if not d or not d.get("f43"):
            return None
        price = (d.get("f43") or 0) / 100
        prev_close = (d.get("f60") or 0) / 100
        pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
        return {
            "最新价": round(price, 2),
            "涨跌幅": round(pct, 2),
            "涨跌额": round(price - prev_close, 2),
            "成交量": d.get("f47", 0),
            "昨日收盘": round(prev_close, 2),
        }
    except Exception as e:
        logger.warning(f"东财指数行情失败: {e}")
        return None


def _try_aliyun_index(acode):
    """阿里云指数行情"""
    if not ALIYUN_CODE:
        return None
    try:
        req = UrlRequest(f"{ALIYUN_HOST}/query/com?symbol={acode}", headers={"Authorization": f"APPCODE {ALIYUN_CODE}"})
        with _retry_urlopen(req) as resp:
            d = json.loads(resp.read().decode())
        if d.get("Code") != 0:
            return None
        o = d["Obj"]
        price = float(o.get("P", 0) or 0)
        prev_close = float(o.get("YC", 0) or 0)
        return {
            "最新价": round(price, 2),
            "涨跌幅": float(o.get("ZF", 0) or 0),
            "涨跌额": float(o.get("ZD", 0) or 0),
            "昨日收盘": round(prev_close, 2),
        }
    except Exception as e:
        logger.warning(f"阿里云指数行情失败: {e}")
        return None


# ========== 涨跌家数 ==========


def _fetch_market_breadth_from_sina():
    """从新浪获取市场涨跌家数"""
    total_up = 0
    total_down = 0
    total_cnt = 0
    try:
        for node in ["sh_a", "sz_a"]:
            for page in range(1, 9):  # 8页×80=640只/市场，足够代表样本
                url = (
                    f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=0&node={node}&_s_r_a=auto"
                )
                req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "http://finance.sina.com.cn"})
                with _retry_urlopen(req) as resp:
                    data = json.loads(resp.read().decode("gbk", errors="replace"))
                if not data:
                    break
                for stock in data:
                    chg = float(stock.get("changepercent", 0))
                    total_cnt += 1
                    if chg > 0:
                        total_up += 1
                    elif chg < 0:
                        total_down += 1
        ratio = round((total_up / total_cnt * 100), 1) if total_cnt > 0 else 50.0
        return {"up_cnt": total_up, "down_cnt": total_down, "total_cnt": total_cnt, "breadth_ratio": ratio}
    except Exception as e:
        logger.warning(f"新浪涨跌家数获取失败: {e}")
        return {"up_cnt": 0, "down_cnt": 0, "total_cnt": 0, "breadth_ratio": 50.0}


def _is_trading_time():
    """判断当前是否为交易相关时间 (周一至周五 8:30-16:00) 北京时区"""
    from datetime import timedelta, timezone
    cst = timezone(timedelta(hours=8))
    now = datetime.now(cst)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 510 <= t <= 960


def _fetch_sse_change():
    """快速获取上证指数涨跌幅"""
    try:
        req = UrlRequest("http://qt.gtimg.cn/q=sh000001", headers={"User-Agent": "Mozilla/5.0"})
        with _retry_urlopen(req) as resp:
            data = resp.read().decode("gbk", errors="replace")
        m = re.search(r'v_sh000001="(.+?)";', data)
        if m:
            parts = m.group(1).split("~")
            if len(parts) >= 33:
                return float(parts[32])
    except Exception as e:
        logger.warning(f"获取上证指数涨跌幅失败: {e}")
        pass
    return 0.0


# ========== 公开 API ==========


def get_stock_quote(code, market="sz"):
    """个股实时行情 — 缓存 → 腾讯 → 东财 → 阿里云

    Returns 17-field dict or None.
    """
    ck = f"q:{market}:{code}"
    cached = _get_cache(ck, _QUOTE_CACHE_TTL)
    if cached:
        return cached

    result = _try_tencent(code, market)
    if not result:
        result = _try_eastmoney(code, market)
    if not result:
        result = _try_aliyun(code, market)

    if result:
        _set_cache(ck, result)
    return result


def get_market_indices():
    """四大指数实时行情 — 腾讯 → 东财 → 阿里云

    Returns dict: {date, time, indices{name: {code,最新价,涨跌幅,...}}, analysis{score,status,...}}
    """
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    index_map = [
        ("上证指数", "1.000001", "sh000001", "SH000001"),
        ("沪深 300", "1.000300", "sh000300", "SH000300"),
        ("深证成指", "0.399001", "sz399001", "SZ399001"),
        ("创业板指", "0.399006", "sz399006", "SZ399006"),
    ]

    results = {}
    for name, secid, tsec, acode in index_map:
        code = secid.split(".")[1]
        tdata = _try_tencent_index(tsec)
        if tdata:
            results[name] = {"code": code, **tdata, "成交量": 0, "日期": now_str, "change": round(tdata["涨跌幅"], 2)}
            continue
        edata = _try_eastmoney_index(secid, "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f167,f168")
        if edata:
            results[name] = {"code": code, **edata, "日期": now_str, "change": round(edata["涨跌幅"], 2)}
            continue
        adata = _try_aliyun_index(acode)
        if adata:
            results[name] = {"code": code, **adata, "成交量": 0, "日期": now_str, "change": round(adata["涨跌幅"], 2)}
        else:
            results[name] = {"code": code, "最新价": 0, "涨跌幅": 0, "涨跌额": 0, "change": 0}

    avg = sum(r.get("涨跌幅", 0) for r in results.values()) / max(len(results), 1)
    if avg < -2:
        status, color, position, signal, score = "大幅下跌", "🔴", 10, "清仓", 30
    elif avg < -1:
        status, color, position, signal, score = "下跌", "🟠", 30, "减仓", 45
    elif avg < 0:
        status, color, position, signal, score = "小幅下跌", "🟡", 50, "观望", 55
    elif avg < 1:
        status, color, position, signal, score = "小幅上涨", "🟢", 70, "持仓", 65
    elif avg < 2:
        status, color, position, signal, score = "上涨", "🟢", 80, "加仓", 75
    else:
        status, color, position, signal, score = "大幅上涨", "🟢", 90, "重仓", 85

    return {
        "date": now_str,
        "time": time_str,
        "indices": results,
        "analysis": {
            "score": score,
            "status": status,
            "color": color,
            "position_ratio": position,
            "signal": signal,
            "reasons": [
                f"{n}{'+' if r.get('涨跌幅', 0) >= 0 else ''}{r.get('涨跌幅', 0):.2f}%" for n, r in results.items()
            ],
        },
    }


def get_market_breadth():
    """市场涨跌家数 — 新浪（60s缓存）

    Returns {up_cnt, down_cnt, total_cnt, breadth_ratio}
    """
    cached = _get_cache("breadth", _BREADTH_CACHE_TTL)
    if cached:
        return cached
    data = _fetch_market_breadth_from_sina()
    _set_cache("breadth", data)
    return data


def get_market_overview(db_conn=None):
    """大盘综合数据（涨跌幅+涨跌比），与 ml_predict.get_market_info 格式一致

    交易时段优先实时数据，否则回退 MySQL。

    Returns {mkt_chg, breadth_ratio, up_cnt, total_cnt, date, source}
    """
    if _is_trading_time():
        breadth = get_market_breadth()
        if breadth.get("total_cnt", 0) > 0:
            mkt_chg = _fetch_sse_change()
            logger.info("使用【实时】大盘数据")
            return {
                "mkt_chg": mkt_chg,
                "breadth_ratio": breadth["breadth_ratio"],
                "up_cnt": breadth["up_cnt"],
                "total_cnt": breadth["total_cnt"],
                "date": datetime.now().strftime("%Y-%m-%d"),
                "source": "realtime",
            }
    # 回退 MySQL
    import pymysql

    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**get_db_config())
        should_close = True
    try:
        cur = db_conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM market_index_daily WHERE index_code='000001.SH'")
        idx_date = cur.fetchone()[0]
        if not idx_date:
            return {"mkt_chg": 0, "breadth_ratio": 50.0, "up_cnt": 0, "total_cnt": 0, "date": None, "source": "db"}
        cur.execute(
            "SELECT change_pct FROM market_index_daily WHERE index_code='000001.SH' AND trade_date=%s", (idx_date,)
        )
        row = cur.fetchone()
        mkt_chg = float(row[0]) if row else 0.0
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        bd = cur.fetchone()[0]
        cur.execute(
            """SELECT SUM(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END), COUNT(*)
            FROM daily_price WHERE trade_date=%s AND pct_chg IS NOT NULL
            AND ts_code NOT LIKE '688.%%' AND ts_code NOT LIKE '8%%' AND ts_code NOT LIKE '4%%' AND ts_code NOT LIKE '9%%'""",
            (bd,),
        )
        res = cur.fetchone()
        up = int(res[0]) if res and res[0] else 0
        total = int(res[1]) if res and res[1] else 0
        ratio = round((up / total * 100), 1) if total > 0 else 50.0
        logger.info("使用【MySQL】历史大盘数据")
        return {
            "mkt_chg": mkt_chg,
            "breadth_ratio": ratio,
            "up_cnt": up,
            "total_cnt": total,
            "date": idx_date.strftime("%Y-%m-%d") if hasattr(idx_date, "strftime") else str(idx_date),
            "source": "db",
        }
    except Exception as e:
        logger.error(f"获取大盘数据失败: {e}")
        return {"mkt_chg": 0, "breadth_ratio": 50.0, "up_cnt": 0, "total_cnt": 0, "date": None, "source": "error"}
    finally:
        if should_close:
            db_conn.close()


if __name__ == "__main__":
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    logging.basicConfig(level=logging.INFO)
    q = get_stock_quote("000001", "sz")
    print(f"个股: {q['名称']} {q['现价']}元 {q['涨跌幅']:+.2f}%" if q else "个股获取失败")
    mi = get_market_indices()
    for n, v in mi.get("indices", {}).items():
        print(f"指数 {n}: {v.get('最新价')} ({v.get('涨跌幅'):+.2f}%)")
    print(f"分析: {mi['analysis']['status']} 仓位{mi['analysis']['position_ratio']}%")
    b = get_market_breadth()
    print(f"涨跌: ↑{b['up_cnt']} / ↓{b['down_cnt']} / 共{b['total_cnt']} ({b['breadth_ratio']}%)")
    o = get_market_overview()
    print(f"大盘: {o['mkt_chg']:+.2f}% 涨跌比{o['breadth_ratio']}% 来源{o['source']}")
