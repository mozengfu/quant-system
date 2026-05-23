#!/usr/bin/env python3
# DEPRECATED: 使用 scripts/run_backtest.py v4 替代
"""
V4组合策略回测 — 模拟每日选股+持有N天收益
基于MySQL本地数据，不用ML增强（简化版）
"""
import pymysql
import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

DB = get_db_config()

# ========== V4策略核心条件 ==========
# SQL筛选：
#   1. close > 5
#   2. 1 < pct_chg < 9.5
#   3. turnover_rate > 1.5
#   4. 排除ST、688/92/8/4
#   5. (MA5>MA10>MA20 AND close>MA5 AND vol_ratio>1.5)
#      OR (pct_chg>4.0 AND vol_ratio>2.0 AND close>MA5)
#   6. 按pct_chg DESC取200
# 主力评分 >= 60（用moneyflow_daily.main_net简化计算）
# 综合评分排序，取Top 10

HOLD_DAYS = [1, 3, 5, 10]  # 持有天数
START_DATE = "2025-01-02"  # 回测起点
END_DATE = "2026-04-30"    # 回测终点
TOP_N = 10  # 每日选几只

def get_trade_dates(conn, start, end):
    """获取交易日列表"""
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date
    """, (start, end))
    dates = [r[0] for r in c.fetchall()]
    c.close()
    return dates

def simple_mainforce_score(conn, ts_code, trade_date):
    """简化主力评分（只用资金流向，不查股东/龙虎榜等慢表）"""
    c = conn.cursor()
    # 近5日主力净流入
    c.execute("""
        SELECT main_net FROM moneyflow_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 5
    """, (ts_code, trade_date))
    rows = c.fetchall()
    
    if len(rows) < 3:
        c.close()
        return 40  # 默认中等
    
    total_net = sum(float(r[0]) for r in rows if r[0])
    
    # 近10日量价配合
    c.execute("""
        SELECT pct_chg, volume_ratio FROM daily_price
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 10
    """, (ts_code, trade_date))
    vp_rows = c.fetchall()
    c.close()
    
    score = 30  # 基础分
    
    # 资金流向 0-30分
    if total_net > 5000:
        score += 30
    elif total_net > 2000:
        score += 25
    elif total_net > 500:
        score += 15
    elif total_net > 0:
        score += 8
    else:
        score += 3
    
    # 量价配合 0-15分
    if len(vp_rows) >= 5:
        up_vol = sum(1 for r in vp_rows if (r[0] or 0) > 0 and (r[1] or 1) > 1.2)
        down_vol = sum(1 for r in vp_rows if (r[0] or 0) < 0 and (r[1] or 1) < 1.0)
        up_total = sum(1 for r in vp_rows if (r[0] or 0) > 0)
        down_total = sum(1 for r in vp_rows if (r[0] or 0) < 0)
        if up_total > 0 and down_total > 0:
            if up_vol/up_total > 0.5 and down_vol/down_total > 0.5:
                score += 15
            elif up_vol/up_total > 0.3:
                score += 8
            else:
                score += 5
    
    # 融资融券替代 0-10分
    if total_net > 1000:
        score += 10
    elif total_net > 0:
        score += 5
    
    # 龙虎榜/机构 0-10分（简化：用近5日是否有大额流入）
    if any(float(r[0]) > 2000 for r in rows if r[0]):
        score += 10
    elif any(float(r[0]) > 500 for r in rows if r[0]):
        score += 5
    
    return min(100, max(0, score))

def quick_score(row):
    """V4综合评分计算"""
    ts_code, name, industry, price, pct_chg, turnover, vol_ratio, ma5, ma10, ma20 = row
    
    price = float(price) if price else 0
    pct_chg = float(pct_chg) if pct_chg else 0
    turnover = float(turnover) if turnover else 0
    vol_ratio = float(vol_ratio) if vol_ratio else 0
    ma5 = float(ma5) if ma5 else 0
    ma10 = float(ma10) if ma10 else 0
    ma20 = float(ma20) if ma20 else 0
    
    score = 0
    if ma5 > ma10 > ma20 and ma20 > 0:
        score += 40
    if price > ma5:
        score += 20
    if vol_ratio > 2.0:
        score += 20
    if pct_chg > 3:
        score += 10
    if turnover > 3:
        score += 10
    return score

def get_future_return(conn, ts_code, entry_date, hold_days):
    """计算持有N天后的收益率"""
    c = conn.cursor()
    # 找entry_date之后第hold_days个交易日的收盘价
    c.execute("""
        SELECT trade_date, close FROM daily_price
        WHERE ts_code = %s AND trade_date > %s
        ORDER BY trade_date ASC
    """, (ts_code, entry_date))
    rows = c.fetchall()
    c.close()
    
    if len(rows) < hold_days:
        return None  # 数据不足（停牌/退市）
    
    entry_price = None
    c2 = conn.cursor()
    c2.execute("SELECT close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (ts_code, entry_date))
    r = c2.fetchone()
    c2.close()
    if not r or not r[0]:
        return None
    
    entry_price = float(r[0])
    if entry_price <= 0:
        return None
    
    exit_price = float(rows[hold_days - 1][1]) if rows[hold_days - 1][1] else 0
    if exit_price <= 0:
        return None
    
    return (exit_price - entry_price) / entry_price * 100

def backtest():
    conn = pymysql.connect(**DB)
    dates = get_trade_dates(conn, START_DATE, END_DATE)
    print(f"回测区间: {START_DATE} ~ {END_DATE}, 共{len(dates)}个交易日")
    print(f"策略: V4组合策略（简化主力评分，无ML增强）")
    print(f"每日选Top{TOP_N}，分别计算持有{HOLD_DAYS}天收益\n")
    
    all_results = []
    win_counts = defaultdict(int)
    total_counts = defaultdict(int)
    return_sums = defaultdict(float)
    
    for i, entry_date in enumerate(dates):
        if i % 50 == 0:
            print(f"  进度: {i}/{len(dates)} ({entry_date})")
        
        # 1. SQL筛选
        c = conn.cursor()
        c.execute("""
            SELECT d.ts_code, s.name, s.industry,
                   d.close, d.pct_chg,
                   d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20
            FROM daily_price d
            JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE d.trade_date = %s
              AND d.close > 5
              AND d.pct_chg > 1
              AND d.pct_chg < 9.5
              AND d.turnover_rate > 1.5
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
              AND d.ts_code NOT LIKE '92%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
              AND (
                  (d.ma5 > d.ma10 AND d.ma10 > d.ma20 AND d.ma5 IS NOT NULL 
                   AND d.ma20 IS NOT NULL AND d.close > d.ma5 AND d.volume_ratio > 1.5)
                  OR (d.pct_chg > 4.0 AND d.volume_ratio > 2.0 AND d.close > d.ma5)
              )
            ORDER BY d.pct_chg DESC
            LIMIT 200
        """, (entry_date,))
        candidates = c.fetchall()
        c.close()
        
        if not candidates:
            continue
        
        # 2. 主力评分 + 综合评分
        scored = []
        for row in candidates:
            ts_code = row[0]
            mf_score = simple_mainforce_score(conn, ts_code, entry_date)
            if mf_score < 60:
                continue
            qs = quick_score(row)
            scored.append({
                'ts_code': ts_code,
                'name': row[1] or '',
                'industry': row[2] or '',
                'price': float(row[3]) if row[3] else 0,
                'pct_chg': float(row[4]) if row[4] else 0,
                'mf_score': mf_score,
                'quick_score': qs,
            })
        
        # 3. 按综合评分排序，取Top N
        scored.sort(key=lambda x: x['quick_score'], reverse=True)
        selected = scored[:TOP_N]
        
        if not selected:
            continue
        
        # 4. 计算各持有期收益
        for stock in selected:
            result = {'date': str(entry_date), **stock}
            for hd in HOLD_DAYS:
                ret = get_future_return(conn, stock['ts_code'], entry_date, hd)
                result[f'ret_{hd}d'] = ret
                if ret is not None:
                    total_counts[hd] += 1
                    return_sums[hd] += ret
                    if ret > 0:
                        win_counts[hd] += 1
            
            all_results.append(result)
    
    conn.close()
    
    # ========== 统计结果 ==========
    print(f"\n{'='*60}")
    print(f"回测完成! 共选出 {len(all_results)} 只次股票")
    print(f"{'='*60}\n")
    
    for hd in HOLD_DAYS:
        if total_counts[hd] > 0:
            win_rate = win_counts[hd] / total_counts[hd] * 100
            avg_ret = return_sums[hd] / total_counts[hd]
            print(f"持有{hd}天: 样本{total_counts[hd]}次, 胜率{win_rate:.1f}%, 平均收益{avg_ret:+.2f}%")
        else:
            print(f"持有{hd}天: 无有效数据")
    
    # 按月统计胜率
    print(f"\n--- 月度胜率（持有1天） ---")
    monthly = defaultdict(lambda: {'win': 0, 'total': 0})
    for r in all_results:
        if r.get('ret_1d') is not None:
            month = r['date'][:7]
            monthly[month]['total'] += 1
            if r['ret_1d'] > 0:
                monthly[month]['win'] += 1
    
    for month in sorted(monthly.keys()):
        d = monthly[month]
        if d['total'] >= 5:
            wr = d['win'] / d['total'] * 100
            bar = '█' * int(wr / 5)
            print(f"  {month}: {wr:5.1f}% ({d['win']:3d}/{d['total']:3d}) {bar}")
    
    # 按行业统计
    print(f"\n--- 行业胜率（持有1天，样本>=10次） ---")
    industry_stats = defaultdict(lambda: {'win': 0, 'total': 0})
    for r in all_results:
        if r.get('ret_1d') is not None:
            ind = r.get('industry', '未知') or '未知'
            industry_stats[ind]['total'] += 1
            if r['ret_1d'] > 0:
                industry_stats[ind]['win'] += 1
    
    sorted_ind = sorted(industry_stats.items(), key=lambda x: x[1]['total'], reverse=True)
    for ind, d in sorted_ind[:20]:
        if d['total'] >= 10:
            wr = d['win'] / d['total'] * 100
            print(f"  {ind}: {wr:5.1f}% ({d['win']:3d}/{d['total']:3d})")
    
    # 保存详细结果
    output_file = os.path.join(os.path.dirname(__file__), 'data', 'backtest_v4_combo.json')
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump({
            'strategy': 'V4组合策略（简化版，无ML）',
            'period': f'{START_DATE} ~ {END_DATE}',
            'hold_days': HOLD_DAYS,
            'summary': {
                hd: {
                    'samples': total_counts[hd],
                    'win_rate': round(win_counts[hd] / total_counts[hd] * 100, 1) if total_counts[hd] > 0 else 0,
                    'avg_return': round(return_sums[hd] / total_counts[hd], 2) if total_counts[hd] > 0 else 0,
                }
                for hd in HOLD_DAYS
            },
            'results': all_results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n详细结果已保存到: {output_file}")

if __name__ == "__main__":
    backtest()
