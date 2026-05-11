#!/usr/bin/env python3
"""
行情数据服务 - 实时行情、历史数据、RPS计算、持仓同步、技术面买卖信号
"""
import os, json, time, logging, hashlib, hmac, urllib.request, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request as UrlRequest

import pandas as pd

from quant_app.utils.config import get_db_config
from quant_app.services.technical_service import calculate_macd, calculate_kdj, calculate_bollinger_bands, calculate_atr, calculate_rsi
from quant_app.utils.indicators import calculate_macd as _calc_macd_full, calculate_kdj as _calc_kdj_full
from quant_app.services.realtime_service import (
    _code_to_secid, _try_aliyun, _try_tencent,
    get_stock_quote as get_stock_realtime,
)

logger = logging.getLogger(__name__)

# ========== 东方财富实时行情 ==========
EASTMONEY_HOST = "http://push2.eastmoney.com"

# ========== 实时行情缓存（30秒过期）==========
_quote_cache = {}
_QUOTE_CACHE_TTL = 30

def _get_cached(key):
    if key in _quote_cache:
        t = _quote_cache[key]
        if time.time() - t['ts'] < _QUOTE_CACHE_TTL:
            return t['data']
    return None

def _set_cache(key, data):
    _quote_cache[key] = {'data': data, 'ts': time.time()}
    # 控制缓存大小
    if len(_quote_cache) > 500:
        now = time.time()
        for k in list(_quote_cache.keys()):
            if now - _quote_cache[k]['ts'] > _QUOTE_CACHE_TTL * 2:
                del _quote_cache[k]

# ========== Tushare 配置 ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")


def get_tushare_pro():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def get_recent_trade_dates(n=5):
    """获取最近n个交易日（优化版，使用trade_cal API）"""
    try:
        pro = get_tushare_pro()
        today_str = datetime.now().strftime("%Y%m%d")
        # 使用trade_cal API一次性获取多个交易日
        cal_df = pro.trade_cal(exchange='SSE', start_date=(datetime.now() - timedelta(days=60)).strftime('%Y%m%d'),
                                end_date=today_str, is_open='1')
        if cal_df is None or len(cal_df) == 0:
            # Fallback: 使用旧的笨方法但减少API调用
            return get_recent_trade_dates_fallback(n)
        dates = cal_df['cal_date'].tolist()[:n]
        return list(reversed(dates))  # 转换为升序（早到晚）
    except Exception as e:
        logger.warning(f"获取交易日历失败: {e}")
        return get_recent_trade_dates_fallback(n)


def get_recent_trade_dates_fallback(n=5):
    """Fallback: 从MySQL获取最近n个交易日，避免循环API调用"""
    try:
        import pymysql
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT %s", (n,)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        if rows:
            dates = [str(r[0]) for r in rows]
            return list(reversed(dates))
        return []
    except Exception as e:
        logger.warning(f"从MySQL获取交易日失败: {e}")
        return []


# ========== RPS 相对强度 ==========
def get_latest_rps_from_db(ts_code):
    """
    从MySQL快速获取个股最新RPS（20日相对强弱）
    MySQL rps_20字段：0=无数据，50=中性（大盘同步），>50=跑赢大盘
    返回: float rps_20，失败返回50
    """
    try:
        import pymysql
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT rps_20 FROM quant_db.daily_price WHERE ts_code=%s AND rps_20 IS NOT NULL AND rps_20 > 0 ORDER BY trade_date DESC LIMIT 1",
            [ts_code]
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and row[0]:
            rps = float(row[0])
            return max(0.0, min(100.0, rps))
        return None
    except Exception:
        return None


def calculate_rps(code, market="sz", n=20):
    """
    计算个股 RPS 相对强度（改进版）
    优先从MySQL读取预计算的rps_20；MySQL无数据时才调Tushare API。
    """
    try:
        # 优先从MySQL读（已预计算好，毫秒级）
        ts_code = f"{code}.{'SZ' if market == 'sz' else 'SH'}"
        rps = get_latest_rps_from_db(ts_code)
        if rps is not None and rps > 0:  # valid data (including 50.0 = market-neutral); None = no data, fall back to Tushare
            return rps
        # MySQL无数据或为默认值50，回退到Tushare API（原有逻辑）
        pro = get_tushare_pro()
        dates = get_recent_trade_dates(n + 2)
        if len(dates) < n + 1:
            return 50
        start_date = dates[0]
        end_date = dates[-1]
        df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or len(df) < 2:
            return 50
        df = df.sort_values("trade_date")
        closes = df["close"].tolist()
        if len(closes) < 2 or closes[0] == 0:
            return 50
        stock_chg = (closes[-1] / closes[0] - 1) * 100
        try:
            hs300 = pro.index_daily(ts_code="000300.SH", start_date=start_date, end_date=end_date)
            if hs300 is not None and len(hs300) >= 2:
                hs300 = hs300.sort_values("trade_date")
                hc = hs300["close"].tolist()
                hs300_chg = (hc[-1] / hc[0] - 1) * 100
            else:
                hs300_chg = 0
        except Exception:
            hs300_chg = 0
        relative_chg = stock_chg - hs300_chg
        rps = 50 + relative_chg * 5
        return max(0, min(100, rps))
    except Exception as e:
        logger.warning(f"RPS 计算失败 {code}: {e}")
        return 50


# ========== 持仓自动同步 ==========

def sync_positions():
    """
    从 stocks.json 同步持仓到 positions.json（直接复制中文字段）
    """
    try:
        from quant_app.utils.config import DATA_DIR
        stocks_path = DATA_DIR / "stocks.json"
        positions_path = DATA_DIR / "positions.json"
        if stocks_path.exists():
            with open(stocks_path, "r", encoding="utf-8") as f:
                stocks_data = json.load(f)
            cn_positions = stocks_data.get("持仓", None)
            if cn_positions and len(cn_positions) > 0:
                positions_path.parent.mkdir(parents=True, exist_ok=True)
                with open(positions_path, "w", encoding="utf-8") as f:
                    json.dump(cn_positions, f, ensure_ascii=False, indent=2)
                logger.info("持仓同步完成: %d 只", len(cn_positions))
            else:
                logger.info("stocks.json 无持仓数据，跳过同步，保留现有 positions.json")
    except Exception as e:
        logger.warning("持仓同步失败: %s", e)


def add_to_positions(signal):
    """
    将买入信号添加到 stocks.json 持仓列表
    """
    try:
        from quant_app.utils.config import DATA_DIR
        stocks_path = DATA_DIR / "stocks.json"
        if stocks_path.exists():
            with open(stocks_path, "r", encoding="utf-8") as f:
                stocks_data = json.load(f)

            # 检查是否已存在
            exists = False
            for pos in stocks_data.get("持仓", []):
                if pos["代码"] == signal["code"]:
                    exists = True
                    logger.info(f"持仓已存在: {signal['name']} ({signal['code']})")
                    break

            if not exists:
                # 计算动态止损止盈（基于均线支撑/压力）
                price = float(signal["price"])
                # 止损止盈优先使用市场状态参数
                sl_pct = -5
                tp_pct = 8
                try:
                    from market_state import get_market_state
                    ms = get_market_state() or {}
                    p = ms.get('params', {})
                    sl_pct = p.get('stop_loss_pct', -5)
                    tp_pct = p.get('take_profit_pct', 8)
                except Exception:
                    pass
                stop_loss = round(price * (1 + sl_pct / 100), 2)
                take_profit = round(price * (1 + tp_pct / 100), 2)

                new_position = {
                    "代码": signal["code"],
                    "名称": signal["name"],
                    "市场": "sz" if signal["code"].startswith("0") or signal["code"].startswith("3") else "sh",
                    "数量": signal["qty"],
                    "成本": signal["price"],
                    "止损": stop_loss,
                    "止盈": take_profit,
                    "止损类型": "固定",
                    "止盈类型": "固定",
                    "ATR": 0,  # 后续由持仓监控更新
                }

                stocks_data["持仓"].append(new_position)

                with open(stocks_path, "w", encoding="utf-8") as f:
                    json.dump(stocks_data, f, ensure_ascii=False, indent=2)

                logger.info(f"已添加持仓: {signal['name']} ({signal['code']}) {signal['qty']}股 @ {signal['price']}元")
    except Exception as e:
        logger.warning(f"添加持仓失败: {e}")


def get_stock_history_from_db(ts_code, days=60):
    """
    从MySQL的daily_price表读取历史数据，用于加速get_technical_buy_sell_signals。
    返回: dict {trade_date: {open, high, low, close, vol, ..., ma5, ma10, ma20, rps, high52w, low52w}}
    失败时返回空字典（会回退到Tushare API）。
    """
    try:
        import pymysql
        import json

        db_config = get_db_config()

        conn = pymysql.connect(**db_config)
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # 计算起始日期
        cursor.execute("SELECT trade_date FROM quant_db.daily_price ORDER BY trade_date DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return {}

        from datetime import datetime, timedelta
        end_date = row['trade_date']
        # MySQL DATE format conversion
        def to_mysql_date(d):
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m-%d")
            s = str(d)
            if len(s) == 8:
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            return s
        mysql_end = to_mysql_date(end_date)
        start_date = (datetime.strptime(mysql_end, '%Y-%m-%d') - timedelta(days=days+30)).strftime('%Y-%m-%d')
        mysql_start = to_mysql_date(start_date)

        cursor.execute(
            "SELECT *, DATE_FORMAT(trade_date, '%%Y%%m%%d') as td_str FROM quant_db.daily_price WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date ASC",
            [ts_code, mysql_start, mysql_end]
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            return {}

        result = {}
        for r in rows:
            d = str(r['td_str'])
            result[d] = {
                'open': float(r['open']) if r['open'] else 0.0,
                'high': float(r['high']) if r['high'] else 0.0,
                'low': float(r['low']) if r['low'] else 0.0,
                'close': float(r['close']) if r['close'] else 0.0,
                'vol': float(r['vol']) if r['vol'] else 0.0,
                'turnover': float(r['turnover_rate']) if r.get('turnover_rate') is not None else 0.0,
                'ma5': float(r['ma5']) if r['ma5'] else 0.0,
                'ma10': float(r['ma10']) if r['ma10'] else 0.0,
                'ma20': float(r['ma20']) if r['ma20'] else 0.0,
                'rps': float(r['rps_20']) if r['rps_20'] else 0.0,
                'high52w': float(r['high_52w']) if r['high_52w'] else 0.0,
                'low52w': float(r['low_52w']) if r['low_52w'] else 0.0,
            }
        return result
    except Exception as e:
        return {}


def _prev_macd(closes):
    """计算最近两期的MACD值，用于金叉/死叉检测
    一次性算出全序列，取最后两期值，避免重复计算"""
    if len(closes) < 27:
        return None, None, None, None
    dif_list, dea_list, _ = _calc_macd_full(closes)
    if not dif_list or dif_list[-1] is None:
        return None, None, None, None
    dif = dif_list[-1]
    dea = dea_list[-1]
    p_dif = dif_list[-2] if len(dif_list) >= 2 and dif_list[-2] is not None else dif
    p_dea = dea_list[-2] if len(dea_list) >= 2 and dea_list[-2] is not None else dea
    return dif, dea, p_dif, p_dea


def _prev_kdj(highs, lows, closes):
    """计算最近两期的KDJ值，用于金叉/死叉检测
    一次性算出全序列，取最后两期值"""
    if len(closes) < 10:
        return None, None, None, None
    k_list, d_list, _ = _calc_kdj_full(highs, lows, closes)
    if not k_list or k_list[-1] is None:
        return None, None, None, None
    k, d = k_list[-1], d_list[-1]
    p_k = k_list[-2] if len(k_list) >= 2 and k_list[-2] is not None else k
    p_d = d_list[-2] if len(d_list) >= 2 and d_list[-2] is not None else d
    return k, d, p_k, p_d


def _ma(closes, n):
    """简单移动平均"""
    if len(closes) < n:
        return sum(closes) / len(closes)
    return sum(closes[-n:]) / n


def get_technical_buy_sell_signals(code, market="sz"):
    """
    技术面买卖点建议分析（趋势优先版）
    流程: 定趋势 → 判时机 → 管风险
    """
    try:
        pro = get_tushare_pro()
        ts_code = f"{code}.{'SZ' if market=='sz' else 'SH'}"

        # 优先从MySQL读取预计算数据（快速）
        hist_data = get_stock_history_from_db(ts_code, days=60)

        if hist_data:
            dates_key = sorted(hist_data.keys())
            if len(dates_key) < 3:
                return {"error": "MySQL数据不足，回退到API"}

            data_rows = []
            for d in dates_key:
                r = hist_data[d]
                data_rows.append({
                    'trade_date': d,
                    'open': r['open'],
                    'high': r['high'],
                    'low': r['low'],
                    'close': r['close'],
                    'vol': r['vol'],
                    'turnover': r['turnover'],
                    'ma5': r['ma5'],
                    'ma10': r['ma10'],
                    'ma20': r['ma20'],
                    'rps': r['rps'],
                    'high52w': r['high52w'],
                    'low52w': r['low52w'],
                })
            import pandas as pd
            df = pd.DataFrame(data_rows)
            logger.info(f"{code} 从MySQL读取 {len(df)} 条数据")
        else:
            data_days_options = [60, 30, 20, 10, 5]
            df = None
            dates = None

            for days in data_days_options:
                dates = get_recent_trade_dates(days)
                if len(dates) < 3:
                    continue
                start_date = dates[0]
                end_date = dates[-1]

                import time
                start_time = time.time()
                df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)

                if time.time() - start_time > 8:
                    logger.warning(f"数据获取超时 {code}: {time.time() - start_time:.1f}s")

                if df is not None and len(df) >= 3:
                    logger.info(f"{code} 获取到 {len(df)} 条数据")
                    break
                df = None

            if df is None or len(df) < 3:
                return {"error": "数据获取失败，请稍后再试"}

        df = df.sort_values("trade_date").reset_index(drop=True)
        closes = df["close"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        vols = df["vol"].tolist()

        # 实时价格
        rt = get_stock_realtime(code, market)
        rt_name = rt.get("名称", "") if rt else ""
        rt_price = rt.get("现价", closes[-1]) if rt else closes[-1]

        # 计算技术指标（单值）
        dif, dea, macd_hist = calculate_macd(closes)
        k, d, j = calculate_kdj(highs, lows, closes)
        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes)
        atr = calculate_atr(highs, lows, closes)
        rsi = calculate_rsi(closes)

        # 前一期值（用于交叉检测）
        macd_cross = _prev_macd(closes)
        prev_dif, prev_dea, p_dif, p_dea = macd_cross
        kdj_cross = _prev_kdj(highs, lows, closes)
        prev_k, prev_d, p_k, p_d = kdj_cross

        curr_close = closes[-1]

        # 均线
        ma5 = _ma(closes, 5)
        ma10 = _ma(closes, 10)
        ma20 = _ma(closes, 20)

        # 量能基准
        avg_vol_5 = sum(vols[-5:]) / 5 if len(vols) >= 5 else sum(vols) / len(vols)
        recent_vol_2 = sum(vols[-2:]) / 2

        # ========== 一、趋势判断 ==========
        # 均线排列
        if ma5 > ma10 > ma20:
            ma_desc = "多头排列"
            ma_score = 20
        elif ma5 < ma10 < ma20:
            ma_desc = "空头排列"
            ma_score = -20
        elif ma5 > ma20:
            ma_desc = "短多长空"
            ma_score = 5
        elif ma5 < ma20:
            ma_desc = "短空长多"
            ma_score = -5
        else:
            ma_desc = "均线缠绕"
            ma_score = 0

        # MACD 趋势
        if dif is not None and dea is not None:
            if dif > dea and dif > 0:
                macd_desc = f"多头(DIF>{round(dif, 3)})"
            elif dif > dea:
                macd_desc = "零轴下金叉"
            elif dif < dea and dif > 0:
                macd_desc = "多头回调"
            else:
                macd_desc = f"空头(DIF<{round(dif, 3)})"
        else:
            macd_desc = "数据不足"

        # 大盘状态
        market_state_ctx = {}
        try:
            from market_state import get_market_state
            ms = get_market_state() or {}
            market_state_ctx = {
                "状态": ms.get("state_name", "未知"),
                "评分": ms.get("score", 0),
                "建议": ms.get("advice", ""),
            }
        except Exception:
            pass

        # 综合趋势评级
        trend_total = ma_score
        if dif is not None and dea is not None:
            trend_total += 10 if dif > dea else -10
        if market_state_ctx.get("评分", 0) > 15:
            trend_total += 10
        elif market_state_ctx.get("评分", 0) < -15:
            trend_total -= 10

        if trend_total >= 15:
            trend_dir = "上升"
        elif trend_total <= -15:
            trend_dir = "下降"
        else:
            trend_dir = "震荡"

        # ========== 二、买入信号（max 100）==========
        buy_score = 0
        buy_reasons = []

        # 趋势层（0~40）
        if ma5 > ma10 and ma10 > ma20:
            buy_score += 20
            buy_reasons.append("均线多头排列")
        elif ma5 > ma20:
            buy_score += 10
            buy_reasons.append("短期均线多头")

        if dif is not None and dea is not None:
            if dif > dea and dif > 0:
                buy_score += 10
                buy_reasons.append(f"MACD金叉")
            elif dif > dea:
                buy_score += 5
                buy_reasons.append("MACD金叉区域")

            # 近期金叉确认
            if prev_dif is not None and prev_dea is not None:
                if prev_dif <= prev_dea and dif > dea:
                    buy_score += 5
                    buy_reasons.append("MACD金叉形成")

        if market_state_ctx.get("状态") in ("趋势上涨", "过热"):
            buy_score += 5

        # 时机层（0~35）
        if trend_dir == "上升":
            if abs(curr_close - ma10) / ma10 < 0.015:
                buy_score += 15
                buy_reasons.append("回踩MA10支撑")
            elif abs(curr_close - ma20) / ma20 < 0.015:
                buy_score += 12
                buy_reasons.append("回踩MA20支撑")
            elif curr_close < bb_middle and curr_close >= bb_lower:
                buy_score += 10
                buy_reasons.append("布林低位支撑")

        if curr_close <= bb_middle and curr_close >= bb_lower * 1.02:
            buy_score += 5

        if k is not None and d is not None:
            if p_k is not None and p_d is not None and p_k <= p_d and k > d and k < 30:
                buy_score += 10
                buy_reasons.append(f"KDJ超卖金叉(K={k:.1f})")
            elif k < 30:
                buy_score += 5
                buy_reasons.append(f"KDJ超卖区(K={k:.1f})")
            elif k < 50:
                buy_score += 3

        # 确认层（0~25）
        if rsi is not None:
            if rsi < 35:
                buy_score += 10
                buy_reasons.append(f"RSI超卖({rsi:.1f})")
            elif rsi < 50:
                buy_score += 5

        if recent_vol_2 < avg_vol_5 * 0.8:
            buy_score += 10
            buy_reasons.append("缩量回调")
        elif recent_vol_2 < avg_vol_5 * 0.95:
            buy_score += 5
            buy_reasons.append("量能萎缩")

        # 弱势市场打折
        weak_market = market_state_ctx.get("状态")
        if weak_market == "趋势下跌":
            buy_score = int(buy_score * 0.5)
        elif weak_market == "恐慌":
            buy_score = int(buy_score * 0.3)

        buy_score = min(100, buy_score)

        # ========== 三、卖出信号（max 100）==========
        sell_score = 0
        sell_reasons = []

        # 趋势反转（0~40）
        if ma5 < ma10 and ma10 < ma20:
            sell_score += 20
            sell_reasons.append("均线空头排列")
        elif ma5 < ma20:
            sell_score += 10
            sell_reasons.append("短期均线空头")

        if dif is not None and dea is not None:
            if dif < dea and dif < 0:
                sell_score += 15
                sell_reasons.append("MACD死叉")
            elif dif < dea:
                sell_score += 10
                sell_reasons.append("MACD死叉区域")

            if prev_dif is not None and prev_dea is not None:
                if prev_dif >= prev_dea and dif < dea:
                    sell_score += 5

        # 超买（0~30）
        if k is not None and d is not None:
            if p_k is not None and p_d is not None and p_k >= p_d and k < d and k > 65:
                sell_score += 15
                sell_reasons.append(f"KDJ高位死叉(K={k:.1f})")
            elif k > 80:
                sell_score += 10
                buy_reasons.append(f"KDJ超买(K={k:.1f})")
            elif k > 70:
                sell_score += 5

        if rsi is not None and rsi > 70:
            sell_score += 10
            sell_reasons.append(f"RSI超买({rsi:.1f})")

        # 风险确认（0~30）
        if curr_close >= bb_upper * 0.98:
            if recent_vol_2 > avg_vol_5 * 1.5:
                sell_score += 15
                sell_reasons.append("放量突破布林上轨")
            else:
                sell_score += 5

        if curr_close < bb_middle:
            sell_score += 5

        # 大盘弱势叠加
        if weak_market in ("趋势下跌", "恐慌") and sell_score > 0:
            sell_score += 15
            sell_reasons.append("大盘弱势")

        sell_score = min(100, sell_score)

        # ========== 四、最佳买入区间 ==========
        # 止损价 = min(布林下轨, 现价-2×ATR, 近期低点)
        stop_loss_candidates = []
        if bb_lower is not None:
            stop_loss_candidates.append(bb_lower)
        if atr is not None and rt_price > atr * 2:
            stop_loss_candidates.append(round(rt_price - 2 * atr, 2))
        if len(lows) >= 5:
            stop_loss_candidates.append(min(lows[-5:]))
        stop_loss = min(stop_loss_candidates) if stop_loss_candidates else round(rt_price * 0.95, 2)

        if curr_close <= bb_lower:
            buy_range = f"现价{curr_close:.2f}元已跌破布林下轨{bb_lower:.2f}，谨慎"
            buy_note = "跌破下轨，等企稳再考虑"
        elif curr_close <= bb_middle:
            buy_range = f"布林下轨{bb_lower:.2f} ~ 中轨{bb_middle:.2f}"
            buy_note = "低位区间，可分批建仓"
        else:
            buy_range = f"布林下轨{bb_lower:.2f} ~ 中轨{bb_middle:.2f}"
            buy_note = "价格在中轨之上，等回踩介入"

        # ========== 五、止盈目标（ATR动态+百分比参考）==========
        atr_15 = round(rt_price + 1.5 * atr, 2) if atr else round(rt_price * 1.05, 2)
        atr_25 = round(rt_price + 2.5 * atr, 2) if atr else round(rt_price * 1.10, 2)
        atr_40 = round(rt_price + 4.0 * atr, 2) if atr else round(rt_price * 1.15, 2)

        target_pct_8 = round(rt_price * 1.08, 2)
        target_pct_15 = round(rt_price * 1.15, 2)
        target_pct_25 = round(rt_price * 1.25, 2)

        # ========== 六、持仓建议 ==========
        hold_days_map = {"趋势上涨": 7, "过热": 4, "震荡": 5, "趋势下跌": 3, "恐慌": 2}
        hold_d = hold_days_map.get(market_state_ctx.get("状态"), 5)
        hold_days = f"建议{hold_d}天内"

        # 波动率
        if len(closes) >= 20:
            returns = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]
            avg_r = sum(returns) / len(returns)
            variance = sum((r - avg_r) ** 2 for r in returns) / len(returns)
            volatility = variance ** 0.5
            vol_level = "高" if volatility > 3 else ("中" if volatility > 1.5 else "低")
        else:
            volatility = 0
            vol_level = "中"

        return {
            "股票代码": f"{market.upper()}{code}",
            "股票名称": rt_name,
            "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "一、趋势判断": {
                "趋势方向": trend_dir,
                "均线排列": ma_desc,
                "MACD趋势": macd_desc,
                "大盘状态": market_state_ctx.get("状态", "未知"),
                "综合评分": trend_total,
            },
            "二、买入信号": {
                "评分": buy_score,
                "理由": " + ".join(buy_reasons) if buy_reasons else "信号不足",
            },
            "三、卖出信号": {
                "评分": sell_score,
                "理由": " + ".join(sell_reasons) if sell_reasons else "信号不足",
            },
            "四、最佳买入区间": {
                "当前价": f"{rt_price:.2f}元",
                "当前价_num": round(rt_price, 2),
                "布林下轨": f"{bb_lower:.2f}元",
                "布林下轨_num": round(bb_lower, 2),
                "布林中轨": f"{bb_middle:.2f}元",
                "布林中轨_num": round(bb_middle, 2),
                "建议区间": buy_range,
                "操作建议": buy_note,
                "止损价": f"{stop_loss:.2f}元",
                "止损价_num": round(stop_loss, 2),
                "止损幅度": f"{(1 - stop_loss / rt_price) * 100:.1f}%",
            },
            "五、止盈目标": {
                "保守目标(ATR1.5x)": f"{atr_15:.2f}元",
                "保守目标_num": round(atr_15, 2),
                "中性目标(ATR2.5x)": f"{atr_25:.2f}元",
                "中性目标_num": round(atr_25, 2),
                "乐观目标(ATR4.0x)": f"{atr_40:.2f}元",
                "乐观目标_num": round(atr_40, 2),
                "保守目标(+8%)": f"{target_pct_8:.2f}元",
                "保守目标_pct_num": round(target_pct_8, 2),
                "中性目标(+15%)": f"{target_pct_15:.2f}元",
                "中性目标_pct_num": round(target_pct_15, 2),
                "乐观目标(+25%)": f"{target_pct_25:.2f}元",
                "乐观目标_pct_num": round(target_pct_25, 2),
            },
            "六、持仓建议": {
                "建议持仓天数": hold_days,
                "波动率": f"{volatility:.2f}%（{vol_level}波动）",
                "ATR（真实波幅）": f"{atr:.2f}元" if atr else "数据不足",
            },
            "七、技术指标": {
                "MACD": {
                    "DIF": round(dif, 3) if dif else 0,
                    "DEA": round(dea, 3) if dea else 0,
                    "MACD柱": round(macd_hist, 3) if macd_hist else 0,
                    "多空状态": "多头" if dif and dif > 0 else "空头",
                },
                "KDJ": {
                    "K": round(k, 1) if k else 50,
                    "D": round(d, 1) if d else 50,
                    "J": round(j, 1) if j else 50,
                    "状态": "超卖" if k and k < 30 else ("超买" if k and k > 80 else ("偏弱" if k and k < 50 else ("偏强" if k and k > 70 else "中性"))),
                },
                "RSI": {
                    "RSI": round(rsi, 1) if rsi else 50,
                    "状态": "超卖" if rsi and rsi < 35 else ("超买" if rsi and rsi > 70 else "中性"),
                },
                "布林带": {
                    "上轨": round(bb_upper, 2),
                    "中轨": round(bb_middle, 2),
                    "下轨": round(bb_lower, 2),
                    "当前价位置": "上轨附近" if rt_price >= bb_upper * 0.98 else ("下轨附近" if rt_price <= bb_lower * 1.02 else "中轨附近"),
                },
            },
        }
    except Exception as e:
        logger.error(f"技术面分析失败 {code}: {e}")
        return {"error": str(e)}
