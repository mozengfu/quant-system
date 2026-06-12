#!/usr/bin/env python3
"""
完整管线回测：成交额Top300 → V11.0 → ML正分 → 风控 → 游资 → 业绩 → 行业分散
对比: ①纯ML(无过滤) ②ML正分过滤 ③完整管线
"""

import json
import logging
import os
import sys

import numpy as np
import pandas as pd
import pymysql

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from ml_predict import _build_features_for_stocks_v8_6, _ensemble_predict, _load_best_model
from quant_app.services.strategy_service import unified_market_state
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
START_DATE, END_DATE = "2024-11-01", "2026-05-08"
SAMPLE_INTERVAL = 5
TOP_N = 3
HOLD_DAYS = 5
TOP_POOL = 300
OUT_PATH = os.path.join(BASE_DIR, "data", "backtest_current_pipeline.json")


def get_trade_dates(conn):
    df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date",
        conn,
        params=(START_DATE, END_DATE),
    )
    return sorted(df["trade_date"].astype(str).tolist())


def get_top_vol_yesterday(conn, date_str, n=TOP_POOL):
    prev = pd.read_sql("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", conn, params=(date_str,))
    prev_date = str(prev.iloc[0, 0])
    df = pd.read_sql(
        """
        SELECT ts_code, amount FROM daily_price WHERE trade_date = %s
          AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200
        ORDER BY amount DESC LIMIT %s""",
        conn,
        params=(prev_date, n),
    )
    return df["ts_code"].tolist()


def forward_return_clean(conn, code, buy_date_str, hold=5):
    df = pd.read_sql(
        """
        SELECT trade_date, pct_chg FROM daily_price WHERE ts_code = %s AND trade_date > %s
        ORDER BY trade_date LIMIT %s""",
        conn,
        params=(code, buy_date_str, hold),
    )
    if len(df) < 2:
        return None
    rets = df["pct_chg"].iloc[:hold].values / 100.0
    rets = rets[~np.isnan(rets)]
    return float((1 + rets).prod() - 1) * 100 if len(rets) > 0 else None


def filter_pipeline(conn, codes, scores, buy_date, feat_df):
    """
    完整生产管线过滤，返回通过过滤的股票列表。
    按生产代码逻辑复制。
    """
    display_date = buy_date
    passed = []
    for c in codes:
        s = scores.get(c, 0)
        passed.append(
            {
                "ts_code": c,
                "ml_score": round(s, 3),
                # 风控和游资过滤需要以下字段，从feat_df取
                "pct_chg": 0,
                "volume_ratio": 0,
                "rps_20": 0,
                "turnover_rate": 0,
                "close": 0,
                "high_52w": 0,
                "low_52w": 0,
                "main_net": 0,
            }
        )

    # 从feat_df补充字段
    for c in passed:
        tc = c["ts_code"]
        row = feat_df[feat_df["ts_code"] == tc]
        if not row.empty:
            r = row.iloc[0]
            c["close"] = float(r.get("close", 0) or 0)
            c["pct_chg"] = float(r.get("pct_chg", 0) or 0)
            c["volume_ratio"] = float(r.get("volume_ratio", 0) or 0)
            c["rps_20"] = float(r.get("rps_20", 0) or 0)
            c["turnover_rate"] = float(r.get("turnover_rate", 0) or 0)
            c["high_52w"] = float(r.get("high_52w", 0) or 0)
            c["low_52w"] = float(r.get("low_52w", 0) or 0)
            c["main_net"] = float(r.get("main_net", 0) or 0)

    # === ML正分过滤 ===
    passed = [c for c in passed if c["ml_score"] > 0]

    # === 风控过滤 ===
    try:
        ms = unified_market_state(conn)
        risk_state = ms.get("state", "range")
        tight = risk_state in ("trend_down", "panic", "overheated")
    except Exception:
        risk_state = "range"
        tight = False

    risk_passed = []
    for c in passed:
        pct, vr, rps = c["pct_chg"], c["volume_ratio"], c["rps_20"]
        h52w, l52w, close = c["high_52w"], c["low_52w"], c["close"]
        blocked = False
        # 涨停追高
        if pct > 9:
            blocked = True
        # 异常放量
        if pct > 5 and vr > 5:
            blocked = True
        # 弱市: 52周高位 + RPS过热
        if tight:
            if h52w > l52w > 0 and close > 0:
                pos = (close - l52w) / (h52w - l52w) * 100
                if pos > 85:
                    blocked = True
            if rps > 95 and pct > 4:
                blocked = True
        if not blocked:
            risk_passed.append(c)
    passed = risk_passed

    # === 游资收割票排除（简化版：连板/封单萎缩/涨停后跌停/高换手/主力流出）===
    try:
        cur = conn.cursor()
        hm_codes = [c["ts_code"] for c in passed]
        if hm_codes:
            ph = ",".join(["%s"] * len(hm_codes))
            # 连板
            cur.execute(
                f"SELECT ts_code, COALESCE(MAX(last_board),0) FROM zt_pool WHERE ts_code IN ({ph}) AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY) AND last_board>0 GROUP BY ts_code",
                (*hm_codes, display_date),
            )
            board_map = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            # 封单萎缩
            cur.execute(
                f"SELECT ts_code, trade_date, seal_amount FROM zt_pool WHERE ts_code IN ({ph}) AND trade_date>=DATE_SUB(%s, INTERVAL 30 DAY) ORDER BY ts_code,trade_date DESC",
                (*hm_codes, display_date),
            )
            seal_map = {}
            for r in cur.fetchall():
                seal_map.setdefault(r[0], []).append(float(r[2] or 0))
            # 15日内涨停+跌停
            cur.execute(
                f"SELECT ts_code, SUM(CASE WHEN pct_chg>=9.5 THEN 1 ELSE 0 END), SUM(CASE WHEN pct_chg<=-9.5 THEN 1 ELSE 0 END), MIN(CASE WHEN pct_chg>=9.5 THEN trade_date END), MAX(CASE WHEN pct_chg<=-9.5 THEN trade_date END) FROM daily_price WHERE ts_code IN ({ph}) AND trade_date>=DATE_SUB(%s, INTERVAL 15 DAY) AND trade_date<=%s GROUP BY ts_code",
                (*hm_codes, display_date, display_date),
            )
            ud_map = {
                r[0]: {"up": r[1] or 0, "down": r[2] or 0, "first_up": r[3], "last_down": r[4]} for r in cur.fetchall()
            }
            # 10日换手
            cur.execute(
                f"SELECT ts_code, AVG(turnover_rate), MAX(turnover_rate) FROM daily_price WHERE ts_code IN ({ph}) AND trade_date>=DATE_SUB(%s, INTERVAL 10 DAY) AND trade_date<=%s GROUP BY ts_code",
                (*hm_codes, display_date, display_date),
            )
            tr_map = {r[0]: {"avg": float(r[1] or 0), "max": float(r[2] or 0)} for r in cur.fetchall()}
            # 10日主力
            cur.execute(
                f"SELECT ts_code, COALESCE(SUM(main_net),0) FROM moneyflow_daily WHERE ts_code IN ({ph}) AND trade_date>=DATE_SUB(%s, INTERVAL 10 DAY) GROUP BY ts_code",
                (*hm_codes, display_date),
            )
            main_map = {r[0]: float(r[1] or 0) for r in cur.fetchall()}

            hm_excluded = set()
            for c in passed:
                tc = c["ts_code"]
                sc = 0
                bd = board_map.get(tc, 0)
                if bd >= 4:
                    sc += 30
                elif bd == 3:
                    sc += 15
                seals = seal_map.get(tc, [])
                if len(seals) >= 2 and seals[1] > 0 and seals[0] < seals[1] * 0.5:
                    sc += 30
                ud = ud_map.get(tc, {})
                if ud.get("up", 0) > 0 and ud.get("down", 0) > 0:
                    fu, ld = ud.get("first_up"), ud.get("last_down")
                    if fu and ld and fu < ld:
                        sc += 20
                tr = tr_map.get(tc, {})
                avg_tr = tr.get("avg", 0)
                if avg_tr > 20:
                    sc += 15
                elif avg_tr > 15 and tr.get("max", 0) > 25:
                    sc += 10
                if main_map.get(tc, 0) < -30000000:
                    sc += 15
                if sc >= 40:
                    hm_excluded.add(tc)
            if hm_excluded:
                passed = [c for c in passed if c["ts_code"] not in hm_excluded]
        cur.close()
    except Exception:
        pass

    # === 业绩过滤 ===
    try:
        cur = conn.cursor()
        e_codes = [c["ts_code"] for c in passed]
        if e_codes:
            ph2 = ",".join(["%s"] * len(e_codes))
            cur.execute(
                f"SELECT e.ts_code, e.net_profit_yoy FROM earnings_report e WHERE e.ts_code IN ({ph2}) AND e.report_date=(SELECT MAX(e2.report_date) FROM earnings_report e2 WHERE e2.ts_code=e.ts_code)",
                e_codes,
            )
            bad = [r[0] for r in cur.fetchall() if r[1] and float(r[1]) < -30]
            if bad:
                passed = [c for c in passed if c["ts_code"] not in bad]
        cur.close()
    except Exception:
        pass

    # === 行业分散约束（简化版：单一行业最多2只）===
    try:
        cur = conn.cursor()
        s_codes = [c["ts_code"] for c in passed]
        if s_codes:
            ph3 = ",".join(["%s"] * len(s_codes))
            cur.execute(f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({ph3})", s_codes)
            ind_map = {r[0]: r[1] or "" for r in cur.fetchall()}
            for c in passed:
                c["industry"] = ind_map.get(c["ts_code"], "")
            seen = {}
            final = []
            for c in passed:
                ind = c.get("industry", "")
                if seen.get(ind, 0) >= 2:
                    continue
                seen[ind] = seen.get(ind, 0) + 1
                final.append(c)
            passed = final
        cur.close()
    except Exception:
        pass

    # 按ml_score降序
    passed.sort(key=lambda x: x["ml_score"], reverse=True)
    return passed


def main():
    bundle, version = _load_best_model()
    if not bundle:
        logger.error("模型加载失败")
        return
    logger.info(f"模型: {version}")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > all_dates[5]]
    logger.info(f"交易日: {len(all_dates)}, 采样日: {len(sample_dates)}")

    results = {"raw_ml": [], "ml_pos": [], "full_pipeline": []}
    stats = {
        "total": len(sample_dates),
        "has_pos": 0,
        "after_risk": 0,
        "after_youzi": 0,
        "after_earnings": 0,
        "has_reco": 0,
    }

    for di, buy_date in enumerate(sample_dates):
        if (di + 1) % 10 == 0:
            logger.info(f"进度: {di + 1}/{len(sample_dates)}")

        vol_codes = get_top_vol_yesterday(conn, buy_date, TOP_POOL)
        if len(vol_codes) < 50:
            continue

        try:
            feat = _build_features_for_stocks_v8_6(conn, vol_codes, as_of_date=buy_date)
        except Exception:
            continue
        if feat is None or feat.empty or len(feat) < 20:
            continue

        preds = _ensemble_predict(feat, bundle)
        codes = feat["ts_code"].tolist()
        score_map = dict(zip(codes, preds))

        # 策略1: 纯ML
        ranked = sorted(score_map.items(), key=lambda x: -x[1])
        raw_top = [c for c, _ in ranked[:TOP_N]]

        # 策略2: ML正分过滤
        pos_codes = [c for c, s in ranked if s > 0]
        if pos_codes:
            stats["has_pos"] += 1
        ml_pos_top = pos_codes[:TOP_N]

        # 策略3: 完整管线
        pipe_codes = filter_pipeline(conn, codes, score_map, buy_date, feat)
        if pipe_codes:
            stats["has_reco"] += 1
        pipe_top = [c["ts_code"] for c in pipe_codes[:TOP_N]]

        # 计算收益
        for label, top in [("raw_ml", raw_top), ("ml_pos", ml_pos_top), ("full_pipeline", pipe_top)]:
            rets = []
            for tc in top:
                fr = forward_return_clean(conn, tc, buy_date, HOLD_DAYS)
                if fr is not None:
                    rets.append(fr)
            if rets:
                results[label].append({"date": buy_date, "avg_ret": round(float(np.mean(rets)), 2), "n": len(rets)})

    conn.close()

    print(f"\n{'=' * 70}")
    print(f"  完整管线回测（{START_DATE} ~ {END_DATE}）")
    print(f"{'=' * 70}")
    print(f"  模型: {version} | 池: 成交额Top{TOP_POOL} | 持仓{HOLD_DAYS}天 | Top{TOP_N}")
    print(f"  采样: {len(sample_dates)}天 / {len(all_dates)}交易日")
    print()

    for label, display in [
        ("raw_ml", "① 纯ML(无过滤)"),
        ("ml_pos", "② ML正分过滤"),
        ("full_pipeline", "③ 完整管线(正分+风控+游资+业绩+行业)"),
    ]:
        store = results[label]
        if not store:
            continue
        rets = np.array([r["avg_ret"] for r in store])
        wins = int((rets > 0).sum())
        total = len(rets)
        cum = float((1 + rets / 100).prod() - 1) * 100
        avg = float(rets.mean())
        std = float(rets.std())
        sharpe = float(avg / std * np.sqrt(252 / HOLD_DAYS)) if std > 0 else 0
        print(f"  {display}:")
        print(f"    交易: {total}次 | 累积: {cum:+.2f}% | 单次: {avg:+.2f}%")
        print(f"    胜率: {wins / total * 100:.1f}% ({wins}W/{total - wins}L) | 夏普: {sharpe:.2f}")
        print()

    print(f"{'=' * 70}")
    print("  过滤统计:")
    print(f"{'=' * 70}")
    print(f"  采样日: {stats['total']}")
    print(f"  有正分股票: {stats['has_pos']} ({stats['has_pos'] / stats['total'] * 100:.1f}%)")
    print(f"  最终有推荐: {stats['has_reco']} ({stats['has_reco'] / stats['total'] * 100:.1f}%)")
    print()

    # 差异分析
    for l1, l2, name in [
        ("raw_ml", "ml_pos", "②比①"),
        ("ml_pos", "full_pipeline", "③比②"),
        ("raw_ml", "full_pipeline", "③比①"),
    ]:
        s1, s2 = results.get(l1, []), results.get(l2, [])
        if s1 and s2:
            d1 = {r["date"]: r["avg_ret"] for r in s1}
            d2 = {r["date"]: r["avg_ret"] for r in s2}
            common = sorted(set(d1) & set(d2))
            if common:
                diffs = [d2[d] - d1[d] for d in common]
                print(
                    f"  {name}: 共同{len(common)}天 | 均值差{np.mean(diffs):+.2f}% | 改进{(np.array(diffs) > 0).sum()}/{len(common)}"
                )

    output = {
        "model": version,
        "params": {
            "start": START_DATE,
            "end": END_DATE,
            "pool": TOP_POOL,
            "interval": SAMPLE_INTERVAL,
            "top_n": TOP_N,
            "hold_days": HOLD_DAYS,
        },
        "stats": stats,
        "raw_ml": results["raw_ml"],
        "ml_pos": results["ml_pos"],
        "full_pipeline": results["full_pipeline"],
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"结果已保存: {OUT_PATH}")


if __name__ == "__main__":
    main()
