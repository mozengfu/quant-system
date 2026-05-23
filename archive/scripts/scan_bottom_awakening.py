#!/usr/bin/env python3
"""底部苏醒策略 — 收盘后扫描（cron）
- 调用 generate_bottom_awakening_candidates
- 输出 Top10 到 stdout
- 写入 stock_pool_awakening 表
- 保存 data/bottom_awakening.json
"""
import os, sys, json, logging
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

import pymysql
from quant_app.utils.config import get_db_config
from quant_app.services.strategy_service import generate_bottom_awakening_candidates

LIMIT = 20
TOP_N = 10

def main():
    conn = pymysql.connect(**get_db_config())
    try:
        candidates, display_date = generate_bottom_awakening_candidates(conn, limit=LIMIT)
        if not candidates:
            print(f"\n{'='*60}")
            print("  底部苏醒策略：无候选")
            print(f"{'='*60}")
            return

        top10 = candidates[:TOP_N]

        # stdout 输出
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  底部苏醒策略 — {display_date}")
        print(f"  候选总数: {len(candidates)}")
        print(sep)
        for i, s in enumerate(top10):
            reasons = s.get('entry_reason', '')
            rank = i + 1
            print(f"  #{rank} {s['name']} ({s['ts_code']})")
            print(f"      得分={s['awakening_score']}  放量={s['vol_expansion']}x  位置={s['position_52w']}%")
            print(f"      涨跌={s['pct_chg']:+.2f}%  量比={s['volume_ratio']:.2f}  换手={s['turnover_rate']:.2f}%")
            if s.get('rps_20', 0): print(f"      RPS={s['rps_20']:.0f}  主力净流={s.get('main_net', 0):.0f}万")
            print(f"      理由: {reasons}")
            print()

        # 写入 stock_pool_awakening 表
        cur = conn.cursor()
        for i, s in enumerate(candidates):
            code_clean = s['ts_code'].split('.')[0]
            reason = s.get('entry_reason', '')
            pos_52w = float(s.get('position_52w', 0))
            vol_exp = float(s.get('vol_expansion', 0))
            main_net = float(s.get('main_net', 0))
            rps = float(s.get('rps_20', 0))

            sql = """
                INSERT INTO stock_pool_awakening
                    (snap_date, ts_code, name, industry, price, change_pct,
                     turnover_rate, vol_ratio, awakening_score, vol_expansion,
                     position_52w, rps_20, main_net, entry_reason, today_rank)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    name=VALUES(name), industry=VALUES(industry), price=VALUES(price),
                    change_pct=VALUES(change_pct), turnover_rate=VALUES(turnover_rate),
                    vol_ratio=VALUES(vol_ratio), awakening_score=VALUES(awakening_score),
                    vol_expansion=VALUES(vol_expansion), position_52w=VALUES(position_52w),
                    rps_20=VALUES(rps_20), main_net=VALUES(main_net),
                    entry_reason=VALUES(entry_reason), today_rank=VALUES(today_rank)
            """
            cur.execute(sql, (
                display_date, s['ts_code'], s['name'], s.get('industry', ''),
                float(s['close']), float(s['pct_chg']),
                float(s['turnover_rate']), float(s['volume_ratio']),
                int(s['awakening_score']), vol_exp, pos_52w,
                rps, main_net, reason, i + 1
            ))
        conn.commit()
        cur.close()
        logger.info(f"底部苏醒-写入DB: {len(candidates)} 条")

        # 保存 data/bottom_awakening.json
        output = {
            "date": display_date,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total": len(candidates),
            "top10": top10,
        }
        json_path = DATA_DIR / "bottom_awakening.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"底部苏醒-保存JSON: {json_path}")

    finally:
        conn.close()


if __name__ == '__main__':
    main()
