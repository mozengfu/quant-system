"""
板块RPS周线 + ML 排序选股管线

流程: 板块周线数据 → 全量周累积收益 → RPS排序 → Top5板块成分股 → ML预测排序

两个模式:
  - weekly=True: 按ISO周聚合板块日频数据，算周线RPS（用户要求的模式）
  - weekly=False: 原60日滚动RPS（旧模式，备用）
"""
import logging

import numpy as np
import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

NOISE_KEYWORDS = [
    '融资融券', '转融', '沪股通', '深股通', '沪深',
    '昨日', '首板', '连板', '破发', '注册制',
    'ST', 'st', '退市', 'IPO', '新股',
    '热股', '多板', '次新', '百元',
]
NOISE_PREFIXES = ('885848', '885849', '885850', '88583')


def _get_max_date(conn):
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM board_concept_hist")
    return cur.fetchone()[0]


def _is_noise_board(name, code):
    if code and any(code.startswith(p) for p in NOISE_PREFIXES):
        return True
    if name:
        for kw in NOISE_KEYWORDS:
            if kw in name:
                return True
    return False


WEEKLY_RPS_WINDOW = 26  # 周线RPS滚动窗口（26周≈6个月，兼顾稳定性和灵敏度）
BOARD_OVERLAP_THRESHOLD = 0.50  # 板块去重阈值：成分股重叠超过此比例则视为同一主题


def _dedup_boards(board_df, top_n, max_candidates=30):
    """
    贪心板块去重。

    按 RPS 从高到低遍历候选板块，跳过与已选板块成分股重叠度 >50% 的，
    直到选够 top_n 个独立主题板块。

    Args:
        board_df: RPS排序后的板块DataFrame
        top_n: 需要的板块数
        max_candidates: 最多检查多少候选板块（防查太多）

    Returns:
        DataFrame: 去重后的板块列表（按原RPS顺序）
    """
    if len(board_df) <= top_n:
        return board_df

    candidates = board_df.head(max_candidates)
    codes = candidates['board_code'].tolist()

    conn = pymysql.connect(**get_db_config())
    try:
        ph = ','.join(['%s'] * len(codes))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT board_code, ts_code
            FROM board_concept_cons
            WHERE board_code IN ({ph})
              AND (is_latest = 1 OR is_latest IS NULL)
        """, codes)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    # {board_code: set(ts_code)}
    board_sets: dict[str, set] = {}
    for bcode, tscode in rows:
        board_sets.setdefault(bcode, set()).add(tscode)

    # 贪心选择：跳过与已选板块重叠过多的
    selected = []
    selected_stocks: set = set()
    for _, row in candidates.iterrows():
        if len(selected) >= top_n:
            break
        code = row['board_code']
        stocks = board_sets.get(code, set())

        if selected_stocks and stocks:
            # 双向最小集重叠检查：任一小集被另一集包含 >50% 即视为同一主题
            is_dup = False
            for sel_code in selected:
                sel_stocks = board_sets.get(sel_code, set())
                if not sel_stocks:
                    continue
                overlap = len(stocks & sel_stocks)
                smaller = min(len(stocks), len(sel_stocks))
                if overlap / max(smaller, 1) > BOARD_OVERLAP_THRESHOLD:
                    is_dup = True
                    break
            if is_dup:
                logger.debug("跳过高重叠板块: %s(%s)", row['board_name'], code)
                continue

        selected.append(code)
        selected_stocks |= stocks

    # 兜底：去重后不足 top_n，从剩余候选中补
    if len(selected) < top_n:
        for _, row in candidates.iterrows():
            if len(selected) >= top_n:
                break
            if row['board_code'] not in selected:
                selected.append(row['board_code'])

    return board_df[board_df['board_code'].isin(selected)]


def get_board_rps(as_of_date=None, period=60, min_days=20, use_weekly=True):
    """计算概念板块 RPS

    Args:
        as_of_date: 截止日期
        period: 交易日窗口（日线模式，周线模式忽略）
        min_days: 最少数据天数（日线模式）
        use_weekly: 是否使用周线（True=周线RPS, False=60日RPS）

    Returns:
        DataFrame: [board_code, board_name, cum_return, n_days/weeks, rank, rps]
    """
    conn = pymysql.connect(**get_db_config())
    try:
        max_date = as_of_date or _get_max_date(conn)
        df = pd.read_sql("""
            SELECT board_code, board_name, trade_date, pct_change
            FROM board_concept_hist
            WHERE trade_date <= %s
            ORDER BY board_code, trade_date
        """, conn, params=(max_date,))
    finally:
        conn.close()

    if df.empty:
        logger.warning("Board RPS: 无板块数据")
        return pd.DataFrame()

    df['pct_change'] = pd.to_numeric(df['pct_change'], errors='coerce')
    noise_mask = df.apply(lambda r: _is_noise_board(r['board_name'], r['board_code']), axis=1)
    df = df[~noise_mask]

    if use_weekly:
        # ===== 周线模式 =====
        # 按ISO周聚合
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        df['year'] = df['trade_date'].dt.isocalendar().year
        df['week'] = df['trade_date'].dt.isocalendar().week

        results = []
        for (code, name), grp in df.groupby(['board_code', 'board_name']):
            grp = grp.dropna(subset=['pct_change'])
            if grp.empty:
                continue

            # 按周计算周收益
            weekly = grp.groupby(['year', 'week'], sort=False)['pct_change'].agg(
                lambda x: float(np.prod(1 + np.array(x) / 100) - 1) * 100
            ).reset_index()
            weekly.columns = ['year', 'week', 'week_ret']

            n_weeks = len(weekly)
            if n_weeks < 4:  # 至少4周数据
                continue

            # 滚动窗口：只取最近 WEEKLY_RPS_WINDOW 周
            window_weeks = min(n_weeks, WEEKLY_RPS_WINDOW)
            cum_ret = float(np.prod(1 + weekly['week_ret'].iloc[-window_weeks:].values / 100) - 1)
            results.append({
                'board_code': code,
                'board_name': name,
                'cum_return': cum_ret,
                'n_weeks': window_weeks,
            })

        if not results:
            logger.warning("Board RPS周线: 无有效板块")
            return pd.DataFrame()

        board_df = pd.DataFrame(results)
        board_df = board_df.sort_values('cum_return', ascending=False).reset_index(drop=True)
        total = len(board_df)
        board_df['rank'] = range(1, total + 1)
        board_df['rps'] = ((total - board_df['rank'] + 1) / total * 100).round(1)
        logger.info(f"Board RPS周线: {total}个板块评分完成")
        return board_df

    else:
        # ===== 日线模式（原逻辑，备用）=====
        results = []
        for (code, name), grp in df.groupby(['board_code', 'board_name']):
            grp = grp.dropna(subset=['pct_change']).sort_values('trade_date')
            n = len(grp)
            if n < min_days:
                continue
            returns = grp['pct_change'].iloc[-period:].values / 100.0
            cum_return = float(np.prod(1 + returns)) - 1.0
            results.append({
                'board_code': code, 'board_name': name,
                'cum_return': cum_return, 'n_days': min(n, period),
            })

        if not results:
            return pd.DataFrame()
        board_df = pd.DataFrame(results)
        board_df = board_df.sort_values('cum_return', ascending=False).reset_index(drop=True)
        total = len(board_df)
        board_df['rank'] = range(1, total + 1)
        board_df['rps'] = ((total - board_df['rank'] + 1) / total * 100).round(1)
        logger.info(f"Board RPS日线: {total}个板块评分完成")
        return board_df


def get_stock_board_rps(conn=None, as_of_date=None):
    """获取每只股票所属板块的最高RPS（用于ML训练特征）

    返回: {ts_code: max_board_rps}
    """
    board_df = get_board_rps(as_of_date=as_of_date, use_weekly=True)
    if board_df.empty:
        return {}

    top = board_df.head(20)
    board_rps_map = dict(zip(top['board_code'], top['rps']))

    close_conn = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        close_conn = True

    try:
        ph = ','.join(['%s'] * len(board_rps_map))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ts_code, board_code FROM board_concept_cons
            WHERE board_code IN ({ph})
              AND (is_latest = 1 OR is_latest IS NULL)
        """, list(board_rps_map.keys()))
        rows = cur.fetchall()
        cur.close()
    finally:
        if close_conn:
            conn.close()

    stock_rps = {}
    for code, board_code in rows:
        rps = board_rps_map.get(board_code, 50)
        if code not in stock_rps or rps > stock_rps[code]:
            stock_rps[code] = rps
    return stock_rps


def compute_weekly_board_rps_history():
    """为训练数据预计算所有周的板RPS

    返回: DataFrame: [year, week, week_start, board_code, board_name, cum_return, rps]
    """
    conn = pymysql.connect(**get_db_config())
    try:
        df = pd.read_sql("""
            SELECT board_code, board_name, trade_date, pct_change
            FROM board_concept_hist
            ORDER BY board_code, trade_date
        """, conn)
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    df['pct_change'] = pd.to_numeric(df['pct_change'], errors='coerce')
    noise_mask = df.apply(lambda r: _is_noise_board(r['board_name'], r['board_code']), axis=1)
    df = df[~noise_mask]
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df['year'] = df['trade_date'].dt.isocalendar().year
    df['week'] = df['trade_date'].dt.isocalendar().week

    # 按板块+周聚合
    weekly = df.groupby(['board_code', 'board_name', 'year', 'week'], sort=False)['pct_change'].agg(
        lambda x: float(np.prod(1 + np.array(x) / 100) - 1) * 100
    ).reset_index()
    weekly.columns = ['board_code', 'board_name', 'year', 'week', 'week_ret']

    # 计算过去N周的累积收益 → RPS
    results = []
    weeks_sorted = list(weekly[['year', 'week']].drop_duplicates().sort_values(['year', 'week']).itertuples(index=False))
    total_weeks = len(weeks_sorted)

    for idx, (y, w) in enumerate(weeks_sorted):
        # 截至当前周的累积收益（滚动窗口）
        if idx < WEEKLY_RPS_WINDOW:
            current = weekly[(weekly['year'] < y) | ((weekly['year'] == y) & (weekly['week'] <= w))]
        else:
            # 只取最近 WEEKLY_RPS_WINDOW 周
            cutoff = weeks_sorted[idx - WEEKLY_RPS_WINDOW + 1]
            current = weekly[((weekly['year'] > cutoff[0]) | ((weekly['year'] == cutoff[0]) & (weekly['week'] >= cutoff[1]))) &
                             ((weekly['year'] < y) | ((weekly['year'] == y) & (weekly['week'] <= w)))]
        if current.empty:
            continue

        board_cum = current.groupby(['board_code', 'board_name'])['week_ret'].agg(
            lambda x: float(np.prod(1 + np.array(x) / 100) - 1)
        ).reset_index()
        board_cum.columns = ['board_code', 'board_name', 'cum_return']
        board_cum = board_cum.sort_values('cum_return', ascending=False).reset_index(drop=True)
        total = len(board_cum)
        if total == 0:
            continue
        board_cum['rank'] = range(1, total + 1)
        board_cum['rps'] = ((total - board_cum['rank'] + 1) / total * 100).round(1)
        board_cum['year'] = y
        board_cum['week'] = w
        results.append(board_cum[['year', 'week', 'board_code', 'board_name', 'cum_return', 'rps']])

    if not results:
        return pd.DataFrame()

    return pd.concat(results, ignore_index=True)


def get_top_board_stocks(top_n_boards=5, as_of_date=None, use_weekly=True):
    """获取 RPS 最高板块的成分股候选池

    Returns:
        dict: {board_codes, board_names, board_rps, ts_codes, stock_map, full_board_rps}
    """
    board_df = get_board_rps(as_of_date=as_of_date, use_weekly=use_weekly)
    if board_df.empty:
        raise ValueError("Board RPS 无数据")

    # 回测验证：板块去重反而不如不去重（14笔累积累积+17.76% vs +22.66%, 夏普1.90 vs 2.10）
    # 旧版直接取Top5胜出，故去重逻辑暂不启用
    # board_df = _dedup_boards(board_df, top_n_boards)

    top = board_df.head(top_n_boards)
    board_codes = top['board_code'].tolist()
    board_names = top['board_name'].tolist()
    board_rps = top['rps'].tolist()

    conn = pymysql.connect(**get_db_config())
    try:
        ph = ','.join(['%s'] * len(board_codes))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT bcc.ts_code, bcc.stock_name
            FROM board_concept_cons bcc
            WHERE bcc.board_code IN ({ph})
              AND (bcc.is_latest = 1 OR bcc.is_latest IS NULL)
        """, board_codes)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    ts_codes = []
    stock_map = {}
    for code, name in rows:
        if code in stock_map:
            continue
        if name and 'ST' in name:
            continue
        if code.startswith('688') or code.startswith('8') or code.startswith('4') or code.startswith('9'):
            continue
        stock_map[code] = name or code
        ts_codes.append(code)

    # 剔除 > 200 元的高价股（每日生成长钱占用多，且 ML 数据外推不稳定）
    if ts_codes:
        try:
            conn2 = pymysql.connect(**get_db_config())
            cur2 = conn2.cursor()
            ph = ','.join(['%s'] * len(ts_codes))
            cur2.execute(
                f"SELECT ts_code FROM daily_price WHERE ts_code IN ({ph}) AND trade_date=(SELECT MAX(trade_date) FROM daily_price) AND close > 200",
                ts_codes
            )
            high_price_codes = {r[0] for r in cur2.fetchall()}
            cur2.close()
            conn2.close()
            if high_price_codes:
                ts_codes = [c for c in ts_codes if c not in high_price_codes]
                logger.info(f"  剔除高价股(>200元): {len(high_price_codes)}只")
        except Exception:
            pass

    logger.info(f"Top{top_n_boards}板块: {board_names}")
    logger.info(f"  成分股: {len(ts_codes)}只")

    return {
        'board_codes': board_codes,
        'board_names': board_names,
        'board_rps': board_rps,
        'ts_codes': ts_codes,
        'stock_map': stock_map,
    }


def board_scan_recommend(top_n=3, as_of_date=None, use_weekly=True):
    """板RPS周线 → Top5板块成分股 → ML排序 → TopN

    Returns:
        list[dict] 或 None
    """
    model_ver = 'V11.0(板RPS周线)' if use_weekly else 'V11.0(板RPS60)'

    try:
        candidates = get_top_board_stocks(as_of_date=as_of_date, use_weekly=use_weekly)
    except Exception as e:
        logger.warning("Board RPS 失败: %s", e)
        return None

    if not candidates['ts_codes']:
        logger.warning("Board RPS 候选池为空")
        return None

    all_codes = candidates['ts_codes']
    logger.info(f"ML排序: {len(all_codes)}只候选股")

    from ml_predict import predict_batch
    predictions = predict_batch(all_codes, as_of_date=as_of_date)
    if not predictions:
        logger.warning("ML 预测返回空")
        return None

    ranked = sorted(
        [(code, pred) for code, pred in predictions.items()
         if code in candidates['stock_map']],
        key=lambda x: x[1].get('probability', 0),
        reverse=True
    )

    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        if as_of_date is None:
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            latest = str(cur.fetchone()[0])
        else:
            latest = str(as_of_date)[:10]

        result = []
        for code, pred in ranked[:top_n]:
            name = candidates['stock_map'].get(code, code)
            cur.execute(
                "SELECT close, pct_chg FROM daily_price WHERE ts_code=%s AND trade_date=%s",
                (code, latest))
            dr = cur.fetchone()
            price = float(dr[0]) if dr else 0
            pct_chg = float(dr[1] or 0) if dr else 0

            result.append({
                'ts_code': code, 'name': name, 'price': price, 'pct_chg': pct_chg,
                'ml_score': float(pred.get('predicted_return', 0)),
                'ml_prob': float(pred.get('probability', 0.5)),
                'model_ver': model_ver,
            })
    finally:
        conn.close()

    if result:
        summary = ", ".join([f"{c['name']}({c['ml_score']:.3f})" for c in result])
        logger.info(f"{model_ver} Top{top_n}: {summary}")

    return result


def board_rps_realtime_signals(top_n_boards=5, as_of_date=None, use_weekly=True):
    """板RPS候选股 + 实时行情因子评分

    盘中监控使用。获取板RPS候选股 → ML评分 → 实时因子评分 → 综合排序。

    实时因子来源: quant_app.services.realtime_scanner 的 scan_stocks 体系
    （量能突破 / 动量 / 趋势 / 流动性 / RSI / 布林 / 盘口 / 日内突破 / 资金博弈 / 多指数强度）

    Returns:
        list[dict] 按综合分降序:
            [{ts_code, name, price, pct_chg, ml_score, ml_prob,
              realtime_score, combined_score}, ...]
    """
    candidates = get_top_board_stocks(
        top_n_boards=top_n_boards, as_of_date=as_of_date, use_weekly=use_weekly
    )
    if not candidates or not candidates['ts_codes']:
        logger.warning("Board RPS 候选池为空")
        return []

    all_codes = candidates['ts_codes']
    logger.info("实时扫描: %d只候选股", len(all_codes))

    # ML 评分
    from ml_predict import predict_batch
    predictions = predict_batch(all_codes, as_of_date=as_of_date)
    if not predictions:
        logger.warning("ML 预测返回空")
        return []

    # 实时行情
    from quant_app.services.realtime_scanner import (
        get_realtime_data, get_index_data, get_daily_series,
        factor_volume_breakout, factor_momentum, factor_trend,
        factor_liquidity, factor_rsi_bonus, factor_bb_bonus,
        factor_orderbook, factor_intraday_breakout, factor_money_flow,
        factor_multi_index_strength,
    )

    rt_data = get_realtime_data()
    if not rt_data:
        logger.warning("QMT 实时数据不可用(非交易时段?)")
        return []

    idx_data = get_index_data()
    index_pct = idx_data.get("000300.SH", {}).get("pctChg", 0) if idx_data else 0

    # 筛选候选股的实时数据
    candidate_set = set(all_codes)
    rt_filtered = {k: v for k, v in rt_data.items() if k in candidate_set}
    if not rt_filtered:
        logger.warning("候选股无实时数据")
        return []

    daily = get_daily_series(list(candidate_set), 60)

    results = []
    for code in all_codes:
        rt = rt_filtered.get(code)
        if not rt:
            continue

        # 涨停过滤 (分板块: 主板10% / 创业板&科创板20%, 留 0.5% 余量)
        pct = rt.get("pctChg", 0)
        limit_up_pct = 19.5 if (code.startswith("3") or code.startswith("688")) else 9.5
        if pct is not None and pct >= limit_up_pct:
            continue

        try:
            f1 = factor_volume_breakout(rt, daily)        # 量能 0~25
            f2 = factor_momentum(rt, daily, index_pct)     # 动量 0~25
            f3 = factor_trend(rt, daily)                   # 趋势 0~20
            f4 = factor_liquidity(rt, daily)               # 流动 0~15
            rsi = factor_rsi_bonus(rt, daily)               # RSI -5~5
            bb = factor_bb_bonus(rt, daily)                 # 布林 0~5
            f5 = factor_orderbook(rt)                       # 盘口 0~10
            f6 = factor_intraday_breakout(rt, daily)         # 日内 0~15
            f7 = factor_money_flow(rt)                      # 资金 0~10
            f8 = factor_multi_index_strength(rt, idx_data)   # 指数 0~10
        except Exception as e:
            logger.debug("因子评分失败 %s: %s", code, e)
            continue

        realtime_score = f1 + f2 + f3 + f4 + rsi + bb + f5 + f6 + f7 + f8

        pred = predictions.get(code, {})
        ml_prob = pred.get('probability', 0)
        ml_score = pred.get('predicted_return', 0)

        # 综合分: ML概率占50%权重 + 实时因子标准化占50%
        combined = (ml_prob * 50) + (realtime_score * 0.5)

        results.append({
            'ts_code': code,
            'name': candidates['stock_map'].get(code, ''),
            'price': rt.get('last', 0),
            'pct_chg': pct or 0,
            'ml_score': round(ml_score, 4),
            'ml_prob': round(ml_prob, 4),
            'realtime_score': realtime_score,
            'combined_score': round(combined, 2),
        })

    results.sort(key=lambda x: x['combined_score'], reverse=True)
    logger.info("实时扫描: %d只通过, 最高综合分=%.1f", len(results), results[0]['combined_score'] if results else 0)
    return results
