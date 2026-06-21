"""策略B 实时扫描 — 完整回测 v2（对齐 realtime_scanner v9）"""
import warnings

import pandas as pd
import pymysql

warnings.filterwarnings("ignore")

DB = {"host":"127.0.0.1","port":3306,"user":"root","password": os.environ.get("MYSQL_PASSWORD", ""),"database":"quant_db","charset":"utf8mb4"}

# 回测参数
TOP_N = 5
HOLD_DAYS = 5
MAX_POSITIONS = 3
STOP_LOSS = -0.08
TAKE_PROFIT = 0.15
MAX_HOLD = 10
MIN_SCORE = 65
START_DATE = "2025-01-01"
END_DATE = "2026-06-05"


def load_data():
    conn = pymysql.connect(**DB)
    q = """
        SELECT ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
        FROM daily_price WHERE trade_date >= %s AND trade_date <= %s
        AND LEFT(ts_code,1) NOT IN ('8','4','9') AND LEFT(ts_code,3) != '688'
        ORDER BY trade_date ASC
    """
    df = pd.read_sql(q, conn, params=(START_DATE, END_DATE))
    conn.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    print(f"数据: {len(df)}行, {df.ts_code.nunique()}只, {df.trade_date.nunique()}天")
    return df


def compute_factors(df):
    df = df.sort_values(["ts_code", "trade_date"])
    for n in [5, 10, 20]:
        df[f"ma{n}"] = df.groupby("ts_code")["close"].transform(lambda x: x.rolling(n, min_periods=5).mean())
    df["avg_vol20"] = df.groupby("ts_code")["vol"].transform(lambda x: x.rolling(20, min_periods=5).mean())
    df["high20"] = df.groupby("ts_code")["high"].transform(lambda x: x.rolling(20, min_periods=5).max())
    df["low20"] = df.groupby("ts_code")["low"].transform(lambda x: x.rolling(20, min_periods=5).min())
    df["ret5"] = df.groupby("ts_code")["close"].transform(lambda x: x.pct_change(5) * 100)

    delta = df.groupby("ts_code")["close"].diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    avg_gain = gain.groupby(df["ts_code"]).transform(lambda x: x.rolling(14, min_periods=5).mean())
    avg_loss = loss.groupby(df["ts_code"]).transform(lambda x: x.rolling(14, min_periods=5).mean())
    rs = avg_gain / avg_loss.replace(0, 1)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)

    bb_std = df.groupby("ts_code")["close"].transform(lambda x: x.rolling(20, min_periods=5).std())
    df["bb_pct_b"] = ((df["close"] - (df["ma20"] - 2 * bb_std)) / (4 * bb_std + 0.001)).clip(0, 1)
    df["intraday_pos"] = ((df["close"] - df["low"]) / (df["high"] - df["low"] + 0.001)).clip(0, 1)

    df = df.dropna(subset=["ma20", "avg_vol20"]).copy()
    df["prev_close"] = df.groupby("ts_code")["close"].shift(1)
    df["ma20_slope"] = df.groupby("ts_code")["ma20"].transform(
        lambda x: x.diff(5) / x.shift(5).replace(0, 1) * 100)
    print(f"因子计算完成: {len(df)}行")
    return df


def score_v9(row, idx_pct=0):
    """对齐 production realtime_scanner v9"""
    vol_ratio = row["vol"] / max(row["avg_vol20"], 1)
    pct = row["pct_chg"]
    ret5 = row["ret5"] if not pd.isna(row["ret5"]) else 0

    # 量能突破 (max 25)
    s1 = 0
    if vol_ratio > 2.0: s1 += 12
    elif vol_ratio > 1.5: s1 += 8
    elif vol_ratio > 1.0: s1 += 4
    pos = (row["close"] - row["low20"]) / max(row["high20"] - row["low20"], 0.01) * 100
    if pos > 80: s1 += 7
    elif pos > 50: s1 += 3
    elif pos < 20: s1 += 6
    if vol_ratio > 1.2 and pct > 0: s1 += 6
    f1 = min(25, s1)

    # 动量 (max 25)
    s2 = 0
    if pct > 5: s2 += 8
    elif pct > 3: s2 += 6
    elif pct > 1: s2 += 3
    elif pct > -1: s2 += 1
    if ret5 > 8: s2 += 6
    elif ret5 > 4: s2 += 4
    elif ret5 > 1: s2 += 2
    rel = pct - idx_pct
    if rel > 3: s2 += 7
    elif rel > 1.5: s2 += 4
    elif rel > 0.5: s2 += 2
    elif rel < -3: s2 -= 3
    f2 = max(0, min(25, s2))

    # 趋势 (max 20)
    s3 = 0
    if row["close"] > row["ma5"]: s3 += 2
    if row["close"] > row["ma10"]: s3 += 3
    if row["close"] > row["ma20"]: s3 += 3
    if row["ma5"] > row["ma10"] and row["ma10"] > row["ma20"]: s3 += 7
    elif row["ma5"] > row["ma10"]: s3 += 3
    ms = row.get("ma20_slope", 0) or 0
    if ms > 2: s3 += 3
    elif ms > 0.5: s3 += 1
    elif ms < -2: s3 -= 2
    f3 = max(0, min(20, s3))

    # 流动性 (max 15)
    s4 = 6
    if row["amount"] > 5e6: s4 += 6
    elif row["amount"] > 1e6: s4 += 3
    if vol_ratio < 0.5: s4 -= 3
    f4 = max(0, min(15, s4))

    # RSI (±5)
    rsi = row["rsi"]
    if rsi < 30: sr = 5
    elif rsi < 40: sr = 3
    elif rsi > 80: sr = -5
    elif rsi > 70: sr = -2
    else: sr = 0

    # Bollinger (0~5)
    pct_b = row["bb_pct_b"]
    if pct_b < 0.2: sb = 5
    elif pct_b < 0.35: sb = 4
    elif 0.35 <= pct_b <= 0.65: sb = 2
    elif pct_b > 0.85: sb = 0
    else: sb = 1

    # 盘口日线近似 (max 10)
    if pct > 0 and vol_ratio > 1.5: f5 = 8
    elif pct > 0 and vol_ratio > 1.2: f5 = 5
    elif pct > 0 and vol_ratio > 1.0: f5 = 3
    elif pct < -3 and vol_ratio < 0.8: f5 = 4
    elif pct > -1: f5 = 1
    else: f5 = 0

    # 日内突破日线近似 (max 15)
    ipos = row["intraday_pos"]
    if ipos > 0.7: f6 = 8
    elif ipos > 0.5: f6 = 4
    elif ipos < 0.3: f6 = 2
    else: f6 = 0
    ha = row["open"] / max(row.get("prev_close", row["open"]), 0.01) - 1
    if ha > 0.02: f6 += 5
    elif ha > 0.01: f6 += 2
    f6 = min(15, f6)

    # 资金流日线近似 (max 10)
    if row["amount"] > 1e7: f7 = 5
    elif row["amount"] > 5e6: f7 = 3
    elif row["amount"] > 1e6: f7 = 1
    else: f7 = 0
    if pct > 2 and vol_ratio > 1.3: f7 += 3
    elif pct > 1: f7 += 1
    f7 = min(10, f7)

    # 多指数强度 (max 10)
    rs2 = pct - idx_pct
    if rs2 > 2: f8 = 8
    elif rs2 > 1: f8 = 5
    elif rs2 > 0: f8 = 2
    elif rs2 < -2: f8 = -2
    else: f8 = 0
    f8 = max(0, min(10, f8))

    return f1 + f2 + f3 + f4 + sr + sb + f5 + f6 + f7 + f8


def backtest(df):
    df = compute_factors(df)

    # 沪深300 指数数据
    idx = df[df.ts_code == "000300.SH"].set_index("trade_date")
    dates = sorted(df.trade_date.unique())
    print(f"回测: {len(dates)}天, Top{TOP_N}, max_pos={MAX_POSITIONS}, min_score={MIN_SCORE}")

    portfolio = []       # [{code, buy_date, buy_price, shares}]
    trades = []          # [{date, code, action, price, pnl_pct, reason}]
    cash = 100000
    initial = cash
    navs = []

    for dt in dates:
        day_all = df[df.trade_date == dt].set_index("ts_code")

        # ---- 更新持仓 ----
        for pos in portfolio[:]:
            if pos["code"] not in day_all.index:
                continue
            cur = day_all.loc[pos["code"], "close"]
            pnl = cur / pos["buy_price"] - 1
            days = (dt - pos["buy_date"]).days

            sell = False
            reason = ""
            if pnl <= STOP_LOSS:
                sell, reason = True, "止损"
            elif pnl >= TAKE_PROFIT:
                sell, reason = True, "止盈"
            elif days >= MAX_HOLD:
                sell, reason = True, "超时"

            if sell:
                cash += pos["shares"] * cur
                trades.append({"date": dt, "code": pos["code"], "action": "SELL",
                               "price": cur, "pnl_pct": pnl * 100, "reason": reason, "days": days})
                portfolio.remove(pos)

        # ---- 净值记录 ----
        mv = sum(p["shares"] * day_all.loc[p["code"], "close"]
                 for p in portfolio if p["code"] in day_all.index)
        navs.append({"date": dt, "nav": cash + mv})

        # ---- 市场状态 ----
        idx_pct = 0.0
        if dt in idx.index:
            v = idx.loc[dt, "pct_chg"]
            if isinstance(v, pd.Series): v = v.iloc[0]
            if not pd.isna(v): idx_pct = float(v)
        if idx_pct < -1.5:
            continue

        # ---- 选股建仓 ----
        available = MAX_POSITIONS - len(portfolio)
        if available <= 0:
            continue

        pool = day_all[(day_all["amount"] > 1e5) & (day_all["close"] > 3) & (day_all["pct_chg"] < 9.5)].copy()
        if len(pool) == 0:
            continue

        # 排除已持仓
        held = {p["code"] for p in portfolio}
        pool = pool[~pool.index.isin(held)]

        # 打分
        scores = []
        for code, r in pool.iterrows():
            scores.append((code, score_v9(r, idx_pct)))
        pool = pool.copy()
        for code, s in scores:
            pool.at[code, "score"] = s

        pool = pool[pool["score"] >= MIN_SCORE]
        if len(pool) == 0:
            continue

        top = pool.nlargest(min(TOP_N, available), "score")
        per_slot = cash * 0.33 / max(available, 1)

        for code, sr in top.iterrows():
            price = sr["close"]
            shares = int(per_slot / price / 100) * 100
            if shares < 100:
                continue
            cost = shares * price
            if cost > cash:
                continue
            cash -= cost
            portfolio.append({"code": code, "buy_date": dt, "buy_price": price, "shares": shares})
            trades.append({"date": dt, "code": code, "action": "BUY",
                           "price": price, "pnl_pct": 0, "reason": f"score={sr['score']:.0f}", "days": 0})

    # 最终清仓
    final_dt = dates[-1]
    final_day = df[df.trade_date == final_dt].set_index("ts_code")
    for pos in portfolio:
        if pos["code"] in final_day.index:
            cur = final_day.loc[pos["code"], "close"]
            cash += pos["shares"] * cur
            trades.append({"date": final_dt, "code": pos["code"], "action": "SELL",
                           "price": cur, "pnl_pct": (cur / pos["buy_price"] - 1) * 100,
                           "reason": "清仓", "days": (final_dt - pos["buy_date"]).days})
    portfolio.clear()

    total_ret = (cash / initial - 1) * 100

    # ---- 报告 ----
    td = pd.DataFrame(trades)
    if len(td) == 0:
        print("无交易")
        return

    buys = td[td.action == "BUY"]
    sells = td[td.action == "SELL"]

    print(f"\n{'='*60}")
    print(f"策略B 实时扫描 回测结果 ({START_DATE} ~ {END_DATE})")
    print(f"{'='*60}")
    print(f"买入: {len(buys)}笔  卖出: {len(sells)}笔")
    print(f"资金: {initial:.0f} → {cash:.0f}  ({total_ret:+.1f}%)")

    if len(sells) > 0:
        wr = (sells["pnl_pct"] > 0).mean() * 100
        avg = sells["pnl_pct"].mean()
        total = sells["pnl_pct"].sum()
        avg_days = sells["days"].mean()
        print(f"胜率: {wr:.1f}%  均收益: {avg:+.2f}%  累计: {total:+.1f}%  均持仓: {avg_days:.1f}天")

        for reason in ["止盈", "止损", "超时", "清仓"]:
            sub = sells[sells.reason == reason]
            if len(sub):
                print(f"  {reason}: {len(sub)}笔  AVG{sub['pnl_pct'].mean():+.2f}%")

    # 按季度
    td["date"] = pd.to_datetime(td["date"])
    td["quarter"] = td["date"].dt.to_period("Q")
    print("\n季度表现:")
    for q, g in td[td.action == "SELL"].groupby("quarter"):
        print(f"  {q}: {len(g):3d}笔  累计{g['pnl_pct'].sum():+.1f}%  胜率{(g['pnl_pct']>0).mean()*100:.0f}%")


if __name__ == "__main__":
    df = load_data()
    backtest(df)
