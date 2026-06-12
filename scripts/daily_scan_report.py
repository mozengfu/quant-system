#!/usr/bin/env python3
"""盘后扫描：读取数据库，输出明日候选Top5和持仓状态"""
import json

from quant_app.utils.config import db_connection

conn = db_connection().__enter__()
cur = conn.cursor()

# 1. 获取最新交易日
cur.execute("SELECT MAX(trade_date) FROM daily_price")
trade_date = cur.fetchone()[0]
print(f"最新交易日: {trade_date}")

# 2. 今日强势股筛选Top30 - 用convert避免collation冲突
cur.execute("""
    SELECT d.ts_code, s.name, d.close, d.pct_chg, d.volume_ratio, d.rps_20,
           d.ma5, d.ma10, d.ma20, d.turnover_rate
    FROM daily_price d
    JOIN stock_info s ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(s.ts_code USING utf8mb4)
    WHERE d.trade_date = %s
      AND d.pct_chg BETWEEN 0.5 AND 9.5
      AND d.volume_ratio BETWEEN 1.5 AND 10
      AND d.turnover_rate BETWEEN 3 AND 20
      AND d.rps_20 >= 40
      AND d.ma5 > d.ma10 AND d.ma10 > d.ma20
      AND s.is_st = 0
    ORDER BY d.rps_20 DESC, d.pct_chg DESC
    LIMIT 30
""", (trade_date,))
rows = cur.fetchall()
print(f"\n筛选结果: {len(rows)} 只")
for r in rows:
    print(f"  {r[0]} {r[1]:　<6s} 收盘{r[2]:>8.2f} 涨幅{r[3]:>+7.2f}% 量比{r[4]:>6.2f} RPS{r[5]:>5.1f} 换手{r[9]:>6.2f}%")

# 3. 获取当前持仓
cur.execute("""
    SELECT p.ts_code, p.name, p.quantity, p.cost, p.stop_loss, p.take_profit,
           d.close as today_close, d.pct_chg
    FROM positions p
    LEFT JOIN daily_price d ON CONVERT(p.ts_code USING utf8mb4) = CONVERT(d.ts_code USING utf8mb4) AND d.trade_date = %s
""", (trade_date,))
holdings = cur.fetchall()

h_codes = set()
holding_details = []
for h in holdings:
    h_codes.add(h[0])
    code, name, qty, cost, sl, tp, close, pct = h
    if close and cost and cost > 0:
        pf = (close - cost) / cost * 100
        pf_amt = (close - cost) * qty
    else:
        pf = 0.0
        pf_amt = 0.0
    status = "正常"
    if tp and tp > 0 and pf >= float(tp) * 0.8:
        status = "接近止盈"
    elif sl and sl < 0 and pf <= float(sl) * 1.2:
        status = "接近止损"
    holding_details.append({
        "code": code, "name": name, "qty": int(qty), "cost": float(cost),
        "close": float(close or 0), "pct_chg": float(pct or 0),
        "profit_pct": round(pf, 2), "profit_amt": round(pf_amt, 2),
        "status": status
    })

print(f"\n当前持仓: {len(holdings)} 只")
for h in holding_details:
    print(f"  {h['name']}({h['code']}): 现价{h['close']:.2f} 成本{h['cost']:.3f} "
          f"浮盈{h['profit_pct']:+.2f}%({h['profit_amt']:+.2f}) - {h['status']}")

# 4. Top5 candidates (exclude holdings)
candidates = []
for r in rows:
    if r[0] not in h_codes:
        candidates.append(r)

top5 = candidates[:5]
top5_list = []
for i, c in enumerate(top5, 1):
    item = {
        "rank": i, "name": c[1], "code": c[0], "close": float(c[2]),
        "pct_chg": round(float(c[3]), 2), "volume_ratio": round(float(c[4]), 2),
        "rps_20": round(float(c[5]), 1), "turnover_rate": round(float(c[9]), 2)
    }
    top5_list.append(item)

print("\n明日候选Top5 (排除持仓):")
for c in top5_list:
    print(f"  {c['rank']}. {c['name']}({c['code']}) +{c['pct_chg']:.2f}% "
          f"量比{c['volume_ratio']:.2f} RPS{c['rps_20']:.1f} 换手{c['turnover_rate']:.2f}%")

# 5. 输出JSON结果
result = {
    "trade_date": str(trade_date),
    "top5": top5_list,
    "holdings": holding_details,
    "strong_pool_total": len(rows)
}
print("\n---JSON_START---")
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        import decimal
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)
print(json.dumps(result, ensure_ascii=False, indent=2, cls=DecimalEncoder))
conn.close()
