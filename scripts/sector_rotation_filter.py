#!/usr/bin/env python3
"""
行业轮动策略层：在 V4 候选池 → ML 排序之间插入行业分散约束

功能：
1. 计算各行业的综合评分（动量 + 资金流 + 北向）
2. 限制 Top5 候选股中单一行业最多 2 只
3. 如果某行业候选过多，只保留该行业评分最高的 2 只

用法：被 strategy_service.generate_v4_ml_candidates() 调用
"""
import logging
import pandas as pd
import pymysql
from datetime import datetime, timedelta
import numpy as np

logger = logging.getLogger(__name__)

# 行业最大集中度（Top5 中单一行业不超过此数）
MAX_STOCKS_PER_SECTOR = 2

# 行业评分权重
SECTOR_MOMENTUM_WEIGHT = 0.4    # 行业动量权重
SECTOR_FLOW_WEIGHT = 0.4        # 行业资金流权重
SECTOR_NORTH_WEIGHT = 0.2       # 北向资金行业配置权重


def score_sectors(conn, trade_date):
    """
    计算行业评分（动量 + 资金流 + 北向配置）
    返回: {行业名: 综合评分}
    """
    scores = {}
    date_str = str(trade_date)[:10] if trade_date else datetime.now().strftime('%Y-%m-%d')

    # 1. 行业动量评分（最近5日板块涨幅）
    try:
        ind_mom = pd.read_sql(f"""
            SELECT board_name, AVG(pct_change) as mom_5d
            FROM board_industry_hist
            WHERE trade_date < '{date_str}'
              AND trade_date >= DATE_SUB('{date_str}', INTERVAL 10 DAY)
            GROUP BY board_name
            ORDER BY mom_5d DESC
        """, conn)
        if not ind_mom.empty:
            max_mom = ind_mom['mom_5d'].max()
            min_mom = ind_mom['mom_5d'].min()
            for _, row in ind_mom.iterrows():
                sector = row['board_name']
                # 归一化到 0~1
                mom_range = max_mom - min_mom
                if mom_range > 0:
                    mom_score = (row['mom_5d'] - min_mom) / mom_range
                else:
                    mom_score = 0.5
                scores[sector] = scores.get(sector, 0) + mom_score * SECTOR_MOMENTUM_WEIGHT * 100
    except Exception as e:
        logger.warning(f"行业动量评分失败: {e}")

    # 2. 行业资金流评分（最近5日主力净流入）
    try:
        sec_flow = pd.read_sql(f"""
            SELECT sector_name, SUM(net_amount) as net_5d
            FROM sector_moneyflow
            WHERE trade_date < '{date_str}'
              AND trade_date >= DATE_SUB('{date_str}', INTERVAL 10 DAY)
            GROUP BY sector_name
        """, conn)
        if not sec_flow.empty:
            max_flow = sec_flow['net_5d'].max()
            min_flow = sec_flow['net_5d'].min()
            for _, row in sec_flow.iterrows():
                sector = row['sector_name']
                flow_range = max_flow - min_flow
                if flow_range > 0:
                    flow_score = (row['net_5d'] - min_flow) / flow_range
                else:
                    flow_score = 0.5
                scores[sector] = scores.get(sector, 0) + flow_score * SECTOR_FLOW_WEIGHT * 100
    except Exception as e:
        logger.warning(f"行业资金流评分失败: {e}")

    return scores


def apply_sector_diversification(candidates, conn, trade_date):
    """
    对候选股列表应用行业分散约束
    candidates: [{'ts_code':..., 'name':..., 'industry':..., 'blend':...}, ...]
    返回: 行业分散后的候选列表
    """
    if not candidates:
        return candidates

    # 获取行业评分
    sector_scores = score_sectors(conn, trade_date)

    # 按行业分组，每组内按混合评分排序
    sector_groups = {}
    for c in candidates:
        ind = c.get('industry', 'OTHER')
        if ind not in sector_groups:
            sector_groups[ind] = []
        sector_groups[ind].append(c)

    # 每个行业最多保留 MAX_STOCKS_PER_SECTOR 只
    result = []
    for ind, stocks in sector_groups.items():
        # 按混合评分降序
        stocks.sort(key=lambda x: x.get('blend', 0), reverse=True)
        result.extend(stocks[:MAX_STOCKS_PER_SECTOR])

    # 按混合评分重新排序
    result.sort(key=lambda x: x.get('blend', 0), reverse=True)

    logger.info(f"行业分散: {len(candidates)} → {len(result)} 只候选 "
                f"(每行业最多{MAX_STOCKS_PER_SECTOR}只)")
    return result
