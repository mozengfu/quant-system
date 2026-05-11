#!/usr/bin/env python3
"""
东方财富实时行情API - 兼容aicloud_api接口格式
"""
import urllib.request
import urllib.error
import ssl
import json
import os
from typing import Optional, Dict

HOST = "https://push2.eastmoney.com"

# SSL bypass for framework Python (cert verification issue)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
QUOTE_FIELDS = "f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,f116,f117,f162,f168,f169,f170,f171"

def _code_to_secid(symbol: str) -> str:
    """
    将股票代码转换为东方财富 secid 格式
    
    Args:
        symbol: 股票代码，支持 'sh600519', 'sz000001', '600519', '000001'
    
    Returns:
        secid 字符串，格式为 '1.600519' 或 '0.000001'
    """
    symbol = symbol.strip().upper()
    
    if '.' in symbol:
        parts = symbol.split('.')
        code = parts[1]
        market = parts[0]
    elif len(symbol) > 2 and (symbol[:2] in ('SH', 'SZ')):
        market = symbol[:2]
        code = symbol[2:]
    else:
        code = symbol
        # 根据代码前缀判断市场
        if code.startswith(('6', '9')):
            market = 'SH'
        else:
            market = 'SZ'
    
    # 转换为 secid 格式
    if market == 'SH':
        return f'1.{code}'
    else:
        return f'0.{code}'


def get_stock_quote(symbol: str) -> Optional[Dict]:
    """
    获取股票实时行情
    
    Args:
        symbol: 股票代码，支持 'sh600519', 'sz000001', '600519', '000001'
    
    Returns:
        股票实时数据字典
    """
    secid = _code_to_secid(symbol)
    url = f'{HOST}/api/qt/stock/get?secid={secid}&fields={QUOTE_FIELDS}&ut=fa5fd1943c7b386f172d6893dbbd1'

    try:
        request = urllib.request.Request(url)
        request.add_header('User-Agent', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')
        response = urllib.request.urlopen(request, timeout=10, context=_SSL_CTX)
        content = response.read().decode('utf-8')
        data = json.loads(content)

        if data.get('data') is None:
            return None

        d = data['data']
        
        def safe_float(val, default=0.0):
            if val is None or val == '-':
                return default
            try:
                return float(val)
            except (ValueError, TypeError):
                return default
        
        # f57=代码, f58=名称, f60=昨收(分), f43=最新价(分)
        # f116=总市值, f117=流通市值
        price = safe_float(d.get('f43'), 0) / 100.0 if safe_float(d.get('f43'), 0) > 0 else 0
        prev_close = safe_float(d.get('f60'), 0) / 100.0 if safe_float(d.get('f60'), 0) > 0 else 0
        
        # 涨跌幅自己算
        change_pct = 0
        if prev_close > 0 and price > 0:
            change_pct = round((price - prev_close) / prev_close * 100, 2)
        
        result = {
            'N': d.get('f58', ''),      # 名称
            'C': d.get('f57', ''),      # 代码
            'P': price,                  # 现价
            'ZF': change_pct,           # 涨跌幅
            'ZD': round(price - prev_close, 2),  # 涨跌额
            'O': safe_float(d.get('f46'), 0) / 100.0,   # 开盘
            'H': safe_float(d.get('f44'), 0) / 100.0,   # 最高
            'L': safe_float(d.get('f45'), 0) / 100.0,   # 最低
            'YC': prev_close,           # 昨收
            'V': safe_float(d.get('f47'), 0) / 100.0,   # 成交量(手)
            'A': safe_float(d.get('f48'), 0) / 10000.0, # 成交额(万)
            'HS': safe_float(d.get('f168'), 0),         # 换手率
            'VR': safe_float(d.get('f169'), 0),         # 量比
            'ZS': safe_float(d.get('f116'), 0),         # 总市值
            'LS': safe_float(d.get('f117'), 0),         # 流通市值
        }
        
        return result
    except Exception as e:
        return None


def get_stock_realtime(code: str, market: str = "sz") -> Optional[Dict]:
    """
    兼容 alicloud_api 的接口格式
    
    Args:
        code: 纯数字代码 '000001'
        market: 市场前缀 'sz' 或 'sh'
    
    Returns:
        兼容格式的字典，字段: 名称, 现价, 昨收, 涨跌幅等
    """
    symbol = f"{market}{code}"
    raw = get_stock_quote(symbol)
    if raw is None:
        return None
    
    return {
        "名称": raw.get("N", ""),
        "代码": raw.get("C", ""),
        "现价": raw.get("P", 0),
        "涨跌幅": raw.get("ZF", 0),
        "涨跌额": raw.get("ZD", 0),
        "今开": raw.get("O", 0),
        "最高": raw.get("H", 0),
        "最低": raw.get("L", 0),
        "昨收": raw.get("YC", 0),
        "成交量": raw.get("V", 0),
        "成交额": raw.get("A", 0),
        "换手率": raw.get("HS", 0),
        "量比": raw.get("VR", 0),
        "总市值": raw.get("ZS", 0),
        "流通市值": raw.get("LS", 0),
        "涨停": round(raw.get("YC", 0) * 1.1, 2) if raw.get("YC", 0) > 0 else 0,
        "跌停": round(raw.get("YC", 0) * 0.9, 2) if raw.get("YC", 0) > 0 else 0,
    }


def get_batch_realtime(positions):
    """批量获取行情（兼容接口）"""
    results = {}
    for pos in positions:
        code = pos.get("ts_code", "").split(".")[0]
        market = pos.get("market", "sz")
        data = get_stock_realtime(code, market)
        if data:
            results[pos["ts_code"]] = data
    return results
