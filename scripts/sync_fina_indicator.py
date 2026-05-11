"""
同步财务指标（fina_indicator）到本地 MySQL
- 用线程池加速（Tushare 要求逐只股票查询）
- 支持断点续传（跳过已有数据）
"""
import os, sys, time, math, logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent.parent))
from quant_app.utils.config import get_db_config
import tushare as ts
import pymysql

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS fina_indicator (
    ts_code VARCHAR(16) NOT NULL,
    end_date VARCHAR(10) NOT NULL COMMENT '报告期',
    roe DECIMAL(10,4) COMMENT '净资产收益率',
    yoy_sales DECIMAL(10,4) COMMENT '营收同比增速',
    grossprofit_margin DECIMAL(10,4) COMMENT '毛利率',
    netprofit_margin DECIMAL(10,4) COMMENT '净利率',
    eps DECIMAL(10,4) COMMENT '每股收益',
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ts_code, end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def to_val(x):
    return None if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x)


def ensure_table():
    conn = pymysql.connect(**get_db_config())
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
    conn.close()
    logger.info("表 fina_indicator 已就绪")


def fetch_one(code, period, pro):
    """查单只股票的财务数据"""
    try:
        df = pro.fina_indicator(ts_code=code, period=period,
                                fields="ts_code,end_date,roe,yoy_sales,grossprofit_margin,netprofit_margin,eps")
        if df is not None and not df.empty:
            row = df.iloc[0]
            return (code, row['end_date'],
                    to_val(row.get('roe')), to_val(row.get('yoy_sales')),
                    to_val(row.get('grossprofit_margin')), to_val(row.get('netprofit_margin')),
                    to_val(row.get('eps')))
    except Exception:
        pass
    return None


def sync_period(period, max_workers=8):
    """
    逐只股票查询（线程池加速），断点续传
    """
    conn = pymysql.connect(**get_db_config())
    with conn.cursor() as cur:
        # 获取已有数据（断点续传）
        cur.execute("SELECT ts_code FROM fina_indicator WHERE end_date=%s", [period])
        done = {r[0] for r in cur.fetchall()}
        logger.info(f"报告期 {period} 已有 {len(done)} 只")

        # 获取需补充的股票列表（排除ST/688/北交所）
        cur.execute("""SELECT DISTINCT d.ts_code FROM daily_price d
                       JOIN stock_info s ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(s.ts_code USING utf8mb4)
                       WHERE d.trade_date = (SELECT MAX(trade_date) FROM daily_price)
                         AND s.name NOT LIKE '%ST%'
                         AND d.ts_code NOT LIKE '688%%'
                         AND d.ts_code NOT LIKE '8%%'
                         AND d.ts_code NOT LIKE '4%%'""")
        all_stocks = [r[0] for r in cur.fetchall()]
    conn.close()

    todo = [c for c in all_stocks if c not in done]
    logger.info(f"需同步 {len(todo)} 只（共 {len(all_stocks)} 只）")

    if not todo:
        return 0

    pro = ts.pro_api()
    inserted = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch_one, code, period, pro) for code in todo]
        for i, f in enumerate(as_completed(futures)):
            if (i + 1) % 200 == 0:
                elapsed = time.time() - start
                logger.info(f"进度 {i+1}/{len(todo)}，已入库 {inserted} 条，耗时 {elapsed:.0f}s")

            result = f.result()
            if result:
                conn = pymysql.connect(**get_db_config())
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT IGNORE INTO fina_indicator VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                            result
                        )
                        conn.commit()
                    inserted += 1
                finally:
                    conn.close()

    elapsed = time.time() - start
    logger.info(f"报告期 {period} 同步完成: {inserted} 条（新增），总耗时 {elapsed:.0f}s")
    return inserted


if __name__ == "__main__":
    ensure_table()
    # 同步最近两个报告期
    sync_period("20251231", max_workers=8)
    sync_period("20250930", max_workers=8)
    logger.info("全部完成")
