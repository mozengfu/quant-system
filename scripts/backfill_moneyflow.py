#!/usr/bin/env python3
"""
资金流向数据回填 — 从 Tushare Pro 拉取 moneyflow 写入 MySQL
补 moneyflow_daily 表 (5/28 之后 6 周缺口)

用法: python3 scripts/backfill_moneyflow.py
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import pymysql

from quant_app.services.market_service import get_tushare_pro
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS moneyflow_daily (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(12) NOT NULL,
            trade_date DATE NOT NULL,
            buy_sm_amount DECIMAL(15,2) DEFAULT 0,
            sell_sm_amount DECIMAL(15,2) DEFAULT 0,
            buy_md_amount DECIMAL(15,2) DEFAULT 0,
            sell_md_amount DECIMAL(15,2) DEFAULT 0,
            buy_lg_amount DECIMAL(15,2) DEFAULT 0,
            sell_lg_amount DECIMAL(15,2) DEFAULT 0,
            buy_elg_amount DECIMAL(15,2) DEFAULT 0,
            sell_elg_amount DECIMAL(15,2) DEFAULT 0,
            net_mf_amount DECIMAL(15,2) DEFAULT 0,
            main_net DECIMAL(15,2) DEFAULT 0,
            UNIQUE KEY uk_code_date (ts_code, trade_date),
            KEY idx_date (trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    logger.info("moneyflow_daily 表已就绪")


def get_latest_date(conn):
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM moneyflow_daily")
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_stock_codes(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ts_code FROM daily_price
        WHERE ts_code NOT LIKE '688.%' AND ts_code NOT LIKE '8%'
          AND ts_code NOT LIKE '4%' AND ts_code NOT LIKE '9%'
    """)
    return [r[0] for r in cur.fetchall()]


def backfill():
    conn = pymysql.connect(**get_db_config())
    ensure_table(conn)

    pro = get_tushare_pro()
    latest = get_latest_date(conn)
    if latest:
        start_date = (latest + timedelta(days=1)).strftime('%Y%m%d')
        logger.info(f"已有数据至 {latest}，从 {start_date} 增量更新")
    else:
        start_date = '20240101'
        logger.info(f"无历史数据，从 {start_date} 开始回填")

    codes = get_stock_codes(conn)
    logger.info(f"共 {len(codes)} 只股票需拉取")

    total_inserted = 0
    batch = []
    cursor = conn.cursor()

    for i, ts_code in enumerate(codes):
        try:
            df = pro.moneyflow(ts_code=ts_code, start_date=start_date)
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    # main_net = 大单 + 特大单的净额
                    buy_lg = float(row.get('buy_lg_amount') or 0)
                    sell_lg = float(row.get('sell_lg_amount') or 0)
                    buy_elg = float(row.get('buy_elg_amount') or 0)
                    sell_elg = float(row.get('sell_elg_amount') or 0)
                    main_net = (buy_lg + buy_elg) - (sell_lg + sell_elg)
                    batch.append((
                        ts_code,
                        str(row.get('trade_date', '')),
                        float(row.get('buy_sm_amount') or 0),
                        float(row.get('sell_sm_amount') or 0),
                        float(row.get('buy_md_amount') or 0),
                        float(row.get('sell_md_amount') or 0),
                        buy_lg, sell_lg, buy_elg, sell_elg,
                        float(row.get('net_mf_amount') or 0),
                        main_net,
                    ))
        except Exception as e:
            logger.warning(f"{ts_code} 拉取失败: {e}")

        if len(batch) >= 500:
            try:
                cursor.executemany(
                    "INSERT IGNORE INTO moneyflow_daily "
                    "(ts_code, trade_date, buy_sm_amount, sell_sm_amount, buy_md_amount, sell_md_amount, "
                    " buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount, net_mf_amount, main_net) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    batch)
                conn.commit()
                total_inserted += len(batch)
            except Exception as e:
                logger.warning(f"批量写入失败: {e}")
                conn.rollback()
            batch = []

        if (i + 1) % 100 == 0:
            logger.info(f"进度: {i+1}/{len(codes)} 只股票, 已插入 {total_inserted} 条")
            time.sleep(0.3)

    if batch:
        try:
            cursor.executemany(
                "INSERT IGNORE INTO moneyflow_daily "
                "(ts_code, trade_date, buy_sm_amount, sell_sm_amount, buy_md_amount, sell_md_amount, "
                " buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount, net_mf_amount, main_net) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                batch)
            conn.commit()
            total_inserted += len(batch)
        except Exception as e:
            logger.warning(f"批量写入失败: {e}")
            conn.rollback()

    cursor.close()
    conn.close()
    logger.info(f"完成! 共插入 {total_inserted} 条资金流向记录")


if __name__ == '__main__':
    backfill()
