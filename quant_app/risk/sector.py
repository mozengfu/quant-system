"""行业分散约束模块。

限制选股结果中单一行业的集中度（每行业最多 2 只）。

移植自 scripts/sector_rotation_filter.py。
"""
import logging

import pandas as pd
import pymysql

logger = logging.getLogger(__name__)

# 行业最大集中度（推荐列表中单一行业不超过此数）
MAX_STOCKS_PER_SECTOR = 2

# 行业评分权重
SECTOR_MOMENTUM_WEIGHT = 0.4
SECTOR_FLOW_WEIGHT = 0.4
SECTOR_NORTH_WEIGHT = 0.2


def score_sectors(conn: pymysql.Connection, trade_date: str) -> dict[str, float]:
    """计算行业综合评分（动量 + 资金流）。

    Returns:
        {行业名: 综合评分}
    """
    scores: dict[str, float] = {}
    date_str = trade_date[:10] if len(trade_date) > 8 else trade_date

    # 行业动量评分（最近 10 日板块涨幅）
    try:
        ind_mom = pd.read_sql(
            """
            SELECT board_name, AVG(pct_change) as mom_10d
            FROM board_industry_hist
            WHERE trade_date < :d AND trade_date >= DATE_SUB(:d, INTERVAL 10 DAY)
            GROUP BY board_name
            """,
            conn,
            params={"d": date_str},
        )
        if not ind_mom.empty:
            mom_range = ind_mom["mom_10d"].max() - ind_mom["mom_10d"].min()
            for _, row in ind_mom.iterrows():
                sector = row["board_name"]
                mom_score = (
                    (row["mom_10d"] - ind_mom["mom_10d"].min()) / mom_range
                    if mom_range > 0
                    else 0.5
                )
                scores[sector] = scores.get(sector, 0) + mom_score * SECTOR_MOMENTUM_WEIGHT * 100
    except Exception as e:
        logger.warning("行业动量评分失败: %s", e)

    # 行业资金流评分
    try:
        sec_flow = pd.read_sql(
            """
            SELECT sector_name, SUM(net_amount) as net_10d
            FROM sector_moneyflow
            WHERE trade_date < :d AND trade_date >= DATE_SUB(:d, INTERVAL 10 DAY)
            GROUP BY sector_name
            """,
            conn,
            params={"d": date_str},
        )
        if not sec_flow.empty:
            flow_range = sec_flow["net_10d"].max() - sec_flow["net_10d"].min()
            for _, row in sec_flow.iterrows():
                sector = row["sector_name"]
                flow_score = (
                    (row["net_10d"] - sec_flow["net_10d"].min()) / flow_range
                    if flow_range > 0
                    else 0.5
                )
                scores[sector] = scores.get(sector, 0) + flow_score * SECTOR_FLOW_WEIGHT * 100
    except Exception as e:
        logger.warning("行业资金流评分失败: %s", e)

    return scores


def apply_sector_diversification(
    candidates: list[dict],
    max_per_sector: int = MAX_STOCKS_PER_SECTOR,
) -> list[dict]:
    """对候选列表应用行业分散约束。

    Args:
        candidates: 候选列表，每个 dict 包含 ts_code, industry, blended_score
        max_per_sector: 单一行业最多保留的股票数

    Returns:
        行业分散后的候选列表
    """
    if not candidates:
        return candidates

    # 按行业分组
    sector_groups: dict[str, list[dict]] = {}
    for c in candidates:
        ind = c.get("industry", "OTHER")
        sector_groups.setdefault(ind, []).append(c)

    # 每个行业最多保留 max_per_sector 只（按 blended_score 排序）
    result = []
    for stocks in sector_groups.values():
        stocks.sort(key=lambda x: x.get("blended_score", 0), reverse=True)
        result.extend(stocks[:max_per_sector])

    # 按混合评分重新排序
    result.sort(key=lambda x: x.get("blended_score", 0), reverse=True)

    logger.info(
        "行业分散: %d → %d 只候选（每行业最多 %d 只）",
        len(candidates), len(result), max_per_sector,
    )
    return result
