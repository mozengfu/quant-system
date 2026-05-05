#!/usr/bin/env python3
"""
多策略回测对比框架

支持的策略:
  v4_combo  — V4组合策略（强势活跃技术面筛选 + 主力评分 >= 60）
  v41_scan  — V4.1快速评分策略（均线多头 + 量价评分）
  v65_ml    — ML增强策略（V4.1评分 x 模拟ML概率因子）

所有策略共用同一回测引擎（移动止损 + 分段止盈 + 高开过滤），
仅在选股维度上差异化，确保对比公平。

用法:
    python3 scripts/compare_strategies.py
    python3 scripts/compare_strategies.py --strategies v4_combo,v41_scan
    python3 scripts/compare_strategies.py --summary

也可通过 run_backtest.py 调用:
    python3 scripts/run_backtest.py compare
"""
import os, sys, json, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mainforce_scoring import get_db_conn, calculate_mainforce_score

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ============================================================
# V4.1 评分函数（内联实现，不依赖 strategy_service 导入）
# ============================================================

def _v41_score(ma5, ma10, ma20, close, pct_chg, vol_ratio, turnover, dt_bonus=0, hc_bonus=0):
    """V4.1 快速评分：均线排列 + 价量强度 + 额外加分"""
    score = 0
    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        score += 40
    if ma5 and close and close > ma5:
        score += 20
    if vol_ratio and vol_ratio > 2:
        score += 20
    if pct_chg and pct_chg > 3:
        score += 10
    if turnover and turnover > 3:
        score += 10
    return score + dt_bonus + hc_bonus


# ============================================================
# 选股函数（各策略专用，接收同一 conn 避免额外连接）
# ============================================================

def _select_v4_combo(conn, trade_date, **kwargs):
    """V4组合选股：技术面筛选 -> 主力评分 >= min_score 排序"""
    min_score = kwargs.get('min_score', 60)
    max_positions = kwargs.get('max_positions', 5)

    pool = _get_technical_pool(conn, trade_date)
    if not pool:
        return []

    candidates = []
    for ts_code, name in pool:
        try:
            result = calculate_mainforce_score(ts_code, trade_date, conn=conn)
            if result['score'] >= min_score:
                candidates.append((ts_code, name, result['score']))
        except Exception:
            continue

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:max_positions]


def _select_v41_scan(conn, trade_date, **kwargs):
    """V4.1快速评分选股：全市场扫描，V4.1评分排序"""
    max_positions = kwargs.get('max_positions', 5)
    min_score = kwargs.get('min_score', 40)

    rows = _get_daily_snapshot(conn, trade_date)
    if not rows:
        return []

    name_map = _get_stock_name_map(conn, [r[0] for r in rows])
    candidates = []

    for r in rows:
        ts_code = r[0]
        info = name_map.get(ts_code)
        if not info or info[1] == 1 or 'ST' in str(info[0]):
            continue

        close = float(r[1]) if r[1] else 0
        pct_chg = float(r[2]) if r[2] else 0
        turnover = float(r[3]) if r[3] else 0
        vol_ratio = float(r[4]) if r[4] else 0
        ma5 = float(r[5]) if r[5] else 0
        ma10 = float(r[6]) if r[6] else 0
        ma20 = float(r[7]) if r[7] else 0

        score = _v41_score(ma5, ma10, ma20, close, pct_chg, vol_ratio, turnover)
        if score >= min_score:
            candidates.append((ts_code, info[0], score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:max_positions]


def _select_v65_ml(conn, trade_date, **kwargs):
    """ML增强选股：V4.1评分 x 模拟ML概率因子"""
    max_positions = kwargs.get('max_positions', 5)
    min_score = kwargs.get('min_score', 40)

    rows = _get_daily_snapshot(conn, trade_date)
    if not rows:
        return []

    name_map = _get_stock_name_map(conn, [r[0] for r in rows])
    candidates = []

    for r in rows:
        ts_code = r[0]
        info = name_map.get(ts_code)
        if not info or info[1] == 1 or 'ST' in str(info[0]):
            continue

        close = float(r[1]) if r[1] else 0
        pct_chg = float(r[2]) if r[2] else 0
        turnover = float(r[3]) if r[3] else 0
        vol_ratio = float(r[4]) if r[4] else 0
        ma5 = float(r[5]) if r[5] else 0
        ma10 = float(r[6]) if r[6] else 0
        ma20 = float(r[7]) if r[7] else 0

        base_score = _v41_score(ma5, ma10, ma20, close, pct_chg, vol_ratio, turnover)
        ml_factor = _ml_probability_factor(close, ma5, ma10, pct_chg, vol_ratio)
        enhanced_score = round(base_score * ml_factor, 1)

        if enhanced_score >= min_score:
            candidates.append((ts_code, info[0], enhanced_score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:max_positions]


def _ml_probability_factor(close, ma5, ma10, pct_chg, vol_ratio):
    """模拟ML概率因子（范围 0.6 ~ 1.5）"""
    factor = 1.0
    if close > ma5 and ma5 > ma10:
        factor += 0.30
    if vol_ratio and vol_ratio > 1.5:
        factor += 0.15
    if pct_chg and 2 < pct_chg < 5:
        factor += 0.10
    elif pct_chg and pct_chg < 0.5:
        factor -= 0.10
    return max(0.6, min(1.5, factor))


# ============================================================
# 数据查询辅助
# ============================================================

def _get_technical_pool(conn, trade_date, min_close=5.0, min_pct_chg=1.0,
                        min_turnover=1.5, min_vr=1.5):
    """强势活跃技术筛选（复用 V4 逻辑）"""
    cur = conn.cursor()
    date_str = _to_date_str(trade_date)
    cur.execute("""
        SELECT dp.ts_code
        FROM daily_price dp
        WHERE dp.trade_date = %s
          AND dp.close > %s
          AND dp.pct_chg > %s
          AND dp.pct_chg < 9.5
          AND dp.turnover_rate > %s
          AND (
              (dp.ma5 > dp.ma10 AND dp.ma10 > dp.ma20
               AND dp.ma5 IS NOT NULL AND dp.ma20 IS NOT NULL
               AND dp.close > dp.ma5 AND dp.volume_ratio > %s)
              OR
              (dp.pct_chg > 4.0 AND dp.volume_ratio > 2.0 AND dp.close > dp.ma5)
          )
          AND dp.ts_code NOT LIKE '688%%'
          AND dp.ts_code NOT LIKE '8%%'
    """, (date_str, min_close, min_pct_chg, min_turnover, min_vr))
    rows = cur.fetchall()
    cur.close()
    if not rows:
        return []
    return _resolve_names(conn, [r[0] for r in rows])


def _get_daily_snapshot(conn, trade_date, min_close=5.0, min_pct_chg=0):
    """获取指定交易日全市场快照（含技术指标），按 pct_chg 降序"""
    cur = conn.cursor()
    date_str = _to_date_str(trade_date)
    cur.execute("""
        SELECT dp.ts_code, dp.close, dp.pct_chg, dp.turnover_rate,
               dp.volume_ratio, dp.ma5, dp.ma10, dp.ma20
        FROM daily_price dp
        WHERE dp.trade_date = %s
          AND dp.close > %s
          AND dp.pct_chg > %s
          AND dp.pct_chg < 9.5
          AND dp.ts_code NOT LIKE '688%%'
          AND dp.ts_code NOT LIKE '8%%'
        ORDER BY dp.pct_chg DESC
    """, (date_str, min_close, min_pct_chg))
    rows = cur.fetchall()
    cur.close()
    return rows


def _to_date_str(ymd):
    """YYYYMMDD -> YYYY-MM-DD"""
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"


def _get_stock_name_map(conn, codes):
    """批量查询股票名称和ST状态，返回 {ts_code: (name, is_st)}"""
    if not codes:
        return {}
    placeholders = ','.join(['%s'] * len(codes))
    cur = conn.cursor()
    cur.execute(
        f"SELECT ts_code, name, is_st FROM stock_info WHERE ts_code IN ({placeholders})",
        codes
    )
    result = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.close()
    return result


def _resolve_names(conn, codes):
    """codes -> [(ts_code, name)] 过滤ST"""
    name_map = _get_stock_name_map(conn, codes)
    result = []
    for code in codes:
        info = name_map.get(code, (code, 0))
        if info[1] == 1 or 'ST' in str(info[0]):
            continue
        result.append((code, info[0]))
    return result


# ============================================================
# 通用回测引擎（共用交易逻辑，选股逻辑可插拔）
# ============================================================

def _backtest_generic(start_date, end_date, strategy_name, select_fn,
                      initial_cash=100000, max_positions=5, max_hold_days=7,
                      selector_kwargs=None):
    """
    通用回测引擎

    所有策略共用同一套交易逻辑：
      - 等仓买入（initial_cash / max_positions）
      - T日收盘选股，T+1开盘买入
      - 高开>2%跳过
      - 固定止损 -5%
      - 移动止损（盈5%保本 / 盈10%+5% / 盈15%+10%）
      - 分段止盈（+6%卖1/3 / +10%卖1/3 / +18%清仓）
      - 超时 max_hold_days 天平仓

    区别仅在 select_fn(conn, trade_date, **kwargs) 的选股逻辑。
    """
    if selector_kwargs is None:
        selector_kwargs = {}

    logger.info("=" * 60)
    logger.info(f"策略回测: {strategy_name}")
    logger.info(f"区间: {start_date} ~ {end_date}")
    logger.info(f"初始资金: {initial_cash}  最大持仓: {max_positions}  最大天数: {max_hold_days}")
    logger.info("=" * 60)

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date >= %s AND trade_date <= %s
        ORDER BY trade_date ASC
    """, (start_date, end_date))
    trade_dates = [str(r[0]).replace('-', '') for r in cur.fetchall()]

    if len(trade_dates) < 5:
        conn.close()
        logger.error("交易日数据不足")
        return {"error": "交易日数据不足"}

    logger.info(f"交易日数量: {len(trade_dates)}")

    # 止损/止盈参数
    initial_stop_pct = -0.05
    trailing_stop_thresholds = [(0.05, 0.00), (0.10, 0.05), (0.15, 0.10)]
    take_profit_tiers = [(0.06, 1/3), (0.10, 1/3), (0.18, 1.0)]
    commission_rate = 0.0003
    slippage = 0.0001

    positions = {}
    trades = []
    daily_values = [float(initial_cash)]

    scan_days = 0
    signal_days = 0
    total_candidates = 0
    gap_up_skipped = 0
    trailing_stop_hits = 0
    tier_tp_hits = [0] * len(take_profit_tiers)

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

            if pnl_pct <= initial_stop_pct:
                should_sell = True
                sell_reason = f"固定止损{initial_stop_pct*100:.0f}%"

            if not should_sell and pnl_pct > 0:
                effective_stop = initial_stop_pct
                for thresh, stop_level in trailing_stop_thresholds:
                    if pnl_pct >= thresh:
                        effective_stop = stop_level
                if pnl_pct <= effective_stop:
                    should_sell = True
                    sell_reason = (f"移动止损(盈{pnl_pct*100:.1f}%"
                                   f"<=止损{effective_stop*100:.0f}%)")
                    trailing_stop_hits += 1

            if not should_sell:
                for tier_idx in range(len(take_profit_tiers) - 1, -1, -1):
                    tp_pct, tp_ratio = take_profit_tiers[tier_idx]
                    tier_key = f'tier_{tier_idx}'
                    if pnl_pct >= tp_pct and not pos.get(tier_key):
                        should_sell = True
                        sell_ratio = tp_ratio
                        if tp_ratio >= 1.0:
                            sell_reason = f"止盈+{tp_pct*100:.0f}%清仓"
                        else:
                            sell_reason = f"止盈+{tp_pct*100:.0f}%卖{tp_ratio:.0%}"
                        tier_tp_hits[tier_idx] += 1
                        break

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

                trades.append({
                    "buy_date": pos['buy_date'],
                    "sell_date": next_date,
                    "code": code,
                    "name": pos['name'],
                    "buy_price": pos['buy_price'],
                    "sell_price": close,
                    "shares": sell_shares,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "hold_days": pos['days_held'],
                    "reason": sell_reason,
                })

                if sell_ratio >= 1.0:
                    codes_to_sell.append(code)
                else:
                    pos['shares'] -= sell_shares
                    pos['cost'] -= pos['cost'] * sell_ratio
                    for tier_idx, (_, tp_ratio_t) in enumerate(take_profit_tiers):
                        if abs(tp_ratio_t - sell_ratio) < 0.01:
                            pos[f'tier_{tier_idx}'] = True
                            break

        for code in codes_to_sell:
            del positions[code]

        # ---- 2. 选股 ----
        slots = max_positions - len(positions)
        if slots > 0:
            call_kwargs = dict(selector_kwargs)
            call_kwargs['max_positions'] = slots
            candidates = select_fn(conn, trade_date, **call_kwargs)
            total_candidates += len(candidates)

            if candidates:
                scan_days += 1
                bought_this_day = 0
                skipped_this_day = 0

                for ts_code, name, score in candidates:
                    if len(positions) >= max_positions or ts_code in positions:
                        continue

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

                    gap_up_pct = (t1_open - t_close) / t_close
                    if gap_up_pct > 0.02:
                        gap_up_skipped += 1
                        skipped_this_day += 1
                        continue

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
                        'cost': cost,
                        'name': name,
                        'score': score,
                        'days_held': 0,
                        'tier_0': False,
                        'tier_1': False,
                        'tier_2': False,
                    }
                    bought_this_day += 1

                if bought_this_day > 0:
                    signal_days += 1

        # ---- 3. 每日市值 ----
        realized = sum(t['pnl'] for t in trades)
        cash_remaining = initial_cash + realized
        for pos in positions.values():
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

    conn.close()

    # ---- 4. 计算指标 ----
    closed_trades = [t for t in trades if t.get('reason')]
    final_value = daily_values[-1]
    total_ret = (final_value - initial_cash) / initial_cash * 100

    trade_days = len(trade_dates)
    years = trade_days / 252
    annual_ret = ((1 + total_ret / 100) ** (1 / max(years, 0.01)) - 1) * 100

    win_trades = [t for t in closed_trades if t['pnl'] > 0]
    loss_trades = [t for t in closed_trades if t['pnl'] <= 0]
    win_rate = len(win_trades) / max(len(closed_trades), 1) * 100

    total_win = sum(t['pnl'] for t in win_trades)
    total_loss = abs(sum(t['pnl'] for t in loss_trades))
    avg_win = total_win / max(len(win_trades), 1)
    avg_loss = total_loss / max(len(loss_trades), 1)
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')

    if len(daily_values) >= 10:
        daily_returns = [(daily_values[j] - daily_values[j-1]) / daily_values[j-1]
                         for j in range(1, len(daily_values))]
        avg_ret = sum(daily_returns) / len(daily_returns)
        var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std = var ** 0.5 if var > 0 else 1e-10
        sharpe = (avg_ret / std) * (252 ** 0.5) if std > 0 else 0
    else:
        sharpe = 0

    peak = daily_values[0]
    max_dd = 0
    for v in daily_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    result = {
        "strategy": strategy_name,
        "total_return": round(total_ret, 2),
        "annual_return": round(annual_ret, 2),
        "win_rate": round(win_rate, 2),
        "pl_ratio": round(pl_ratio, 2) if pl_ratio != float('inf') else "N/A",
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 2),
        "trade_count": len(closed_trades),
        "final_value": round(final_value, 2),
        "initial_cash": initial_cash,
        "trade_days": trade_days,
        "信号天数": signal_days,
        "高开跳过": gap_up_skipped,
    }

    # 打印简版结果
    logger.info(f"  总收益率: {result['total_return']}%  "
                f"年化: {result['annual_return']}%  "
                f"胜率: {result['win_rate']}%  "
                f"盈亏比: {result['pl_ratio']}  "
                f"夏普: {result['sharpe']}  "
                f"最大回撤: {result['max_dd']}%  "
                f"交易次数: {result['trade_count']}")

    return result


# ============================================================
# 策略注册表
# ============================================================

_STRATEGIES = {
    "v4_combo": {
        "name": "V4组合策略",
        "desc": "技术面筛选+主力评分>=60",
        "select_fn": _select_v4_combo,
        "kwargs": {"min_score": 60},
    },
    "v41_scan": {
        "name": "V4.1快速评分",
        "desc": "均线多头+量价V4.1评分",
        "select_fn": _select_v41_scan,
        "kwargs": {"min_score": 40},
    },
    "v65_ml": {
        "name": "ML增强策略",
        "desc": "V4.1评分x模拟ML概率因子",
        "select_fn": _select_v65_ml,
        "kwargs": {"min_score": 40},
    },
}


# ============================================================
# 对外接口
# ============================================================

def run_comparison(strategies_list=None, start_date="20251001", end_date="20260424",
                   initial_cash=100000, max_positions=5, max_hold_days=7):
    """
    运行多策略对比回测。

    所有策略在相同时间区间、相同初始资金下运行，
    使用同一交易引擎（移动止损+分段止盈+高开过滤），
    仅选股逻辑不同，确保对比公平。

    参数:
        strategies_list: 策略名列表，默认全部运行
        start_date: 回测开始日期 YYYYMMDD
        end_date: 回测结束日期 YYYYMMDD
        initial_cash: 初始资金
        max_positions: 最大持仓数
        max_hold_days: 最大持仓天数

    返回:
        dict {strategy_name: metrics_dict, ...}
    """
    if strategies_list is None:
        strategies_list = list(_STRATEGIES.keys())

    for name in strategies_list:
        if name not in _STRATEGIES:
            logger.error(f"未知策略: {name}，可用: {list(_STRATEGIES.keys())}")
            return None

    results = {}
    logger.info("=" * 70)
    logger.info("多策略对比回测")
    logger.info(f"区间: {start_date} ~ {end_date}")
    logger.info(f"初始资金: {initial_cash}  最大持仓: {max_positions}  持仓天数: {max_hold_days}")
    logger.info(f"策略列表: {strategies_list}")
    logger.info("=" * 70)

    for name in strategies_list:
        info = _STRATEGIES[name]
        logger.info(f"\n>>> 运行策略: {info['name']} ({info['desc']})")

        result = _backtest_generic(
            start_date=start_date,
            end_date=end_date,
            strategy_name=name,
            select_fn=info['select_fn'],
            initial_cash=initial_cash,
            max_positions=max_positions,
            max_hold_days=max_hold_days,
            selector_kwargs=info['kwargs'],
        )

        if "error" in result:
            logger.error(f"  {name} 回测失败: {result['error']}")

        results[name] = result

    # 保存结果
    output = {
        "meta": {
            "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": start_date,
            "end_date": end_date,
            "initial_cash": initial_cash,
            "max_positions": max_positions,
            "max_hold_days": max_hold_days,
        },
        "results": results,
    }

    output_path = os.path.join(DATA_DIR, "strategy_comparison.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"\n对比结果已保存: {output_path}")

    # 打印对比表
    _print_comparison_table(results)

    return results


def print_summary():
    """从 data/strategy_comparison.json 加载并显示已有对比结果。"""
    output_path = os.path.join(DATA_DIR, "strategy_comparison.json")
    if not os.path.exists(output_path):
        logger.error(f"未找到对比结果文件: {output_path}")
        logger.error("请先运行 run_comparison() 或 python3 scripts/compare_strategies.py")
        return

    with open(output_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    meta = data.get("meta", {})
    results = data.get("results", {})

    if not results:
        logger.error("对比结果为空")
        return

    logger.info("=" * 70)
    logger.info(f"多策略对比结果 (从文件加载)")
    logger.info(f"运行时间: {meta.get('run_time', 'N/A')}")
    logger.info(f"区间: {meta.get('start_date', 'N/A')} ~ {meta.get('end_date', 'N/A')}")
    logger.info("=" * 70)

    _print_comparison_table(results)


# ============================================================
# 对比表格打印
# ============================================================

def _print_comparison_table(results):
    """打印多策略对比表格，最优值以 * 标记。"""
    if not results:
        return

    valid = {k: v for k, v in results.items() if "error" not in v}
    if not valid:
        logger.error("没有有效的回测结果")
        return

    # 对比指标定义：(显示名, key, 格式化模板, 是否越小越好)
    metrics = [
        ("策略",           "strategy",     "{:>14}",      False),
        ("总收益率%",      "total_return", "{:>10.2f}",   False),
        ("年化收益率%",    "annual_return","{:>12.2f}",   False),
        ("胜率%",          "win_rate",     "{:>8.2f}",    False),
        ("盈亏比",         "pl_ratio",     "{:>8}",       False),
        ("夏普比率",       "sharpe",       "{:>10.2f}",   False),
        ("最大回撤%",      "max_dd",       "{:>10.2f}",   True),
        ("交易次数",       "trade_count",  "{:>10}",      False),
    ]

    # 找到各指标最优值所属的策略
    best = {}
    for label, key, fmt, lower_better in metrics:
        if key == "strategy":
            continue
        values = {}
        for sname, sv in valid.items():
            val = sv.get(key)
            if isinstance(val, (int, float)) and val != float('inf'):
                values[sname] = val
        if not values:
            continue
        if lower_better:
            best[key] = min(values, key=values.get)
        else:
            best[key] = max(values, key=values.get)

    # 策略显示名映射
    name_map = {k: _STRATEGIES.get(k, {}).get("name", k) for k in valid}

    # 打印表头
    parts = []
    for label, key, fmt, _ in metrics:
        width = int(fmt.split(':')[-1].split('}')[0]) if ':' in fmt else 10
        parts.append(f"{label:>{width}}")
    logger.info(" ".join(parts))
    logger.info("-" * (len(" ".join(parts))))

    # 打印每行
    for sname in sorted(valid.keys()):
        sv = valid[sname]
        row = []
        for label, key, fmt, lower_better in metrics:
            if key == "strategy":
                display = name_map.get(sname, sname)[:14]
                row.append(f"{display:>14}")
            else:
                val = sv.get(key, "N/A")
                if isinstance(val, (int, float)) and val != float('inf'):
                    formatted = fmt.format(val)
                    # 标记最优
                    if key in best and best[key] == sname:
                        # 给数值加上 * 后缀（保持对齐）
                        formatted = formatted.rstrip() + "*"
                    row.append(formatted)
                else:
                    # 非数字（如 "N/A"）
                    width = int(fmt.split(':')[-1].split('}')[0]) if ':' in fmt else 10
                    row.append(f"{'N/A':>{width}}")
        logger.info(" ".join(row))

    logger.info("")
    logger.info("* 标记列内最优（最大回撤取最小值，其余取最大值）")


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="多策略对比回测框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 scripts/compare_strategies.py\n"
            "  python3 scripts/compare_strategies.py --strategies v4_combo,v41_scan\n"
            "  python3 scripts/compare_strategies.py --summary\n"
            "  python3 scripts/run_backtest.py compare\n"
        ),
    )
    parser.add_argument("--strategies", default=None,
                        help="策略列表逗号分隔，默认全部")
    parser.add_argument("--start", default="20251001",
                        help="开始日期 YYYYMMDD")
    parser.add_argument("--end", default="20260424",
                        help="结束日期 YYYYMMDD")
    parser.add_argument("--summary", action="store_true",
                        help="显示已有对比结果")
    parser.add_argument("--positions", type=int, default=5,
                        help="最大持仓数")
    parser.add_argument("--hold", type=int, default=7,
                        help="最大持仓天数")

    args = parser.parse_args()

    if args.summary:
        print_summary()
    else:
        strategies = args.strategies.split(",") if args.strategies else None
        run_comparison(strategies, start_date=args.start, end_date=args.end,
                       max_positions=args.positions, max_hold_days=args.hold)
