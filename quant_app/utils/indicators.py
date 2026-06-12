"""
通用技术指标计算模块 — 全序列版本（返回 list）
所有指标统一入口，确保各模块计算结果一致。
"""

import logging

logger = logging.getLogger(__name__)


def calculate_ema(closes, period):
    """计算指数移动平均(EMA)，返回完整序列"""
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return [None] * (period - 1) + ema


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD指标，返回 (dif_list, dea_list, macd_hist_list)"""
    if len(closes) < slow:
        return [], [], []
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    dif = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None:
            dif.append(None)
        else:
            dif.append(f - s)
    valid_dif = [d for d in dif if d is not None]
    dea_full = calculate_ema(valid_dif, signal)
    none_count = sum(1 for d in dif if d is None)
    dea = [None] * none_count + dea_full
    macd_hist = []
    for d, de in zip(dif, dea):
        if d is None or de is None:
            macd_hist.append(None)
        else:
            macd_hist.append((d - de) * 2)
    return dif, dea, macd_hist


def calculate_kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """计算KDJ指标，返回 (k_list, d_list, j_list)"""
    if len(closes) < n:
        return [], [], []
    k_list, d_list, j_list = [], [], []
    rsv_list = []
    for i in range(len(closes)):
        if i < n - 1:
            rsv_list.append(None)
            continue
        low_n = min(lows[i - n + 1 : i + 1])
        high_n = max(highs[i - n + 1 : i + 1])
        close_curr = closes[i]
        if high_n == low_n:
            rsv = 50
        else:
            rsv = (close_curr - low_n) / (high_n - low_n) * 100
        rsv_list.append(rsv)
    for i in range(len(rsv_list)):
        if rsv_list[i] is None:
            k_list.append(None)
            d_list.append(None)
            j_list.append(None)
        elif i == n - 1:
            k = rsv_list[i]
            d = k
            k_list.append(k)
            d_list.append(d)
            j_list.append(3 * k - 2 * d)
        else:
            prev_k = k_list[-1] if k_list[-1] is not None else 50
            prev_d = d_list[-1] if d_list[-1] is not None else 50
            k = (prev_k * (m1 - 1) + rsv_list[i]) / m1
            d = (prev_d * (m2 - 1) + k) / m2
            k_list.append(k)
            d_list.append(d)
            j_list.append(3 * k - 2 * d)
    return k_list, d_list, j_list


def calculate_bollinger_bands(closes, period=20, std_dev=2):
    """计算布林带，返回 (upper_list, middle_list, lower_list)"""
    upper, middle, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            upper.append(None)
            middle.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1 : i + 1]
            ma = sum(window) / period
            variance = sum((x - ma) ** 2 for x in window) / period
            std = variance**0.5
            middle.append(ma)
            upper.append(ma + std_dev * std)
            lower.append(ma - std_dev * std)
    return upper, middle, lower


def calculate_atr(highs, lows, closes, period=14):
    """计算ATR (Average True Range)，返回标量（与全序列版兼容）"""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h = highs[i]
        l = lows[i]
        c_prev = closes[i - 1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period
