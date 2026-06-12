#!/usr/bin/env python3
"""
主升浪标签 v4 - 全 numpy 数组, 避免 Python 对象开销
- 直接操作 numpy 数组, 不用 pandas
- 每只股处理完直接生成 SQL rows, 不进 buffer (flush by 100 行)
"""
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

CHUNK_INSERT = 200  # 每 200 行写一次


def _rolling_mean(arr, w):
    """向量化 rolling mean, 长度不变, 前 w-1 为 0"""
    n = len(arr)
    out = np.zeros(n)
    cumsum = np.cumsum(np.concatenate([[0], arr]))
    out[w-1:] = (cumsum[w:] - cumsum[:-w]) / w
    return out


def _rolling_max(arr, w):
    """向量化 rolling max (慢但可用)"""
    n = len(arr)
    out = np.zeros(n)
    for i in range(w-1, n):
        out[i] = arr[i-w+1:i+1].max()
    return out


def process_one_stock(rows, ts_code, industry):
    """输入: rows 列表 (date, open, high, low, close, vol, amount, pct_chg)
       输出: SQL tuples 列表"""
    if len(rows) < 60:
        return []
    n = len(rows)
    # 全转 float
    trade_dates = [r[0] for r in rows]
    close = np.array([float(r[4]) if r[4] else 0.0 for r in rows])
    high = np.array([float(r[2]) if r[2] else 0.0 for r in rows])
    low = np.array([float(r[3]) if r[3] else 0.0 for r in rows])
    vol = np.array([float(r[5]) if r[5] else 0.0 for r in rows])
    pct = np.array([float(r[7]) if r[7] else 0.0 for r in rows])
    if np.any(close <= 0):
        return []

    ma20 = _rolling_mean(close, 20)
    vol_ma5 = _rolling_mean(vol, 5)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    range_pct = np.where(prev_close > 0, (high - low) / prev_close * 100, 0)
    range_ma20 = _rolling_mean(range_pct, 20)
    # ma20_slope: (ma20[i] - ma20[i-10]) / ma20[i-10]
    ma20_slope = np.zeros(n)
    if n > 10:
        m_curr = ma20[10:]
        m_prev = ma20[:-10]
        ma20_slope[10:] = np.where(m_prev > 0, (m_curr / m_prev - 1) * 100, 0)
    high_20d = _rolling_max(high, 20)
    close_20d_high = (close >= high_20d * 0.999).astype(int)
    # return_3d
    return_3d = np.zeros(n)
    if n > 3:
        return_3d[:-3] = (close[3:] / close[:-3] - 1) * 100
    # max_dd_3d
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
    trigger = np.where(main_wave == 1,
                       np.where(pct >= 9.5, 'limit_up', 'breakout'),
                       'none')

    # 生成 SQL tuples (主升浪 + 宽松主升候选作为负样本)
    out_tuples = []
    pos_idx = np.where(main_wave == 1)[0]
    for i in pos_idx:
        out_tuples.append((
            trade_dates[i], ts_code, 1,
            float(return_3d[i]), 0.0, float(max_dd_3d[i]),
            trigger[i], industry,
        ))
    # 负样本: 严格非主升且非主升候选
    neg_idx = np.where((main_wave == 0) & (main_wave_relaxed == 0))[0]
    if len(neg_idx) > 0:
        # 下采样 4:1
        sample_size = min(len(pos_idx) * 4 if len(pos_idx) > 0 else 100, len(neg_idx))
        if sample_size > 0:
            sel = np.random.RandomState(42).choice(neg_idx, size=sample_size, replace=False)
            for i in sel:
                out_tuples.append((
                    trade_dates[i], ts_code, 0,
                    float(return_3d[i]), 0.0, float(max_dd_3d[i]),
                    trigger[i], industry,
                ))
    return out_tuples


def main():
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute("DELETE FROM main_wave_labels")
    conn.commit()
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT DISTINCT ts_code FROM daily_price")
    codes = [r[0] for r in cur.fetchall()]
    logger.info(f"Processing {len(codes)} stocks (v4 full numpy)...")

    t0 = time.time()
    pos_count = 0
    insert_total = 0
    buffer = []

    for i, code in enumerate(codes):
        try:
            cur.execute("""
                SELECT trade_date, open, high, low, close, vol, amount, pct_chg
                FROM daily_price WHERE ts_code=%s ORDER BY trade_date
            """, (code,))
            rows = cur.fetchall()
            ind = ind_map.get(code, 'OTHER')
            tuples = process_one_stock(rows, code, ind)
            pos_count += sum(1 for t in tuples if t[2] == 1)
            buffer.extend(tuples)
            if len(buffer) >= CHUNK_INSERT:
                cur.executemany("""INSERT INTO main_wave_labels
                    (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", buffer)
                conn.commit()
                insert_total += len(buffer)
                buffer = []
            if (i+1) % 200 == 0:
                logger.info(f"  {i+1}/{len(codes)} stocks, pos={pos_count}, "
                           f"inserted={insert_total}, "
                           f"elapsed={time.time()-t0:.0f}s, "
                           f"buffer={len(buffer)}")
        except Exception as e:
            logger.error(f"  {code}: {type(e).__name__}: {e}")
            try:
                conn.rollback()
            except: pass
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
