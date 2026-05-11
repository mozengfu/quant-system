#!/usr/bin/env python3
"""
V4+ML 回测对比：V6.5 vs V8.0

通过子进程隔离运行，每个版本独立加载模型和特征构建函数。
避免文件重命名/模块缓存污染等问题。

用法:
    # 单版本运行
    python3 scripts/backtest_v4_ml_v65_vs_v80.py --model v6.5
    python3 scripts/backtest_v4_ml_v65_vs_v80.py --model v8.0

    # 对比运行（自动跑两轮并合并结果）
    python3 scripts/backtest_v4_ml_v65_vs_v80.py

输出:
    data/backtest_v4_v65_vs_v80.json — 两套回测参数对比
"""
import os, sys, json, logging, subprocess
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

OUT_DIR = os.path.join(PROJECT_DIR, 'data')
START_DATE, END_DATE = "2025-10-01", "2026-05-08"
TOP_N = 5
V41_CANDIDATE_LIMIT = 30
INITIAL_CASH = 100000.0
MAX_POSITIONS = 5
MAX_HOLD_DAYS = 7
COMMISSION = 0.0003
SLIPPAGE = 0.0001
STOP_LOSS = -0.03
TRAILING_THRESHOLDS = [(0.05, 0.00), (0.10, 0.05), (0.15, 0.10)]
TAKE_PROFIT_TIERS = [(0.06, 1/3), (0.10, 1/3), (0.18, 1.0)]
ML_PERCENTILE_THRESHOLD = 0.35   # ML 百分位低于此值过滤
ML_BLEND_WEIGHT = 0.40           # 混合评分中 ML 权重

# 版本 → 特征构建函数映射
FEATURE_BUILDERS = {
    'v6.5': '_build_features_for_stocks_v6_3',
    'v8.0': '_build_features_for_stocks_v8_0',
}


def load_df(sql):
    from quant_app.utils.config import get_db_config
    import pymysql
    conn = pymysql.connect(**get_db_config())
    df = pd.read_sql(sql, conn)
    conn.close()
    return df


def load_common_data():
    """加载回测所需的公共数据"""
    logger.info("加载数据...")
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


def run_single_model(model_version, daily, dt_d, dti_d, hc_d,
                     trade_dates, trade_dates_ymd):
    """
    运行单个模型版本的回测。
    model_version: 'v6.5' 或 'v8.0'
    直接加载指定版本模型，使用对应的特征构建函数。
    """
    import pandas as pd
    import numpy as np
    import pymysql
    from quant_app.utils.model_loader import load_model
    from quant_app.utils.config import get_db_config
    from ml_predict import _ensemble_predict, _scores_to_percentile

    # 直接加载指定版本模型（绕过 _load_best_model 的版本优先逻辑）
    bundle = load_model(model_version)
    if bundle is None:
        logger.error(f"模型 {model_version} 加载失败，跳过")
        return None

    # 确认版本
    version = bundle.get('version', model_version)
    ic = bundle.get('final_rank_ic', 0)
    logger.info(f"  模型: {model_version}, 版本标签: {version}, IC={ic:.4f}")

    # 获取对应特征构建函数
    feat_func_name = FEATURE_BUILDERS.get(model_version)
    if feat_func_name is None:
        logger.error(f"未知模型版本特征映射: {model_version}")
        return None
    import ml_predict
    feature_builder = getattr(ml_predict, feat_func_name)

    # 预计算 V4.1 候选列表
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

    # ML 预测
    conn = pymysql.connect(**get_db_config())
    ml_cache = {}
    for di, date in enumerate(trade_dates):
        cands = [tc for tc, _ in daily_v41_candidates.get(date, [])]
        if not cands:
            continue
        if (di + 1) % 30 == 0:
            logger.info(f"  ML预测进度: {di+1}/{len(trade_dates)} ({datetime.now().strftime('%H:%M')})")
        try:
            feat_df = feature_builder(conn, cands, as_of_date=date)
            if feat_df is not None and not feat_df.empty:
                preds = _ensemble_predict(feat_df, bundle)
                for i, (_, row) in enumerate(feat_df.iterrows()):
                    ml_cache[(row['ts_code'], date)] = float(preds[i])
        except Exception as e:
            logger.warning(f"  ML失败 {date}: {e}")
        for tc in cands:
            if (tc, date) not in ml_cache:
                ml_cache[(tc, date)] = 0.0
    conn.close()

    # 价格查找表
    price_data = {}
    for _, row in daily.iterrows():
        tc = row['ts_code']
        dt_ymd = str(row['trade_date'])[:10].replace('-', '')
        price_data[(tc, dt_ymd)] = (float(row['close'] or 0), float(row['pct_chg'] or 0))

    # 回测引擎（百分位过滤 + 混合评分排序）
    daily_buys = {}
    ml_passed = 0
    ml_filtered = 0
    pct_threshold = ML_PERCENTILE_THRESHOLD
    blend_weight = ML_BLEND_WEIGHT

    for date in trade_dates:
        cands = daily_v41_candidates.get(date, [])
        if not cands:
            continue
        # 收集当日所有 ML 分数 → 百分位
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
        daily_buys[date] = [(tc, v4sc, ml) for tc, v4sc, ml, _, _ in filtered[:TOP_N]]

    cash = INITIAL_CASH
    positions = {}
    trades = []
    equity_curve = []
    scan_days = 0
    gap_up_skipped = 0

    for i in range(len(trade_dates_ymd) - 1):
        today = trade_dates_ymd[i]
        tomorrow = trade_dates_ymd[i + 1]
        today_str = trade_dates[i]

        sell_codes = []
        for code, pos in list(positions.items()):
            price_info = price_data.get((code, tomorrow))
            if not price_info or price_info[0] <= 0:
                pos['days_held'] += 1
                if pos['days_held'] >= MAX_HOLD_DAYS:
                    sell_codes.append((
                        code, '超时卖出',
                        price_info[0] if price_info else pos['buy_price']
                    ))
                continue

            close = price_info[0]
            pct = (close - pos['buy_price']) / pos['buy_price']
            pos['days_held'] += 1

            if pct < STOP_LOSS:
                sell_codes.append((code, f'止损{STOP_LOSS * 100:.0f}%', close))
                continue

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

        if today_str in daily_buys:
            scan_days += 1
            buy_list = daily_buys[today_str]
            for code, v4sc, mlscore in buy_list:
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

    for code, pos in list(positions.items()):
        pi = price_data.get((code, trade_dates_ymd[-1]))
        sell_price = pi[0] if pi and pi[0] > 0 else pos['buy_price']
        remaining = 1.0 - pos.get('tiers_sold', 0.0)
        if remaining > 0 and pos['shares'] > 0:
            sell_value = pos['shares'] * remaining * sell_price * (1 - COMMISSION - SLIPPAGE)
            cash += sell_value

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

    peak = equity_vals[0]
    max_dd = 0
    for v in equity_vals:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        'model_version': model_version,
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
        'ml_passed': ml_passed,
        'ml_filtered': ml_filtered,
        'ml_percentile_threshold': pct_threshold,
        'ml_blend_weight': blend_weight,
    }


def run_single(model_version):
    """单版本运行入口（子进程调用），支持环境变量调参"""
    import pandas as pd
    import numpy as np

    # 支持环境变量覆盖参数（用于调优模式）
    global ML_PERCENTILE_THRESHOLD, ML_BLEND_WEIGHT
    env_pct = os.environ.get('ML_BACKTEST_PCT')
    env_bw = os.environ.get('ML_BACKTEST_BW')
    if env_pct:
        ML_PERCENTILE_THRESHOLD = float(env_pct)
    if env_bw:
        ML_BLEND_WEIGHT = float(env_bw)

    logger.info(f"  ML_PERCENTILE_THRESHOLD={ML_PERCENTILE_THRESHOLD}, ML_BLEND_WEIGHT={ML_BLEND_WEIGHT}")

    daily, dt_d, dti_d, hc_d = load_common_data()

    dly = daily.copy()
    dly['trade_date'] = pd.to_datetime(dly['trade_date'])
    dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')
    trade_dates = sorted(dly['date_str'].unique())
    trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]
    trade_dates_ymd = [d.replace('-', '') for d in trade_dates]
    logger.info(f"回测: {trade_dates[0]} ~ {trade_dates[-1]}, {len(trade_dates)}天")

    result = run_single_model(model_version, daily, dt_d, dti_d, hc_d,
                              trade_dates, trade_dates_ymd)
    if result is None:
        logger.error(f"模型 {model_version} 回测失败")
        return

    pct = result.get('ml_percentile_threshold', ML_PERCENTILE_THRESHOLD)
    bw = result.get('ml_blend_weight', ML_BLEND_WEIGHT)
    print(f"\n  {model_version} 回测结果:")
    print(f"    收益率: {result['total_return_pct']:.2f}%")
    print(f"    胜率:   {result['win_rate_pct']:.1f}%")
    print(f"    夏普:   {result['sharpe_ratio']:.2f}")
    print(f"    回撤:   {result['max_drawdown_pct']:.2f}%")
    print(f"    交易:   {result['total_trades']}")
    print(f"    盈亏比: {result['profit_factor']:.2f}")
    print(f"    ML通过/过滤: {result['ml_passed']}/{result['ml_filtered']} "
          f"(pct_th={pct}, bw={bw})")

    # 输出到 stdout 供父进程捕获
    print(f"\n__RESULT__{json.dumps(result)}__RESULT_END__")


def run_comparison():
    """对比运行：在两个子进程中执行两轮回测"""
    results = {}

    for ver in ['v6.5', 'v8.0']:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"启动子进程: {ver}")
        logger.info(f"{'=' * 60}")

        proc = subprocess.run(
            [sys.executable, __file__, '--model', ver],
            capture_output=True, text=True, cwd=PROJECT_DIR,
            timeout=600,
        )

        # 打印子进程输出
        for line in proc.stdout.split('\n'):
            if line.startswith('__RESULT__'):
                json_str = line.replace('__RESULT__', '').replace('__RESULT_END__', '')
                results[ver] = json.loads(json_str)
            else:
                print(line)

        if proc.stderr:
            for line in proc.stderr.split('\n'):
                if line.strip():
                    print(f"  [ERR] {line}")

        if ver not in results:
            logger.warning(f"  {ver} 未获取到结果，跳过")
            continue

    if not results:
        logger.error("没有获取到任何回测结果")
        return

    # 输出对比
    print(f"\n{'=' * 80}")
    print(f"  V4+ML 回测对比结果")
    print(f"{'=' * 80}")
    h = f"  {'模型':<8s} {'收益率':>8s} {'胜率':>6s} {'夏普':>6s} {'回撤':>8s} {'交易':>4s} {'盈亏比':>6s}"
    print(h)
    print(f"  {'-' * 60}")

    for ver in ['v6.5', 'v8.0']:
        if ver not in results:
            continue
        r = results[ver]
        print(
            f"  {ver:<8s} {r['total_return_pct']:>6.2f}%  {r['win_rate_pct']:>5.1f}%  "
            f"{r['sharpe_ratio']:>5.2f}  {r['max_drawdown_pct']:>6.2f}%  "
            f"{r['total_trades']:>3d}  {r['profit_factor']:>5.2f}"
        )

    if 'v6.5' in results and 'v8.0' in results:
        diff = results['v8.0']['total_return_pct'] - results['v6.5']['total_return_pct']
        print(f"\n  V8.0 vs V6.5 收益差: {diff:+.2f}%")
        winner = 'V8.0' if diff > 0 else 'V6.5'
        print(f"  ★ 胜出: {winner}")

    # 保存结果
    output = {
        'comparison': 'V6.5 vs V8.0',
        'period': f'{START_DATE} ~ {END_DATE}',
        'params': {
            'v41_candidate_limit': V41_CANDIDATE_LIMIT,
            'ml_percentile_threshold': ML_PERCENTILE_THRESHOLD,
            'ml_blend_weight': ML_BLEND_WEIGHT,
            'top_n': TOP_N,
            'initial_cash': INITIAL_CASH,
            'max_positions': MAX_POSITIONS,
            'max_hold_days': MAX_HOLD_DAYS,
            'stop_loss': STOP_LOSS,
            'take_profit_tiers': TAKE_PROFIT_TIERS,
        },
        'results': results,
    }

    if 'v6.5' in results and 'v8.0' in results:
        output['diff'] = {
            'total_return_pct_diff': round(
                results['v8.0']['total_return_pct'] - results['v6.5']['total_return_pct'], 2
            ),
            'winner': winner,
        }

    out_path = os.path.join(OUT_DIR, 'backtest_v4_v65_vs_v80.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


def run_tune_fine():
    """
    细粒度调优模式：围绕粗调最优值 (pct=0.2, bw=0.2) 做加密扫描。
    pct_threshold: [0.10, 0.15, 0.20, 0.25, 0.30]
    blend_weight:  [0.10, 0.15, 0.20, 0.25, 0.30]
    共 25 个组合。
    """
    pct_candidates = [0.10, 0.15, 0.20, 0.25, 0.30]
    bw_candidates = [0.10, 0.15, 0.20, 0.25, 0.30]

    logger.info(f"{'=' * 80}")
    logger.info(f"  细粒度调优：围绕粗调最优点加密扫描")
    logger.info(f"  ML_PERCENTILE_THRESHOLD: {pct_candidates}")
    logger.info(f"  ML_BLEND_WEIGHT:         {bw_candidates}")
    logger.info(f"  共 {len(pct_candidates) * len(bw_candidates)} 个组合")
    logger.info(f"{'=' * 80}")

    results = []
    for pct in pct_candidates:
        for bw in bw_candidates:
            logger.info(f"\n  测试: pct_th={pct}, bw={bw}")
            env = os.environ.copy()
            env['ML_BACKTEST_PCT'] = str(pct)
            env['ML_BACKTEST_BW'] = str(bw)

            proc = subprocess.run(
                [sys.executable, __file__, '--model', 'v8.0'],
                capture_output=True, text=True, cwd=PROJECT_DIR,
                timeout=600, env=env,
            )

            for line in proc.stdout.split('\n'):
                if line.startswith('__RESULT__'):
                    json_str = line.replace('__RESULT__', '').replace('__RESULT_END__', '')
                    r = json.loads(json_str)
                    r['pct_threshold'] = pct
                    r['blend_weight'] = bw
                    results.append(r)
                    print(f"    → 收益: {r['total_return_pct']:.2f}%, 夏普: {r['sharpe_ratio']:.2f}, "
                          f"胜率: {r['win_rate_pct']:.1f}%, ML通过率: {r['ml_passed']/(r['ml_passed']+r['ml_filtered']+1)*100:.0f}%")
                else:
                    pass  # suppress subprocess logs

    if not results:
        logger.error("调优未获取到任何结果")
        return

    results.sort(key=lambda x: x['total_return_pct'], reverse=True)

    print(f"\n{'=' * 80}")
    print(f"  细粒度调优结果 (按收益率降序)")
    print(f"{'=' * 80}")
    h = f"  {'pct_th':>6s} {'bw':>4s} {'收益率':>8s} {'夏普':>6s} {'胜率':>6s} {'交易':>4s} {'盈亏比':>6s} {'回撤':>8s} {'ML通过率':>8s}"
    print(h)
    print(f"  {'-' * 78}")

    for r in results:
        total = r['ml_passed'] + r['ml_filtered']
        pass_rate = r['ml_passed'] / total * 100 if total > 0 else 0
        print(
            f"  {r['pct_threshold']:>5.2f}  {r['blend_weight']:>3.2f}  "
            f"{r['total_return_pct']:>6.2f}%  {r['sharpe_ratio']:>5.2f}  "
            f"{r['win_rate_pct']:>5.1f}%  {r['total_trades']:>3d}  {r['profit_factor']:>5.2f}  "
            f"{r['max_drawdown_pct']:>6.2f}%  {pass_rate:>6.0f}%"
        )

    best = results[0]
    print(f"\n  ★ 最优参数: pct_threshold={best['pct_threshold']}, blend_weight={best['blend_weight']}")
    print(f"    收益率: {best['total_return_pct']:.2f}%, 夏普: {best['sharpe_ratio']:.2f}, 回撤: {best['max_drawdown_pct']:.2f}%")

    # 对比粗调最优
    print(f"\n  对比粗调最优 (pct=0.20, bw=0.20): 收益 +38.80%, 夏普 1.97, 回撤 18.56%")
    diff_ret = best['total_return_pct'] - 38.80
    print(f"  收益差: {diff_ret:+.2f}%")

    out_path = os.path.join(OUT_DIR, 'backtest_v4_ml_tune_fine.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'tune_fine_results': results, 'best': {
            'pct_threshold': best['pct_threshold'],
            'blend_weight': best['blend_weight'],
            'total_return_pct': best['total_return_pct'],
            'sharpe_ratio': best['sharpe_ratio'],
            'max_drawdown_pct': best['max_drawdown_pct'],
        }}, f, ensure_ascii=False, indent=2)
    print(f"\n  调优结果已保存: {out_path}")


def run_tune():
    """
    调优模式：扫描百分位阈值和 ML 权重的组合，寻找最优参数。
    使用 v8.0 模型（默认最优模型）。
    注意：使用子进程隔离避免模型加载污染。
    """
    pct_candidates = [0.20, 0.35, 0.50, 0.65]
    bw_candidates = [0.0, 0.20, 0.40, 0.60, 0.80, 1.0]

    logger.info(f"{'=' * 80}")
    logger.info(f"  调优模式：扫描参数空间")
    logger.info(f"  ML_PERCENTILE_THRESHOLD: {pct_candidates}")
    logger.info(f"  ML_BLEND_WEIGHT:         {bw_candidates}")
    logger.info(f"  共 {len(pct_candidates) * len(bw_candidates)} 个组合")
    logger.info(f"{'=' * 80}")

    results = []
    for pct in pct_candidates:
        for bw in bw_candidates:
            logger.info(f"\n  测试: pct_th={pct}, bw={bw}")
            # 通过环境变量传递参数，子进程读取
            env = os.environ.copy()
            env['ML_BACKTEST_PCT'] = str(pct)
            env['ML_BACKTEST_BW'] = str(bw)

            proc = subprocess.run(
                [sys.executable, __file__, '--model', 'v8.0'],
                capture_output=True, text=True, cwd=PROJECT_DIR,
                timeout=600, env=env,
            )

            for line in proc.stdout.split('\n'):
                if line.startswith('__RESULT__'):
                    json_str = line.replace('__RESULT__', '').replace('__RESULT_END__', '')
                    r = json.loads(json_str)
                    r['pct_threshold'] = pct
                    r['blend_weight'] = bw
                    results.append(r)
                    print(f"    → 收益: {r['total_return_pct']:.2f}%, 夏普: {r['sharpe_ratio']:.2f}, "
                          f"胜率: {r['win_rate_pct']:.1f}%, ML通过率: {r['ml_passed']/(r['ml_passed']+r['ml_filtered']+1)*100:.0f}%")
                else:
                    pass  # suppress subprocess logs

    # 排序输出
    if not results:
        logger.error("调优未获取到任何结果")
        return

    results.sort(key=lambda x: x['total_return_pct'], reverse=True)

    print(f"\n{'=' * 80}")
    print(f"  调优结果 (按收益率降序)")
    print(f"{'=' * 80}")
    h = f"  {'pct_th':>6s} {'bw':>4s} {'收益率':>8s} {'夏普':>6s} {'胜率':>6s} {'交易':>4s} {'盈亏比':>6s} {'ML通过率':>8s}"
    print(h)
    print(f"  {'-' * 68}")

    for r in results:
        total = r['ml_passed'] + r['ml_filtered']
        pass_rate = r['ml_passed'] / total * 100 if total > 0 else 0
        print(
            f"  {r['pct_threshold']:>5.2f}  {r['blend_weight']:>3.1f}  "
            f"{r['total_return_pct']:>6.2f}%  {r['sharpe_ratio']:>5.2f}  "
            f"{r['win_rate_pct']:>5.1f}%  {r['total_trades']:>3d}  {r['profit_factor']:>5.2f}  "
            f"{pass_rate:>6.0f}%"
        )

    best = results[0]
    print(f"\n  ★ 最优参数: pct_threshold={best['pct_threshold']}, blend_weight={best['blend_weight']}")
    print(f"    收益率: {best['total_return_pct']:.2f}%, 夏普: {best['sharpe_ratio']:.2f}")

    out_path = os.path.join(OUT_DIR, 'backtest_v4_ml_tune.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'tune_results': results, 'best': {
            'pct_threshold': best['pct_threshold'],
            'blend_weight': best['blend_weight'],
            'total_return_pct': best['total_return_pct'],
            'sharpe_ratio': best['sharpe_ratio'],
        }}, f, ensure_ascii=False, indent=2)
    print(f"\n  调优结果已保存: {out_path}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='V4+ML V6.5 vs V8.0 回测对比')
    parser.add_argument('--model', choices=['v6.5', 'v8.0', 'all'],
                       default='all', help='模型版本 (默认 all 对比运行)')
    parser.add_argument('--tune', action='store_true', help='调优模式：扫描参数空间')
    parser.add_argument('--tune-fine', action='store_true', help='细粒度调优：围绕最优值加密扫描')
    args = parser.parse_args()

    print(f"\n{'=' * 80}")
    print(f"  V4+ML 回测对比：V6.5 vs V8.0")
    print(f"  区间: {START_DATE} ~ {END_DATE}")
    print(f"  策略: V4.1 初筛({V41_CANDIDATE_LIMIT}只) → ML百分位过滤(≥{ML_PERCENTILE_THRESHOLD}) → "
          f"混合评分(ML权重{ML_BLEND_WEIGHT})取Top{TOP_N}")
    print(f"  风控: {STOP_LOSS * 100:.0f}%止损, {MAX_HOLD_DAYS}天最长持仓")
    print(f"{'=' * 80}\n")

    if args.tune_fine:
        run_tune_fine()
    elif args.tune:
        run_tune()
    elif args.model == 'all':
        run_comparison()
    else:
        run_single(args.model)


if __name__ == '__main__':
    main()
