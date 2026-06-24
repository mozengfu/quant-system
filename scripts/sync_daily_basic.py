#!/usr/bin/env python3
"""
同步每日基本面数据（daily_basic）— PE/PB/市值/换手率

数据源: Tushare pro.daily_basic
输出表: daily_basic (ts_code, trade_date, pe, pb, total_mv, circ_mv, ...)

用法:
  python3 scripts/sync_daily_basic.py                    # 最近交易日增量
  python3 scripts/sync_daily_basic.py --start 20250101   # 历史回填
  python3 scripts/sync_daily_basic.py --start 20250101 --end 20250601  # 指定区间
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd
import pymysql
import tushare as ts
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code VARCHAR(20) NOT NULL,
    trade_date DATE NOT NULL,
    pe DECIMAL(12,4),
    pe_ttm DECIMAL(12,4),
    pb DECIMAL(12,4),
    total_mv DECIMAL(20,2),
    circ_mv DECIMAL(20,2),
    total_share DECIMAL(20,2),
    float_share DECIMAL(20,2),
    turnover_rate DECIMAL(10,4),
    volume_ratio DECIMAL(10,4),
    PRIMARY KEY (ts_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

INSERT_SQL = """INSERT IGNORE INTO daily_basic
(ts_code, trade_date, pe, pe_ttm, pb, total_mv, circ_mv, total_share, float_share, turnover_rate, volume_ratio)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def ensure_table():
    conn = pymysql.connect(**get_db_config())
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        conn.commit()
    conn.close()


def get_existing_dates(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_basic ORDER BY trade_date")
    return set(r[0] for r in cur.fetchall())


def sync_date(pro, conn, trade_date):
    """同步单个交易日"""
    trade_str = trade_date.strftime('%Y%m%d')
    for retry in range(3):
        try:
            df = pro.daily_basic(trade_date=trade_str)
            break
        except Exception as e:
            logger.warning(f"  Tushare重试 {trade_str}: {e}")
            time.sleep(2)
            df = None
    if df is None or df.empty:
        return 0

    rows = []
    for _, r in df.iterrows():
        rows.append((
            r.get('ts_code'), trade_date,
            to_float(r.get('pe')), to_float(r.get('pe_ttm')),
            to_float(r.get('pb')), to_float(r.get('total_mv')),
            to_float(r.get('circ_mv')), to_float(r.get('total_share')),
            to_float(r.get('float_share')), to_float(r.get('turnover_rate')),
            to_float(r.get('volume_ratio')),
        ))

    with conn.cursor() as cur:
        cur.executemany(INSERT_SQL, rows)
        conn.commit()
    return len(rows)


def to_float(x):
    if x is None or (isinstance(x, float) and (pd.isna(x))):
        return None
    try:
        return float(x)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description='同步 daily_basic (PE/PB/市值)')
    parser.add_argument('--start', type=str, default=None, help='开始日期 YYYYMMDD')
    parser.add_argument('--end', type=str, default=None, help='结束日期 YYYYMMDD')
    args = parser.parse_args()

    ensure_table()
    pro = ts.pro_api()
    conn = pymysql.connect(**get_db_config())

    existing = get_existing_dates(conn)
    logger.info(f"已有 {len(existing)} 个交易日数据")

    if args.start:
        start = datetime.strptime(args.start, '%Y%m%d')
        end = datetime.strptime(args.end, '%Y%m%d') if args.end else datetime.now()
    else:
        # 自动取最近未同步的日期
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        max_d = cur.fetchone()[0]
        cur.close()
        start = max_d - timedelta(days=10) if max_d else datetime.now() - timedelta(days=10)
        end = max_d if max_d else datetime.now()

    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # 工作日
            if current not in existing:
                n = sync_date(pro, conn, current)
                if n > 0:
                    total += n
                    logger.info(f"  {current}: {n} 条")
                time.sleep(0.5)  # Tushare 限频
        current += timedelta(days=1)

    conn.close()
    logger.info(f"完成! 共新增 {total} 条")


if __name__ == '__main__':
    main()
