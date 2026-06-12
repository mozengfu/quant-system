"""游资 5 因子评分模块。

判断股票是否处于游资出货阶段（≥40 分排除）：
1. 连板（≥4 板 30 分，3 板 15 分）
2. 封单萎缩 50%+（30 分）
3. 先涨停再跌停（20 分）
4. 高换手（avg > 20% 15 分，avg > 15% + max > 25% 10 分）
5. 主力资金流出（<-3000 万 15 分）

移植自 quant_app/services/strategy_service.py (lines ~2480-2625)。
"""

import logging

import pymysql

logger = logging.getLogger(__name__)

# 排除阈值
HOT_MONEY_THRESHOLD = 40


def score_hot_money(conn: pymysql.Connection, ts_codes: list[str], trade_date: str) -> dict[str, dict]:
    """批量计算游资 5 因子评分。

    Args:
        conn: 数据库连接
        ts_codes: 股票代码列表
        trade_date: 交易日期 (YYYY-MM-DD)

    Returns:
        {ts_code: {"score": int, "reasons": [str], "excluded": bool}, ...}
    """
    if not ts_codes:
        return {}

    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(ts_codes))

    try:
        # 1. 连板数据（60 天内最高连板）
        cur.execute(
            f"""
            SELECT ts_code, COALESCE(MAX(last_board), 0) as max_board
            FROM zt_pool
            WHERE ts_code IN ({placeholders})
              AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
              AND last_board > 0
            GROUP BY ts_code
            """,
            (*ts_codes, trade_date),
        )
        board_map = {r[0]: r[1] or 0 for r in cur.fetchall()}

        # 2. 封单数据（30 天内，按时间倒序）
        cur.execute(
            f"""
            SELECT ts_code, trade_date, seal_amount
            FROM zt_pool
            WHERE ts_code IN ({placeholders})
              AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
            ORDER BY ts_code, trade_date DESC
            """,
            (*ts_codes, trade_date),
        )
        seal_map: dict[str, list[float]] = {}
        for r in cur.fetchall():
            seal_map.setdefault(r[0], []).append(float(r[2] or 0))

        # 3. 15 日内涨跌停 + 时间顺序
        cur.execute(
            f"""
            SELECT ts_code,
                   SUM(CASE WHEN pct_chg >= 9.5 THEN 1 ELSE 0 END) as up_cnt,
                   SUM(CASE WHEN pct_chg <= -9.5 THEN 1 ELSE 0 END) as down_cnt,
                   MIN(CASE WHEN pct_chg >= 9.5 THEN trade_date END) as first_up,
                   MAX(CASE WHEN pct_chg <= -9.5 THEN trade_date END) as last_down
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date >= DATE_SUB(%s, INTERVAL 15 DAY)
              AND trade_date <= %s
            GROUP BY ts_code
            """,
            (*ts_codes, trade_date, trade_date),
        )
        ud_map = {}
        for r in cur.fetchall():
            ud_map[r[0]] = {
                "up": r[1] or 0,
                "down": r[2] or 0,
                "first_up": r[3],
                "last_down": r[4],
            }

        # 4. 换手率（10 天）
        cur.execute(
            f"""
            SELECT ts_code, AVG(turnover_rate) as avg_tr, MAX(turnover_rate) as max_tr
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
              AND trade_date <= %s
            GROUP BY ts_code
            """,
            (*ts_codes, trade_date, trade_date),
        )
        tr_map = {}
        for r in cur.fetchall():
            tr_map[r[0]] = {"avg": r[1] or 0, "max": r[2] or 0}

        # 5. 主力资金（10 天累计）
        cur.execute(
            f"""
            SELECT ts_code, COALESCE(SUM(main_net), 0) as total
            FROM moneyflow_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
            GROUP BY ts_code
            """,
            (*ts_codes, trade_date),
        )
        main_map = {r[0]: r[1] or 0 for r in cur.fetchall()}

    finally:
        cur.close()

    # 计算评分
    results: dict[str, dict] = {}
    for tc in ts_codes:
        score = 0
        reasons: list[str] = []

        # ① 连板
        board = board_map.get(tc, 0)
        if board >= 4:
            score += 30
            reasons.append(f"高连板{board}")
        elif board == 3:
            score += 15
            reasons.append("连板3次")

        # ② 封单萎缩
        seals = seal_map.get(tc, [])
        if len(seals) >= 2 and seals[1] > 0 and seals[0] < seals[1] * 0.5:
            ratio = int((1 - seals[0] / seals[1]) * 100)
            score += 30
            reasons.append(f"封单萎缩{ratio}%")

        # ③ 先涨停再跌停
        ud = ud_map.get(tc, {})
        if ud.get("up", 0) > 0 and ud.get("down", 0) > 0:
            fu = ud.get("first_up")
            ld = ud.get("last_down")
            if fu and ld and fu < ld:
                score += 20
                reasons.append("涨停后跌停")

        # ④ 高换手
        avg_tr = tr_map.get(tc, {}).get("avg", 0)
        max_tr = tr_map.get(tc, {}).get("max", 0)
        if avg_tr > 20:
            score += 15
            reasons.append(f"高换手{int(avg_tr)}%")
        elif avg_tr > 15 and max_tr > 25:
            score += 10
            reasons.append("换手异常")

        # ⑤ 主力资金流出
        main_net = main_map.get(tc, 0)
        if main_net < -30_000_000:
            score += 15
            reasons.append("主力流出")

        results[tc] = {
            "score": score,
            "reasons": reasons,
            "excluded": score >= HOT_MONEY_THRESHOLD,
        }

    return results


def filter_hot_money(
    candidates: list[dict],
    conn: pymysql.Connection,
    trade_date: str,
) -> list[dict]:
    """对候选列表执行游资出货过滤（≥40 分排除）。

    Args:
        candidates: 候选列表，每个 dict 包含 ts_code, name
        conn: 数据库连接
        trade_date: 交易日期 (YYYY-MM-DD)

    Returns:
        过滤后的候选列表
    """
    if not candidates:
        return candidates

    ts_codes = [c["ts_code"] for c in candidates]
    scores = score_hot_money(conn, ts_codes, trade_date)

    excluded = set()
    for c in candidates:
        info = scores.get(c["ts_code"], {"score": 0, "reasons": [], "excluded": False})
        if info["excluded"]:
            excluded.add(c["ts_code"])
            c["risk_filtered"] = True
            c["risk_reason"] = "游资出货: " + "; ".join(info["reasons"]) + f"({info['score']}分)"
            logger.info(
                "游资排除: %s(%s) %d分 %s",
                c["name"],
                c["ts_code"],
                info["score"],
                "; ".join(info["reasons"]),
            )

    return [c for c in candidates if c["ts_code"] not in excluded]
