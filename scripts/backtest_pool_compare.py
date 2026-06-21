#!/usr/bin/env python3
"""对比回测：旧池(Top500成交额) vs 新池(全A股)
在Windows训练机上运行: python backtest_pool_compare.py
"""
import os
import sys
import time
import warnings

import numpy as np
import pymysql

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))

from predict_v11_oos import build_features, ensemble_predict, load_model

START_DATE, END_DATE = "2025-10-01", "2026-06-04"
SAMPLE_INTERVAL = 5
TOP_N = 3
HOLD_DAYS = 5

DB_CONFIG = {
    "host": "192.168.10.30", "port": 3306,
    "user": "root", "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": "quant_db", "charset": "utf8mb4",
}

MODEL_PATH = os.path.join(BASE_DIR, "data", "ml_stock_model_v11_0_oos_v2.pkl")
load_model(MODEL_PATH)

conn = pymysql.connect(**DB_CONFIG, connect_timeout=10)
cur = conn.cursor()

cur.execute(f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}' ORDER BY trade_date")
trade_dates = sorted([str(r[0]) for r in cur.fetchall()])
sample_dates = trade_dates[::SAMPLE_INTERVAL]
print(f"回测: {START_DATE} ~ {END_DATE}, {len(trade_dates)}天, 采样{len(sample_dates)}次, Top{TOP_N}, 持{HOLD_DAYS}天\n")

def get_pool(date_str, mode):
    if mode == "old":
        cur.execute(f"""SELECT ts_code FROM daily_price WHERE trade_date='{date_str}'
            AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200
            ORDER BY amount DESC LIMIT 500""")
    else:
        cur.execute(f"""SELECT ts_code FROM daily_price WHERE trade_date='{date_str}'
            AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200""")
    return [r[0] for r in cur.fetchall()]

def run_backtest(mode, label):
    returns = []
    t0 = time.time()
    for i, sd in enumerate(sample_dates):
        t1 = time.time()
        pool = get_pool(sd, mode)
        feat = build_features(conn, pool, as_of_date=sd)
        if feat is None or feat.empty:
            print(f"  [{i+1}/{len(sample_dates)}] {sd} pool={len(pool)} -> 特征失败")
            continue
        preds = ensemble_predict(feat)
        ca = feat["ts_code"].tolist()
        ranked = sorted(zip(ca, preds), key=lambda x: -x[1])[:TOP_N]
        top_codes = [tc for tc, _ in ranked]

        hold_idx = trade_dates.index(sd)
        exit_idx = min(hold_idx + HOLD_DAYS, len(trade_dates)-1)
        exit_date = trade_dates[exit_idx]

        day_ret = []
        for tc in top_codes:
            cur.execute(f"SELECT close FROM daily_price WHERE ts_code='{tc}' AND trade_date='{sd}'")
            er = cur.fetchone()
            cur.execute(f"SELECT close FROM daily_price WHERE ts_code='{tc}' AND trade_date='{exit_date}'")
            xr = cur.fetchone()
            if er and xr:
                day_ret.append((float(xr[0]) - float(er[0])) / float(er[0]) * 100)
        if day_ret:
            avg_r = np.mean(day_ret)
            returns.append(avg_r)
            print(f"  [{i+1}/{len(sample_dates)}] {sd} pool={len(pool)} ret={avg_r:+.2f}% ({time.time()-t1:.1f}s)")
        else:
            print(f"  [{i+1}/{len(sample_dates)}] {sd} pool={len(pool)} -> 无数据")

    returns = np.array(returns)
    elapsed = time.time() - t0
    if len(returns) == 0:
        return {"n":0,"cum":0,"win_rate":0,"avg":0,"sharpe":0,"time":elapsed}
    cum = np.prod(1 + returns/100) * 100 - 100
    avg = returns.mean()
    std = returns.std()
    sharpe = (avg / std) * (252/HOLD_DAYS)**0.5 if std > 0 else 0
    return {
        "n": len(returns), "cum": round(cum, 2),
        "win_rate": round((returns > 0).mean() * 100, 1),
        "avg": round(avg, 2), "sharpe": round(sharpe, 2),
        "time": round(elapsed, 1),
    }

print("="*50)
print("旧池: 成交额 Top500")
print("="*50)
old = run_backtest("old", "OLD")

print(f"\n{'='*50}")
print("新池: 全A股")
print("="*50)
new = run_backtest("new", "NEW")

print(f"\n{'='*50}")
print(f"{'指标':<12} {'旧(Top500)':<18} {'新(全A股)':<18}")
print("="*50)
for k, label in [("n","交易次数"),("cum","累积收益%"),("win_rate","胜率%"),("avg","平均收益%"),("sharpe","夏普"),("time","耗时秒")]:
    print(f"{label:<12} {str(old[k]):<18} {str(new[k]):<18}")

cur.close()
conn.close()
