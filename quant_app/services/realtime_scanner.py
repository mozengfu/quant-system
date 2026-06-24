"""
实时选股扫描引擎 v9
v7基础 + QMT实时数据增强: 盘口(含深度)/日内突破/资金博弈(时间修正)/多指数强度
"""
import logging
import math
import os
from collections import defaultdict
from datetime import datetime

import requests

logger = logging.getLogger(__name__)
QMT_HOST = "http://192.168.10.25:1430"


def _get_db_config():
    """统一数据库配置，复用 quant_app.utils.config"""
    from quant_app.utils.config import get_db_config
    return get_db_config(charset="utf8mb4")


def _get_qmt(endpoint):
    try:
        r = requests.get(f"{QMT_HOST}/{endpoint}", timeout=5)
        return r.json()
    except Exception as e:
        logger.warning(f"QMT {endpoint}: {e}")
        return None


def _mysql_query(sql, params=None):
    try:
        import pymysql
        conn = pymysql.connect(**_get_db_config(), connect_timeout=3)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        logger.warning(f"MySQL: {e}")
        return []


def get_realtime_data():
    data = _get_qmt("market/snapshot")
    if data and data.get("stocks"):
        return {s["code"]: s for s in data["stocks"]}
    return {}


def get_index_data():
    data = _get_qmt("market/index")
    if data and data.get("indices"):
        return {i["code"]: i for i in data["indices"]}
    return {}


def get_daily_series(codes, days=60):
    if not codes:
        return {}
    placeholders = ",".join(["%s"] * len(codes))
    sql = f"""
        SELECT ts_code, trade_date, close, open, high, low, vol, pct_chg, amount
        FROM daily_price WHERE ts_code IN ({placeholders})
        AND trade_date >= (SELECT DATE_SUB(MAX(trade_date), INTERVAL %s DAY) FROM daily_price)
        ORDER BY ts_code, trade_date ASC
    """
    rows = _mysql_query(sql, [*codes, days])
    result = defaultdict(list)
    for row in rows:
        code, date, close, o, h, l, vol, pct, amt = row
        result[code].append({
            "date": str(date), "close": float(close or 0),
            "open": float(o or 0), "high": float(h or 0), "low": float(l or 0),
            "vol": float(vol or 0), "pct_chg": float(pct or 0), "amount": float(amt or 0)
        })
    return dict(result)


def _ma(arr, n):
    if len(arr) < n:
        return sum(arr) / len(arr) if arr else 0
    return sum(arr[-n:]) / n


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _bollinger(closes, period=20, std=2):
    if len(closes) < period:
        return (0, 0, 0, 0.5)
    recent = closes[-period:]
    mid = sum(recent) / period
    s = math.sqrt(sum((x - mid) ** 2 for x in recent) / period)
    price = closes[-1]
    pct_b = (price - (mid - std * s)) / (2 * std * s) if s > 0 else 0.5
    return (mid, mid + std * s, mid - std * s, pct_b)


def _time_fraction():
    """返回今日交易时间已过比例 (0.0~1.0)
    9:30开盘, 11:30午休, 13:00开盘, 15:00收盘, 共4小时=240分钟
    """
    from datetime import datetime, time
    now = datetime.now().time()
    morning_open = time(9, 30)
    morning_close = time(11, 30)
    afternoon_open = time(13, 0)
    market_close = time(15, 0)
    if now < morning_open:
        return 0.0
    if now >= market_close:
        return 1.0
    if now <= morning_close:
        minutes = (now.hour - 9) * 60 + now.minute - 30
        return max(0.02, minutes / 240.0)
    if now < afternoon_open:
        return 120.0 / 240.0  # 午休 => 50%
    minutes = 120 + (now.hour - 13) * 60 + now.minute
    return min(1.0, minutes / 240.0)


# ============================================================
# 因子函数
# ============================================================

def factor_volume_breakout(rt, daily_data):
    """量能突破 — 时间修正量比 + 价格位置 (max 25)
    QRT实时vol是当日累积量，需按时间分位修正后才能与日均量比较。
    """
    score = 0
    code = rt.get("code", "")
    vol = rt.get("volume", 0)
    if code not in daily_data or len(daily_data[code]) < 5:
        return 0
    hist = daily_data[code]
    vols = [d["vol"] for d in hist[-20:]]
    avg_vol = sum(vols) / len(vols) if vols else 1
    # 时间修正: 预期当前应完成的日均量比例
    tf = _time_fraction()
    expected_vol = avg_vol * max(tf, 0.05)
    vol_ratio = vol / expected_vol if expected_vol > 0 else 1
    if vol_ratio > 2.0:  score += 12
    elif vol_ratio > 1.5: score += 8
    elif vol_ratio > 1.0: score += 4

    closes = [d["close"] for d in hist[-20:]]
    high_n, low_n = max(closes), min(closes)
    price = rt.get("last", 0)
    if high_n > low_n:
        pos = (price - low_n) / (high_n - low_n) * 100
        if pos > 80:    score += 7
        elif pos > 50:  score += 3
        elif pos < 20:  score += 6
    if vol_ratio > 1.2 and rt.get("pctChg", 0) > 0:
        score += 6
    return min(25, score)


def factor_momentum(rt, daily_data, index_pct=0):
    """动量 — 日内涨幅 + 5日动量 + 相对大盘强度 (max 25)"""
    score = 0
    code = rt.get("code", "")
    pct_chg = rt.get("pctChg", 0)
    if code not in daily_data or len(daily_data[code]) < 5:
        return 0
    hist = daily_data[code]
    if pct_chg > 5:      score += 8
    elif pct_chg > 3:    score += 6
    elif pct_chg > 1:    score += 3
    elif pct_chg > -1:   score += 1
    elif pct_chg < -5:   score -= 5

    if len(hist) >= 5:
        ret5 = (hist[-1]["close"] / hist[-5]["close"] - 1) * 100
        if ret5 > 8:     score += 6
        elif ret5 > 4:   score += 4
        elif ret5 > 1:   score += 2

    relative = pct_chg - index_pct
    if relative > 3:     score += 7
    elif relative > 1.5: score += 4
    elif relative > 0.5: score += 2
    elif relative < -2:  score -= 4

    if len(hist) >= 3:
        up_days = sum(1 for d in hist[-3:] if d["pct_chg"] > 0)
        score += up_days * 1.5
    return max(0, min(25, score))


def factor_trend(rt, daily_data):
    """趋势 — 均线多头排列 + 均线斜率 (max 20)"""
    score = 0
    code = rt.get("code", "")
    price = rt.get("last", 0)
    if code not in daily_data or len(daily_data[code]) < 20:
        return 8
    hist = daily_data[code]
    closes = [d["close"] for d in hist]
    ma5, ma10, ma20 = _ma(closes, 5), _ma(closes, 10), _ma(closes, 20)
    if price > ma5:  score += 3
    if price > ma10: score += 3
    if price > ma20: score += 4
    if ma5 > ma10 > ma20: score += 6
    elif ma5 > ma10: score += 2

    if len(closes) >= 10:
        slope = (ma5 - _ma(closes[-10:], 5)) / _ma(closes[-10:], 5) * 100
        if slope > 2:       score += 4
        elif slope > 0.5:   score += 1
        elif slope < -2:    score -= 2
    return max(0, min(20, score))


def factor_liquidity(rt, daily_data):
    """流动性 — 时间修正成交额 + 波动惩罚 (max 15)"""
    score = 6
    code = rt.get("code", "")
    amount = rt.get("amount", 0)
    tf = _time_fraction()
    adj_amount = amount / max(tf, 0.05)
    if adj_amount > 5e9:   score += 6
    elif adj_amount > 1e9: score += 3
    elif adj_amount > 5e8: score += 1

    if code in daily_data and len(daily_data[code]) >= 10:
        pcts = [abs(d["pct_chg"]) for d in daily_data[code][-10:]]
        if sum(pcts) / len(pcts) > 5: score -= 3
    return max(0, min(15, score))


def factor_rsi_bonus(rt, daily_data):
    """RSI — 超买超卖 (±5)"""
    code = rt.get("code", "")
    if code not in daily_data or len(daily_data[code]) < 16:
        return 0
    closes = [d["close"] for d in daily_data[code]]
    rsi = _rsi(closes, 14)
    if rsi < 30:    return 5
    elif rsi < 40:  return 3
    elif rsi > 80:  return -5
    elif rsi > 70:  return -2
    return 0


def factor_bb_bonus(rt, daily_data):
    """布林带 — 低位加分 (0~5)"""
    code = rt.get("code", "")
    if code not in daily_data or len(daily_data[code]) < 22:
        return 2
    closes = [d["close"] for d in daily_data[code]]
    _, _, _, pct_b = _bollinger(closes, 20, 2)
    if pct_b < 0.2:   return 5
    elif pct_b < 0.4: return 3
    elif pct_b > 0.9: return 0
    elif pct_b > 0.7: return 1
    return 2


# ============================================================
# v9 新增因子 — QMT 实时数据专属
# ============================================================

def factor_orderbook(rt):
    """盘口因子 — 价差 + 买卖失衡 + 深度分布 (max 10)
    使用 10 档 Level-2 数据：
    - 价差: 1档价差小 = 流动性好
    - 深度分布: 10 档买卖盘总挂单比
    - 压力位: 卖1~5档平均挂单厚度
    - 大单识别: bid/ask 每档是否有异常大单（档位占比 > 40%）
    """
    bid1 = rt.get("bid1", 0)
    ask1 = rt.get("ask1", 0)
    last = rt.get("last", 0)
    if last <= 0 or bid1 <= 0 or ask1 <= 0:
        return 3

    # 读取 10 档
    bids = [rt.get(f"bid{i}", 0) for i in range(1, 11)]
    asks = [rt.get(f"ask{i}", 0) for i in range(1, 11)]
    bid_vols = [rt.get(f"bidVol{i}", 0) for i in range(1, 11)]
    ask_vols = [rt.get(f"askVol{i}", 0) for i in range(1, 11)]

    total_bid_vol = sum(bid_vols)
    total_ask_vol = sum(ask_vols)

    score = 0

    # 1. 1档价差（流动性）
    spread_pct = (ask1 - bid1) / last * 100
    if spread_pct < 0.05:      score += 3
    elif spread_pct < 0.1:     score += 2
    elif spread_pct < 0.2:     score += 1
    elif spread_pct > 0.5:     score -= 2

    # 2. 买卖盘总失衡（10 档总挂单）
    if total_bid_vol > 0 and total_ask_vol > 0:
        depth_ratio = total_bid_vol / (total_bid_vol + total_ask_vol)
        if depth_ratio > 0.65:    score += 3
        elif depth_ratio > 0.55:  score += 1
        elif depth_ratio < 0.35:  score -= 2
        elif depth_ratio < 0.45:  score -= 1

    # 3. 卖盘压力（卖1~5档平均挂单 vs 卖6~10档）
    ask_vol_top5 = sum(ask_vols[:5])
    ask_vol_deep = sum(ask_vols[5:])
    if ask_vol_top5 > 0 and ask_vol_deep > 0:
        ask_front_ratio = ask_vol_top5 / (ask_vol_top5 + ask_vol_deep)
        if ask_front_ratio > 0.7:
            score -= 1  # 卖盘集中在近档，抛压大
        elif ask_front_ratio < 0.3:
            score += 1  # 卖盘分散，抛压小

    # 4. 买盘支撑强度（买1~5档 vs 买6~10档）
    bid_vol_top5 = sum(bid_vols[:5])
    bid_vol_deep = sum(bid_vols[5:])
    if bid_vol_top5 > 0 and bid_vol_deep > 0:
        bid_front_ratio = bid_vol_top5 / (bid_vol_top5 + bid_vol_deep)
        if bid_front_ratio > 0.7:
            score += 1  # 买盘集中在近档，支撑强

    # 5. 大单检测（某档挂单占比 > 40% 视为大单）
    big_bid = sum(1 for v in bid_vols if v > 0 and total_bid_vol > 0 and v / total_bid_vol > 0.4)
    big_ask = sum(1 for v in ask_vols if v > 0 and total_ask_vol > 0 and v / total_ask_vol > 0.4)
    if big_bid > 0:
        score += 1  # 买方有大单护盘
    if big_ask > 0:
        score -= 1  # 卖方有大单压制

    return max(0, min(10, score))


def factor_intraday_breakout(rt, daily_data):
    """日内突破因子 — 突破前高/前低 + 开盘价区间 (max 15)
    模拟盘中发现：突破前日高点的票次日胜率更高。
    QMT 提供 realtime last + daily high/low/open。
    """
    code = rt.get("code", "")
    last = rt.get("last", 0)
    if code not in daily_data or len(daily_data[code]) < 2:
        return 3
    hist = daily_data[code]
    prev = hist[-1]  # 前一日
    prev_high = prev.get("high", 0)
    prev_low = prev.get("low", 0)
    prev_close = prev.get("close", 0)

    score = 0
    if prev_high > 0:
        if last > prev_high:
            score += 8  # 突破前日高点 — 强势信号
        elif last > prev_high * 0.98:
            score += 4  # 接近前高
        elif last < prev_low:
            score -= 5  # 跌破前低
        elif last > prev_close:
            score += 2  # 高于昨收

    # 开盘区间位置: (现价-开盘)/(最高-最低)
    today_open = prev.get("open", 0)  # 用昨收近似，实时open从daily获取
    if prev_high > prev_low:
        intraday_pos = (last - prev["open"]) / (prev_high - prev_low) if prev_high > prev_low else 0.5
        if 0.3 < intraday_pos < 0.7:
            score += 3  # 中位偏上
        elif intraday_pos > 0.7:
            score += 5  # 高位 — 动能强但注意追高风险

    return max(0, min(15, score))


def factor_money_flow(rt):
    """资金博弈因子 — 时间修正成交额 + 盘口偏向 (max 10)
    实时成交额是累积值，按时间分位修正后与日均可比。
    """
    amount = rt.get("amount", 0)
    bid1 = rt.get("bid1", 0)
    ask1 = rt.get("ask1", 0)
    last = rt.get("last", 0)
    score = 0

    # 时间修正成交额阈值
    tf = _time_fraction()
    adj_amount = amount / max(tf, 0.05)  # 估算全天成交额
    if adj_amount > 5e9:        score += 5
    elif adj_amount > 2e9:      score += 3
    elif adj_amount > 5e8:      score += 1

    # 主动买盘：现价接近ask1 → 有资金追买
    if last > 0 and ask1 > last:
        buy_pressure = (last - bid1) / (ask1 - bid1) if (ask1 - bid1) > 0 else 0.5
        if buy_pressure > 0.7:  score += 3
        elif buy_pressure > 0.5: score += 1

    return max(0, min(10, score))


def factor_multi_index_strength(rt, idx_data):
    """多指数相对强度 — vs 上证+创业板+科创50 (max 10)
    相对多个指数都强的票，是真正的资金合力方向。
    """
    pct_chg = rt.get("pctChg", 0)
    if not idx_data:
        return 3
    score = 0
    benchmarks = ["000001.SH", "399006.SZ", "000688.SH"]  # 上证, 创业板, 科创50
    count = 0
    for bc in benchmarks:
        idx = idx_data.get(bc, {})
        idx_pct = idx.get("pctChg", 999)
        if idx_pct == 999:
            continue
        count += 1
        rel = pct_chg - idx_pct
        if rel > 2:        score += 4
        elif rel > 1:      score += 2
        elif rel > 0:      score += 1
        elif rel < -2:     score -= 2

    if count > 0:
        score = score / count * 3  # 归一化到0~10
    return max(0, min(10, round(score)))


# ============================================================
# 主扫描函数
# ============================================================

def scan_stocks():
    rt_data = get_realtime_data()
    idx_data = get_index_data()
    if not rt_data:
        return {"ts": datetime.now().isoformat(), "signals": [], "error": "no realtime data"}

    index_pct = idx_data.get("000300.SH", {}).get("pctChg", 0)
    codes = list(rt_data.keys())
    daily = get_daily_series(codes, 60)

    signals = []
    for code, rt in rt_data.items():
        try:
            # 涨停板过滤（买不到）
            pct = rt.get("pctChg", 0)
            if pct >= 9.5:
                continue

            # v7 基础因子
            f1 = factor_volume_breakout(rt, daily)          # 量能 max 25
            f2 = factor_momentum(rt, daily, index_pct)       # 动量 max 25
            f3 = factor_trend(rt, daily)                     # 趋势 max 20
            f4 = factor_liquidity(rt, daily)                 # 流动性 max 15
            rsi = factor_rsi_bonus(rt, daily)                # RSI ±5
            bb = factor_bb_bonus(rt, daily)                  # 布林 0~5

            # v9 新增因子 — QMT实时数据驱动
            f5 = factor_orderbook(rt)                        # 盘口 max 10
            f6 = factor_intraday_breakout(rt, daily)          # 日内突破 max 15
            f7 = factor_money_flow(rt)                       # 资金博弈 max 10
            f8 = factor_multi_index_strength(rt, idx_data)    # 多指数强度 max 10

            total = f1 + f2 + f3 + f4 + rsi + bb + f5 + f6 + f7 + f8

            # 新阈值：总分范围 0~140
            if total >= 80:
                level = "STRONG_BUY"
            elif total >= 60:
                level = "BUY"
            elif total >= 40:
                level = "WATCH"
            elif total >= 25:
                level = "NEUTRAL"
            else:
                level = "AVOID"

            signals.append({
                "code": code, "price": rt.get("last", 0), "pctChg": rt.get("pctChg", 0),
                "score": total, "level": level,
                "factors": {
                    "volume": f1, "momentum": f2, "trend": f3, "liquidity": f4,
                    "rsi": rsi, "bollinger": bb,
                    "orderbook": f5, "breakout": f6, "moneyflow": f7, "multi_idx": f8,
                },
                "detail": {
                    "bid": rt.get("bid1", 0), "ask": rt.get("ask1", 0),
                    "volume": int(rt.get("volume", 0)), "amount": rt.get("amount", 0),
                }
            })
        except Exception as e:
            logger.warning(f"扫描{code}失败: {e}")

    signals.sort(key=lambda x: x["score"], reverse=True)
    return {
        "ts": datetime.now().isoformat(),
        "market": {
            "index_pct": index_pct,
            "index_level": "BULL" if index_pct > 0.5 else ("BEAR" if index_pct < -0.5 else "NEUTRAL"),
        },
        "total_scanned": len(signals),
        "buy_candidates": sum(1 for s in signals if s["level"] in ("STRONG_BUY", "BUY")),
        "signals": signals,
    }
