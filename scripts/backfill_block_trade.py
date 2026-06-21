#!/usr/bin/env python3
"""
大宗交易数据回填 — 从 Tushare Pro 按日期拉 block_trade, 算 premium_rate, 写入 MySQL
补 block_trade 表 (股票数据, 排除 ETF)

用法: python3 scripts/backfill_block_trade.py
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import pymysql

from quant_app.services.market_service import get_tushare_pro
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def get_latest_date(conn):
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM block_trade")
    row = cur.fetchone()
    return row[0] if row and row[0] else None


def get_trade_dates(conn, start_date, end_date):
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start_date, end_date))
    return [r[0] for r in cur.fetchall()]


def backfill():
    conn = pymysql.connect(**get_db_config())

    pro = get_tushare_pro()
    latest = get_latest_date(conn)
    if latest:
        start_date = latest + timedelta(days=1)
        logger.info(f"已有数据至 {latest}, 从 {start_date} 增量更新")
    else:
        start_date = datetime(2024, 1, 1).date()
        logger.info(f"无历史数据, 从 {start_date} 开始回填")

    end_date = datetime.now().date()
    trade_dates = get_trade_dates(conn, start_date, end_date)
    if not trade_dates:
        logger.info("没有需要更新的日期")
        return
    logger.info(f"共 {len(trade_dates)} 个交易日需要拉取")

    total_inserted = 0
    cursor = conn.cursor()

    for i, td in enumerate(trade_dates):
        td_str = td.strftime('%Y%m%d')
        try:
            df = pro.block_trade(trade_date=td_str)
            if df is None or len(df) == 0:
                continue

            # 只取股票 (排除 ETF 1x/15x 开头)
            df = df[~df['ts_code'].str.startswith(('1', '5'))].copy()
            if len(df) == 0:
                continue

            # 拉 daily_price 的 close / pct_change, 算 premium_rate
            codes_in_batch = df['ts_code'].unique().tolist()
            ph = ','.join(['%s'] * len(codes_in_batch))
            cursor.execute(f"""
                SELECT ts_code, close, pct_chg FROM daily_price
                WHERE trade_date = %s AND ts_code IN ({ph})
            """, (td_str, *codes_in_batch))
            dp_map = {r[0]: (float(r[1] or 0), float(r[2] or 0)) for r in cursor.fetchall()}

            # 聚合: 同一只股票同一天可能有多笔交易 (不同 buyer/seller), 合并成一行
            df['_price'] = df['price'].astype(float)
            df['_vol'] = df['vol'].astype(float)
            df['_amount'] = df['amount'].astype(float)
            agg = df.groupby('ts_code').agg(
                price=('_price', 'mean'),       # 加权平均价 (按 vol 加权更准, 但 vol 单位不一致, 简化为算术平均)
                vol=('_vol', 'sum'),
                amount=('_amount', 'sum'),
                buyer=('buyer', 'first'),
                seller=('seller', 'first'),
            ).reset_index()

            batch = []
            for _, row in agg.iterrows():
                tc = row['ts_code']
                price = float(row.get('price') or 0)
                vol_w = float(row.get('vol') or 0)  # 万股
                amount_w = float(row.get('amount') or 0)  # 万元
                close, pct_chg = dp_map.get(tc, (0, 0))
                pre_close = close / (1 + pct_chg / 100) if pct_chg != 0 and close > 0 else close
                premium_rate = ((price - pre_close) / pre_close * 100) if pre_close > 0 else 0.0
                deal_amount = amount_w * 10000  # 元
                batch.append((
                    td, tc, '',  # name
                    close, pct_chg,
                    price, int(vol_w * 10000) if vol_w > 0 else 0,
                    deal_amount, premium_rate,
                    str(row.get('buyer', ''))[:100],
                    str(row.get('seller', ''))[:100],
                ))

            try:
                cursor.executemany(
                    "INSERT IGNORE INTO block_trade "
                    "(trade_date, ts_code, name, close, pct_change, "
                    " deal_price, deal_volume, deal_amount, premium_rate, buyer, seller) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    batch)
                conn.commit()
                total_inserted += len(batch)
            except Exception as e:
                logger.warning(f"{td_str} 写入失败: {e}")
                conn.rollback()

        except Exception as e:
            logger.warning(f"{td_str} 拉取失败: {e}")

        if (i + 1) % 5 == 0:
            logger.info(f"进度: {i+1}/{len(trade_dates)} 交易日, 已插入 {total_inserted} 条")
        time.sleep(0.3)

    cursor.close()
    conn.close()
    logger.info(f"完成! 共插入 {total_inserted} 条大宗交易记录")


if __name__ == '__main__':
    backfill()
