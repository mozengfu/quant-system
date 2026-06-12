#!/usr/bin/env python3
"""
同步北向资金（north_moneyflow）
数据源: Tushare pro.moneyflow_hsgt
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import logging, time, argparse
from datetime import datetime, timedelta
import pandas as pd, pymysql, tushare as ts
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

CREATE_SQL = """CREATE TABLE IF NOT EXISTS north_moneyflow (
    trade_date DATE NOT NULL PRIMARY KEY,
    north_money DECIMAL(20,2),
    hgt DECIMAL(20,2),
    sgt DECIMAL(20,2)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
INSERT_SQL = "INSERT IGNORE INTO north_moneyflow (trade_date, north_money, hgt, sgt) VALUES (%s,%s,%s,%s)"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', type=str, default='20240101')
    parser.add_argument('--end', type=str, default=None)
    args = parser.parse_args()

    conn = pymysql.connect(**get_db_config())
    with conn.cursor() as cur:
        cur.execute(CREATE_SQL)
        conn.commit()
    pro = ts.pro_api()

    start = datetime.strptime(args.start, '%Y%m%d')
    end = datetime.strptime(args.end, '%Y%m%d') if args.end else datetime.now()

    total = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            trade_str = d.strftime('%Y%m%d')
            try:
                df = pro.moneyflow_hsgt(trade_date=trade_str)
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    with conn.cursor() as cur:
                        cur.execute(INSERT_SQL, (d,
                            float(row.get('north_money', 0)),
                            float(row.get('hgt', 0)),
                            float(row.get('sgt', 0))))
                        conn.commit()
                    total += 1
            except Exception as e:
                logger.warning(f"  {trade_str}: {e}")
            time.sleep(0.5)
        d += timedelta(days=1)

    conn.close()
    logger.info(f"完成! 共 {total} 条")

if __name__ == '__main__':
    main()
