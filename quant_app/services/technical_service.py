"""
技术指标服务 - MA, MACD, KDJ, BOLL, ATR, RSI 等指标计算
统一委托 quant_app.utils.indicators 的全序列版，本层只做标量取尾适配。
"""
import logging

from quant_app.utils.indicators import (
    calculate_atr as _calc_atr,
)
from quant_app.utils.indicators import (
    calculate_bollinger_bands as _calc_bb_full,
)
from quant_app.utils.indicators import (
    calculate_ema as _calc_ema_full,
)
from quant_app.utils.indicators import (
    calculate_kdj as _calc_kdj_full,
)
from quant_app.utils.indicators import (
    calculate_macd as _calc_macd_full,
)

logger = logging.getLogger(__name__)


def _last_or_none(lst):
    """取列表最后一个非 None 元素，无则返回 None"""
    if not lst:
        return None
    v = lst[-1]
    if isinstance(v, list):
        return v
    return v


def calculate_ema(closes, period):
    """计算指数移动平均线 (EMA)，返回最后一个值"""
    full = _calc_ema_full(closes, period)
    return _last_or_none(full)


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD指标，返回 (DIF, DEA, MACD_HIST) 最后一个值"""
    full_dif, full_dea, full_hist = _calc_macd_full(closes, fast, slow, signal)
    return _last_or_none(full_dif), _last_or_none(full_dea), _last_or_none(full_hist)


def calculate_kdj(highs, lows, closes, n=9, m1=3, m2=3):
    """计算KDJ指标，返回 (K, D, J) 最后一个值"""
    full_k, full_d, full_j = _calc_kdj_full(highs, lows, closes, n, m1, m2)
    return _last_or_none(full_k), _last_or_none(full_d), _last_or_none(full_j)


def calculate_bollinger_bands(closes, period=20, std_dev=2):
    """计算布林带，返回 (upper, middle, lower) 最后一个值"""
    full_u, full_m, full_l = _calc_bb_full(closes, period, std_dev)
    return _last_or_none(full_u), _last_or_none(full_m), _last_or_none(full_l)


def calculate_rsi(closes, period=14):
    """计算RSI（相对强弱指标）— Wilder 平滑版"""
    if len(closes) < period + 1:
        return None
    # 先算第一期的平均涨幅/跌幅
    gains = 0
    losses = 0
    for i in range(1, period + 1):
        diff = closes[-period + i] - closes[-period + i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder 平滑：后续 K 线用平滑公式
    for i in range(period + 1, len(closes)):
        diff = closes[-len(closes) + i] - closes[-len(closes) + i - 1]
        if diff >= 0:
            avg_gain = (avg_gain * (period - 1) + diff) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) - diff) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100 - 100 / (1 + rs)
    return round(rsi, 2)


def calculate_atr(highs, lows, closes, period=14):
    """计算ATR (Average True Range)"""
    return _calc_atr(highs, lows, closes, period)
