#!/usr/bin/env python3
"""
补全 sync_tushare_boards.py Step5漏掉的板块 (ths_daily限频截断)
只针对 board_concept_hist 中最新交易日 < target 的板块,按板块粒度增量拉取。
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

#加载 .env
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
 with open(env_path) as f:
 for line in f:
 line = line.strip()
 if line and not line.startswith("#") and "=" in line:
 key, val = line.split("=",1)
 os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))
import pymysql
import tushare as ts

logging.basicConfig(
 level=logging.INFO,
 format="%(asctime)s %(levelname)s %(message)s",
 force=True,
)
logger = logging.getLogger(__name__)
if hasattr(sys.stdout, "reconfigure"):
 sys.stdout.reconfigure(line_buffering=True)


def get_pro():
 token = os.environ.get("TUSHARE_TOKEN", "")
 if not token:
 raise ValueError("TUSHARE_TOKEN 未设置")
 ts.set_token(token)
 return ts.pro_api()


def get_db():
 from quant_app.utils.config import get_db_config
 return pymysql.connect(**get_db_config())


def safe_float(v):
 import math
 if v is None:
 return0
 if isinstance(v, (int, float)):
 if math.isnan(v) or math.isinf(v):
 return0
 return float(v)
 s = str(v).strip()
 if not s or s in ("N/A", "-", "nan", "NaN", "NA", "None"):
 return0
 try:
 s = s.replace("%", "").replace("亿", "").replace("万", "").replace(",", "")
 f = float(s)
 if math.isnan(f) or math.isinf(f):
 return0
 return f
 except (ValueError, TypeError):
 return0


def find_missing_boards(target_date):
 """找出最新交易日 < target_date 的所有概念板块"""
 conn = get_db()
 cur = conn.cursor()
 cur.execute("""
 SELECT b.board_code, b.board_name, MAX(h.trade_date) AS latest_date
 FROM quant_db.board_concept b
 LEFT JOIN quant_db.board_concept_hist h ON b.board_code = h.board_code
 WHERE b.is_latest = TRUE
 GROUP BY b.board_code, b.board_name
 HAVING latest_date < %s OR latest_date IS NULL
 ORDER BY latest_date, b.board_code
 """, (target_date,))
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return rows


def topup_one(pro, code, name, start_date, end_date):
 """单板块增量补全"""
 df = pro.ths_daily(ts_code=code, start_date=start_date, end_date=end_date)
 if df is None or df.empty:
 return0

 conn = get_db()
 cur = conn.cursor()
 rows =0
 for _, row in df.iterrows():
 trade_date = str(row.get("trade_date", ""))
 if len(trade_date) ==8:
 trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

 sql = """INSERT INTO quant_db.board_concept_hist
 (trade_date, board_code, board_name, open, close, high, low,
 pct_change, change_amount, volume, amount, turnover_rate)
 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
 ON DUPLICATE KEY UPDATE
 board_name=VALUES(board_name), open=VALUES(open), close=VALUES(close),
 high=VALUES(high), low=VALUES(low), pct_change=VALUES(pct_change),
 change_amount=VALUES(change_amount), volume=VALUES(volume),
 amount=VALUES(amount), turnover_rate=VALUES(turnover_rate)"""
 cur.execute(sql, (
 trade_date, code, name,
 safe_float(row.get("open",0)), safe_float(row.get("close",0)),
 safe_float(row.get("high",0)), safe_float(row.get("low",0)),
 safe_float(row.get("pct_change",0)), safe_float(row.get("change",0)),
 safe_float(row.get("vol",0)),
 safe_float(row.get("vol",0)) * safe_float(row.get("avg_price",0)) /100,
 safe_float(row.get("turnover_rate",0)),
 ))
 rows +=1

 conn.commit()
 cur.close()
 conn.close()
 return rows


def main():
 logger.info("=" *60)
 logger.info("概念板块补全脚本 (针对 Step5限频截断遗留)")
 logger.info("=" *60)

 target = datetime.now().strftime("%Y-%m-%d")
 end_date = datetime.now().strftime("%Y%m%d")

 missing = find_missing_boards(target_date=target)
 logger.info(f"目标:拉取到 {target} (Tushare: {end_date})")
 logger.info(f"待补全板块数: {len(missing)}")

 if not missing:
 logger.info("✅ 无需补全,所有板块已是最新")
 return

 logger.info("待补全板块列表:")
 for code, name, dt in missing:
 logger.info(f" {code} {name} (latest={dt})")

 pro = get_pro()
 total_rows =0
 total_failed =0
 succeeded =0

 for i, (code, name, latest) in enumerate(missing):
 try:
 if latest:
 start_dt = latest + timedelta(days=1)
 start_date = start_dt.strftime("%Y%m%d")
 else:
 # 完全没数据 →拉近60 天
 start_date = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

 rows = topup_one(pro, code, name, start_date, end_date)
 total_rows += rows
 succeeded +=1
 logger.info(f" [{i+1}/{len(missing)}] {code} {name}: +{rows} rows (from {start_date})")

 # Tushare限频: ~120 req/min 安全
 time.sleep(0.5)

 except Exception as e:
 total_failed +=1
 logger.warning(f" [{i+1}/{len(missing)}] {code} {name}失败: {e}")
 #失败后额外 sleep避免连续触发
 time.sleep(1.0)

 logger.info("=" *60)
 logger.info(f"补全完成:成功 {succeeded}/{len(missing)}, 总写入 {total_rows} 行,失败 {total_failed}")
 logger.info("=" *60)


if __name__ == "__main__":
 main()
