#!/usr/bin/env python3
"""
综合策略回测 — 对比6种方案（静默版）
"""
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
os.environ['PYTHONWARNINGS'] = 'ignore'

import numpy as np
import pymysql

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.services.strategy_service import _v4_score_single
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
START_DATE, END_DATE = "2024-11-01", "2026-05-08"
SAMPLE_INTERVAL = 5
TOP_N = 3
OUT_PATH = os.path.join(BASE_DIR, "data", "backtest_all_strategies.json")


def get_trade_dates(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date", (START_DATE, END_DATE))
    return sorted([r[0].strftime('%Y-%m-%d') for r in cur.fetchall()])


def get_top_vol_yesterday(conn, date_str, n=500):
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (date_str,))
    prev_date = cur.fetchone()[0].strftime('%Y-%m-%d')
    cur.execute("""
        SELECT ts_code FROM daily_price
        WHERE trade_date = %s AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200 ORDER BY amount DESC LIMIT %s
    """, (prev_date, n))
    return [r[0] for r in cur.fetchall()]


def forward_return_clean(conn, code, buy_date_str, hold=5):
    cur = conn.cursor()
    cur.execute("""
        SELECT pct_chg FROM daily_price
        WHERE ts_code = %s AND trade_date > %s
        ORDER BY trade_date LIMIT %s
    """, (code, buy_date_str, hold))
    rets = np.array([r[0] for r in cur.fetchall() if r[0] is not None], dtype=float) / 100.0
    if len(rets) < 2:
        return None
    return float((1 + rets[:hold]).prod() - 1) * 100


def load_bundle():
    from quant_app.utils.model_loader import get_model_path
    from scripts.predict_v11 import load_v11_model
    mp = get_model_path("v11.0")
    if not mp or not os.path.exists(mp):
        mp = "data/ml_stock_model_v11_0.pkl"
    return load_v11_model(mp)


def main():
    bundle = load_bundle()
    version = bundle.get("version", "v11.0")
    print(f"模型: {version}, {bundle.get('n_features', '?')}特征")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > all_dates[5]]
    print(f"交易日: {len(all_dates)}, 采样日: {len(sample_dates)}")

    from ml_predict import _ensemble_predict
    from scripts.predict_v11 import build_features_v11_inference

    results = {k: [] for k in ["1_纯ML基线", "2_位置过滤", "3_V4_ML", "4_动量惩罚", "5_短持有2日", "6_低波择时"]}

    for di, buy_date in enumerate(sample_dates):
        if (di + 1) % 5 == 0:
            print(f"进度: {di+1}/{len(sample_dates)}")

        vol_codes = get_top_vol_yesterday(conn, buy_date, 500)
        if len(vol_codes) < 100:
            continue

        try:
            feat = build_features_v11_inference(conn, vol_codes, as_of_date=buy_date)
        except Exception:
            continue
        if feat is None or feat.empty or len(feat) < 50:
            continue

        v80f = bundle["feature_cols"]
        medians = bundle.get("global_medians", {})
        for col in v80f:
            if col not in feat.columns:
                feat[col] = medians.get(col, 0.0)
        feat = feat.fillna(0)
        ml_preds = _ensemble_predict(feat, bundle)
        codes = feat["ts_code"].tolist()
        ml_map = dict(zip(codes, ml_preds))

        # 策略1: 纯ML基线
        ranked = sorted(zip(codes, ml_preds), key=lambda x: -x[1])
        s1 = [c for c, _ in ranked[:TOP_N]]

        # 策略2: 位置过滤 pos_52w < 0.8
        s2 = []
        pf = [(r["ts_code"], ml_map.get(r["ts_code"], -999)) for _, r in feat.iterrows()
              if float(r.get("pos_52w", 1.0)) < 0.8]
        pf.sort(key=lambda x: -x[1])
        s2 = [c for c, _ in pf[:TOP_N]]

        # 策略3: V4+ML
        s3 = []
        v4p = [(r["ts_code"], _v4_score_single(r)) for _, r in feat.iterrows()]
        v4p = [(c, s) for c, s in v4p if s >= 0]
        v4p.sort(key=lambda x: -x[1])
        v4t30 = [c for c, _ in v4p[:30]]
        if v4t30:
            v4s = {c: ml_map[c] for c in v4t30 if c in ml_map}
            s3 = [c for c, _ in sorted(v4s.items(), key=lambda x: -x[1])[:TOP_N]]

        # 策略4: 动量惩罚
        s4 = []
        adj = {}
        for _, r in feat.iterrows():
            tc = r["ts_code"]
            ms = ml_map.get(tc, -999)
            mom = max(0, float(r.get("chg_20d", 0))) * 0.3 + max(0, float(r.get("pos_52w", 0.5)) - 0.6) * 0.5
            adj[tc] = ms - mom
        s4 = [c for c, _ in sorted(adj.items(), key=lambda x: -x[1])[:TOP_N]]

        # 策略5: 短持有2日
        s5 = s1[:]

        # 策略6: 低波择时
        cur = conn.cursor()
        cur.execute("""
            SELECT change_pct FROM market_index_daily
            WHERE index_code='000001.SH' AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 30
        """, (buy_date,))
        chgs = np.array([r[0] for r in cur.fetchall() if r[0] is not None], dtype=float)
        regime = 0
        if len(chgs) >= 20:
            v20 = np.std(chgs[-20:]) * np.sqrt(20)
            all_vol = np.std(chgs) * np.sqrt(20) if len(chgs) > 1 else v20
            if v20 < all_vol * 0.8:
                regime = 1
            elif v20 > all_vol * 1.2:
                regime = -1
        s6 = s1[:] if regime >= 0 else []

        # 计算收益
        strategies = [("1_纯ML基线", s1, results["1_纯ML基线"], 5),
                      ("2_位置过滤", s2, results["2_位置过滤"], 5),
                      ("3_V4_ML", s3, results["3_V4_ML"], 5),
                      ("4_动量惩罚", s4, results["4_动量惩罚"], 5),
                      ("5_短持有2日", s5, results["5_短持有2日"], 2),
                      ("6_低波择时", s6, results["6_低波择时"], 5)]
        for _, tl, st, h in strategies:
            rets = [forward_return_clean(conn, tc, buy_date, h) for tc in tl]
            rets = [r for r in rets if r is not None]
            if rets:
                st.append({"date": buy_date, "avg_ret": round(float(np.mean(rets)), 2), "n": len(rets)})

    conn.close()

    # 输出结果
    print(f"\n{'='*60}")
    print(f"综合策略回测（{START_DATE} ~ {END_DATE}）")
    print(f"{'='*60}")
    print(f"模型: {version}, Top{TOP_N}")
    print()

    summary = []
    for label, store in results.items():
        if not store:
            print(f"  {label}: 无交易")
            continue
        rets = np.array([r["avg_ret"] for r in store])
        wins = int((rets > 0).sum())
        total = len(rets)
        cum = float((1 + rets / 100).prod() - 1) * 100
        avg = float(rets.mean())
        std = float(rets.std())
        sharpe = float(avg / std * np.sqrt(252 / 5)) if std > 0 else 0
        cum_s = (1 + rets / 100).cumprod()
        dd = float(((cum_s / np.maximum.accumulate(cum_s)) - 1).min() * 100)
        print(f"  {label}:")
        print(f"    交易: {total}次, 累积: {cum:+.2f}%, 均值: {avg:+.2f}%")
        print(f"    胜率: {wins/total*100:.1f}%, 夏普: {sharpe:.2f}, 最大回撤: {dd:.2f}%")
        print()
        summary.append({"strategy": label, "trades": total, "cum_return": round(cum, 2),
                        "avg_return": round(avg, 2), "win_rate": round(wins/total*100, 1),
                        "sharpe": round(sharpe, 2), "max_drawdown": round(dd, 2)})

    print(f"{'='*60}")
    print("按夏普排序:")
    for i, s in enumerate(sorted(summary, key=lambda x: -x["sharpe"]), 1):
        print(f"  #{i} {s['strategy']}: 夏普={s['sharpe']:.2f}, 累积={s['cum_return']:+.2f}%")

    # 保存
    with open(OUT_PATH, "w") as f:
        json.dump({"model": version, "params": {"start": START_DATE, "end": END_DATE,
                   "interval": SAMPLE_INTERVAL, "top_n": TOP_N},
                   "summary": summary, "details": results}, f, indent=2, default=str)
    print(f"\n结果保存: {OUT_PATH}")


if __name__ == "__main__":
    main()
