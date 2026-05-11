#!/usr/bin/env python3
"""
融资融券数据回填 — 从 Tushare Pro 拉取 margin_detail 写入 MySQL
创建 margin_daily 表，包含每只股票每日的融资融券余额和交易数据

用法: python3 scripts/backfill_margin.py
"""

import os, sys, time, logging
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config
from quant_app.services.market_service import get_tushare_pro

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def ensure_table(conn):
    """创建 margin_daily 表"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS margin_daily (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(12) NOT NULL,
            trade_date DATE NOT NULL,
            rzye DECIMAL(15,2) COMMENT '融资余额',
            rqye DECIMAL(15,2) COMMENT '融券余额',
            rzmre DECIMAL(15,2) COMMENT '融资买入额',
            rqyl DECIMAL(15,2) COMMENT '融券余量',
            rqmcl DECIMAL(15,2) COMMENT '融券卖出量',
            UNIQUE KEY uk_code_date (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    logger.info("margin_daily 表已就绪")


def get_latest_date(conn):
    """获取已有数据的最大交易日期"""
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM margin_daily")
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_stock_codes(conn):
    """获取所有需要拉取融资融券数据的股票代码"""
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
            df = pro.margin_detail(ts_code=ts_code, start_date=start_date)
            if df is not None and len(df) > 0:
                for _, row in df.iterrows():
                    trade_date = row.get('trade_date', '')
                    batch.append((
                        ts_code,
                        trade_date,
                        float(row['rzye']) if pd.notna(row.get('rzye')) else None,
                        float(row['rqye']) if pd.notna(row.get('rqye')) else None,
                        float(row['rzmre']) if pd.notna(row.get('rzmre')) else None,
                        float(row['rqyl']) if pd.notna(row.get('rqyl')) else None,
                        float(row['rqmcl']) if pd.notna(row.get('rqmcl')) else None,
                    ))
        except Exception as e:
            logger.warning(f"{ts_code} 拉取失败: {e}")

        # 每 500 条批量写入
        if len(batch) >= 500:
            try:
                cursor.executemany(
                    "INSERT IGNORE INTO margin_daily (ts_code, trade_date, rzye, rqye, rzmre, rqyl, rqmcl) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    batch
                )
                conn.commit()
                total_inserted += len(batch)
            except Exception as e:
                logger.warning(f"批量写入失败: {e}")
                conn.rollback()
            batch = []

        if (i + 1) % 100 == 0:
            logger.info(f"进度: {i+1}/{len(codes)} 只股票, 已插入 {total_inserted} 条")
            time.sleep(0.3)  # Tushare 频率限制

    # 剩余批次
    if batch:
        try:
            cursor.executemany(
                "INSERT IGNORE INTO margin_daily (ts_code, trade_date, rzye, rqye, rzmre, rqyl, rqmcl) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                batch
            )
            conn.commit()
            total_inserted += len(batch)
        except Exception as e:
            logger.warning(f"批量写入失败: {e}")
            conn.rollback()

    cursor.close()
    conn.close()
    logger.info(f"完成! 共插入 {total_inserted} 条融资融券记录")


if __name__ == '__main__':
    backfill()
