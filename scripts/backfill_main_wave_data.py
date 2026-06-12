#!/usr/bin/env python3
"""
主升浪模型 — 5 类新数据 backfill
  - top_list         (龙虎榜)
  - top_inst         (机构席位)
  - limit_list_d     (涨停股)
  - research_report  (研报)
  - sector_relay_state (板块接力, 衍生计算)

断点续传：进度写在 data/backfill_main_wave.json
"""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from datetime import datetime, timedelta
from pathlib import Path

import pymysql
import tushare as ts
from dotenv import load_dotenv

load_dotenv()

from quant_app.utils.config import TUSHARE_TOKEN, get_db_config

DB_CONFIG = get_db_config()
PROGRESS_FILE = Path(__file__).parent.parent / "data" / "backfill_main_wave.json"
LOG_FILE = Path(__file__).parent / "backfill_main_wave.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

# ========== 进度管理 ==========
def _load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {"start_date": "20240101", "end_date": datetime.now().strftime("%Y%m%d"),
            "top_list": {}, "top_inst": {}, "limit_list_d": {}, "research_report": {}}


def _save_progress(p):
    PROGRESS_FILE.write_text(json.dumps(p, indent=2, ensure_ascii=False))


def _trading_days(start, end):
    """从 daily_price DISTINCT 取交易日 (兼容无 trade_cal 表的情况)"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as c:
            c.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date",
                      (f"{start[:4]}-{start[4:6]}-{start[6:8]}", f"{end[:4]}-{end[4:6]}-{end[6:8]}"))
            return [r[0].strftime("%Y%m%d") for r in c.fetchall()]
    finally:
        conn.close()


def _throttle(api_name, count=1):
    """避免触发限流"""
    time.sleep(0.35 * count)


# ========== Backfill 各表 ==========
def backfill_top_list(dates, progress):
    conn = pymysql.connect(**DB_CONFIG)
    done = set(progress.get("top_list", {}).keys())
    pending = [d for d in dates if d not in done]
    logger.info(f"top_list: {len(done)} done, {len(pending)} pending")
    for i, d in enumerate(pending):
        try:
            df = pro.top_list(trade_date=d)
            if df is None or df.empty:
                progress["top_list"][d] = 0
                _save_progress(progress)
                continue
            df = df.rename(columns={"pct_change": "pct_change"})
            rows = [tuple(r) for r in df[['trade_date','ts_code','name','close','pct_change','turnover_rate','amount',
                                          'l_sell','l_buy','l_amount','net_amount','net_rate','amount_rate',
                                          'float_values','reason']].itertuples(index=False, name=None)]
            import pandas as pd
            rows = [tuple(None if pd.isna(v) else v for v in r) for r in rows]
            with conn.cursor() as c:
                c.executemany("""INSERT IGNORE INTO top_list
                    (trade_date,ts_code,name,close,pct_change,turnover_rate,amount,
                     l_sell,l_buy,l_amount,net_amount,net_rate,amount_rate,float_values,reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
                conn.commit()
            progress["top_list"][d] = len(rows)
            if (i+1) % 20 == 0:
                logger.info(f"  top_list {i+1}/{len(pending)} ({d}: {len(rows)} rows)")
        except Exception as e:
            logger.error(f"  top_list {d}: {e}")
            time.sleep(5)
        _throttle("top_list")
    conn.close()


def backfill_top_inst(dates, progress):
    conn = pymysql.connect(**DB_CONFIG)
    done = set(progress.get("top_inst", {}).keys())
    pending = [d for d in dates if d not in done]
    logger.info(f"top_inst: {len(done)} done, {len(pending)} pending")
    for i, d in enumerate(pending):
        try:
            df = pro.top_inst(trade_date=d)
            if df is None or df.empty:
                progress["top_inst"][d] = 0
                _save_progress(progress)
                continue
            rows = [tuple(r) for r in df[['trade_date','ts_code','exalter','buy','buy_rate','sell','sell_rate',
                                          'net_buy','side','reason']].itertuples(index=False, name=None)]
            import pandas as pd
            rows = [tuple(None if pd.isna(v) else v for v in r) for r in rows]
            with conn.cursor() as c:
                c.executemany("""INSERT INTO top_inst
                    (trade_date,ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy,side,reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
                conn.commit()
            progress["top_inst"][d] = len(rows)
            if (i+1) % 20 == 0:
                logger.info(f"  top_inst {i+1}/{len(pending)} ({d}: {len(rows)} rows)")
        except Exception as e:
            logger.error(f"  top_inst {d}: {e}")
            time.sleep(5)
        _throttle("top_inst")
    conn.close()


def backfill_limit_list_d(dates, progress):
    conn = pymysql.connect(**DB_CONFIG)
    done = set(progress.get("limit_list_d", {}).keys())
    pending = [d for d in dates if d not in done]
    logger.info(f"limit_list_d: {len(done)} done, {len(pending)} pending")
    for i, d in enumerate(pending):
        try:
            df = pro.limit_list_d(trade_date=d)
            if df is None or df.empty:
                progress["limit_list_d"][d] = 0
                _save_progress(progress)
                continue
            df = df.rename(columns={"limit": "limit_type"})
            cols = ['trade_date','ts_code','industry','name','close','pct_chg','amount','limit_amount',
                    'float_mv','total_mv','turnover_ratio','fd_amount','first_time','last_time',
                    'open_times','up_stat','limit_times','limit_type']
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            rows = [tuple(r) for r in df[cols].itertuples(index=False, name=None)]
            import pandas as pd
            rows = [tuple(None if pd.isna(v) else v for v in r) for r in rows]
            with conn.cursor() as c:
                c.executemany(f"""INSERT IGNORE INTO limit_list_d
                    ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})""", rows)
                conn.commit()
            progress["limit_list_d"][d] = len(rows)
            if (i+1) % 20 == 0:
                logger.info(f"  limit_list_d {i+1}/{len(pending)} ({d}: {len(rows)} rows)")
        except Exception as e:
            logger.error(f"  limit_list_d {d}: {e}")
            time.sleep(5)
        _throttle("limit_list_d")
    conn.close()


def backfill_research_report(dates, progress):
    """研报按 1 个月批量拉，更快"""
    conn = pymysql.connect(**DB_CONFIG)
    done_months = set(progress.get("research_report", {}).keys())
    # 生成月份
    start_dt = datetime.strptime(dates[0], "%Y%m%d")
    end_dt = datetime.strptime(dates[-1], "%Y%m%d")
    months = []
    cur = start_dt.replace(day=1)
    while cur <= end_dt:
        months.append(cur.strftime("%Y%m"))
        cur = (cur + timedelta(days=32)).replace(day=1)
    pending = [m for m in months if m not in done_months]
    logger.info(f"research_report: {len(done_months)} months done, {len(pending)} pending")
    for i, m in enumerate(pending):
        start = m + "01"
        end_dt_m = (datetime.strptime(start, "%Y%m%d") + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        end = min(end_dt_m.strftime("%Y%m%d"), dates[-1])
        total_rows = 0
        try:
            for offset in range(0, 2000, 500):
                df = pro.research_report(start_date=start, end_date=end, limit=500, offset=offset)
                if df is None or df.empty:
                    break
                rows = [tuple(r) for r in df[['trade_date','title','report_type','author','name','ts_code',
                                              'inst_csname','ind_name','url']].itertuples(index=False, name=None)]
                with conn.cursor() as c:
                    c.executemany("""INSERT INTO research_report
                        (trade_date,title,report_type,author,name,ts_code,inst_csname,ind_name,url)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
                    conn.commit()
                total_rows += len(rows)
                if len(df) < 500:
                    break
                time.sleep(0.4)
            progress["research_report"][m] = total_rows
            if (i+1) % 5 == 0:
                logger.info(f"  research_report {i+1}/{len(pending)} ({m}: {total_rows} rows)")
        except Exception as e:
            logger.error(f"  research_report {m}: {e}")
            time.sleep(10)
    conn.close()


# ========== 板块接力序 (衍生, 当日即时计算) ==========
def compute_sector_relay_state(trade_date):
    """对单日计算板块接力序, 写入 sector_relay_state"""
    import pandas as pd
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as c:
            # 取当日涨停股
            c.execute("""SELECT ts_code, name, industry, pct_chg
                        FROM limit_list_d WHERE trade_date=%s ORDER BY industry, first_time""", (trade_date,))
            limit_rows = c.fetchall()
            if not limit_rows:
                return 0

            df = pd.DataFrame(limit_rows, columns=['ts_code','name','industry','pct_chg'])
            rows_to_insert = []
            # 按 industry 分组, 在组内按 first_time 排序, relay_index = 1,2,3...
            # groupby rank
            df['relay_index'] = df.groupby('industry').cumcount() + 1
            df['sector_type'] = 'industry'
            df['sector_name'] = df['industry']
            df['is_limit_up'] = 1
            df['has_breakout'] = 0
            df['score'] = 0
            df['trade_date'] = trade_date
            rows = [tuple(r) for r in df[['trade_date','sector_type','sector_name','relay_index',
                                          'ts_code','name','pct_chg','is_limit_up','has_breakout','score']
                                        ].itertuples(index=False, name=None)]
            with conn.cursor() as c:
                c.executemany("""INSERT IGNORE INTO sector_relay_state
                    (trade_date,sector_type,sector_name,relay_index,ts_code,name,pct_chg,is_limit_up,has_breakout,score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
                conn.commit()
            return len(rows)
    finally:
        conn.close()


# ========== Main ==========
def main():
    progress = _load_progress()
    dates = _trading_days(progress["start_date"], progress["end_date"])
    logger.info(f"Backfill range: {dates[0]} ~ {dates[-1]} ({len(dates)} trading days)")

    # 4 个独立表 backfill
    backfill_top_list(dates, progress)
    backfill_top_inst(dates, progress)
    backfill_limit_list_d(dates, progress)
    backfill_research_report(dates, progress)

    # 板块接力 — 增量, 只算近 30 天
    recent = dates[-30:] if len(dates) > 30 else dates
    for d in recent:
        try:
            n = compute_sector_relay_state(d)
            if n: logger.info(f"  sector_relay {d}: {n} rows")
        except Exception as e:
            logger.error(f"  sector_relay {d}: {e}")
        time.sleep(0.2)

    _save_progress(progress)
    logger.info("All backfill done.")


if __name__ == "__main__":
    main()
