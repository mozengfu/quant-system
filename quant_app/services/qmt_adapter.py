"""
QMT 数据适配器 - 替代 Tushare 行情数据源
"""
import logging
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

QMT_HOST = "http://192.168.10.25:1430"


def _get(endpoint, timeout=5):
    try:
        r = requests.get(f"{QMT_HOST}/{endpoint}", timeout=timeout)
        return r.json()
    except Exception as e:
        logger.warning(f"QMT {endpoint} 失败: {e}")
        return None


def get_index_daily(ts_code, limit=5):
    """获取指数日线数据（替代 tushare pro.index_daily）"""
    try:
        import pymysql
        conn = pymysql.connect(
            host="127.0.0.1", port=3306, user="root",
            password="root123", database="quant_db", charset="utf8mb4"
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_date, open, high, low, close, vol, amount, pct_chg "
            "FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s",
            (ts_code, limit)
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"trade_date": str(r[0]), "open": float(r[1] or 0), "high": float(r[2] or 0),
                 "low": float(r[3] or 0), "close": float(r[4] or 0), "vol": float(r[5] or 0),
                 "amount": float(r[6] or 0), "pct_chg": float(r[7] or 0)} for r in rows]
    except Exception as e:
        logger.warning(f"get_index_daily({ts_code}): {e}")
        return []


def get_realtime_indices():
    """获取实时指数行情"""
    data = _get("market/index")
    if data and data.get("indices"):
        return data["indices"]
    return []


def get_realtime_snapshot(codes=None):
    """获取实时个股快照"""
    data = _get("market/snapshot")
    if data and data.get("stocks"):
        stocks = data["stocks"]
        if codes:
            stocks = [s for s in stocks if s["code"] in codes]
        return stocks
    return []


def get_recent_trade_dates(n=5):
    """获取最近n个交易日（替代 tushare trade_cal）"""
    try:
        import pymysql
        conn = pymysql.connect(
            host="127.0.0.1", port=3306, user="root",
            password="root123", database="quant_db", charset="utf8mb4"
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date IS NOT NULL ORDER BY trade_date DESC LIMIT %s", (n,)
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        dates = [str(r[0]) for r in rows]
        return list(reversed(dates))
    except Exception as e:
        logger.warning(f"get_recent_trade_dates: {e}")
        # fallback
        today = datetime.now()
        return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n, 0, -1)]


def get_daily_data(ts_code, start_date=None, end_date=None, limit=100):
    """获取个股日线数据"""
    try:
        import pymysql
        conn = pymysql.connect(
            host="127.0.0.1", port=3306, user="root",
            password="root123", database="quant_db", charset="utf8mb4"
        )
        cur = conn.cursor()
        sql = "SELECT trade_date, open, high, low, close, vol, amount, pct_chg FROM daily_price WHERE ts_code=%s"
        params = [ts_code]
        if start_date:
            sql += " AND trade_date >= %s"
            params.append(start_date)
        if end_date:
            sql += " AND trade_date <= %s"
            params.append(end_date)
        sql += " ORDER BY trade_date ASC LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return [{"trade_date": str(r[0]), "open": float(r[1] or 0), "high": float(r[2] or 0),
                 "low": float(r[3] or 0), "close": float(r[4] or 0), "vol": float(r[5] or 0),
                 "amount": float(r[6] or 0), "pct_chg": float(r[7] or 0)} for r in rows]
    except Exception as e:
        logger.warning(f"get_daily_data({ts_code}): {e}")
        return []


def calculate_rps(code, market="sz", n=20):
    """计算RPS相对强度（从MySQL，替代tushare版本）"""
    try:
        ts_code = f"{code}.{'SZ' if market == 'sz' else 'SH'}"
        data = get_daily_data(ts_code, limit=n + 5)
        if len(data) < n:
            return 50

        # 计算个股n日涨幅
        closes = [d["pct_chg"] for d in data[-n:]]
        stock_ret = sum(closes) / len(closes) if closes else 0

        # 计算大盘涨幅（用上证指数）
        idx_data = get_index_daily("000001.SH", limit=n + 5)
        if len(idx_data) < n:
            return 50
        idx_closes = [d["pct_chg"] for d in idx_data[-n:]]
        idx_ret = sum(idx_closes) / len(idx_closes) if idx_closes else 0

        # RPS: 个股相对大盘的超额收益，映射到0-100
        rps = 50 + (stock_ret - idx_ret) * 10
        return max(0, min(100, rps))
    except Exception as e:
        logger.warning(f"calculate_rps({code}): {e}")
        return 50
