#!/usr/bin/env python3
"""
实时行情 API 封装模块 — 三级备选：阿里云 → 东方财富 → 腾讯
"""
import json
import logging
import os
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

logger = logging.getLogger(__name__)

ALIYUN_HOST = "http://alirmcom2.market.alicloudapi.com"
ALIYUN_CODE = os.environ.get('ALIBABA_APP_CODE', '')


def _try_aliyun(code, market):
    """阿里云实时行情"""
    try:
        url = f"{ALIYUN_HOST}/query/com?symbol={market}{code}"
        req = UrlRequest(url, headers={"Authorization": f"APPCODE {ALIYUN_CODE}"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode())
            if raw.get("Code") != 0:
                logger.warning(f"阿里云API返回错误: {raw.get('Message', '')}")
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


def _try_eastmoney(code, market):
    """东方财富实时行情 (push2.eastmoney.com)"""
    prefix = "1" if market.upper() == "SH" else "0"
    secid = f"{prefix}.{code}"
    fields = "f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f168,f169,f170"
    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}&ut=fa5fd1943c7b386f172d6893dbbd1"

    try:
        req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode())
            d = raw.get("data")
            if not d:
                return None

            def sf(v, default=0.0):
                if v is None or v == "-":
                    return default
                try:
                    return float(v)
                except (ValueError, TypeError):
                    return default

            price = sf(d.get("f43")) / 100.0
            prev_close = sf(d.get("f60")) / 100.0
            change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

            return {
                "名称": d.get("f58", ""),
                "现价": price,
                "昨收": prev_close,
                "今开": sf(d.get("f46")) / 100.0,
                "最高": sf(d.get("f44")) / 100.0,
                "最低": sf(d.get("f45")) / 100.0,
                "成交量": int(sf(d.get("f47"))),
                "成交额": sf(d.get("f48")),
                "换手率": sf(d.get("f168")),
                "量比": sf(d.get("f169")),
                "涨停价": round(prev_close * 1.1, 2) if prev_close > 0 else 0,
                "跌停价": round(prev_close * 0.9, 2) if prev_close > 0 else 0,
                "涨跌幅": change_pct,
                "涨跌额": round(price - prev_close, 2),
            }
    except Exception as e:
        logger.warning(f"东方财富行情失败 {market}{code}: {e}")
    return None


def _try_tencent(code, market):
    """腾讯财经实时行情 (qt.gtimg.cn)"""
    symbol = f"{market.lower()}{code}"
    url = f"http://qt.gtimg.cn/q={symbol}"
    try:
        req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            data = resp.read().decode("gbk")
            if "~" not in data:
                return None
            parts = data.strip().rstrip(";").split("~")
            if len(parts) < 45:
                return None

            price = float(parts[3])
            prev_close = float(parts[4])
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
                "涨停价": round(prev_close * 1.1, 2) if prev_close > 0 else 0,
                "跌停价": round(prev_close * 0.9, 2) if prev_close > 0 else 0,
                "涨跌幅": float(parts[32]),
                "涨跌额": round(price - prev_close, 2),
            }
    except Exception as e:
        logger.warning(f"腾讯行情失败 {market}{code}: {e}")
    return None


# fallback 优先级
_FALLBACKS = [_try_eastmoney, _try_tencent]


def get_stock_realtime(code, market="sz"):
    """
    获取单股实时行情，阿里云 → 东方财富 → 腾讯 三级备选

    Args:
        code: 股票代码（纯数字，如 000551）
        market: 市场（sz 或 sh）

    Returns:
        dict or None（三个数据源全部失败时返回 None）
    """
    result = _try_aliyun(code, market)
    if result:
        return result

    for fn in _FALLBACKS:
        logger.info(f"阿里云失败，尝试 {fn.__name__}: {market}{code}")
        result = fn(code, market)
        if result:
            return result

    logger.error(f"所有行情源均失败: {market}{code}")
    return None


def get_batch_realtime(positions):
    """
    批量获取实时行情

    Args:
        positions: list of dict with 'code', 'market' keys

    Returns:
        dict: {(market, code): quote_data}
    """
    results = {}
    for pos in positions:
        code = pos.get("code", "")
        market = pos.get("market", "sz")
        data = get_stock_realtime(code, market)
        if data:
            results[(market, code)] = data
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = get_stock_realtime("000551", "sz")
    print(json.dumps(result, ensure_ascii=False, indent=2) if result else "获取失败")
