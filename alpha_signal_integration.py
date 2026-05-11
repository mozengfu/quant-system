#!/usr/bin/env python3
"""
Alpha 信号集成模块
读取 alpha_signals 表，为模型预测结果增加“Alpha 增强因子”
"""

import os
import pymysql
from datetime import datetime

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

def get_alpha_boost_map(date=None):
    """
    获取指定日期的 Alpha 信号增强因子映射
    返回: {ts_code: boost_value}
    """
    if not date:
        date = datetime.now().strftime('%Y-%m-%d')
    
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT ts_code, MAX(score_boost) as max_boost 
            FROM alpha_signals 
            WHERE signal_date = %s 
            GROUP BY ts_code
        """, (date,))
        
        result = {}
        for ts_code, boost in cur.fetchall():
            # boost 范围 -1.0 到 +1.0
            result[ts_code] = float(boost)
        return result
    finally:
        cur.close()
        conn.close()

def apply_alpha_boost(candidates, boost_map, scale_factor=10.0):
    """
    将 Alpha 信号应用到候选股池中
    candidates: list of dict (包含 ts_code)
    boost_map: {ts_code: boost_value}
    scale_factor: 将 boost (-1~1) 映射到得分的放大倍数 (例如 10 分)
    """
    for c in candidates:
        code = c['ts_code']
        if code in boost_map:
            boost_val = boost_map[code]
            # Alpha 增强分 = boost * scale_factor (即 -10 到 +10 分)
            c['alpha_boost_score'] = round(boost_val * scale_factor, 1)
            c['alpha_signal'] = True
            # 累加到总分 (如果是 ml_daily_top5.py 的 candidates)
            # 注意：如果是 ml_daily_top5.py，c 里已经有 ml_score, rank_pct 等
            # 如果是 ml_predict.py 的 df，处理方式不同
            
            # 为了通用性，我们只加一个字段，让调用方决定如何加
            # 但对于 ml_daily_top5.py，我们可以直接改 total_score
            if 'total_score' in c:
                c['total_score'] += c['alpha_boost_score']
        else:
            c['alpha_boost_score'] = 0
            c['alpha_signal'] = False
            
    return candidates
