#!/usr/bin/env python3
"""
补全2024年历史数据 - 从Tushare下载并写入MySQL
按交易日批量下载，效率高（~242次API调用）
"""

import os
import sys
import time
import pymysql
import tushare as ts
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')
ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

DB_CONFIG = {
    'host': 'localhost',
    'unix_socket': '/tmp/mysql.sock',
    'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db',
    'connect_timeout': 5,
}

def get_conn():
    return pymysql.connect(**DB_CONFIG, autocommit=True)


def get_trade_dates_2024():
    """获取2024年所有交易日"""
    print("获取2024年交易日...")
    all_dates = []
    for m in range(1, 13):
        start = f"2024{m:02d}01"
        end = f"2024{m:02d}28"
        try:
            df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
            if df is not None and not df.empty:
                trading = df[df['is_open'] == 1]
                all_dates.extend(trading['cal_date'].tolist())
        except Exception as e:
            print(f"  月{m} 失败: {e}")
        time.sleep(0.1)
    dates = sorted(set(all_dates))
    print(f"共 {len(dates)} 个交易日")
    return dates


def backfill_daily_by_date(trade_dates):
    """按交易日批量下载daily_price"""
    print(f"\n=== 回填 daily_price (按日期批量) ===")
    
    conn = get_conn()
    cur = conn.cursor()
    total_rows = 0
    errors = 0
    
    for i, td in enumerate(trade_dates):
        try:
            df = pro.daily(trade_date=td)
            if df is None or df.empty:
                print(f"  {td}: 无数据")
                continue
            
            # 过滤主板+创业板
            df = df[~df['ts_code'].str.startswith(('688', '689', '8', '4', '9', '16'))]
            
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    r['ts_code'], r['trade_date'],
                    float(r.get('open', 0)), float(r.get('high', 0)),
                    float(r.get('low', 0)), float(r.get('close', 0)),
                    float(r.get('pre_close', 0)),
                    float(r.get('vol', 0)), float(r.get('amount', 0)),
                    float(r.get('pct_chg', 0))
                ))
            
            if rows:
                cur.executemany(
                    """INSERT IGNORE INTO daily_price 
                       (ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_chg)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows
                )
                total_rows += len(rows)
        except Exception as e:
            errors += 1
            print(f"  {td} 失败: {e}")
            time.sleep(2)
            continue
        
        if (i + 1) % 20 == 0:
            pct = (i + 1) / len(trade_dates) * 100
            print(f"  进度: {i+1}/{len(trade_dates)} ({pct:.0f}%), 已写入 {total_rows:,} 行")
            conn.commit()
        
        time.sleep(0.15)  # 控制频率
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"daily_price 完成: {total_rows:,} 行, 失败 {errors} 天")


def backfill_daily_basic_by_date(trade_dates):
    """按交易日批量下载daily_basic"""
    print(f"\n=== 回填 daily_basic (pe/pb/turnover_rate) ===")
    
    conn = get_conn()
    cur = conn.cursor()
    total_rows = 0
    errors = 0
    
    for i, td in enumerate(trade_dates):
        try:
            df = pro.daily_basic(trade_date=td,
                                 fields='ts_code,trade_date,pe,pb,turnover_rate')
            if df is None or df.empty:
                continue
            
            # 过滤
            df = df[~df['ts_code'].str.startswith(('688', '689', '8', '4', '9', '16'))]
            
            if df.empty:
                continue
            
            updates = []
            for _, r in df.iterrows():
                pe = r.get('pe')
                pb = r.get('pb')
                tr = r.get('turnover_rate')
                if pe is not None and (pe == 0 or str(pe) == 'nan'):
                    pe = None
                if pb is not None and str(pb) == 'nan':
                    pb = None
                if tr is not None and str(tr) == 'nan':
                    tr = None
                td_val = r['trade_date']
                if '-' not in str(td_val):
                    td_val = f"{str(td_val)[:4]}-{str(td_val)[4:6]}-{str(td_val)[6:8]}"
                updates.append((pe, pb, tr, r['ts_code'], td_val))
            
            if updates:
                cur.executemany(
                    """UPDATE daily_price SET pe=%s, pb=%s, turnover_rate=%s
                       WHERE ts_code=%s AND trade_date=%s""", updates
                )
                total_rows += len(updates)
        except Exception as e:
            errors += 1
            print(f"  {td} 失败: {e}")
            time.sleep(2)
            continue
        
        if (i + 1) % 30 == 0:
            pct = (i + 1) / len(trade_dates) * 100
            print(f"  进度: {i+1}/{len(trade_dates)} ({pct:.0f}%), 已更新 {total_rows:,} 行")
            conn.commit()
        
        time.sleep(0.2)
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"daily_basic 完成: 更新 {total_rows:,} 行, 失败 {errors}")


def backfill_index(trade_dates):
    """补全2024年指数"""
    print(f"\n=== 回填 market_index_daily ===")
    
    indices = [
        ('000001.SH', '上证指数'),
        ('399001.SZ', '深证成指'),
        ('399006.SZ', '创业板指'),
        ('000300.SH', '沪深300'),
        ('000016.SH', '上证50'),
    ]
    
    conn = get_conn()
    cur = conn.cursor()
    
    for ts_code, name in indices:
        try:
            df = pro.index_daily(ts_code=ts_code,
                                 start_date='20240101', end_date='20241231')
            if df is None or df.empty:
                print(f"  {name}: 无数据")
                continue
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    ts_code, r['trade_date'],
                    float(r['open']), float(r['high']),
                    float(r['low']), float(r['close']),
                    float(r['vol']), float(r['pct_chg'])
                ))
            cur.executemany(
                """INSERT IGNORE INTO market_index_daily 
                   (ts_code,trade_date,open,high,low,close,vol,pct_chg)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows
            )
            print(f"  {name}: {len(rows)} 条")
        except Exception as e:
            print(f"  {name} 失败: {e}")
        time.sleep(0.3)
    
    cur.close()
    conn.close()


def print_stats():
    print("\n=== 数据统计 ===")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM daily_price")
    r = cur.fetchone()
    print(f"daily_price: {r[0]} ~ {r[1]}, 共 {r[2]:,} 行")
    cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_price")
    print(f"  股票数: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_price")
    print(f"  交易日数: {cur.fetchone()[0]}")
    # 2024年数据
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date < '2025-01-01'")
    print(f"  2024年数据: {cur.fetchone()[0]:,} 行")
    cur.close()
    conn.close()


if __name__ == '__main__':
    print(f"开始下载2024年数据...")
    print(f"Token: {TUSHARE_TOKEN[:10]}...")
    
    trade_dates = get_trade_dates_2024()
    if not trade_dates:
        print("获取交易日失败，退出")
        sys.exit(1)
    
    backfill_daily_by_date(trade_dates)
    backfill_daily_basic_by_date(trade_dates)
    backfill_index(trade_dates)
    print_stats()
    print("\n全部完成！")
