#!/usr/bin/env python3
"""
补全2024年资金流数据 + 指数数据 + 优化ML训练
"""

import os
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
    dates = []
    for m in range(1, 13):
        start = f"2024{m:02d}01"
        end = f"2024{m:02d}28"
        try:
            df = pro.trade_cal(exchange='SSE', start_date=start, end_date=end)
            if df is not None and not df.empty:
                trading = df[df['is_open'] == 1]
                dates.extend(trading['cal_date'].tolist())
        except Exception as _e:
            print(f"Error in backfill_2024_moneyflow.py: {_e}")
        time.sleep(0.1)
    return sorted(set(dates))


def backfill_moneyflow_2024(trade_dates):
    """按交易日批量下载2024年资金流数据"""
    print(f"\n=== 回填 moneyflow_daily ({len(trade_dates)} 天) ===")
    
    conn = get_conn()
    cur = conn.cursor()
    total_rows = 0
    
    for i, td in enumerate(trade_dates):
        try:
            df = pro.moneyflow(trade_date=td)
            if df is None or df.empty:
                continue
            
            # 过滤
            df = df[~df['ts_code'].str.startswith(('688', '689', '8', '4', '9', '16'))]
            
            rows = []
            for _, r in df.iterrows():
                buy_sm = float(r.get('buy_sm_amount', 0) or 0)
                sell_sm = float(r.get('sell_sm_amount', 0) or 0)
                buy_md = float(r.get('buy_md_amount', 0) or 0)
                sell_md = float(r.get('sell_md_amount', 0) or 0)
                buy_lg = float(r.get('buy_lg_amount', 0) or 0)
                sell_lg = float(r.get('sell_lg_amount', 0) or 0)
                buy_elg = float(r.get('buy_elg_amount', 0) or 0)
                sell_elg = float(r.get('sell_elg_amount', 0) or 0)
                net_mf = float(r.get('net_mf_amount', 0) or 0)
                # 主力=大单+特大单
                main_net = (buy_lg - sell_lg) + (buy_elg - sell_elg)
                
                rows.append((
                    r['ts_code'], r['trade_date'],
                    buy_sm, sell_sm, buy_md, sell_md,
                    buy_lg, sell_lg, buy_elg, sell_elg,
                    net_mf, main_net
                ))
            
            if rows:
                cur.executemany(
                    """INSERT IGNORE INTO moneyflow_daily 
                       (ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,
                        buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount,main_net)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows
                )
                total_rows += len(rows)
        except Exception as e:
            print(f"  {td} 失败: {e}")
            time.sleep(2)
            continue
        
        if (i + 1) % 20 == 0:
            pct = (i + 1) / len(trade_dates) * 100
            print(f"  进度: {i+1}/{len(trade_dates)} ({pct:.0f}%), 已写入 {total_rows:,} 行")
            conn.commit()
        
        time.sleep(0.15)
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"moneyflow_daily 完成: {total_rows:,} 行")


def backfill_index_full():
    """下载全量指数数据"""
    print(f"\n=== 回填 market_index_daily (全量) ===")
    
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
            # 查已有数据
            cur.execute("SELECT MAX(trade_date) FROM market_index_daily WHERE index_code=%s", (ts_code,))
            existing_max = cur.fetchone()[0]
            start = '20240101'
            if existing_max:
                start = str(existing_max).replace('-', '')
            
            df = pro.index_daily(ts_code=ts_code, start_date=start, end_date='20260430')
            if df is None or df.empty:
                print(f"  {name}: 无数据")
                continue
            
            rows = []
            for _, r in df.iterrows():
                rows.append((
                    ts_code, name, r['trade_date'],
                    float(r['close']), float(r['pct_chg']),
                    float(r['vol']), float(r['amount'])
                ))
            
            if rows:
                cur.executemany(
                    """INSERT IGNORE INTO market_index_daily 
                       (index_code,index_name,trade_date,close_price,change_pct,volume,amount)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""", rows
                )
                print(f"  {name}: {len(rows)} 条")
        except Exception as e:
            print(f"  {name} 失败: {e}")
        time.sleep(0.3)
    
    cur.close()
    conn.close()
    print("指数回填完成")


def print_stats():
    print("\n=== 数据统计 ===")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM moneyflow_daily")
    r = cur.fetchone()
    print(f"moneyflow_daily: {r[0]} ~ {r[1]}, {r[2]:,} 行")
    cur.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM market_index_daily")
    r = cur.fetchone()
    print(f"market_index_daily: {r[0]} ~ {r[1]}, {r[2]:,} 行")
    cur.close()
    conn.close()


if __name__ == '__main__':
    print("补全2024资金流 + 指数数据...")
    
    trade_dates = get_trade_dates_2024()
    if trade_dates:
        backfill_moneyflow_2024(trade_dates)
    
    backfill_index_full()
    print_stats()
    print("\n全部完成！")
