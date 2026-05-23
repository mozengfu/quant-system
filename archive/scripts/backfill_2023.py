#!/usr/bin/env python3
"""
2023 年历史数据回填脚本
- daily_price: 按日期批量拉取 daily + daily_basic（快速）
- moneyflow_daily: 按股逐只拉取（无批量 API）
- margin_daily: 按股逐只拉取（无批量 API）
"""
import os, sys, time, math
from datetime import datetime
from collections import defaultdict

import pymysql
import tushare as ts
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quant_app.utils.config import get_db_config

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '6c43592a47016661112af3fb757a0a2b5215657d68b0f9043e549d32')
DB_CONFIG = get_db_config()

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backfill_2023.log')

def log(msg):
    ts_str = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts_str}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def get_conn():
    return pymysql.connect(**DB_CONFIG, autocommit=True)


def get_trade_dates_2023():
    """获取 2023 年所有交易日"""
    df = pro.trade_cal(exchange='SSE', start_date='20230101', end_date='20231231')
    df = df[df['is_open'] == 1]
    return sorted(df['cal_date'].tolist())


def backfill_daily_price():
    """按日期批量回填 daily_price（每交易日 1 次 API 调用）"""
    log("=== 回填 daily_price (2023) ===")
    trade_dates = get_trade_dates_2023()
    log(f"2023 年共 {len(trade_dates)} 个交易日")

    # 先看已有数据情况
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM daily_price")
    r = cur.fetchone()
    log(f"现有 daily_price: {r[0]} ~ {r[1]}")
    cur.close()
    conn.close()

    total_inserted = 0
    total_updated = 0

    for td in trade_dates:
        try:
            t0 = time.time()
            # 日线数据（所有股票一天一次调用）
            df = pro.daily(trade_date=td)
            if df is None or df.empty:
                log(f"  {td}: 无日线数据")
                continue

            # daily_basic（pe/pb/turnover_rate/volume_ratio）
            basic = pro.daily_basic(trade_date=td,
                                    fields='ts_code,trade_date,pe,pb,turnover_rate_f,volume_ratio')
            basic_map = {}
            if basic is not None and not basic.empty:
                for _, r in basic.iterrows():
                    basic_map[r['ts_code']] = {
                        'pe': float(r['pe']) if pd.notna(r.get('pe')) else None,
                        'pb': float(r['pb']) if pd.notna(r.get('pb')) else None,
                        'turnover_rate': float(r['turnover_rate_f']) if pd.notna(r.get('turnover_rate_f')) else None,
                        'volume_ratio': float(r['volume_ratio']) if pd.notna(r.get('volume_ratio')) else None,
                    }

            rows = []
            for _, row in df.iterrows():
                sc = row['ts_code']
                b = basic_map.get(sc, {})
                rows.append((
                    sc, td,
                    float(row['open'] or 0), float(row['high'] or 0),
                    float(row['low'] or 0), float(row['close'] or 0),
                    float(row['pre_close'] or 0),
                    int(row['vol'] or 0), float(row['amount'] or 0),
                    float(row['pct_chg'] or 0),
                    b.get('pe'), b.get('pb'),
                    b.get('turnover_rate'), b.get('volume_ratio'),
                ))

            conn = get_conn()
            cur = conn.cursor()
            cur.executemany(
                """INSERT IGNORE INTO daily_price
                   (ts_code, trade_date, open, high, low, close, pre_close, vol, amount, pct_chg, pe, pb, turnover_rate, volume_ratio)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows
            )
            inserted = cur.rowcount

            # 对于已存在的记录，更新 basic 字段
            updated = 0
            for sc, b in basic_map.items():
                if b['pe'] is not None or b['pb'] is not None:
                    cur.execute(
                        """UPDATE daily_price SET pe=COALESCE(NULLIF(%s,0),pe), pb=COALESCE(NULLIF(%s,0),pb),
                           turnover_rate=%s, volume_ratio=%s
                           WHERE ts_code=%s AND trade_date=%s""",
                        (b['pe'], b['pb'], b['turnover_rate'], b['volume_ratio'], sc, td)
                    )
                    updated += 1
            conn.commit()
            cur.close()
            conn.close()

            elapsed = time.time() - t0
            total_inserted += inserted
            total_updated += updated
            log(f"  {td}: INSERT={inserted}, UPDATE={updated}, {elapsed:.1f}s")

            time.sleep(0.1)  # API 限速

        except Exception as e:
            log(f"  {td} 失败: {e}")
            continue

    log(f"daily_price 回填完成: INSERT {total_inserted} 行, UPDATE {total_updated} 行")


def backfill_moneyflow():
    """按股逐只回填 moneyflow_daily"""
    log("\n=== 回填 moneyflow_daily (2023) ===")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM stock_info ORDER BY ts_code")
    all_stocks = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    total = len(all_stocks)
    inserted = 0

    for i, ts_code in enumerate(all_stocks):
        if (i + 1) % 200 == 0:
            elapsed_total = (time.time() - backfill_moneyflow._t0)
            eta = (elapsed_total / (i + 1)) * (total - i - 1)
            log(f"  进度 {i+1}/{total} ({(i+1)/total*100:.1f}%), ETA {eta/60:.0f}min")

        try:
            df = pro.moneyflow(ts_code=ts_code, start_date='20230101', end_date='20231231')
            if df is None or df.empty:
                continue

            rows = []
            for _, row in df.iterrows():
                buy_lg = float(row.get('buy_lg_amount', 0) or 0)
                buy_elg = float(row.get('buy_elg_amount', 0) or 0)
                sell_lg = float(row.get('sell_lg_amount', 0) or 0)
                sell_elg = float(row.get('sell_elg_amount', 0) or 0)
                rows.append((
                    ts_code, row['trade_date'],
                    float(row.get('buy_sm_amount', 0) or 0),
                    float(row.get('sell_sm_amount', 0) or 0),
                    float(row.get('buy_md_amount', 0) or 0),
                    float(row.get('sell_md_amount', 0) or 0),
                    buy_lg, sell_lg,
                    buy_elg, sell_elg,
                    float(row.get('net_mf_amount', 0) or 0),
                    buy_lg + buy_elg - sell_lg - sell_elg,
                ))

            if rows:
                conn = get_conn()
                cur = conn.cursor()
                cur.executemany(
                    """INSERT IGNORE INTO moneyflow_daily
                       (ts_code, trade_date, buy_sm_amount, sell_sm_amount,
                        buy_md_amount, sell_md_amount, buy_lg_amount, sell_lg_amount,
                        buy_elg_amount, sell_elg_amount, net_mf_amount, main_net)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows
                )
                conn.commit()
                inserted += len(rows)
                cur.close()
                conn.close()

            time.sleep(0.05)

        except Exception as e:
            log(f"  {ts_code} 失败: {e}")
            continue

    log(f"moneyflow_daily 回填完成: 插入 {inserted} 行")

backfill_moneyflow._t0 = time.time()


def backfill_margin():
    """按股逐只回填 margin_daily"""
    log("\n=== 回填 margin_daily (2023) ===")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM stock_info ORDER BY ts_code")
    all_stocks = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()

    total = len(all_stocks)
    inserted = 0
    skipped = 0

    for i, ts_code in enumerate(all_stocks):
        if (i + 1) % 500 == 0:
            log(f"  进度 {i+1}/{total} ({(i+1)/total*100:.1f}%)")

        try:
            df = pro.margin(ts_code=ts_code, start_date='20230101', end_date='20231231')
            if df is None or df.empty:
                skipped += 1
                continue

            rows = []
            for _, row in df.iterrows():
                rows.append((
                    ts_code, row['trade_date'],
                    float(row.get('rzye', 0) or 0),
                    float(row.get('rqye', 0) or 0),
                    float(row.get('rzmre', 0) or 0),
                    float(row.get('rqyl', 0) if 'rqyl' in row else 0),
                    float(row.get('rqmcl', 0) if 'rqmcl' in row else 0),
                ))

            if rows:
                conn = get_conn()
                cur = conn.cursor()
                cur.executemany(
                    """INSERT IGNORE INTO margin_daily
                       (ts_code, trade_date, rzye, rqye, rzmre, rqyl, rqmcl)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    rows
                )
                conn.commit()
                inserted += len(rows)
                cur.close()
                conn.close()

            time.sleep(0.05)

        except Exception as e:
            skipped += 1
            continue

    log(f"margin_daily 回填完成: 插入 {inserted} 行, 跳过 {skipped} 次（无融资融券数据）")


def check_result():
    """回填后检查"""
    log("\n=== 回填结果检查 ===")
    conn = get_conn()
    for table in ['daily_price', 'moneyflow_daily', 'margin_daily']:
        r = pd.read_sql(f"SELECT MIN(trade_date), MAX(trade_date), COUNT(*) FROM {table}", conn)
        log(f"  {table}: {r.iloc[0][0]} ~ {r.iloc[0][1]}, 行数={int(r.iloc[0][2]):,}")
    conn.close()


if __name__ == "__main__":
    log("=" * 60)
    log("2023 年历史数据回填开始")
    log("=" * 60)

    backfill_daily_price()
    backfill_moneyflow()
    backfill_margin()
    check_result()

    log("=" * 60)
    log("回填完成")
    log("=" * 60)
