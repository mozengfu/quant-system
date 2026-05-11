#!/usr/bin/env python3
"""运行 V4 组合策略并输出 TOP5（底部起步和强势活跃策略已下线，详见CLAUDE.md）"""
import os, sys, json
from pathlib import Path

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

def print_top5(name, stocks, total=0):
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  【{name}】")
    print(sep)
    if not stocks:
        print("  (无结果)")
        return
    print(f"  候选总数: {total}")
    print()
    for i, s in enumerate(stocks[:5]):
        print(f"  #{i+1}  {s.get('名称','')} ({s.get('代码','')})")
        print(f"      评分: {s.get('增强评分', s.get('综合评分', ''))}", end="")
        ml = s.get('ml概率', '')
        if ml: print(f"  |  ML概率: {ml}", end="")
        print()
        print(f"      涨跌幅: {s.get('涨跌幅','')}  RPS: {s.get('RPS','')}")
        if s.get('行业',''): print(f"      行业: {s['行业']}")
        if s.get('入选理由',''): print(f"      理由: {s['入选理由']}")
        print()

print("=" * 70)
print("  量化系统 — V4 组合策略")
print("  底部起步/强势活跃策略已下线（回测亏损，详见CLAUDE.md）")
print("=" * 70)

# V4组合策略：从数据库直接运行
import pymysql
from quant_app.utils.config import get_db_config
from pathlib import Path

scripts_dir = str(Path(__file__).resolve().parent / "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)
from mainforce_scoring import calculate_mainforce_score

conn = pymysql.connect(**get_db_config())
cur = conn.cursor()
cur.execute("SELECT MAX(trade_date) FROM daily_price")
latest_date = cur.fetchone()[0]
today_str = str(latest_date)

sql = """
    SELECT d.ts_code, s.name, s.industry,
           d.close, d.pct_chg, d.turnover_rate, d.volume_ratio,
           d.ma5, d.ma10, d.ma20, d.rps_20
    FROM daily_price d
    JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
    WHERE d.trade_date = %s
      AND d.close > 5 AND d.pct_chg > 1 AND d.pct_chg < 9.5 AND d.turnover_rate > 1.5
      AND s.is_st = 0 AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '92%%'
      AND d.ts_code NOT LIKE '8%%' AND d.ts_code NOT LIKE '4%%'
      AND (
          (d.ma5 > d.ma10 AND d.ma10 > d.ma20 AND d.ma5 IS NOT NULL AND d.ma20 IS NOT NULL AND d.close > d.ma5 AND d.volume_ratio > 1.5)
          OR (d.pct_chg > 4.0 AND d.volume_ratio > 2.0 AND d.close > d.ma5)
      )
    ORDER BY d.pct_chg DESC LIMIT 200
"""
cur.execute(sql, (today_str,))
candidates = cur.fetchall()
cur.close()
conn.close()

# 主力评分过滤
stocks = []
for r in candidates:
    ts_code, price = r[0], float(r[3]) if r[3] else 0
    if price <= 0: continue
    mf = calculate_mainforce_score(ts_code, latest_date)
    if mf.get('score', 0) < 60: continue

    ma5, ma10, ma20 = [float(r[i]) if r[i] else 0 for i in (7,8,9)]
    pct_chg, vol_ratio, turnover = float(r[4]) if r[4] else 0, float(r[6]) if r[6] else 0, float(r[5]) if r[5] else 0
    rps = float(r[10]) if r[10] else 0
    qs = (40 if ma5 > ma10 > ma20 and ma20 > 0 else 0) + (20 if price > ma5 else 0) + (20 if vol_ratio > 2 else 0) + (10 if pct_chg > 3 else 0) + (10 if turnover > 3 else 0)

    stocks.append({
        '代码': ts_code.split('.')[0], '名称': r[1] or '', '行业': r[2] or '',
        '涨跌幅': f"{pct_chg:+.2f}%", '综合评分': qs, 'RPS': rps,
        '主力评分': int(mf['score']), '阶段判断': mf.get('level',''),
    })

# ML增强
try:
    from ml_predict import ml_enhanced_score
    conn2 = pymysql.connect(**get_db_config())
    stocks = ml_enhanced_score(stocks, db_conn=conn2)
    conn2.close()
except Exception:
    for s in stocks:
        s['ml概率'] = 0.5
        s['增强评分'] = s.get('综合评分', 0)

stocks.sort(key=lambda x: x.get('增强评分', 0), reverse=True)

print_top5("V4 组合策略（条件B收紧 + 主力评分 ≥60）", stocks, len(stocks))

# 保存结果
with open(DATA_DIR / "stock_pool_v4.json", 'w') as f:
    json.dump({"scan_date": today_str, "stocks": stocks[:20]}, f, ensure_ascii=False, indent=2)

print(f"{'='*70}")
print(f"  V4 扫描完成，共 {len(stocks)} 只符合条件")
print(f"  底部起步/强势活跃策略已关闭")
print(f"{'='*70}")
