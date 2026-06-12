#!/usr/bin/env python3
"""预计算 V11 预测, 写入 MySQL 表, 后续用 join 取"""
import logging
import os
import sys
import time

import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS v11_predictions (
    trade_date DATE NOT NULL,
    ts_code VARCHAR(20) NOT NULL,
    v11_prob DECIMAL(8,4),
    v11_pred_ret DECIMAL(8,4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

def main():
    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()
    logger.info("Table v11_predictions ready")

    from ml_predict import predict_batch
    # 拉交易日
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date BETWEEN '2025-12-01' AND '2026-06-09' ORDER BY trade_date")
    all_dates = [r[0] for r in cur.fetchall()]
    logger.info(f"{len(all_dates)} trading days")

    # 看已有
    cur.execute("SELECT COUNT(DISTINCT trade_date) FROM v11_predictions WHERE trade_date BETWEEN '2025-12-01' AND '2026-06-09'")
    done = cur.fetchone()[0]
    logger.info(f"  Already done: {done} dates")

    # 所有股票
    cur.execute("SELECT DISTINCT ts_code FROM stock_info WHERE SUBSTRING(ts_code,1,2) IN ('60','00','30','68')")
    all_codes = [r[0] for r in cur.fetchall()]
    logger.info(f"  {len(all_codes)} stocks")

    todo = [d for d in all_dates if d >= pd.Timestamp('2025-12-01').date()]
    logger.info(f"Computing V11 for {len(todo)} dates × {len(all_codes)} stocks...")

    t0 = time.time()
    total = 0
    for i, d in enumerate(todo):
        try:
            preds = predict_batch(all_codes, db_conn=conn, as_of_date=d.strftime('%Y%m%d'))
            if not preds: continue
            rows = []
            for code, p in preds.items():
                rows.append((d, code, float(p.get('probability', 0.5)), float(p.get('predicted_return', 0))))
            # 批量插入
            cur.executemany("""INSERT IGNORE INTO v11_predictions
                (trade_date, ts_code, v11_prob, v11_pred_ret)
                VALUES (%s, %s, %s, %s)""", rows)
            conn.commit()
            total += len(rows)
            if (i+1) % 20 == 0:
                rate = (i+1) / (time.time() - t0)
                eta = (len(todo) - i - 1) / rate / 60
                logger.info(f"  {i+1}/{len(todo)} dates, {total} rows, {rate:.1f} dates/s, ETA {eta:.0f} min")
        except Exception as e:
            logger.error(f"  {d}: {e}")
            continue
    logger.info(f"Done. {total} rows, {(time.time()-t0)/60:.0f} min")
    conn.close()

if __name__ == '__main__':
    main()
