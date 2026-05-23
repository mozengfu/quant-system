#!/usr/bin/env python3
"""
V4 纯策略 vs V4+ML 统一回测对比

同一时间段、同一交易引擎、同一风控参数，唯一区别是选股方法：
  - V4 纯策略：V4 评分 + 龙虎榜/股东加分 → 混合评分 Top5
  - V4+ML    ：V4 评分 + 龙虎榜/股东加分 → ML 预测 → 百分位过滤 → 混合评分 Top5

用法:
    python3 scripts/backtest_v4_vs_v4ml.py            # 对比 v4_only vs v4+ml(v8.0)
    python3 scripts/backtest_v4_vs_v4ml.py --tune     # 调优：扫描 ML 参数
"""
import os, sys, json, subprocess
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np

# ========== 回测参数 ==========
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

START_DATE, END_DATE = "2025-10-01", "2026-05-08"
TOP_N = 5
V41_CANDIDATE_LIMIT = 30
INITIAL_CASH = 100000.0
MAX_POSITIONS = 5
MAX_HOLD_DAYS = 7
COMMISSION = 0.0003
SLIPPAGE = 0.0001
STOP_LOSS = -0.03
TAKE_PROFIT_TIERS = [(0.06, 1 / 3), (0.10, 1 / 3), (0.18, 1.0)]
ML_PERCENTILE_THRESHOLD = 0.35
ML_BLEND_WEIGHT = 0.40

# ========== 数据加载 ==========

def load_df(sql):
    from quant_app.utils.config import get_db_config
    import pymysql
    conn = pymysql.connect(**get_db_config())
    df = __import__('pandas').read_sql(sql, conn)
    conn.close()
    return df


def load_common_data():
    import pandas as pd
    daily = load_df(f"""
        SELECT ts_code, trade_date, close, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, high_52w, low_52w, vol, amount
        FROM daily_price
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
    """)
    for c in ['vol', 'amount', 'close']:
        daily[c] = daily[c].fillna(0)

    mf = load_df(f"""
        SELECT ts_code, trade_date, main_net FROM moneyflow_daily
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}'
    """)
    mf['main_net'] = mf['main_net'].fillna(0)
    daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left')
    daily['main_net'] = daily['main_net'].fillna(0)
    daily = daily.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

    dt_df = load_df(f"""
        SELECT ts_code, trade_date, net_buy FROM dragon_tiger
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' AND net_buy != 0
    """)
    dti_df = load_df(f"""
        SELECT ts_code, trade_date, net_buy FROM dragon_tiger_inst
        WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' AND net_buy != 0
    """)
    hc_df = load_df(f"""
        SELECT ts_code, end_date as trade_date, holder_num_change FROM holder_change
        WHERE end_date >= '{START_DATE}' AND end_date <= '{END_DATE}'
    """)
    dt_d, dti_d, hc_d = defaultdict(list), defaultdict(list), defaultdict(list)
    for _, r in dt_df.iterrows():
        dt_d[r['ts_code']].append((str(r['trade_date'])[:10], float(r['net_buy'] or 0)))
    for _, r in dti_df.iterrows():
        dti_d[r['ts_code']].append((str(r['trade_date'])[:10], float(r['net_buy'] or 0)))
    for _, r in hc_df.iterrows():
        hc_d[r['ts_code']].append((str(r['trade_date'])[:10], int(r['holder_num_change'] or 0)))

    return daily, dt_d, dti_d, hc_d


# ========== V4 评分（与 V4+ML 脚本一致） ==========

def v4_score(row):
    pct = float(row.get('pct_chg', 0))
    vr = float(row.get('volume_ratio', 0))
    tr = float(row.get('turnover_rate', 0))
    ma5 = float(row.get('ma5', 0))
    ma10 = float(row.get('ma10', 0))
    ma20 = float(row.get('ma20', 0))
    rps = float(row.get('rps_20', 0))
    close = float(row.get('close', 0))
    h52w = float(row.get('high_52w', 0) or 0)
    l52w = float(row.get('low_52w', 0) or 0)
    main_net = float(row.get('main_net', 0) or 0)
    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return -1
    if not ((1.0 < vr < 10 and tr > 1.5 and ma5 > ma10 > ma20 and close > ma5) or
            (pct > 4.0 and vr > 2.0 and close > ma5)):
        return -1
    sc = 0
    if -3 <= pct < 0:
        sc += 30
    elif 0 <= pct <= 3:
        sc += 25
    elif 3 < pct <= 5:
        sc += 30
    elif 5 < pct <= 10:
        sc += 20
    else:
        return -1
    if vr > 3:
        sc += 30
    elif vr > 1.5:
        sc += 25
    elif vr > 1.0:
        sc += 10
    if 5 <= tr <= 10:
        sc += 20
    elif 3 <= tr < 5:
        sc += 15
    elif 2 <= tr < 3:
        sc += 8
    elif tr > 20:
        sc += 5
    sc += 30 if ma5 > ma10 > ma20 else 16
    if rps >= 80:
        sc += 20
    elif rps >= 60:
        sc += 15
    elif rps >= 40:
        sc += 10
    if h52w and l52w and h52w > l52w > 0:
        pos = (close - l52w) / (h52w - l52w) * 100
        if pos < 60:
            sc += 15
        elif pos >= 85:
            return -1
    if main_net > 5000:
        sc += 15
    elif main_net > 1000:
        sc += 10
    elif main_net > 0:
        sc += 5
    return sc


def dragon_bonus(tc, date, dt_d, dti_d):
    td_30 = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    inst = sum(nb for td, nb in dti_d.get(tc, []) if td >= td_30)
    if inst > 30000000:
        return 15
    elif inst > 5000000:
        return 12
    if sum(1 for td, _ in dt_d.get(tc, []) if td >= td_30) > 0:
        return 8
    return 0


def holder_bonus(tc, date, hc_d):
    rows = sorted([(td, c) for td, c in hc_d.get(tc, []) if td <= date], reverse=True)
    if len(rows) < 2:
        return 0
    dec = sum(1 for _, c in rows[:4] if c < 0)
    if dec >= 3:
        return 10
    elif dec >= 2:
        return 7
    elif dec >= 1:
        return 4
    return 0


# ========== 交易引擎 ==========

def run_backengine(price_data, daily_buys, trade_dates_ymd, label="策略"):
    """通用交易引擎：接收每日买入列表 {(date): [(code, score, ...)]}，返回指标"""
    cash = INITIAL_CASH
    positions = {}
    trades = []
    equity_curve = []
    scan_days = 0
    gap_up_skipped = 0

    for i in range(len(trade_dates_ymd) - 1):
        today = trade_dates_ymd[i]
        tomorrow = trade_dates_ymd[i + 1]
        today_str = today  # ymd format

        # 卖出处理
        sell_codes = []
        for code, pos in list(positions.items()):
            price_info = price_data.get((code, tomorrow))
            if not price_info or price_info[0] <= 0:
                pos['days_held'] += 1
                if pos['days_held'] >= MAX_HOLD_DAYS:
                    sell_codes.append((code, '超时卖出', price_info[0] if price_info else pos['buy_price']))
                continue

            close = price_info[0]
            pos['days_held'] += 1

            if pos['days_held'] <= 0:
                pass

            if close > 0 and pos['buy_price'] > 0:
                pct = (close - pos['buy_price']) / pos['buy_price']
                if pct < STOP_LOSS:
                    sell_codes.append((code, f'止损{STOP_LOSS * 100:.0f}%', close))
                    continue

                # 阶梯止盈
                remaining = 1.0 - pos.get('tiers_sold', 0.0)
                if remaining > 0:
                    for ti, (tp_level, tp_ratio) in enumerate(TAKE_PROFIT_TIERS):
                        if pct >= tp_level and not pos.get(f'tier_{ti}_sold', False):
                            sell_shares = int(pos['shares'] * tp_ratio * remaining)
                            if sell_shares > 0:
                                sell_value = sell_shares * close * (1 - COMMISSION - SLIPPAGE)
                                cash += sell_value
                                pos['tiers_sold'] = pos.get('tiers_sold', 0.0) + tp_ratio * remaining
                                pos[f'tier_{ti}_sold'] = True
                                trades.append({
                                    'type': f'止盈{tp_level * 100:.0f}%',
                                    'date': tomorrow,
                                    'code': code,
                                    'price': close,
                                    'shares': sell_shares,
                                    'value': round(sell_value, 2),
                                    'pnl': round(sell_value - sell_shares * pos['buy_price'], 2),
                                })
                            break

            if pos['days_held'] >= MAX_HOLD_DAYS:
                sell_codes.append((code, '超时卖出', close))

        for code, reason, sell_price in sell_codes:
            if code in positions:
                pos = positions.pop(code)
                remaining_shares = pos['shares'] * (1 - pos.get('tiers_sold', 0.0))
                if remaining_shares > 0:
                    sell_value = remaining_shares * sell_price * (1 - COMMISSION - SLIPPAGE)
                    cash += sell_value
                    pnl_pct = (sell_price - pos['buy_price']) / pos['buy_price'] * 100
                    trades.append({
                        'type': reason,
                        'date': tomorrow,
                        'code': code,
                        'price': round(sell_price, 2),
                        'shares': int(remaining_shares),
                        'value': round(sell_value, 2),
                        'pnl': round(sell_value - remaining_shares * pos['buy_price'], 2),
                        'pnl_pct': round(pnl_pct, 1),
                        'hold_days': pos['days_held'],
                    })

        # 买入处理
        if today_str in daily_buys:
            scan_days += 1
            buy_list = daily_buys[today_str]
            for code, *_ in buy_list:
                if len(positions) >= MAX_POSITIONS:
                    break
                if code in positions:
                    continue
                price_info = price_data.get((code, today))
                if not price_info or price_info[0] <= 0:
                    continue
                buy_price = price_info[0]
                price_info_tomorrow = price_data.get((code, tomorrow))
                if price_info_tomorrow and price_info_tomorrow[0] > 0:
                    gap_pct = (price_info_tomorrow[0] - buy_price) / buy_price * 100
                    if gap_pct > 2:
                        gap_up_skipped += 1
                        continue
                position_cash = cash / (MAX_POSITIONS - len(positions))
                shares = int(position_cash / (buy_price * (1 + COMMISSION)))
                if shares <= 0:
                    continue
                cost = shares * buy_price * (1 + COMMISSION)
                if cost > cash:
                    shares = int(cash / (buy_price * (1 + COMMISSION)))
                    if shares <= 0:
                        continue
                    cost = shares * buy_price * (1 + COMMISSION)
                cash -= cost
                positions[code] = {
                    'buy_date': today,
                    'buy_price': buy_price,
                    'shares': shares,
                    'days_held': 0,
                    'highest_pct': 0.0,
                    'tiers_sold': 0.0,
                }
                trades.append({
                    'type': '买入',
                    'date': today_str,
                    'code': code,
                    'price': round(buy_price, 2),
                    'shares': shares,
                    'value': round(cost, 2),
                })

        # 计算净值
        pos_value = 0.0
        for code, pos in list(positions.items()):
            pi = price_data.get((code, today))
            if pi and pi[0] > 0:
                remaining = 1.0 - pos.get('tiers_sold', 0.0)
                pos_value += pos['shares'] * pi[0] * remaining
        equity_curve.append({
            'date': today_str,
            'cash': round(cash, 2),
            'position': round(pos_value, 2),
            'total': round(cash + pos_value, 2),
        })

    # 平仓
    for code, pos in list(positions.items()):
        pi = price_data.get((code, trade_dates_ymd[-1]))
        sell_price = pi[0] if pi and pi[0] > 0 else pos['buy_price']
        remaining = 1.0 - pos.get('tiers_sold', 0.0)
        if remaining > 0 and pos['shares'] > 0:
            sell_value = pos['shares'] * remaining * sell_price * (1 - COMMISSION - SLIPPAGE)
            cash += sell_value

    # 统计
    total_trades = [t for t in trades if t['type'] not in ('买入',)]
    win_trades = [t for t in total_trades if t.get('pnl', 0) > 0]
    lose_trades = [t for t in total_trades if t.get('pnl', 0) <= 0]
    final_value = equity_curve[-1]['total'] if equity_curve else cash
    total_return = (final_value / INITIAL_CASH - 1) * 100
    win_rate = len(win_trades) / len(total_trades) * 100 if total_trades else 0
    avg_win = np.mean([t.get('pnl', 0) for t in win_trades]) if win_trades else 0
    avg_loss = np.mean([t.get('pnl', 0) for t in lose_trades]) if lose_trades else 0
    profit_factor = abs(
        sum(t.get('pnl', 0) for t in win_trades) /
        (sum(t.get('pnl', 0) for t in lose_trades) or 1)
    )

    equity_vals = [e['total'] for e in equity_curve]
    daily_returns = []
    for j in range(1, len(equity_vals)):
        if equity_vals[j - 1] > 0:
            daily_returns.append(equity_vals[j] / equity_vals[j - 1] - 1)
    sharpe = (
        np.mean(daily_returns) / (np.std(daily_returns) + 1e-9) * np.sqrt(250)
        if daily_returns else 0
    )

    peak = equity_vals[0] if equity_vals else INITIAL_CASH
    max_dd = 0
    for v in equity_vals:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        'label': label,
        'total_return_pct': round(total_return, 2),
        'win_rate_pct': round(win_rate, 1),
        'sharpe_ratio': round(sharpe, 2),
        'max_drawdown_pct': round(max_dd, 2),
        'total_trades': len(total_trades),
        'profit_factor': round(profit_factor, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'gap_up_skipped': gap_up_skipped,
        'scan_days': scan_days,
    }


# ========== 策略运行入口 ==========

def run_v4_only(daily, dt_d, dti_d, hc_d, trade_dates, trade_dates_ymd):
    """纯 V4 策略回测"""
    import pandas as pd
    dly = daily.copy()
    dly['trade_date'] = pd.to_datetime(dly['trade_date'])
    dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')

    daily_buys = {}
    for date in trade_dates:
        day = dly[dly['date_str'] == date]
        if day.empty:
            continue
        cands = []
        for _, row in day.iterrows():
            tc = row['ts_code']
            sc = v4_score(row)
            if sc < 0:
                continue
            sc += dragon_bonus(tc, date, dt_d, dti_d)
            sc += holder_bonus(tc, date, hc_d)
            cands.append((tc, sc))
        cands.sort(key=lambda x: x[1], reverse=True)
        daily_buys[date.replace('-', '')] = [(tc, sc) for tc, sc in cands[:TOP_N]]

    price_data = {}
    for _, row in daily.iterrows():
        tc = row['ts_code']
        dt_ymd = str(row['trade_date'])[:10].replace('-', '')
        price_data[(tc, dt_ymd)] = (float(row['close'] or 0), float(row['pct_chg'] or 0))

    return run_backengine(price_data, daily_buys, trade_dates_ymd, label="V4 纯策略")


def run_v4_ml(daily, dt_d, dti_d, hc_d, trade_dates, trade_dates_ymd, model_version='v8.0'):
    """V4+ML 策略回测"""
    import pandas as pd
    import pymysql
    from quant_app.utils.model_loader import load_model
    from quant_app.utils.config import get_db_config
    from ml_predict import _ensemble_predict, _scores_to_percentile

    FEATURE_BUILDERS = {
        'v6.5': '_build_features_for_stocks_v6_3',
        'v8.0': '_build_features_for_stocks_v8_0',
    }

    bundle = load_model(model_version)
    if bundle is None:
        return None
    version = bundle.get('version', model_version)
    ic = bundle.get('final_rank_ic', 0)

    feat_func_name = FEATURE_BUILDERS.get(model_version)
    import ml_predict
    feature_builder = getattr(ml_predict, feat_func_name)

    dly = daily.copy()
    dly['trade_date'] = pd.to_datetime(dly['trade_date'])
    dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')

    daily_v41_candidates = {}
    for date in trade_dates:
        day = dly[dly['date_str'] == date]
        if day.empty:
            continue
        cands = []
        for _, row in day.iterrows():
            tc = row['ts_code']
            sc = v4_score(row)
            if sc < 0:
                continue
            sc += dragon_bonus(tc, date, dt_d, dti_d)
            sc += holder_bonus(tc, date, hc_d)
            cands.append((tc, sc))
        cands.sort(key=lambda x: x[1], reverse=True)
        daily_v41_candidates[date] = cands[:V41_CANDIDATE_LIMIT]

    conn = pymysql.connect(**get_db_config())
    ml_cache = {}
    for di, date in enumerate(trade_dates):
        cands = [tc for tc, _ in daily_v41_candidates.get(date, [])]
        if not cands:
            continue
        try:
            feat_df = feature_builder(conn, cands, as_of_date=date)
            if feat_df is not None and not feat_df.empty:
                preds = _ensemble_predict(feat_df, bundle)
                for i, (_, row) in enumerate(feat_df.iterrows()):
                    ml_cache[(row['ts_code'], date)] = float(preds[i])
        except Exception as e:
            pass
        for tc in cands:
            if (tc, date) not in ml_cache:
                ml_cache[(tc, date)] = 0.0
    conn.close()

    price_data = {}
    for _, row in daily.iterrows():
        tc = row['ts_code']
        dt_ymd = str(row['trade_date'])[:10].replace('-', '')
        price_data[(tc, dt_ymd)] = (float(row['close'] or 0), float(row['pct_chg'] or 0))

    daily_buys = {}
    ml_passed = 0
    ml_filtered = 0
    pct_threshold = ML_PERCENTILE_THRESHOLD
    blend_weight = ML_BLEND_WEIGHT

    for date in trade_dates:
        date_ymd = date.replace('-', '')
        cands = daily_v41_candidates.get(date, [])
        if not cands:
            continue
        cands_with_ml = []
        for tc, v4sc in cands:
            ml = ml_cache.get((tc, date), 0.0)
            cands_with_ml.append((tc, v4sc, ml))
        ml_raws = np.array([x[2] for x in cands_with_ml])
        ml_pcts = _scores_to_percentile(ml_raws)

        filtered = []
        for i, (tc, v4sc, ml) in enumerate(cands_with_ml):
            ml_pct = float(ml_pcts[i])
            if ml_pct >= pct_threshold:
                blend = v4sc * (1 - blend_weight) + ml_pct * 100 * blend_weight
                filtered.append((tc, v4sc, ml, ml_pct, blend))
                ml_passed += 1
            else:
                ml_filtered += 1
        filtered.sort(key=lambda x: x[4], reverse=True)
        daily_buys[date_ymd] = [(tc, v4sc, ml) for tc, v4sc, ml, _, _ in filtered[:TOP_N]]

    result = run_backengine(price_data, daily_buys, trade_dates_ymd, label=f"V4+ML ({model_version})")
    result['ml_passed'] = ml_passed
    result['ml_filtered'] = ml_filtered
    result['ml_percentile_threshold'] = pct_threshold
    result['ml_blend_weight'] = blend_weight
    result['model_version'] = model_version
    result['model_ic'] = ic
    return result


def run_comparison():
    """对比运行"""
    import pandas as pd
    print(f"\n{'=' * 80}")
    print(f"  V4 纯策略 vs V4+ML 统一回测对比")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  选股: Top{TOP_N}, 最大持仓: {MAX_POSITIONS}, 止损: {STOP_LOSS * 100:.0f}%, 最长持仓: {MAX_HOLD_DAYS}天")
    print(f"{'=' * 80}\n")

    import pandas as pd
    daily, dt_d, dti_d, hc_d = load_common_data()
    dly = daily.copy()
    dly['trade_date'] = pd.to_datetime(dly['trade_date'])
    dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')
    trade_dates = sorted(dly['date_str'].unique())
    trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]
    trade_dates_ymd = [d.replace('-', '') for d in trade_dates]
    print(f"交易日: {trade_dates[0]} ~ {trade_dates[-1]}, 共 {len(trade_dates)} 天\n")

    # 先跑纯 V4
    print("  运行 V4 纯策略...")
    v4_result = run_v4_only(daily, dt_d, dti_d, hc_d, trade_dates, trade_dates_ymd)

    # 再跑 V4+ML
    print("  运行 V4+ML (v8.0)...")
    ml_result = run_v4_ml(daily, dt_d, dti_d, hc_d, trade_dates, trade_dates_ymd, 'v8.0')

    # 输出对比
    print(f"\n{'=' * 80}")
    print(f"  对比结果")
    print(f"{'=' * 80}")
    print(f"  {'指标':<15s} {'V4 纯策略':>12s} {'V4+ML v8.0':>12s}")
    print(f"  {'-' * 42}")
    metrics = [
        ('总收益率%', 'total_return_pct', lambda x: f"{x:.2f}%"),
        ('胜率%', 'win_rate_pct', lambda x: f"{x:.1f}%"),
        ('夏普比率', 'sharpe_ratio', lambda x: f"{x:.2f}"),
        ('最大回撤%', 'max_drawdown_pct', lambda x: f"{x:.2f}%"),
        ('交易次数', 'total_trades', lambda x: f"{x:d}"),
        ('盈亏比', 'profit_factor', lambda x: f"{x:.2f}"),
        ('平均盈利', 'avg_win', lambda x: f"{x:.0f}"),
        ('平均亏损', 'avg_loss', lambda x: f"{x:.0f}"),
        ('扫描天数', 'scan_days', lambda x: f"{x:d}"),
    ]
    for label, key, fmt in metrics:
        v4_val = fmt(v4_result[key])
        ml_val = fmt(ml_result[key]) if ml_result else "N/A"
        print(f"  {label:<15s} {v4_val:>12s} {ml_val:>12s}")

    if ml_result:
        diff = ml_result['total_return_pct'] - v4_result['total_return_pct']
        print(f"\n  ML 增强收益差: {diff:+.2f}%")
        winner = 'V4+ML' if diff > 0 else 'V4 纯策略'
        print(f"  ★ 胜出: {winner}")
        print(f"  ML 通过/过滤: {ml_result['ml_passed']}/{ml_result['ml_filtered']}")

    out = {
        'comparison': 'V4 vs V4+ML',
        'period': f'{START_DATE} ~ {END_DATE}',
        'params': {
            'top_n': TOP_N, 'max_positions': MAX_POSITIONS,
            'max_hold_days': MAX_HOLD_DAYS, 'stop_loss': STOP_LOSS,
            'take_profit_tiers': TAKE_PROFIT_TIERS,
            'ml_percentile_threshold': ML_PERCENTILE_THRESHOLD,
            'ml_blend_weight': ML_BLEND_WEIGHT,
        },
        'v4_only': v4_result,
        'v4_ml': ml_result,
    }
    out_path = os.path.join(PROJECT_DIR, 'data', 'backtest_v4_vs_v4ml.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


def run_tune():
    """调优模式：扫描 ML 参数"""
    pct_candidates = [0.10, 0.20, 0.35, 0.50, 0.65]
    bw_candidates = [0.0, 0.20, 0.40, 0.60, 0.80, 1.0]

    print(f"\n{'=' * 80}")
    print(f"  调优模式：扫描百分位阈值 × ML 权重")
    print(f"  共 {len(pct_candidates) * len(bw_candidates)} 个组合")
    print(f"{'=' * 80}\n")

    results = []
    for pct in pct_candidates:
        for bw in bw_candidates:
            global ML_PERCENTILE_THRESHOLD, ML_BLEND_WEIGHT
            ML_PERCENTILE_THRESHOLD = pct
            ML_BLEND_WEIGHT = bw
            print(f"  测试: pct_th={pct}, bw={bw}")

            proc = subprocess.run(
                [sys.executable, __file__, '--run-ml-only', str(pct), str(bw)],
                capture_output=True, text=True, cwd=PROJECT_DIR, timeout=600,
            )
            for line in proc.stdout.split('\n'):
                if line.startswith('__RESULT__'):
                    json_str = line.replace('__RESULT__', '').replace('__RESULT_END__', '')
                    r = json.loads(json_str)
                    r['pct_threshold'] = pct
                    r['blend_weight'] = bw
                    results.append(r)
                    print(f"    → 收益: {r['total_return_pct']:.2f}%, 夏普: {r['sharpe_ratio']:.2f}, "
                          f"胜率: {r['win_rate_pct']:.1f}%")

    if not results:
        print("调优未获取到任何结果")
        return

    results.sort(key=lambda x: x['total_return_pct'], reverse=True)

    print(f"\n{'=' * 80}")
    print(f"  调优结果 (按收益率降序)")
    print(f"{'=' * 80}")
    print(f"  {'pct_th':>6s} {'bw':>4s} {'收益率':>8s} {'夏普':>6s} {'胜率':>6s} {'交易':>4s} {'盈亏比':>6s} {'回撤':>8s}")
    print(f"  {'-' * 64}")
    for r in results:
        print(
            f"  {r['pct_threshold']:>5.2f}  {r['blend_weight']:>3.1f}  "
            f"{r['total_return_pct']:>6.2f}%  {r['sharpe_ratio']:>5.2f}  "
            f"{r['win_rate_pct']:>5.1f}%  {r['total_trades']:>3d}  {r['profit_factor']:>5.2f}  "
            f"{r['max_drawdown_pct']:>6.2f}%"
        )

    best = results[0]
    print(f"\n  ★ 最优: pct_th={best['pct_threshold']}, bw={best['blend_weight']}")
    print(f"    收益: {best['total_return_pct']:.2f}%, 夏普: {best['sharpe_ratio']:.2f}, 回撤: {best['max_drawdown_pct']:.2f}%")

    out_path = os.path.join(PROJECT_DIR, 'data', 'backtest_v4_ml_tune.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'tune_results': results, 'best': {
            'pct_threshold': best['pct_threshold'],
            'blend_weight': best['blend_weight'],
            'total_return_pct': best['total_return_pct'],
            'sharpe_ratio': best['sharpe_ratio'],
        }}, f, ensure_ascii=False, indent=2)
    print(f"\n  已保存: {out_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--tune', action='store_true', help='调优模式')
    parser.add_argument('--run-ml-only', nargs=2, type=float,
                       help='内部用：只跑 V4+ML，指定 pct bw')
    args = parser.parse_args()

    if args.run_ml_only:
        global ML_PERCENTILE_THRESHOLD, ML_BLEND_WEIGHT
        ML_PERCENTILE_THRESHOLD = args.run_ml_only[0]
        ML_BLEND_WEIGHT = args.run_ml_only[1]
        daily, dt_d, dti_d, hc_d = load_common_data()
        import pandas as pd
        dly = daily.copy()
        dly['trade_date'] = pd.to_datetime(dly['trade_date'])
        dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')
        trade_dates = sorted(dly['date_str'].unique())
        trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]
        trade_dates_ymd = [d.replace('-', '') for d in trade_dates]
        result = run_v4_ml(daily, dt_d, dti_d, hc_d, trade_dates, trade_dates_ymd, 'v8.0')
        if result:
            print(f"__RESULT__{json.dumps(result)}__RESULT_END__")
        return

    if args.tune:
        run_tune()
    else:
        run_comparison()


if __name__ == '__main__':
    main()
