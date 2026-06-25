#!/usr/bin/env python3
"""
从 daily_basic 回填 daily_price 的 turnover_rate 和 volume_ratio
在 sync_daily_basic 执行后运行（17:05 → 17:07）
"""
import sys, os, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from quant_app.utils.config import get_db_config
import pymysql, logging

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

conn = pymysql.connect(**get_db_config())
cur = conn.cursor()

# 今天最新的交易日
cur.execute("SELECT MAX(trade_date) FROM daily_basic")
max_date = cur.fetchone()[0]
if not max_date:
    log.warning("daily_basic 无数据")
    sys.exit(0)

log.info(f"回填交易日: {max_date}")

# 检查 daily_price 有多少 turnover_rate=0
cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date=%s AND (turnover_rate=0 OR turnover_rate IS NULL)", (max_date,))
zero_count = cur.fetchone()[0]
log.info(f"daily_price 中 turnover_rate=0: {zero_count} 只")

if zero_count == 0:
    log.info("无需回填")
    sys.exit(0)

# 回填
cur.execute("""
    UPDATE daily_price d
    JOIN daily_basic b ON d.ts_code=b.ts_code AND d.trade_date=b.trade_date
    SET d.turnover_rate = b.turnover_rate,
        d.volume_ratio  = b.volume_ratio
    WHERE d.trade_date = %s
      AND (d.turnover_rate = 0 OR d.turnover_rate IS NULL)
""", (max_date,))
affected = cur.rowcount
conn.commit()
log.info(f"回填完成: 影响 {affected} 行")

# 验证
cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date=%s AND turnover_rate>0", (max_date,))
filled = cur.fetchone()[0]
log.info(f"现在 turnoever_rate>0: {filled} 只")

cur.close()
conn.close()
