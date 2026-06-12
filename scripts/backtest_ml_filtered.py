#!/usr/bin/env python3
"""
ML初筛 + 多条件过滤 回测（V11直接推理）

过滤条件：
1. 当日涨幅 0~3%
2. 量比 0.8~3.0
3. 非连续3日下跌
4. 换手率 1~15%
"""

import json
import logging
import os
import sys
import warnings

import numpy as np
import pandas as pd
import pymysql

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.config import get_db_config
from quant_app.utils.model_loader import get_model_path

DB_CONFIG = get_db_config()
START_DATE = "2025-06-01"
END_DATE = "2026-06-04"
SAMPLE_INTERVAL = 5
TOP_N = 3
HOLD_DAYS = 5
POOL_SIZE = 200

OUT_PATH = os.path.join(BASE_DIR, "data", "backtest_ml_filtered.json")

def get_trade_dates(conn):
    df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        conn, params=(START_DATE, END_DATE))
    return sorted(df["trade_date"].astype(str).tolist())

def get_top_vol_prev(conn, date_str, n=500):
    prev = pd.read_sql("SELECT MAX(trade_date) FROM daily_price WHERE trade_date<%s", conn, params=(date_str,))
    prev_date = str(prev.iloc[0,0])
    df = pd.read_sql("""SELECT ts_code FROM daily_price
        WHERE trade_date=%s AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200
        ORDER BY amount DESC LIMIT %s""", conn, params=(prev_date, n))
    return df["ts_code"].tolist()

def get_consecutive_down(conn, code, date_str, days=3):
    df = pd.read_sql("""SELECT pct_chg FROM daily_price
        WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date DESC LIMIT %s""",
        conn, params=(code, date_str, days))
    if len(df) < days: return False
    return (df["pct_chg"] < 0).all()

def forward_return(conn, code, buy_date, hold=5):
    df = pd.read_sql("""SELECT pct_chg FROM daily_price
        WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s""",
        conn, params=(code, buy_date, hold))
    if len(df) < 2: return None
    rets = df["pct_chg"].iloc[:hold].values / 100.0
    rets = rets[~np.isnan(rets)]
    if len(rets) == 0: return None
    return float((1 + rets).prod() - 1) * 100

def apply_filters(conn, df, trade_date):
    filtered = df.copy()

    # 1. 涨幅 0~3%
    m = (filtered["pct_chg"] >= 0) & (filtered["pct_chg"] <= 3)
    filtered = filtered[m]
    if len(filtered) < TOP_N * 2: return df

    # 2. 量比 0.8~3
    m = (filtered["volume_ratio"] >= 0.8) & (filtered["volume_ratio"] <= 3.0)
    filtered = filtered[m]
    if len(filtered) < TOP_N * 2: return df

    # 3. 换手率 1~15%
    m = (filtered["turnover_rate"] >= 1.0) & (filtered["turnover_rate"] <= 15.0)
    filtered = filtered[m]
    if len(filtered) < TOP_N * 2: return df

    # 4. 非连续3日跌
    keep = []
    for _, row in filtered.iterrows():
        if not get_consecutive_down(conn, row["ts_code"], trade_date):
            keep.append(row)
    if len(keep) >= TOP_N:
        return pd.DataFrame(keep)
    return filtered

def summarize(label, results):
    if not results: return f"  {label}: 无交易"
    rets = np.array([r["avg_ret"] for r in results])
    wins, total = int((rets > 0).sum()), len(rets)
    cum = float((1 + rets/100).prod() - 1) * 100
    avg, std = float(rets.mean()), float(rets.std())
    sharpe = float(avg/std * np.sqrt(252/HOLD_DAYS)) if std > 0 else 0
    return (f"  {label}: 采样{total}次  累积{cum:+.1f}%  均值{avg:+.2f}%  "
            f"胜率{wins/total*100:.0f}%  夏普{sharpe:.2f}")

def main():
    from ml_predict import _ensemble_predict
    from scripts.predict_v11 import build_features_v11_inference, load_v11_model

    logger.info("加载V11.0模型...")
    bundle = load_v11_model(str(get_model_path("v11.0")))
    logger.info(f"V11.0: {bundle.get('n_models','?')}子模型, {bundle.get('n_features','?')}特征")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > all_dates[10]]
    logger.info(f"交易日:{len(all_dates)} 采样日:{len(sample_dates)}")

    pure_results, filtered_results = [], []

    for di, trade_date in enumerate(sample_dates):
        codes = get_top_vol_prev(conn, trade_date, POOL_SIZE)
        if len(codes) < 50: continue

        # V11 特征+预测
        try:
            feat = build_features_v11_inference(conn, codes, as_of_date=trade_date)
            if feat is None or len(feat) < 30: continue
            for c in bundle['feature_cols']:
                if c not in feat.columns: feat[c] = bundle.get('global_medians',{}).get(c, 0)
            preds = _ensemble_predict(feat.fillna(0), bundle)
            feat['ml_score'] = preds
        except Exception:
            continue

        # 获取当日行情
        ph = ",".join(["%s"]*len(codes))
        info = pd.read_sql(f"""SELECT ts_code,close,pct_chg,volume_ratio,turnover_rate
            FROM daily_price WHERE trade_date=%s AND ts_code IN ({ph})""",
            conn, params=(trade_date,)+tuple(codes))

        merged = feat[['ts_code','ml_score']].merge(info, on='ts_code', how='inner')
        if len(merged) < TOP_N * 3: continue
        merged = merged.sort_values('ml_score', ascending=False)

        # 纯ML TopN
        pure_codes = merged.head(TOP_N)['ts_code'].tolist()

        # ML初筛Top100 + 过滤
        top100 = merged.head(100)
        filtered = apply_filters(conn, top100, trade_date)
        filtered_codes = filtered.head(TOP_N)['ts_code'].tolist()

        for label, clist, store in [("纯ML", pure_codes, pure_results),
                                     ("ML+过滤", filtered_codes, filtered_results)]:
            rets = [fr for tc in clist if (fr := forward_return(conn, tc, trade_date, HOLD_DAYS)) is not None]
            if rets:
                store.append({"date": trade_date, "avg_ret": round(float(np.mean(rets)),2), "n": len(rets)})

        if (di+1) % 10 == 0:
            logger.info(f"进度:{di+1}/{len(sample_dates)} 纯ML:{len(pure_results)} 过滤:{len(filtered_results)}")

    conn.close()

    print(f"\n{'='*55}")
    print(f"ML初筛+过滤 回测 ({START_DATE} ~ {END_DATE})")
    print(f"V11.0 | 间隔{SAMPLE_INTERVAL}d | 持仓{HOLD_DAYS}d | Top{TOP_N} | 池{POOL_SIZE}")
    print(f"{'='*55}")
    print(summarize("纯ML", pure_results))
    print(summarize("ML+过滤", filtered_results))

    if pure_results and filtered_results:
        p = np.array([r["avg_ret"] for r in pure_results])
        f = np.array([r["avg_ret"] for r in filtered_results])
        n = min(len(p), len(f))
        diff = f[:n] - p[:n]
        print(f"\n  过滤增量: 均值{diff.mean():+.2f}% 胜出{(diff>0).sum()}/{n}")

    json.dump({"pure_ml": pure_results, "ml_filtered": filtered_results},
              open(OUT_PATH,"w"), indent=2, default=str)
    logger.info(f"结果: {OUT_PATH}")

if __name__ == "__main__":
    main()
