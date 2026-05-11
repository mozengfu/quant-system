#!/usr/bin/env python3
"""ML 实际收益深度分析"""
import sys
sys.path.insert(0, "/Users/mozengfu/workspace/quant-system")
from quant_app.utils.config import get_db_config
import pymysql, pandas as pd, numpy as np

conn = pymysql.connect(**get_db_config())
c = conn.cursor()
c.execute("SELECT ts_code, trade_date, close FROM daily_price WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY ORDER BY ts_code, trade_date")
cols = [d[0] for d in c.description]
daily = pd.DataFrame(c.fetchall(), columns=cols)
daily["close"] = pd.to_numeric(daily["close"], errors="coerce")
daily["trade_date"] = pd.to_datetime(daily["trade_date"])
conn.close()

fwd = {}
for tc, grp in daily.groupby("ts_code"):
    grp = grp.sort_values("trade_date")
    closes = grp["close"].values
    dates = grp["trade_date"].values
    for i in range(len(dates)):
        if closes[i] <= 0: continue
        ds = pd.Timestamp(dates[i]).strftime("%Y-%m-%d")
        if i+3 < len(closes) and closes[i+3] > 0:
            fwd[(tc, ds)] = (closes[i+3]-closes[i])/closes[i]*100

ml_preds = pd.read_parquet("data/ml_preds_v6_3.parquet")
ml_preds["trade_date"] = pd.to_datetime(ml_preds["trade_date"])
ml_period = ml_preds[(ml_preds["trade_date"] >= "2025-01-02") & (ml_preds["trade_date"] <= "2026-04-30")]

# 按成交额分档
c2 = conn = pymysql.connect(**get_db_config())
c = c2.cursor()
c.execute("SELECT ts_code, trade_date, amount FROM daily_price WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY")
cols = [d[0] for d in c.description]
amt_df = pd.DataFrame(c.fetchall(), columns=cols)
amt_df["amount"] = pd.to_numeric(amt_df["amount"], errors="coerce")
amt_df["trade_date"] = pd.to_datetime(amt_df["trade_date"])
c2.close()

# ML 选股按规模分析
rets_all = []
rets_by_size = {"大盘": [], "中小盘": []}

for date, grp in ml_period.groupby("trade_date"):
    top = grp.nlargest(15, "_ml_pred")
    ds = pd.Timestamp(date).strftime("%Y-%m-%d")
    # 找当天成交额中位数
    day_amt = amt_df[amt_df["trade_date"] == pd.Timestamp(date)]
    if day_amt.empty: continue
    median_amt = day_amt["amount"].median()
    
    for _, row in top.iterrows():
        r = fwd.get((row["ts_code"], ds))
        if r is not None:
            rets_all.append(r)
            tc_amt = day_amt[day_amt["ts_code"] == row["ts_code"]]
            if not tc_amt.empty and tc_amt.iloc[0]["amount"] > median_amt:
                rets_by_size["大盘"].append(r)
            else:
                rets_by_size["中小盘"].append(r)

arr = np.array(rets_all)
print("=== ML Top15 3日收益统计 ===")
print(f"样本: {len(arr)}笔, 胜率(>0): {(arr>0).sum()/len(arr)*100:.1f}%")
print(f"中位数: {np.median(arr):.2f}%, 均值: {np.mean(arr):.2f}%")
print(f"标准差: {np.std(arr):.2f}%")
print(f"10分位: {np.percentile(arr, 10):.2f}%, 90分位: {np.percentile(arr, 90):.2f}%")
print(f"亏损>5%: {(arr<-5).sum()/len(arr)*100:.1f}%, 盈利>5%: {(arr>5).sum()/len(arr)*100:.1f}%")
print(f"最大亏损: {np.min(arr):.2f}%, 最大盈利: {np.max(arr):.2f}%")

print("\n=== 按规模拆分 ===")
for k, v in rets_by_size.items():
    a = np.array(v)
    print(f"{k}: {len(a)}笔, 胜率{(a>0).sum()/len(a)*100:.1f}%, 中位数{np.median(a):.2f}%, 均值{np.mean(a):.2f}%")

# 最差的10天
daily_rets = {}
for date, grp in ml_period.groupby("trade_date"):
    top = grp.nlargest(15, "_ml_pred")
    ds = pd.Timestamp(date).strftime("%Y-%m-%d")
    day_rets = []
    for _, row in top.iterrows():
        r = fwd.get((row["ts_code"], ds))
        if r is not None:
            day_rets.append(r)
    if len(day_rets) >= 5:
        daily_rets[ds] = day_rets

worst = sorted(daily_rets.items(), key=lambda x: np.mean(x[1]))[:10]
print("\n=== 最差的10天 ===")
for ds, rets in worst:
    a = np.array(rets)
    print(f"  {ds}: 胜率{(a>0).sum()/len(a)*100:.0f}%, 均收益{np.mean(a):.2f}%")
