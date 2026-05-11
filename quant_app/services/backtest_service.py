"""
回测服务 - 单股回测策略

包含 V3 C3.0 / V4 / 增强版（MACD+KDJ+布林带）回测函数。
V4 规则回测保留，新增 V6 ML 预测信号验证回测。
"""
import os
import json
import time
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

from app_core import get_tushare_pro, get_stock_realtime, get_recent_trade_dates
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


# ========== 技术指标计算（移植自 app_core.py，返回完整时间序列） ==========


def calculate_ema(closes, period):
    if len(closes) < period:
        return [sum(closes) / len(closes)] * len(closes)
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in closes[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return [None] * (period - 1) + ema


def calculate_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return [], [], []
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    dif = []
    for f, s in zip(ema_fast, ema_slow):
        if f is None or s is None: dif.append(None)
        else: dif.append(f - s)
    valid_dif = [d for d in dif if d is not None]
    dea_full = calculate_ema(valid_dif, signal)
    none_count = sum(1 for d in dif if d is None)
    dea = [None] * none_count + dea_full
    macd_hist = []
    for d, de in zip(dif, dea):
        if d is None or de is None: macd_hist.append(None)
        else: macd_hist.append((d - de) * 2)
    return dif, dea, macd_hist


def calculate_kdj(highs, lows, closes, n=9, m1=3, m2=3):
    if len(closes) < n: return [], [], []
    k_list, d_list, j_list, rsv_list = [], [], [], []
    for i in range(len(closes)):
        if i < n - 1: rsv_list.append(None); continue
        low_n = min(lows[i-n+1:i+1]); high_n = max(highs[i-n+1:i+1])
        rsv = 50 if high_n == low_n else (closes[i] - low_n) / (high_n - low_n) * 100
        rsv_list.append(rsv)
    for i in range(len(rsv_list)):
        if rsv_list[i] is None: k_list.append(None); d_list.append(None); j_list.append(None)
        elif i == n - 1:
            k, d = rsv_list[i], rsv_list[i]
            k_list.append(k); d_list.append(d); j_list.append(3*k - 2*d)
        else:
            pk = k_list[-1] if k_list[-1] is not None else 50
            pd_ = d_list[-1] if d_list[-1] is not None else 50
            k = (pk * (m1-1) + rsv_list[i]) / m1
            d = (pd_ * (m2-1) + k) / m2
            k_list.append(k); d_list.append(d); j_list.append(3*k - 2*d)
    return k_list, d_list, j_list


def calculate_bollinger_bands(closes, period=20, std_dev=2):
    upper, middle, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1: upper.append(None); middle.append(None); lower.append(None)
        else:
            window = closes[i-period+1:i+1]
            ma = sum(window) / period
            variance = sum((x - ma) ** 2 for x in window) / period
            std = variance ** 0.5
            middle.append(ma); upper.append(ma + std_dev * std); lower.append(ma - std_dev * std)
    return upper, middle, lower


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return 14
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else 14


# ========== 旧版回测兼容（指向 V4）=========

def backtest_stock(code, market="sz", start_date="", end_date="", strategy="ma_cross"):
    return backtest_stock_v4(code, market, start_date, end_date)

def backtest_stock_enhanced(code, market="sz", start_date="", end_date=""):
    return backtest_stock_v4(code, market, start_date, end_date)


# ========== V4 单股回测（基于MySQL daily_price表）=========

def backtest_stock_v4(code, market="sz", start_date="", end_date=""):
    try:
        import pymysql
        db_config = get_db_config(connect_timeout=5)
        ts_code = "%s.%s" % (code, "SZ" if market == "sz" else "SH")
        if not end_date:
            conn = pymysql.connect(**db_config); cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM quant_db.daily_price"); row = cur.fetchone()
            cur.close(); conn.close()
            end_date = row[0].strftime("%Y%m%d") if row and row[0] else ""
        if not start_date:
            end_dt = datetime.strptime(end_date, "%Y%m%d")
            start_dt = end_dt - timedelta(days=180)
            start_date = start_dt.strftime("%Y%m%d")
        start_sql = start_date[:4]+"-"+start_date[4:6]+"-"+start_date[6:]
        end_sql = end_date[:4]+"-"+end_date[4:6]+"-"+end_date[6:]

        conn = pymysql.connect(**db_config); cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, open, close, high, low, vol, pct_chg,
                   turnover_rate, volume_ratio, ma5, ma10, ma20
            FROM quant_db.daily_price WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
            ORDER BY trade_date ASC
        """, [ts_code, start_sql, end_sql])
        rows = cur.fetchall(); cur.close(); conn.close()
        if not rows: return {"error": "无历史数据"}
        if len(rows) < 25: return {"error": "历史数据不足（仅 %d 条）" % len(rows)}

        dates = [r[0].strftime("%Y%m%d") for r in rows]
        closes = [float(r[2]) for r in rows]; highs = [float(r[3]) for r in rows]
        lows = [float(r[4]) for r in rows]; pct_chgs = [float(r[6]) if r[6] else 0 for r in rows]
        vol_ratios = [float(r[8]) if r[8] else 0 for r in rows]
        ma5_list = [float(r[9]) if r[9] else 0 for r in rows]
        ma10_list = [float(r[10]) if r[10] else 0 for r in rows]
        ma20_list = [float(r[11]) if r[11] else 0 for r in rows]

        trades = []; initial_cash = 100000.0; cash = initial_cash
        BUY_COMMISSION = 0.0003; SELL_COMMISSION = 0.0003; MAX_HOLD_DAYS = 7
        shares_held = 0; entry_price = 0; entry_date_idx = 0
        base_stop = 0; trailing_stop = 0
        shares_sold_t1 = False; shares_sold_t2 = False; peak_pnl = 0; hold_days = 0

        for i in range(25, len(closes)):
            price = closes[i]; vol_ratio = vol_ratios[i]; pct_chg = pct_chgs[i]
            ma5 = ma5_list[i]; ma10 = ma10_list[i]; ma20 = ma20_list[i]
            if shares_held == 0:
                if pct_chg > 9.5: continue
                buy = False; reason = ""
                if ma5 > ma10 > ma20 and price > ma5 and vol_ratio > 1.0:
                    buy = True; reason = "均线多头(%.2f>%.2f>%.2f) 量比%.2f" % (ma5, ma10, ma20, vol_ratio)
                elif pct_chg > 3.0 and vol_ratio > 1.0:
                    buy = True; reason = "涨幅%.2f%% 量比%.2f" % (pct_chg, vol_ratio)
                if buy:
                    qty = int(cash / price / 100) * 100
                    if qty > 0:
                        cash -= qty * price * (1 + BUY_COMMISSION)
                        shares_held = qty; entry_price = price; entry_date_idx = i
                        base_stop = round(price * 0.95, 2); trailing_stop = base_stop
                        shares_sold_t1 = False; shares_sold_t2 = False; peak_pnl = 0; hold_days = 0
                        trades.append({"日期": dates[i], "操作": "买入", "价格": round(price, 2), "数量": qty, "原因": reason})
            else:
                hold_days += 1; pnl_pct = (price - entry_price) / entry_price * 100
                if pnl_pct > peak_pnl: peak_pnl = pnl_pct
                if peak_pnl >= 15: trailing_stop = round(entry_price*1.10, 2)
                elif peak_pnl >= 10: trailing_stop = round(entry_price*1.05, 2)
                elif peak_pnl >= 5: trailing_stop = round(entry_price*1.00, 2)
                else: trailing_stop = base_stop
                sold = False

                def sell(t, pnl, reason):
                    nonlocal cash, shares_held, sold
                    proceeds = shares_held * price * (1 - SELL_COMMISSION)
                    cash += proceeds
                    trades.append({"日期": dates[i], "操作": t, "价格": round(price, 2), "数量": shares_held, "盈亏": round((price-entry_price)*shares_held - shares_held*entry_price*(BUY_COMMISSION+SELL_COMMISSION), 2), "收益率": "%.2f%%" % pnl_pct, "原因": reason})
                    shares_held = 0; sold = True

                if not sold and shares_held > 0 and (price >= entry_price*1.18 or pct_chg >= 17.5):
                    sell("清仓", pnl_pct, "止盈+18%%"); continue
                if not sold and shares_held > 0 and not shares_sold_t2 and (price >= entry_price*1.10 or pct_chg >= 9.5):
                    q = int(shares_held/2)
                    if q > 0:
                        proceeds = q * price * (1 - SELL_COMMISSION); cash += proceeds
                        shares_held -= q; shares_sold_t2 = True
                        trades.append({"日期": dates[i], "操作": "卖1/2", "价格": round(price, 2), "数量": q, "盈亏": round((price-entry_price)*q - q*entry_price*(BUY_COMMISSION+SELL_COMMISSION), 2), "收益率": "%.2f%%" % pnl_pct, "原因": "止盈+10%%"})
                if not sold and shares_held > 0 and not shares_sold_t1 and (price >= entry_price*1.06 or pct_chg >= 5.5):
                    q = int(shares_held/2)
                    if q > 0:
                        proceeds = q * price * (1 - SELL_COMMISSION); cash += proceeds
                        shares_held -= q; shares_sold_t1 = True
                        trades.append({"日期": dates[i], "操作": "卖1/2", "价格": round(price, 2), "数量": q, "盈亏": round((price-entry_price)*q - q*entry_price*(BUY_COMMISSION+SELL_COMMISSION), 2), "收益率": "%.2f%%" % pnl_pct, "原因": "止盈+6%%"})
                if not sold and shares_held > 0 and (price <= trailing_stop or hold_days >= MAX_HOLD_DAYS):
                    r = "止损(=%.2f)" % trailing_stop if price <= trailing_stop else "超时%d天清仓" % MAX_HOLD_DAYS
                    sell("卖出", pnl_pct, r)

        if shares_held > 0:
            sell("期末平仓", (closes[-1]-entry_price)/entry_price*100, "回测结束强制平仓")

        final_value = cash
        total_return = (final_value - initial_cash) / initial_cash * 100
        closed = [t for t in trades if t.get("操作") in ("卖出", "卖1/2", "清仓", "期末平仓")]
        wins = sum(1 for t in closed if t.get("盈亏", 0) > 0)
        losses = len(closed) - wins
        rt_name = ""
        try:
            rt = get_stock_realtime(code, market); rt_name = rt.get("名称", "") if rt else ""
        except Exception:
            pass
        return {"股票代码": "%s%s" % (market.upper(), code), "股票名称": rt_name,
                "回测区间": "%s ~ %s" % (start_date, end_date), "策略": "V4 规则回测",
                "初始资金": "10万元", "最终市值": "%.2f元" % final_value, "总收益率": "%.2f%%" % total_return,
                "交易次数": len(closed), "盈利次数": wins, "亏损次数": losses,
                "胜率": "%.1f%%" % (wins/max(len(closed),1)*100), "交易记录": trades[-30:]}
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"error": str(e)}


# ========== 三种策略回测（基于实际 SQL 筛选条件）=========

def _run_strategy_backtest(code, market, start_date, end_date, strategy_name, condition_fn):
    from datetime import datetime, timedelta
    import pymysql
    import numpy as np
    try:
        db_config = get_db_config(connect_timeout=5)
        ts_code = "%s.%s" % (code, "SZ" if market == "sz" else "SH")
        if not end_date:
            conn = pymysql.connect(**db_config); cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM quant_db.daily_price"); row = cur.fetchone()
            cur.close(); conn.close()
            end_date = row[0].strftime("%Y%m%d") if row and row[0] else ""
        if not start_date:
            end_dt = datetime.strptime(end_date, "%Y%m%d")
            start_dt = end_dt - timedelta(days=360)
            start_date = start_dt.strftime("%Y%m%d")
        start_fmt = start_date[:4]+"-"+start_date[4:6]+"-"+start_date[6:]
        end_fmt = end_date[:4]+"-"+end_date[4:6]+"-"+end_date[6:]

        conn = pymysql.connect(**db_config); cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, close, pct_chg, volume_ratio, turnover_rate,
                   ma5, ma10, ma20
            FROM quant_db.daily_price
            WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s
            ORDER BY trade_date ASC
        """, [ts_code, start_fmt, end_fmt])
        rows = cur.fetchall(); cur.close(); conn.close()
        if not rows or len(rows) < 20:
            return {"error": "数据不足（需至少20个交易日）"}

        dates = [r[0].strftime("%Y%m%d") for r in rows]
        closes = [float(r[1]) for r in rows]
        pct_chgs = [float(r[2]) if r[2] else 0 for r in rows]
        vrs = [float(r[3]) if r[3] else 0 for r in rows]
        trs = [float(r[4]) if r[4] else 0 for r in rows]
        ma5s = [float(r[5]) if r[5] else 0 for r in rows]
        ma10s = [float(r[6]) if r[6] else 0 for r in rows]
        ma20s = [float(r[7]) if r[7] else 0 for r in rows]

        signals = []
        for i in range(20, len(rows)):
            if pct_chgs[i] > 9.5: continue
            d = {'close': closes[i], 'pct_chg': pct_chgs[i], 'vr': vrs[i],
                 'tr': trs[i], 'ma5': ma5s[i], 'ma10': ma10s[i], 'ma20': ma20s[i], 'date': dates[i]}
            if condition_fn(d):
                a5 = (closes[min(i+5, len(closes)-1)] - closes[i]) / closes[i] * 100
                a10 = (closes[min(i+10, len(closes)-1)] - closes[i]) / closes[i] * 100
                signals.append({'trade_date': dates[i], 'price': round(closes[i], 2),
                                'pct_chg': round(pct_chgs[i], 2), 'vr': round(vrs[i], 2),
                                'ret_5d': round(a5, 2), 'ret_10d': round(a10, 2)})

        if not signals:
            return {"error": "回测期内未触发买入信号，条件较严格"}

        win5 = sum(1 for s in signals if s['ret_5d'] > 0)
        win10 = sum(1 for s in signals if s['ret_10d'] > 0)
        avg5 = np.mean([s['ret_5d'] for s in signals])
        avg10 = np.mean([s['ret_10d'] for s in signals])

        rt_name = ""
        try:
            rt = get_stock_realtime(code, market); rt_name = rt.get("名称", "") if rt else ""
        except Exception:
            pass

        top3 = sorted(signals, key=lambda x: x['ret_5d'], reverse=True)[:3]
        worst3 = sorted(signals, key=lambda x: x['ret_5d'])[:3]

        return {
            "股票名称": rt_name, "股票代码": "%s%s" % (market.upper(), code),
            "策略": strategy_name, "回测区间": "%s ~ %s" % (start_date, end_date),
            "触发次数": len(signals),
            "5日胜率": "%.1f%% (%d/%d)" % (win5/len(signals)*100, win5, len(signals)),
            "10日胜率": "%.1f%% (%d/%d)" % (win10/len(signals)*100, win10, len(signals)),
            "5日平均收益": "%.2f%%" % avg5, "10日平均收益": "%.2f%%" % avg10,
            "最佳3次": top3, "最差3次": worst3,
            "全部信号": signals[-50:],
        }
    except Exception as e:
        import traceback; traceback.print_exc()
        return {"error": str(e)}


def backtest_bottom(code, market="sz", start_date="", end_date=""):
    """底部起步：涨幅-5%~10% + 量比>1.0"""
    return _run_strategy_backtest(code, market, start_date, end_date, "底部起步",
        lambda d: -5 <= d['pct_chg'] <= 10 and d['vr'] > 1.0)


def backtest_strong(code, market="sz", start_date="", end_date=""):
    """强势活跃：涨幅>3% + 量比>2 + 换手率>5%"""
    return _run_strategy_backtest(code, market, start_date, end_date, "强势活跃",
        lambda d: d['pct_chg'] > 3 and d['vr'] > 2.0 and d['tr'] > 5.0)


def backtest_combo(code, market="sz", start_date="", end_date=""):
    """组合策略：均线多头+放量 或 涨幅>3%+放量"""
    def cond(d):
        if d['close'] <= 5 or d['tr'] <= 1.5: return False
        if not (1 < d['pct_chg'] < 9.5): return False
        bull = d['ma5'] > d['ma10'] > d['ma20'] and d['close'] > d['ma5'] and d['vr'] > 1.5
        breakout = d['pct_chg'] > 3.0 and d['vr'] > 1.5
        return bull or breakout
    return _run_strategy_backtest(code, market, start_date, end_date, "组合策略", cond)
