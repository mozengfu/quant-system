#!/usr/bin/env python3
"""v5: 加连接健康检查 + 50股步进 + 异常全部捕获"""
import logging
import os
import sys
import time

import numpy as np
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

CHUNK_INSERT = 100


def _rolling_mean(arr, w):
    n = len(arr)
    out = np.zeros(n)
    cumsum = np.cumsum(np.concatenate([[0.0], arr]))
    out[w-1:] = (cumsum[w:] - cumsum[:-w]) / w
    return out


def _rolling_max(arr, w):
    n = len(arr)
    out = np.zeros(n)
    for i in range(w-1, n):
        out[i] = arr[i-w+1:i+1].max()
    return out


def process(rows, ts_code, industry):
    if len(rows) < 60: return []
    n = len(rows)
    trade_dates = [r[0] for r in rows]
    close = np.array([float(r[4]) if r[4] else 0.0 for r in rows])
    high = np.array([float(r[2]) if r[2] else 0.0 for r in rows])
    vol = np.array([float(r[5]) if r[5] else 0.0 for r in rows])
    pct = np.array([float(r[7]) if r[7] else 0.0 for r in rows])
    if np.any(close <= 0): return []

    ma20 = _rolling_mean(close, 20)
    vol_ma5 = _rolling_mean(vol, 5)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    range_pct = np.where(prev_close > 0, (high - np.array([float(r[3]) if r[3] else 0.0 for r in rows])) / prev_close * 100, 0)
    range_ma20 = _rolling_mean(range_pct, 20)
    ma20_slope = np.zeros(n)
    if n > 10:
        m_prev = ma20[:-10]
        ma20_slope[10:] = np.where(m_prev > 0, (ma20[10:] / m_prev - 1) * 100, 0)
    high_20d = _rolling_max(high, 20)
    close_20d_high = (close >= high_20d * 0.999).astype(int)
    return_3d = np.zeros(n)
    if n > 3:
        return_3d[:-3] = (close[3:] / close[:-3] - 1) * 100
    max_dd_3d = np.zeros(n)
    for i in range(n - 3):
        if close[i] > 0:
            max_dd_3d[i] = close[i+1:i+4].min() / close[i] - 1
    max_dd_3d *= 100

    c1 = (pct >= 3.0) & (pct <= 9.5)
    c2 = np.where(vol_ma5 > 0, vol / vol_ma5, 0) >= 1.8
    c3 = (range_ma20 <= 8.0) & (np.abs(ma20_slope) < 3)
    c4 = close_20d_high == 1
    c5 = return_3d >= 5.0
    c6 = max_dd_3d >= -8.0

    main_wave = (c1 & c2 & c3 & c4 & c5 & c6).astype(int)
    main_wave_relaxed = (c1 & c2 & c4).astype(int)
    trigger = np.where(main_wave == 1, np.where(pct >= 9.5, 'limit_up', 'breakout'), 'none')

    out_tuples = []
    pos_idx = np.where(main_wave == 1)[0]
    for i in pos_idx:
        out_tuples.append((trade_dates[i], ts_code, 1, float(return_3d[i]), 0.0, float(max_dd_3d[i]), trigger[i], industry))
    neg_idx = np.where((main_wave == 0) & (main_wave_relaxed == 0))[0]
    if len(neg_idx) > 0:
        sample_size = min(max(len(pos_idx) * 4, 50), len(neg_idx))
        if sample_size > 0:
            sel = np.random.RandomState(42).choice(neg_idx, size=sample_size, replace=False)
            for i in sel:
                out_tuples.append((trade_dates[i], ts_code, 0, float(return_3d[i]), 0.0, float(max_dd_3d[i]), trigger[i], industry))
    return out_tuples


def main():
    DB = get_db_config()
    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM main_wave_labels")
    conn.commit()
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT ts_code FROM daily_price")
    codes = [r[0] for r in cur.fetchall()]
    logger.info(f"Processing {len(codes)} stocks (v5 robust)...")

    t0 = time.time()
    pos_count = 0
    insert_total = 0
    buffer = []
    last_ping = time.time()

    for i, code in enumerate(codes):
        try:
            # 每 30 秒 ping 一次, 防止连接静默死
            if time.time() - last_ping > 30:
                try:
                    conn.ping(reconnect=True)
                    last_ping = time.time()
                except: pass
            cur.execute("""
                SELECT trade_date, open, high, low, close, vol, amount, pct_chg
                FROM daily_price WHERE ts_code=%s ORDER BY trade_date
            """, (code,))
            rows = cur.fetchall()
            ind = ind_map.get(code, 'OTHER')
            tuples = process(rows, code, ind)
            pos_count += sum(1 for t in tuples if t[2] == 1)
            buffer.extend(tuples)
            if len(buffer) >= CHUNK_INSERT:
                cur.executemany("""INSERT INTO main_wave_labels
                    (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", buffer)
                conn.commit()
                insert_total += len(buffer)
                buffer = []
            if (i+1) % 100 == 0:
                logger.info(f"  {i+1}/{len(codes)} stocks, pos={pos_count}, inserted={insert_total}, elapsed={time.time()-t0:.0f}s")
        except Exception as e:
            logger.error(f"  {code}: {type(e).__name__}: {e}")
            try: conn.rollback()
            except: pass
            try:
                conn.ping(reconnect=True)
                cur = conn.cursor()
            except Exception as e2:
                logger.error(f"  reconnect fail: {e2}")
                # 重连
                conn = pymysql.connect(**DB)
                cur = conn.cursor()
            continue

    if buffer:
        cur.executemany("""INSERT INTO main_wave_labels
            (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", buffer)
        conn.commit()
        insert_total += len(buffer)

    logger.info(f"Done. inserted={insert_total}, pos={pos_count}, total {time.time()-t0:.0f}s")
    conn.close()


if __name__ == '__main__':
    main()
