#!/usr/bin/env python3
"""
方向三：板块轮动 + 资金持续性 — 识别主力持续流入的热点板块，在热点板块内选股
"""

import os
import logging
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

import pymysql
import numpy as np

logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()


def get_hot_sectors(top_n=8, lookback_days=5, db_conn=None):
    """
    获取当前热点板块（基于资金持续流入）

    一次SQL查询完成所有板块的连续流入天数计算，
    避免 N+1 问题（原来每个板块单独查一次，50板块=50次额外查询）
    """
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True

    try:
        cur = db_conn.cursor()

        # 最新交易日期
        cur.execute("SELECT MAX(trade_date) FROM sector_moneyflow")
        latest = cur.fetchone()[0]
        if not latest:
            return []

        # 一次查询获取所有板块的资金流明细（lookback_days+3天，用于趋势判断和量能分析）
        cur.execute("""
            SELECT sector_name, trade_date, net_amount, pct_change,
                   buy_elg_amount + sell_elg_amount as total_trade
            FROM sector_moneyflow
            WHERE trade_date >= DATE_SUB(%s, INTERVAL %s DAY)
              AND trade_date <= %s
            ORDER BY sector_name, trade_date DESC
        """, (latest, lookback_days + 3, latest))

        # 按板块分组，在内存中计算
        from collections import defaultdict
        sector_data = defaultdict(list)
        for row in cur.fetchall():
            sector_data[row[0]].append({
                'date': row[1],
                'net': float(row[2]) if row[2] else 0,
                'pct': float(row[3]) if row[3] else 0,
                'trade': float(row[4]) if row[4] else 0,  # 成交额
            })

        sectors = []
        for name, rows in sector_data.items():
            recent_rows = rows[:lookback_days]
            nets = [r['net'] for r in recent_rows]
            trades = [r['trade'] for r in recent_rows]

            days = len(rows)
            inflow_days = sum(1 for n in nets if n > 0)
            total_net = sum(nets)
            avg_pct = np.mean([r['pct'] for r in recent_rows]) if recent_rows else 0

            # 连续净流入天数
            continuous = 0
            for n in nets:
                if n > 0: continuous += 1
                else: break

            # 资金趋势判断
            if len(nets) >= 4:
                recent_avg = np.mean(nets[:3])
                older_avg = np.mean(nets[3:])
                if recent_avg > older_avg * 1.2: trend = 'accelerating'
                elif recent_avg < older_avg * 0.8: trend = 'weakening'
                else: trend = 'steady'
            else:
                trend = 'steady'

            # 量能趋势：最近3天 vs 之前，成交额放大说明市场关注度提升
            vol_score = 0
            if len(trades) >= 4 and sum(trades[:3]) > 0:
                recent_vol = np.mean(trades[:3])
                older_vol = np.mean(trades[3:])
                if older_vol > 0:
                    vol_ratio = recent_vol / older_vol
                    vol_score = min((vol_ratio - 0.8) / 0.8, 1.0) * 10  # 放量20%以上开始给分

            # 热度评分（新权重: 连续性25% + 规模25% + 涨幅25% + 占比15% + 量能10%）
            cont_score = min(continuous / lookback_days, 1.0) * 25
            net_score = min(np.log10(max(total_net, 0) + 1) / 4, 1.0) * 25
            pct_score = min(max(avg_pct, 0) / 3, 1.0) * 25  # 涨幅提升到和规模同等
            inflow_score = (inflow_days / max(days, 1)) * 15
            score = max(0, min(100, cont_score + net_score + pct_score + inflow_score + vol_score))

            sectors.append({
                'sector_name': name,
                'score': round(score, 1),
                'continuous_days': continuous,
                'total_net': round(total_net, 1),
                'avg_pct': round(avg_pct, 2),
                'inflow_days': inflow_days,
                'trade_days': days,
                'trend': trend,
                'stock_count': len(rows),
            })

        sectors.sort(key=lambda x: x['score'], reverse=True)
        return sectors[:top_n]

    except Exception as e:
        logger.error(f"热点板块分析失败: {e}")
        return []
    finally:
        if should_close and db_conn:
            db_conn.close()


def _build_industry_map(codes, db_conn):
    """批量加载股票行业信息（一次SQL替代N次逐条查询）"""
    if not codes:
        return {}
    cur = db_conn.cursor()
    # 批量查询：提取纯代码（去掉 .SZ/.SH 后缀）
    ts_codes = list(codes)
    code_placeholders = ','.join(['%s'] * len(ts_codes))
    cur.execute(
        f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({code_placeholders})",
        ts_codes
    )
    return {row[0]: (row[1] or '') for row in cur.fetchall()}


def get_sector_bonus(ts_code, hot_sectors, db_conn=None, industry_map=None):
    """
    判断某只股票是否属于热点板块，返回额外加分

    参数:
        industry_map: 可选的行业缓存 dict，格式 {ts_code: industry}，
                      批量调用时传入可避免N次SQL查询
    返回: (bonus_score, sector_name, is_hot)
    """
    try:
        # 优先使用缓存
        if industry_map is not None:
            industry = industry_map.get(ts_code, '')
        else:
            should_close = False
            if db_conn is None:
                db_conn = pymysql.connect(**DB_CONFIG)
                should_close = True
            try:
                cur = db_conn.cursor()
                cur.execute("SELECT industry FROM stock_info WHERE ts_code = %s", (ts_code,))
                row = cur.fetchone()
                industry = row[0] if row and row[0] else ''
            finally:
                if should_close and db_conn:
                    db_conn.close()

        if not industry:
            return 0, '', False

        # 匹配热点板块，按排名阶梯式给分（top-8而非top-5，尾部板块也能拿到小分）
        for rank, hs in enumerate(hot_sectors):
            sector = hs['sector_name']
            if sector in industry or industry in sector:
                # 阶梯系数：前3名0.15, 4-6名0.10, 7-8名0.05
                if rank < 3:    ratio = 0.15
                elif rank < 6:  ratio = 0.10
                else:           ratio = 0.05
                bonus = int(hs['score'] * ratio)
                return bonus, sector, True

        return 0, '', False

    except Exception as e:
        logger.error(f"板块加分失败 {ts_code}: {e}")
        return 0, '', False


def get_fund_flow_continuity(ts_code, lookback=5, db_conn=None):
    """
    计算个股主力资金持续流入情况
    
    返回: dict {
        'continuous_inflow': 3,      # 连续主力流入天数
        'total_net_5d': 1234.5,      # 5日主力净流入(万)
        'avg_mainforce_pct': 2.3,    # 主力买入占比%
        'trend': 'accelerating',     # 资金趋势
        'score': 12,                 # 资金面加分 0-15
    }
    """
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True
    
    try:
        cur = db_conn.cursor()
        
        cur.execute("""
            SELECT net_mf_amount, main_net, buy_lg_amount + buy_elg_amount as big_buy,
                   sell_lg_amount + sell_elg_amount as big_sell,
                   buy_sm_amount + buy_md_amount + sell_sm_amount + sell_md_amount as small_total
            FROM moneyflow_daily
            WHERE ts_code = %s
            ORDER BY trade_date DESC
            LIMIT %s
        """, (ts_code, lookback + 2))
        
        rows = cur.fetchall()
        if not rows:
            return {
                'continuous_inflow': 0,
                'total_net_5d': 0,
                'avg_mainforce_pct': 0,
                'trend': 'unknown',
                'score': 0,
            }
        
        nets = [float(r[0]) if r[0] else 0 for r in rows[:lookback]]
        total_net = sum(nets)
        
        # 连续流入天数
        continuous = 0
        for val in nets:
            if val > 0:
                continuous += 1
            else:
                break
        
        # 主力占比
        total_amounts = []
        for r in rows[:lookback]:
            big_buy = float(r[2]) if r[2] else 0
            big_sell = float(r[3]) if r[3] else 0
            small_total = float(r[4]) if r[4] else 0
            total = big_buy + big_sell + small_total
            if total > 0:
                total_amounts.append((big_buy - big_sell) / total * 100)
        
        avg_mainforce_pct = np.mean(total_amounts) if total_amounts else 0
        
        # 趋势
        if len(nets) >= 4:
            recent = np.mean(nets[:2])
            older = np.mean(nets[2:])
            if recent > older * 1.3:
                trend = 'accelerating'
            elif recent < older * 0.7:
                trend = 'weakening'
            else:
                trend = 'steady'
        else:
            trend = 'steady'
        
        # 评分
        score = 0
        if continuous >= 3:
            score += 5
        elif continuous >= 2:
            score += 3
        elif continuous >= 1:
            score += 1
        
        if total_net > 5000:  # 5000万以上
            score += 5
        elif total_net > 1000:
            score += 3
        elif total_net > 0:
            score += 1
        
        if trend == 'accelerating':
            score += 5
        elif trend == 'steady' and continuous >= 2:
            score += 2
        
        score = min(15, score)
        
        return {
            'continuous_inflow': continuous,
            'total_net_5d': round(total_net, 1),
            'avg_mainforce_pct': round(avg_mainforce_pct, 1),
            'trend': trend,
            'score': score,
        }
    
    except Exception as e:
        logger.error(f"资金持续性分析失败 {ts_code}: {e}")
        return {
            'continuous_inflow': 0,
            'total_net_5d': 0,
            'avg_mainforce_pct': 0,
            'trend': 'unknown',
            'score': 0,
        }
    finally:
        if should_close and db_conn:
            db_conn.close()


def enhanced_stock_score(base_score, ts_code, hot_sectors=None, db_conn=None, industry_map=None):
    """
    单股增强评分：基础分 + 热点板块加分 + 资金持续性加分

    参数:
        industry_map: 批量调用时传入行业缓存避免N次SQL
    返回: (enhanced_score, details)
    """
    details = {}

    sector_bonus, sector_name, is_hot = get_sector_bonus(
        ts_code, hot_sectors or [], db_conn=db_conn, industry_map=industry_map
    )
    details['sector_bonus'] = sector_bonus
    details['sector_name'] = sector_name
    details['is_hot_sector'] = is_hot

    flow = get_fund_flow_continuity(ts_code, db_conn=db_conn)
    details['fund_flow'] = flow
    details['fund_bonus'] = flow['score']

    enhanced = base_score + sector_bonus + flow['score']
    return min(100, enhanced), details


def batch_enhanced_scores(stock_base_scores, db_conn=None):
    """
    批量增强评分（一次连接 + 一次热点板块查询 + 一次行业批量加载）

    参数:
        stock_base_scores: [(ts_code, base_score), ...] 股票基础分列表
    返回: {ts_code: (enhanced_score, details), ...}
    """
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True

    try:
        # 1. 一次查询热点板块
        hot_sectors = get_hot_sectors(top_n=8, db_conn=db_conn)  # 默认top-8，梯度给分

        # 2. 批量加载行业信息（替代 N 次逐条 SQL）
        codes = [s[0] for s in stock_base_scores]
        industry_map = _build_industry_map(codes, db_conn)

        # 3. 逐股计算增强评分（资金流向仍需逐个查，但行业已缓存）
        results = {}
        for ts_code, base_score in stock_base_scores:
            enhanced, details = enhanced_stock_score(
                base_score, ts_code,
                hot_sectors=hot_sectors, db_conn=db_conn,
                industry_map=industry_map
            )
            results[ts_code] = (enhanced, details)

        return results

    except Exception as e:
        logger.error(f"批量增强评分失败: {e}")
        return {}
    finally:
        if should_close and db_conn:
            db_conn.close()


if __name__ == '__main__':
    conn = pymysql.connect(**DB_CONFIG)
    
    print("=== 热点板块 Top 5 ===")
    sectors = get_hot_sectors(top_n=5, db_conn=conn)
    for s in sectors:
        trend_icon = {'accelerating': '📈', 'steady': '➡️', 'weakening': '📉'}[s['trend']]
        print(f"  {s['sector_name']} 评分={s['score']:.0f} "
              f"连续流入{s['continuous_days']}天 "
              f"累计净流={s['total_net']:.0f}万 "
              f"涨幅{s['avg_pct']:.2f}% "
              f"{trend_icon}")
    
    print("\n=== 测试个股评分 ===")
    test_codes = ['000001.SZ', '600000.SH', '300750.SZ']
    for code in test_codes:
        score, details = enhanced_stock_score(70, code, hot_sectors=sectors, db_conn=conn)
        print(f"\n  {code}: 原始70 → 增强{score}")
        print(f"    板块加分: +{details['sector_bonus']} ({details['sector_name']})")
        flow = details['fund_flow']
        print(f"    资金加分: +{details['fund_bonus']} "
              f"(连续{flow['continuous_inflow']}天, "
              f"5日净流{flow['total_net_5d']:.0f}万, "
              f"趋势{flow['trend']})")
    
    conn.close()
