#!/usr/bin/env python3
"""
主升浪标签 - 流式写库版
- 每只股票处理完立刻写 MySQL, 不在内存累积
- 每 500 只股强制 flush + 打印进度
- 全部完成后单独跑一次 parquet 导出
"""
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

OUTPUT_PARQUET = Path(__file__).parent.parent / "data" / "main_wave_labels.parquet"
CHUNK_SIZE = 200  # 每 200 只股写一次 MySQL


def stream_one_stock(cur, ts_code):
    cur.execute("""
        SELECT trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_price
        WHERE ts_code = %s ORDER BY trade_date
    """, (ts_code,))
    rows = cur.fetchall()
    if len(rows) < 60:
        return None
    rows_f = []
    for r in rows:
        rows_f.append((r[0],
                       float(r[1]) if r[1] else 0.0,
                       float(r[2]) if r[2] else 0.0,
                       float(r[3]) if r[3] else 0.0,
                       float(r[4]) if r[4] else 0.0,
                       float(r[5]) if r[5] else 0.0,
                       float(r[6]) if r[6] else 0.0,
                       float(r[7]) if r[7] else 0.0))
    return rows_f


def build_labels(rows):
    """输入 rows 列表, 返回 labels DataFrame (可能为空)"""
    if not rows: return None
    g = pd.DataFrame(rows, columns=['trade_date','open','high','low','close','vol','amount','pct_chg'])
    g = g.sort_values('trade_date').reset_index(drop=True)
    n = len(g)
    if n < 60: return None
    close = g['close'].values
    high = g['high'].values
    low = g['low'].values
    vol = g['vol'].values
    pct = g['pct_chg'].values
    # 跳过 close 全 0 或有负值
    if np.any(close <= 0): return None

    ma20 = pd.Series(close).rolling(20).mean().values
    vol_ma5 = pd.Series(vol).rolling(5).mean().values
    prev_close = np.concatenate([[close[0]], close[:-1]])
    range_pct = np.where(prev_close > 0, (high - low) / prev_close * 100, 0)
    range_ma20 = pd.Series(range_pct).rolling(20).mean().values
    ma20_slope = np.zeros(n)
    ma20_slope[10:] = np.where(ma20[:-10] > 0, (ma20[10:] / ma20[:-10] - 1) * 100, 0)
    high_20d = pd.Series(high).rolling(20).max().values
    close_20d_high = (close >= high_20d * 0.999).astype(int)
    close_fwd_3d = np.roll(close, -3)
    return_3d = np.where(close > 0, (close_fwd_3d / close - 1) * 100, 0)
    return_3d[-3:] = 0
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

    return pd.DataFrame({
        'trade_date': g['trade_date'].values,
        'pct_chg': pct,
        'vol': vol,
        'vol_ma5': vol_ma5,
        'range_ma20': range_ma20,
        'ma20_slope': ma20_slope,
        'close_20d_high': close_20d_high,
        'return_3d': return_3d,
        'return_5d': 0.0,
        'max_dd_3d': max_dd_3d,
        'main_wave': main_wave,
        'main_wave_relaxed': main_wave_relaxed,
        'trigger_type': np.where(main_wave==1, np.where(pct>=9.5, 'limit_up', 'breakout'), 'none'),
        'label': main_wave,
    })


def main():
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()

    # 清表 + 准备 industry
    cur.execute("DELETE FROM main_wave_labels")
    conn.commit()
    cur.execute("SELECT ts_code, industry FROM stock_info")
    ind_map = {r[0]: r[1] for r in cur.fetchall()}
    logger.info(f"Industry map: {len(ind_map)} entries")

    cur.execute("SELECT DISTINCT ts_code FROM daily_price")
    codes = [r[0] for r in cur.fetchall()]
    logger.info(f"Processing {len(codes)} stocks (streaming to MySQL)...")

    t0 = time.time()
    pos_count = 0
    rows_buffer = []
    insert_total = 0
    BATCH = 1000

    for i, code in enumerate(codes):
        try:
            rows = stream_one_stock(cur, code)
            if rows is None: continue
            labels = build_labels(rows)
            if labels is None or labels.empty: continue
            pos_count += int(labels['label'].sum())
            ind = ind_map.get(code, 'OTHER')
            # 准备行
            for _, r in labels.iterrows():
                rows_buffer.append((
                    r['trade_date'], code, int(r['label']),
                    float(r['return_3d']), 0.0, float(r['max_dd_3d']),
                    r['trigger_type'], ind,
                ))
            if len(rows_buffer) >= BATCH:
                cur.executemany("""INSERT INTO main_wave_labels
                    (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows_buffer)
                conn.commit()
                insert_total += len(rows_buffer)
                rows_buffer = []
            if (i+1) % 200 == 0:
                logger.info(f"  {i+1}/{len(codes)} stocks, pos={pos_count}, "
                           f"inserted={insert_total}, "
                           f"elapsed={time.time()-t0:.0f}s")
        except Exception as e:
            logger.error(f"  {code}: {type(e).__name__}: {e}")
            continue

    if rows_buffer:
        cur.executemany("""INSERT INTO main_wave_labels
            (trade_date,ts_code,label,return_3d,return_5d,max_drawdown_3d,trigger_type,industry)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows_buffer)
        conn.commit()
        insert_total += len(rows_buffer)

    logger.info(f"Done. {insert_total} rows inserted, {pos_count} positive. Total {time.time()-t0:.0f}s")
    conn.close()


if __name__ == '__main__':
    main()
