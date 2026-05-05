#!/usr/bin/env python3
"""
强势活跃 + 主力评分 组合策略回测 V4

V4 新增优化：移动止损 + 分段止盈

继承 V3 所有逻辑：
1. 强势活跃技术筛选
2. 主力评分>=60
3. 持仓最长7天
4. 高开>2%跳过不追高
5. 最大持仓5只，等仓

V4 新增：
【移动止损】
- 亏损时：止损-5%（相对买入价，不变）
- 盈利≥5%时：止损上移到保本价（买入价）
- 盈利≥10%时：止损上移到成本价+5%
- 盈利≥15%时：止损上移到成本价+10%

【分段止盈】（优化版，降低预期达成难度）
- +6%：卖出 1/3
- +10%：再卖出 1/3
- +18%：全部清仓

回测区间：2025-10-01 ~ 2026-04-24
"""
import os, sys, json, time, logging
from datetime import datetime, timedelta
import pymysql

sys.path.insert(0, os.path.dirname(__file__))
from mainforce_scoring import calculate_mainforce_score, get_db_conn

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ============================================================
# 第一部分：强势活跃技术筛选（从 daily_price 表读取）
# ============================================================

def get_technical_pool(conn, trade_date, min_close=5.0, min_pct_chg=1.0, min_turnover=1.5, min_vr=1.5):
    """
    强势活跃技术筛选，返回候选池列表 [(ts_code, name), ...]
    """
    cur = conn.cursor()
    
    query = """
        SELECT dp.ts_code, dp.close, dp.pct_chg, dp.turnover_rate, dp.volume_ratio,
               dp.ma5, dp.ma10, dp.ma20
        FROM daily_price dp
        WHERE dp.trade_date = %s
          AND dp.close > %s
          AND dp.pct_chg > %s
          AND dp.pct_chg < 9.5
          AND dp.turnover_rate > %s
          AND (
              (dp.ma5 > dp.ma10 AND dp.ma10 > dp.ma20 AND dp.ma5 IS NOT NULL AND dp.ma20 IS NOT NULL AND dp.close > dp.ma5 AND dp.volume_ratio > %s)
              OR
              (dp.pct_chg > 4.0 AND dp.volume_ratio > 2.0 AND dp.close > dp.ma5)
          )
          AND dp.ts_code NOT LIKE '688%%'
          AND dp.ts_code NOT LIKE '8%%'
    """
    date_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
    cur.execute(query, (date_str, min_close, min_pct_chg, min_turnover, min_vr))
    rows = cur.fetchall()
    cur.close()
    
    if rows:
        codes = [r[0] for r in rows]
        placeholders = ','.join(['%s'] * len(codes))
        cur = conn.cursor()
        cur.execute(f"SELECT ts_code, name, is_st FROM stock_info WHERE ts_code IN ({placeholders})", codes)
        info_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.close()
        pool = []
        for r in rows:
            ts_code = r[0]
            name = info_map.get(ts_code, (ts_code, 0))
            if name[1] == 1 or 'ST' in str(name[0]):
                continue
            pool.append((ts_code, name[0]))
    else:
        pool = []
    return pool


# ============================================================
# 第二部分：V4 组合策略回测引擎（移动止损 + 分段止盈）
# ============================================================

def backtest_combo_v4(start_date="20251001", end_date="20260424", min_score=60, max_positions=5, max_hold_days=7, initial_stop_pct=-0.05):
    """
    组合策略回测 V4：强势活跃筛选 + 主力评分>=60 + 持仓7天 + 高开>2%过滤
                    + 移动止损 + 分段止盈

    参数:
        start_date: 回测开始日期 YYYYMMDD
        end_date: 回测结束日期 YYYYMMDD
        min_score: 主力评分最低要求
        max_positions: 最大持仓数
        max_hold_days: 最大持仓天数
        initial_stop_pct: 初始止损百分比（负值），默认-0.05
    """
    logger.info("=" * 60)
    logger.info("强势活跃+主力评分 组合策略回测 V4")
    logger.info("=" * 60)
    logger.info(f"回测区间: {start_date} ~ {end_date}")
    logger.info(f"主力评分门槛: >= {min_score}")
    logger.info(f"最大持仓: {max_positions} 只")
    logger.info(f"最大持仓天数: {max_hold_days} 天")
    logger.info(f"高开过滤: T+1开盘相比T收盘高开>2%跳过")
    logger.info(f"移动止损: 亏损-5% | 盈≥5%保本 | 盈≥10%+5% | 盈≥15%+10%")
    logger.info(f"分段止盈: +6%卖1/3 | +10%再卖1/3 | +18%清仓")
    
    conn = get_db_conn()
    cur = conn.cursor()
    
    # 获取交易日列表
    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date ASC
    """, (start_date, end_date))
    trade_dates = [str(r[0]).replace('-', '') for r in cur.fetchall()]
    
    if len(trade_dates) < 5:
        logger.error("交易日数据不足")
        conn.close()
        return {"error": "交易日数据不足"}
    
    logger.info(f"交易日数量: {len(trade_dates)}")
    date_idx = {d: i for i, d in enumerate(trade_dates)}
    
    # 回测参数
    initial_cash = 100000.0

    # 移动止损阈值
    trailing_stop_thresholds = [
        (0.05, 0.00),   # 盈利≥5% → 止损上移到保本价(0%)
        (0.10, 0.05),   # 盈利≥10% → 止损上移到+5%
        (0.15, 0.10),   # 盈利≥15% → 止损上移到+10%
    ]
    
    # 分段止盈
    take_profit_tiers = [
        (0.06, 1/3),    # +6% 卖出 1/3
        (0.10, 1/3),    # +10% 再卖 1/3
        (0.18, 1.0),    # +18% 清仓（剩余全部）
    ]
    
    commission_rate = 0.0003
    slippage = 0.0001
    
    # 回测状态
    positions = {}   # code -> { ... }
    trades = []
    daily_values = [initial_cash]
    
    scan_days = 0
    signal_days = 0
    total_candidates = 0
    gap_up_skipped = 0
    
    # 统计 V4 特有指标
    trailing_stop_hits = 0   # 移动止损触发次数
    tier_tp_hits = []        # 各段止盈触发次数
    for _ in take_profit_tiers:
        tier_tp_hits.append(0)
    
    for i in range(len(trade_dates) - 1):
        trade_date = trade_dates[i]
        next_date = trade_dates[i + 1]
        
        # ---- 1. 检查持仓卖出 ----
        codes_to_sell = []
        
        for code, pos in list(positions.items()):
            cur = conn.cursor()
            cur.execute("""
                SELECT close, pct_chg FROM daily_price
                WHERE ts_code = %s AND trade_date = %s
            """, (code, next_date))
            row = cur.fetchone()
            cur.close()
            
            if not row or not row[0]:
                pos['days_held'] += 1
                continue
            
            close = float(row[0])
            pos['days_held'] += 1
            pnl_pct = (close - pos['buy_price']) / pos['buy_price']
            
            should_sell = False
            sell_reason = ""
            sell_ratio = 1.0
            
            # === 第一优先级：固定止损（亏损时，止损线为-5%）===
            if pnl_pct <= initial_stop_pct:
                should_sell = True
                sell_reason = f"固定止损{initial_stop_pct*100:.0f}%"
            
            # === 第二优先级：移动止损（盈利时动态上移止损线）===
            if not should_sell and pnl_pct > 0:
                # 根据当前盈利幅度，计算应适用的止损线
                effective_stop = initial_stop_pct  # 默认-5%
                for thresh, stop_level in trailing_stop_thresholds:
                    if pnl_pct >= thresh:
                        effective_stop = stop_level
                
                if pnl_pct <= effective_stop:
                    should_sell = True
                    sell_reason = f"移动止损(盈{pnl_pct*100:.1f}%≤止损{effective_stop*100:.0f}%)"
                    trailing_stop_hits += 1
            
            # === 第三优先级：分段止盈 ===
            if not should_sell:
                # 从高到低检查止盈档位，触发最高可达档位
                for tier_idx in range(len(take_profit_tiers) - 1, -1, -1):
                    tp_pct, tp_ratio = take_profit_tiers[tier_idx]
                    tier_key = f'tier_{tier_idx}'
                    
                    if pnl_pct >= tp_pct and not pos.get(tier_key):
                        # 该档位未触发过，且当前盈利达标
                        should_sell = True
                        sell_ratio = tp_ratio
                        if tp_ratio >= 1.0:
                            sell_reason = f"止盈+{tp_pct*100:.0f}%清仓"
                        else:
                            sell_reason = f"止盈+{tp_pct*100:.0f}%卖{tp_ratio:.0%}"
                        tier_tp_hits[tier_idx] += 1
                        break
            
            # === 第四优先级：超时平仓 ===
            if not should_sell and pos['days_held'] >= max_hold_days:
                should_sell = True
                sell_reason = f"超时{max_hold_days}天平仓"
            
            if should_sell:
                sell_shares = int(pos['shares'] * sell_ratio / 100) * 100
                if sell_shares == 0:
                    sell_shares = pos['shares']
                    sell_ratio = 1.0
                
                sell_value = sell_shares * close * (1 - commission_rate)
                pnl = sell_value - pos['cost'] * sell_ratio
                
                realized_pnl = pnl
                trades.append({
                    "buy_date": pos['buy_date'],
                    "sell_date": next_date,
                    "code": code,
                    "name": pos['name'],
                    "buy_price": pos['buy_price'],
                    "sell_price": close,
                    "shares": sell_shares,
                    "pnl": round(realized_pnl, 2),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "hold_days": pos['days_held'],
                    "reason": sell_reason,
                })
                
                if sell_ratio >= 1.0:
                    # 全部清仓
                    codes_to_sell.append(code)
                else:
                    # 部分卖出
                    pos['shares'] -= sell_shares
                    pos['cost'] -= pos['cost'] * sell_ratio
                    # 标记该止盈档位已触发
                    for tier_idx, (tp_pct, tp_ratio_t) in enumerate(take_profit_tiers):
                        if abs(tp_ratio_t - sell_ratio) < 0.01:
                            pos[f'tier_{tier_idx}'] = True
                            break
        
        for code in codes_to_sell:
            del positions[code]
        
        # ---- 2. 选股（每日执行） ----
        tech_pool = get_technical_pool(conn, trade_date)
        total_candidates += len(tech_pool)
        
        if len(tech_pool) > 0 and len(positions) < max_positions:
            scan_days += 1
            
            # 主力评分
            scored = []
            for ts_code, name in tech_pool:
                if ts_code in positions:
                    continue
                try:
                    norm_date = trade_date.replace('-', '')
                    result = calculate_mainforce_score(ts_code, norm_date)
                    if result['score'] >= min_score:
                        scored.append((ts_code, name, result['score']))
                except Exception as e:
                    logger.debug(f"评分失败 {ts_code}: {e}")
            
            # 降序排序，取前 max_positions - len(positions) 只
            scored.sort(key=lambda x: x[2], reverse=True)
            slots = max_positions - len(positions)
            selected = scored[:slots]
            
            if selected:
                logger.info(f"[{trade_date}] 候选{len(tech_pool)}只, 评分达标{len(scored)}只, 尝试买入{len(selected)}只: "
                           + ", ".join([f"{s[1]}({s[2]})" for s in selected]))
                
                # 买入（含高开过滤）
                bought = 0
                skipped_this_day = 0
                for ts_code, name, score in selected:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT dp_t.close AS t_close, dp_t1.open AS t1_open
                        FROM daily_price dp_t
                        JOIN daily_price dp_t1 ON dp_t.ts_code = dp_t1.ts_code
                        WHERE dp_t.ts_code = %s 
                          AND dp_t.trade_date = %s
                          AND dp_t1.trade_date = %s
                    """, (ts_code, trade_date, next_date))
                    row = cur.fetchone()
                    cur.close()
                    
                    if not row or not row[0] or not row[1]:
                        continue
                    
                    t_close = float(row[0])
                    t1_open = float(row[1])
                    
                    if t_close <= 0:
                        continue
                    
                    # 计算高开幅度
                    gap_up_pct = (t1_open - t_close) / t_close
                    
                    # 高开>2%跳过
                    if gap_up_pct > 0.02:
                        gap_up_skipped += 1
                        skipped_this_day += 1
                        continue
                    
                    # 正常买入
                    open_price = t1_open * (1 + slippage)
                    if open_price <= 0:
                        continue
                    
                    per_position_cash = initial_cash / max_positions
                    shares = int(per_position_cash / open_price / 100) * 100
                    if shares <= 0:
                        continue
                    
                    cost = shares * open_price * (1 + commission_rate)
                    positions[ts_code] = {
                        'buy_price': open_price,
                        'buy_date': next_date,
                        'shares': shares,
                        'initial_shares': shares,  # 记录初始股数，用于分段卖出计算
                        'cost': cost,
                        'name': name,
                        'score': score,
                        'days_held': 0,
                        # 分段止盈标记
                        'tier_0': False,  # +6% 1/3
                        'tier_1': False,  # +10% 1/3
                        'tier_2': False,  # +18% 清仓
                    }
                    bought += 1
                    logger.info(f"  ✅ 买入 {name}({ts_code}): 开盘{open_price:.2f}, 高开{gap_up_pct*100:.2f}%, 评分{score}")
                
                if bought > 0 or skipped_this_day > 0:
                    signal_days += 1
                    logger.info(f"  📊 当日结果: 买入{bought}只, 高开跳过{skipped_this_day}只")
        
        # ---- 3. 计算每日市值 ----
        realized = sum(t['pnl'] for t in trades)
        cash_remaining = initial_cash + realized
        for code, pos in positions.items():
            cash_remaining -= pos['cost']
        
        total_value = cash_remaining
        for code, pos in positions.items():
            cur = conn.cursor()
            cur.execute("""
                SELECT close FROM daily_price
                WHERE ts_code = %s AND trade_date = %s
            """, (code, next_date))
            row = cur.fetchone()
            cur.close()
            if row and row[0]:
                total_value += pos['shares'] * float(row[0])
        
        daily_values.append(total_value)
        
        if (i + 1) % 20 == 0:
            logger.info(f"进度: {i+1}/{len(trade_dates)} 持仓{len(positions)}只 市值{total_value:.0f} 高开跳过累计{gap_up_skipped}")
    
    conn.close()
    
    # ---- 4. 计算回测结果 ----
    closed_trades = [t for t in trades if t.get('reason')]
    win_trades = [t for t in closed_trades if t['pnl'] > 0]
    loss_trades = [t for t in closed_trades if t['pnl'] <= 0]
    
    final_value = daily_values[-1]
    total_return = (final_value - initial_cash) / initial_cash * 100
    
    win_rate = len(win_trades) / max(len(closed_trades), 1) * 100
    
    total_win = sum(t['pnl'] for t in win_trades)
    total_loss = abs(sum(t['pnl'] for t in loss_trades))
    avg_win = total_win / max(len(win_trades), 1)
    avg_loss = total_loss / max(len(loss_trades), 1)
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    
    # 夏普比率
    if len(daily_values) >= 10:
        daily_returns = [(daily_values[j] - daily_values[j-1]) / daily_values[j-1] for j in range(1, len(daily_values))]
        avg_ret = sum(daily_returns) / len(daily_returns)
        var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std = var ** 0.5
        sharpe = (avg_ret / std) * (252 ** 0.5) if std > 0 else 0
    else:
        sharpe = 0
    
    # 最大回撤
    peak = daily_values[0]
    max_dd = 0
    for v in daily_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
    
    result = {
        "版本": "V4",
        "策略": "强势活跃+主力评分组合 V4 (评分>=60 + 持仓7天 + 高开>2%过滤 + 移动止损 + 分段止盈)",
        "回测区间": f"{start_date} ~ {end_date}",
        "参数": {
            "评分门槛": min_score,
            "最大持仓数": max_positions,
            "最大持仓天数": max_hold_days,
            "固定止损": f"{initial_stop_pct*100:.0f}%",
            "移动止损": "盈≥5%保本 | 盈≥10%+5% | 盈≥15%+10%",
            "分段止盈": "+6%卖1/3 | +10%卖1/3 | +18%清仓",
            "高开过滤": ">2%跳过",
        },
        "初始资金": initial_cash,
        "最终市值": round(final_value, 2),
        "总收益率": round(total_return, 2),
        "总交易次数": len(closed_trades),
        "盈利次数": len(win_trades),
        "亏损次数": len(loss_trades),
        "胜率": round(win_rate, 2),
        "盈亏比": round(pl_ratio, 2),
        "平均盈利": round(avg_win, 2),
        "平均亏损": round(avg_loss, 2),
        "夏普比率": round(sharpe, 2),
        "最大回撤": round(max_dd, 2),
        "信号天数": signal_days,
        "扫描天数": scan_days,
        "候选池总数": total_candidates,
        "高开跳过信号数": gap_up_skipped,
        "V4特有统计": {
            "移动止损触发次数": trailing_stop_hits,
            "分段止盈触发": {
                "+6%卖1/3": tier_tp_hits[0],
                "+10%卖1/3": tier_tp_hits[1],
                "+18%清仓": tier_tp_hits[2],
            },
        },
        "交易记录": closed_trades[-100:],
    }
    
    # 保存结果
    output_file = os.path.join(DATA_DIR, "backtest_combo_v4.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    # 打印结果
    logger.info("=" * 60)
    logger.info("V4 组合策略回测结果")
    logger.info("=" * 60)
    logger.info(f"最终市值: {final_value:.2f} 元")
    logger.info(f"总收益率: {total_return:.2f}%")
    logger.info(f"胜率: {win_rate:.1f}% ({len(win_trades)}/{len(closed_trades)})")
    logger.info(f"盈亏比: {pl_ratio:.2f}")
    logger.info(f"夏普比率: {sharpe:.2f}")
    logger.info(f"最大回撤: {max_dd:.2f}%")
    logger.info(f"信号天数: {signal_days}")
    logger.info(f"扫描天数: {scan_days}")
    logger.info(f"高开>2%跳过信号数: {gap_up_skipped}")
    logger.info(f"--- V4 特有统计 ---")
    logger.info(f"移动止损触发次数: {trailing_stop_hits}")
    logger.info(f"分段止盈: +6%卖1/3={tier_tp_hits[0]}次, +10%卖1/3={tier_tp_hits[1]}次, +18%清仓={tier_tp_hits[2]}次")
    logger.info(f"结果已保存: {output_file}")
    
    return result


# ============================================================
# 第三部分：V3 vs V4 对比分析
# ============================================================

def print_v4_comparison(v3_result, v4_result):
    """打印 V3 vs V4 对比表"""
    logger.info("")
    logger.info("=" * 80)
    logger.info("V3 vs V4 组合策略对比")
    logger.info("=" * 80)
    
    v3_desc = "V3: 评分>=60, 持仓7天, 高开>2%过滤, 固定止损-5%, 止盈+8%半/+15%全"
    v4_desc = "V4: 评分>=60, 持仓7天, 高开>2%过滤, 移动止损, 分段止盈+6%/+10%/+18%"
    
    headers = ["指标", "V3 基准", "V4 移动止损+分段止盈"]
    
    def val(r, key, fmt="{}"):
        v = r.get(key)
        if v is None or v == "N/A":
            return "N/A"
        return fmt.format(v)
    
    rows = [
        ("总收益率(%)", val(v3_result, "总收益率", "{:.2f}"), val(v4_result, "总收益率", "{:.2f}")),
        ("胜率(%)", val(v3_result, "胜率", "{:.1f}"), val(v4_result, "胜率", "{:.1f}")),
        ("盈亏比", val(v3_result, "盈亏比", "{:.2f}"), val(v4_result, "盈亏比", "{:.2f}")),
        ("最大回撤(%)", val(v3_result, "最大回撤", "{:.2f}"), val(v4_result, "最大回撤", "{:.2f}")),
        ("夏普比率", val(v3_result, "夏普比率", "{:.2f}"), val(v4_result, "夏普比率", "{:.2f}")),
        ("总交易次数", val(v3_result, "总交易次数", "{}"), val(v4_result, "总交易次数", "{}")),
        ("盈利次数", val(v3_result, "盈利次数", "{}"), val(v4_result, "盈利次数", "{}")),
        ("亏损次数", val(v3_result, "亏损次数", "{}"), val(v4_result, "亏损次数", "{}")),
        ("信号天数", val(v3_result, "信号天数", "{}"), val(v4_result, "信号天数", "{}")),
        ("平均盈利(元)", val(v3_result, "平均盈利", "{:.2f}"), val(v4_result, "平均盈利", "{:.2f}")),
        ("平均亏损(元)", val(v3_result, "平均亏损", "{:.2f}"), val(v4_result, "平均亏损", "{:.2f}")),
    ]
    
    col_widths = [14, 22, 28]
    
    def fmt_row(cells):
        parts = []
        for j, cell in enumerate(cells):
            w = col_widths[j]
            parts.append(str(cell).ljust(w))
        return "  ".join(parts)
    
    logger.info(fmt_row(headers))
    logger.info("-" * (sum(col_widths) + 4))
    for row in rows:
        logger.info(fmt_row(row))
    
    logger.info("")
    logger.info(f"--- V4 特有统计 ---")
    v4_stats = v4_result.get("V4特有统计", {})
    logger.info(f"移动止损触发次数: {v4_stats.get('移动止损触发次数', 0)}")
    tp_stats = v4_stats.get("分段止盈触发", {})
    logger.info(f"分段止盈: +6%={tp_stats.get('+6%卖1/3', 0)}次, +10%={tp_stats.get('+10%卖1/3', 0)}次, +18%={tp_stats.get('+18%清仓', 0)}次")
    
    logger.info("")
    logger.info(f"--- 参数对照 ---")
    logger.info(f"V3: 固定止损-5%, 止盈+8%卖半/+15%清仓")
    logger.info(f"V4: 移动止损(盈≥5%保本/≥10%+5%/≥15%+10%), 分段止盈+6%/+10%/+18%")
    
    logger.info("")
    logger.info("--- 关键指标变化分析 ---")
    
    v3_return = v3_result.get("总收益率")
    v4_return = v4_result.get("总收益率")
    
    if all(isinstance(x, (int, float)) for x in [v3_return, v4_return]):
        delta_return = v4_return - v3_return
        if delta_return > 0:
            logger.info(f"✅ V4 vs V3: 收益提升 {delta_return:+.2f}个百分点")
        else:
            logger.info(f"⚠️ V4 vs V3: 收益变化 {delta_return:+.2f}个百分点")
    
    v3_wr = v3_result.get("胜率")
    v4_wr = v4_result.get("胜率")
    if isinstance(v3_wr, (int, float)) and isinstance(v4_wr, (int, float)):
        delta_wr = v4_wr - v3_wr
        if delta_wr > 0:
            logger.info(f"✅ 胜率变化: {delta_wr:+.1f}个百分点")
        else:
            logger.info(f"⚠️ 胜率变化: {delta_wr:+.1f}个百分点")
    
    v3_dd = v3_result.get("最大回撤")
    v4_dd = v4_result.get("最大回撤")
    if isinstance(v3_dd, (int, float)) and isinstance(v4_dd, (int, float)):
        delta_dd = v4_dd - v3_dd
        if delta_dd < 0:
            logger.info(f"✅ 最大回撤改善: {delta_dd:+.2f}个百分点(回撤减小)")
        else:
            logger.info(f"⚠️ 最大回撤变化: {delta_dd:+.2f}个百分点")
    
    v3_pl = v3_result.get("盈亏比")
    v4_pl = v4_result.get("盈亏比")
    if isinstance(v3_pl, (int, float)) and isinstance(v4_pl, (int, float)):
        delta_pl = v4_pl - v3_pl
        if delta_pl > 0:
            logger.info(f"✅ 盈亏比提升: {delta_pl:+.2f}")
        else:
            logger.info(f"⚠️ 盈亏比变化: {delta_pl:+.2f}")
    
    logger.info("")
    logger.info(f"--- 结论 ---")
    if isinstance(v4_return, (int, float)) and isinstance(v3_return, (int, float)):
        if v4_return > v3_return:
            logger.info("🎉 V4 优化成功！收益超过 V3 基准")
        else:
            logger.info("📉 V4 优化未超越 V3，需要继续调整策略")


def load_result(filename):
    """加载已有的回测结果文件"""
    filepath = os.path.join(DATA_DIR, filename)
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "20251001"
    end = sys.argv[2] if len(sys.argv) > 2 else "20260424"
    
    # 1. 运行 V4 回测
    v4_result = backtest_combo_v4(start, end, min_score=60, max_hold_days=7)
    
    if "error" in v4_result:
        logger.error("V4 回测失败")
        sys.exit(1)
    
    # 2. 加载 V3 结果
    v3_result = load_result("backtest_combo_v3.json")
    
    if not v3_result:
        logger.info("未找到 V3 结果文件，请先运行 V3 回测")
    else:
        logger.info("已加载 V3 回测结果")
        # 3. 对比分析
        print_v4_comparison(v3_result, v4_result)
        
        # 4. 保存对比结果
        comparison = {
            "V3": v3_result,
            "V4": v4_result,
        }
        comp_file = os.path.join(DATA_DIR, "backtest_combo_v4_comparison.json")
        with open(comp_file, 'w', encoding='utf-8') as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)
        logger.info(f"\n对比结果已保存: {comp_file}")
