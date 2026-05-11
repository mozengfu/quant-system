#!/usr/bin/env python3
"""
参数矩阵扫描 — 在内存中批量测试不同参数组合
只查一次数据库，然后循环所有参数组合
"""
import pymysql
import json
import os
import sys
from collections import defaultdict
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

DB = get_db_config()
START_DATE = "2025-01-02"
END_DATE = "2026-04-30"
HOLD_DAYS = [1, 3, 5]
TOP_N = 10

def load_data(conn):
    """一次性加载所有数据到内存"""
    print("加载基础行情数据...")
    c = conn.cursor()
    
    # daily_price
    c.execute("""
        SELECT d.ts_code, d.trade_date, d.close, d.pct_chg, d.turnover_rate,
               d.volume_ratio, d.ma5, d.ma10, d.ma20, d.rps_20,
               d.vol, d.open, d.high, d.low,
               s.name, s.industry, s.is_st
        FROM daily_price d
        JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date >= %s AND d.trade_date <= %s
        AND d.volume_ratio IS NOT NULL AND d.rps_20 IS NOT NULL
        AND d.ma5 IS NOT NULL AND d.ma10 IS NOT NULL AND d.ma20 IS NOT NULL
    """, (START_DATE, END_DATE))
    
    rows = c.fetchall()
    print(f"  daily_price: {len(rows)} 条")
    
    # 按日期组织
    by_date = defaultdict(list)
    for r in rows:
        by_date[r[1]].append({
            'ts_code': r[0],
            'trade_date': r[1],
            'close': float(r[2]) if r[2] else 0,
            'pct_chg': float(r[3]) if r[3] else 0,
            'turnover': float(r[4]) if r[4] else 0,
            'vol_ratio': float(r[5]) if r[5] else 0,
            'ma5': float(r[6]) if r[6] else 0,
            'ma10': float(r[7]) if r[7] else 0,
            'ma20': float(r[8]) if r[8] else 0,
            'rps': float(r[9]) if r[9] else 0,
            'vol': float(r[10]) if r[10] else 0,
            'open': float(r[11]) if r[11] else 0,
            'high': float(r[12]) if r[12] else 0,
            'low': float(r[13]) if r[13] else 0,
            'name': r[14] or '',
            'industry': r[15] or '',
            'is_st': r[16],
        })
    
    c.close()
    
    # 获取交易日列表
    trade_dates = sorted(by_date.keys())
    
    # 加载 moneyflow（按ts_code组织）
    print("加载资金流向数据...")
    c = conn.cursor()
    c.execute("""
        SELECT ts_code, trade_date, main_net
        FROM moneyflow_daily
        WHERE trade_date >= %s AND trade_date <= %s
    """, (START_DATE, END_DATE))
    
    mf_by_code = defaultdict(dict)
    for r in c.fetchall():
        mf_by_code[r[0]][r[1]] = float(r[2]) if r[2] else 0
    c.close()
    print(f"  moneyflow: {len(mf_by_code)} 只股票")
    
    # 预计算每只股票的未来收益（持有1/3/5天）
    # 按ts_code组织所有交易日价格
    print("预计算未来收益...")
    price_by_code = defaultdict(list)
    for date in trade_dates:
        for stock in by_date[date]:
            price_by_code[stock['ts_code']].append((date, stock['close']))
    
    # 构建 (ts_code, date) -> future_return 映射
    future_ret = {}
    for ts_code, price_list in price_by_code.items():
        for i, (date, price) in enumerate(price_list):
            if price <= 0:
                continue
            rets = {}
            for hd in HOLD_DAYS:
                if i + hd < len(price_list):
                    exit_price = price_list[i + hd][1]
                    if exit_price > 0:
                        rets[hd] = (exit_price - price) / price * 100
                    else:
                        rets[hd] = None
                else:
                    rets[hd] = None
            future_ret[(ts_code, date)] = rets
    
    print(f"  future_ret: {len(future_ret)} 条")
    
    return by_date, trade_dates, mf_by_code, future_ret

def scan_with_params(by_date, trade_dates, mf_by_code, future_ret, params):
    """按给定参数执行扫描，返回统计结果"""
    min_rps = params['min_rps']
    min_pct = params['min_pct']
    max_pct = params['max_pct']
    max_vol_shrink = params['max_vol_shrink']
    min_vol_today = params['min_vol_today']
    require_macd = params.get('require_macd', False)
    prev_pct_min = params.get('prev_pct_min', -999)
    prev_pct_max = params.get('prev_pct_max', 999)
    
    win = {1: 0, 3: 0, 5: 0}
    total = {1: 0, 3: 0, 5: 0}
    ret_sum = {1: 0.0, 3: 0.0, 5: 0.0}
    
    for date in trade_dates:
        day_stocks = by_date.get(date, [])
        if not day_stocks:
            continue
        
        # 前一天的数据（用于prev_pct过滤）
        idx = trade_dates.index(date)
        prev_date = trade_dates[idx - 1] if idx > 0 else None
        prev_stocks = by_date.get(prev_date, []) if prev_date else []
        prev_close_map = {s['ts_code']: s['pct_chg'] for s in prev_stocks}
        
        candidates = []
        for s in day_stocks:
            # 基础过滤
            if s['is_st']:
                continue
            if s['close'] < 5:
                continue
            if s['ts_code'].startswith('68') or s['ts_code'].startswith('92') or s['ts_code'].startswith('8') or s['ts_code'].startswith('4'):
                continue
            if s['rps'] < min_rps:
                continue
            if s['pct_chg'] < min_pct or s['pct_chg'] > max_pct:
                continue
            if s['vol_ratio'] < min_vol_today:
                continue
            if s['turnover'] < 1.5:
                continue
            if not (s['ma5'] > s['ma10'] > s['ma20'] and s['close'] > s['ma10']):
                continue
            
            # 前日涨跌幅过滤
            if prev_date and s['ts_code'] in prev_close_map:
                prev_pct = prev_close_map[s['ts_code']]
                if prev_pct < prev_pct_min or prev_pct > prev_pct_max:
                    continue
            
            # 近3日缩量（用前几天的数据近似）
            if idx >= 3:
                recent_vrs = []
                for di in range(max(0, idx-2), idx+1):
                    d = trade_dates[di]
                    # 找同一只股票
                    for ss in by_date.get(d, []):
                        if ss['ts_code'] == s['ts_code']:
                            recent_vrs.append(ss['vol_ratio'])
                            break
                if len(recent_vrs) >= 2:
                    if min(recent_vrs) > max_vol_shrink:
                        continue
            
            # MACD过滤（简化版）
            if require_macd:
                if idx < 5:
                    continue
                # 取近5日收盘价近似计算
                closes = []
                for di in range(max(0, idx-9), idx+1):
                    d = trade_dates[di]
                    for ss in by_date.get(d, []):
                        if ss['ts_code'] == s['ts_code']:
                            closes.append(ss['close'])
                            break
                if len(closes) < 10:
                    continue
                # 简化判断：close > ma5 > ma10 且 close - ma5 < ma5 * 0.03
                dist_ma5 = abs(s['close'] - s['ma5']) / s['ma5'] if s['ma5'] > 0 else 999
                if dist_ma5 > 0.05:
                    continue
            
            # 均线支撑过滤
            dist_ma10 = abs(s['close'] - s['ma10']) / s['ma10'] if s['ma10'] > 0 else 999
            dist_ma20 = abs(s['close'] - s['ma20']) / s['ma20'] if s['ma20'] > 0 else 999
            s['_dist_ma10'] = dist_ma10
            s['_dist_ma20'] = dist_ma20
            if dist_ma10 > 0.05 and dist_ma20 > 0.06:
                continue
            
            candidates.append(s)
        
        if not candidates:
            continue
        
        # 按综合评分排序
        scored = []
        for s in candidates:
            score = 0
            if s['vol_ratio'] > 2.0:
                score += 20
            elif s['vol_ratio'] > 1.5:
                score += 15
            else:
                score += 8
            
            if s['_dist_ma10'] < 0.02:
                score += 20
            elif s['_dist_ma20'] < 0.03:
                score += 15
            else:
                score += 5
            
            if s['rps'] >= 80:
                score += 10
            elif s['rps'] >= 70:
                score += 8
            elif s['rps'] >= 60:
                score += 5
            
            # 主力资金
            ts = s['ts_code']
            net_5d = 0
            if ts in mf_by_code:
                mf_data = mf_by_code[ts]
                dates_5d = [d for d in trade_dates[max(0,idx-4):idx+1] if d in mf_data]
                net_5d = sum(mf_data[d] for d in dates_5d)
            
            if net_5d > 5000:
                score += 15
            elif net_5d > 2000:
                score += 10
            elif net_5d > 0:
                score += 5
            
            s['_score'] = score
            scored.append(s)
        
        scored.sort(key=lambda x: x['_score'], reverse=True)
        selected = scored[:TOP_N]
        
        for s in selected:
            fr = future_ret.get((s['ts_code'], date), {})
            for hd in HOLD_DAYS:
                ret = fr.get(hd)
                if ret is not None:
                    total[hd] += 1
                    ret_sum[hd] += ret
                    if ret > 0:
                        win[hd] += 1
    
    return total, win, ret_sum

def main():
    conn = pymysql.connect(**DB)
    by_date, trade_dates, mf_by_code, future_ret = load_data(conn)
    conn.close()
    
    print(f"\n交易日数: {len(trade_dates)}")
    print(f"数据范围: {trade_dates[0]} ~ {trade_dates[-1]}\n")
    
    # 参数网格
    param_sets = [
        # 基础参数（RPS/涨幅/缩量/量比/MACD/前日涨幅）
        {'name': 'A1-基准', 'min_rps': 60, 'min_pct': 0.5, 'max_pct': 4.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'A2-RPS70', 'min_rps': 70, 'min_pct': 0.5, 'max_pct': 4.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'A3-RPS80', 'min_rps': 80, 'min_pct': 0.5, 'max_pct': 4.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'B1-涨幅0-3%', 'min_rps': 70, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'B2-涨幅-1~3%', 'min_rps': 70, 'min_pct': -1.0, 'max_pct': 3.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'B3-涨幅0-2%', 'min_rps': 70, 'min_pct': 0.0, 'max_pct': 2.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'C1-缩量0.6', 'min_rps': 70, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.6, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'C2-缩量0.7', 'min_rps': 70, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.7, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'D1-MACD+RPS70', 'min_rps': 70, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': True},
        {'name': 'E1-前日跌-2~0', 'min_rps': 70, 'min_pct': 0.5, 'max_pct': 3.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False, 'prev_pct_min': -2.0, 'prev_pct_max': 0.0},
        {'name': 'E2-前日跌-3~1', 'min_rps': 70, 'min_pct': 0.5, 'max_pct': 3.0, 'max_vol_shrink': 0.8, 'min_vol_today': 1.2, 'require_macd': False, 'prev_pct_min': -3.0, 'prev_pct_max': 1.0},
        {'name': 'F1-综合最优', 'min_rps': 70, 'min_pct': -1.0, 'max_pct': 3.0, 'max_vol_shrink': 0.7, 'min_vol_today': 1.2, 'require_macd': False, 'prev_pct_min': -3.0, 'prev_pct_max': 1.0},
        {'name': 'F2-综合+MACD', 'min_rps': 70, 'min_pct': -1.0, 'max_pct': 3.0, 'max_vol_shrink': 0.7, 'min_vol_today': 1.2, 'require_macd': True, 'prev_pct_min': -3.0, 'prev_pct_max': 1.0},
        {'name': 'G1-RPS80+缩量0.7', 'min_rps': 80, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.7, 'min_vol_today': 1.2, 'require_macd': False},
        {'name': 'G2-RPS85+缩量0.6', 'min_rps': 85, 'min_pct': 0.0, 'max_pct': 3.0, 'max_vol_shrink': 0.6, 'min_vol_today': 1.5, 'require_macd': False},
        {'name': 'H1-严格版', 'min_rps': 75, 'min_pct': 0.0, 'max_pct': 2.5, 'max_vol_shrink': 0.7, 'min_vol_today': 1.3, 'require_macd': True, 'prev_pct_min': -3.0, 'prev_pct_max': 1.0},
    ]
    
    results = []
    for i, params in enumerate(param_sets):
        p = params.copy()
        name = p.pop('name')
        print(f"[{i+1}/{len(param_sets)}] 测试: {name}...", end=" ")
        total, win, ret_sum = scan_with_params(by_date, trade_dates, mf_by_code, future_ret, p)
        
        row = {'name': name, **params}
        for hd in HOLD_DAYS:
            wr = win[hd] / total[hd] * 100 if total[hd] > 0 else 0
            avg = ret_sum[hd] / total[hd] if total[hd] > 0 else 0
            row[f'wr_{hd}d'] = wr
            row[f'avg_{hd}d'] = avg
            row[f'n_{hd}d'] = total[hd]
        results.append(row)
        print(f"1d={row['wr_1d']:.1f}%({row['n_1d']}), 3d={row['wr_3d']:.1f}%({row['n_3d']}), 5d={row['wr_5d']:.1f}%({row['n_5d']})")
    
    # 打印对比表
    print(f"\n{'='*90}")
    print(f"{'策略':<16} {'1d胜率':>7} {'1d收益':>8} {'1d样本':>7} | {'3d胜率':>7} {'3d收益':>8} {'3d样本':>7} | {'5d胜率':>7} {'5d收益':>8} {'5d样本':>7}")
    print(f"{'='*90}")
    for r in results:
        print(f"{r['name']:<16} {r['wr_1d']:6.1f}% {r['avg_1d']:>7.2f}% {r['n_1d']:>6} | {r['wr_3d']:6.1f}% {r['avg_3d']:>7.2f}% {r['n_3d']:>6} | {r['wr_5d']:6.1f}% {r['avg_5d']:>7.2f}% {r['n_5d']:>6}")
    print(f"{'='*90}")
    
    # 找出最优（以3天持有综合得分排序：胜率*0.6 + 收益*10*0.4）
    for r in results:
        r['score_3d'] = r['wr_3d'] * 0.6 + r['avg_3d'] * 10 * 0.4
    
    results.sort(key=lambda x: x['score_3d'], reverse=True)
    print(f"\n🏆 3日持有最优策略 TOP5:")
    for i, r in enumerate(results[:5]):
        print(f"  {i+1}. {r['name']}: 3d胜率{r['wr_3d']:.1f}%, 3d收益{r['avg_3d']:+.2f}%, 综合得分{r['score_3d']:.2f}")
    
    # 保存
    output_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'param_scan_results.json')
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {output_file}")

if __name__ == "__main__":
    main()
