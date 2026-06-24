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

# P0 筛选参数（板块RPS主线识别 v1）
RANK_SLOPE_LOOKBACK = 4       # 看最近 N 周的 rank 序列
RANK_SLOPE_MIN_DELTA = -1     # rank 差值 >= -1 视为"爬升或持平"（允许 1 名小波动）
BREADTH_LOOKBACK_DAYS = 3     # 成分股上涨占比回看天数
BREADTH_MIN_UPSHARE = 0.70    # 上涨家数占比阈值

# 自挂持久化的限频控制（防止 monitor 每分钟重复写库）
_PERSIST_MIN_INTERVAL = 3600  # 至少 1 小时才重写一次
_persist_last_run = {'ts': 0.0, 'key': None}
_persist_in_progress = False  # 防止 save_board_rps_history 内回触发死循环


def _auto_persist_rps_history(board_df, as_of_date):
    """get_board_rps 周线模式末尾自动持久化 hook。

    设计：仅当 (as_of_date, year-week) 变化 或 距上次写入 > 1h 时才写。
    monitor 每分钟调用一次，不会产生重复写入。
    save_board_rps_history 内部也会调 get_board_rps → 用 _persist_in_progress 防递归。
    """
    global _persist_in_progress
    if _persist_in_progress:
        return
    import time as _time
    try:
        key = str(as_of_date)
        now = _time.time()
        # 同一天同一周，1 小时内不重写
        if (_persist_last_run['key'] == key
                and (now - _persist_last_run['ts']) < _PERSIST_MIN_INTERVAL):
            return
        _persist_in_progress = True
        try:
            n = save_board_rps_history(as_of_date=as_of_date)
        finally:
            _persist_in_progress = False
        if n:
            _persist_last_run.update({'ts': now, 'key': key})
            logger.debug(f"_auto_persist_rps_history: 写入 {n} 条 (key={key})")
    except Exception as e:
        # hook 失败不能影响主流程
        logger.debug(f"_auto_persist_rps_history 跳过: {e}")


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
        # P0 自挂钩子：把当周全量 rank 写入 board_rps_history（限频 1 次/小时）
        _auto_persist_rps_history(board_df, as_of_date=max_date)
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

    # ============== P0 主线过滤（2026-06-20 补充） ==============
    # 过滤 1: rank 4 周斜率（连续爬升 / 持平）—— 不允许断崖式下滑
    # 过滤 2: 成分股近 3 日上涨家数占比 ≥ 70% —— 排除权重独拉指数
    slope_series = get_rank_slope_map(as_of_date=as_of_date)
    breadth_map = get_board_breadth(board_codes, as_of_date=as_of_date)

    kept_codes, kept_names, kept_rps = [], [], []
    dropped = []
    for bc, bn, br in zip(board_codes, board_names, board_rps):
        ok_slope, slope_info = is_rank_climbing(bc, slope_series)
        upshare = breadth_map.get(bc)
        ok_breadth = (upshare is None) or (upshare >= BREADTH_MIN_UPSHARE)

        if ok_slope and ok_breadth:
            kept_codes.append(bc)
            kept_names.append(bn)
            kept_rps.append(br)
            logger.info(
                "  [P0 保留] %s(%s) rank序列=%s delta=%s upshare=%s",
                bn, bc, slope_info.get('ranks'), slope_info.get('delta'),
                f"{upshare:.1%}" if upshare is not None else 'N/A',
            )
        else:
            reasons = []
            if not ok_slope:
                reasons.append(f"rank下滑序列={slope_info.get('ranks')}")
            if not ok_breadth:
                reasons.append(f"upshare={upshare:.1%}<{BREADTH_MIN_UPSHARE:.0%}")
            logger.info("  [P0 剔除] %s(%s) %s", bn, bc, '; '.join(reasons))
            dropped.append({'board_code': bc, 'board_name': bn,
                           'rps': br, 'reasons': reasons,
                           'slope': slope_info, 'upshare': upshare})

    # 如果过滤后不足，从原始 TopN 候选里按 rps 补回（确保成分股池不空）
    if not kept_codes and len(board_codes) >= top_n_boards:
        kept_codes = board_codes[:top_n_boards]
        kept_names = board_names[:top_n_boards]
        kept_rps = board_rps[:top_n_boards]
        logger.warning("P0 过滤后无板块，回退到原始 Top%d（可能所有候选都不过滤）", top_n_boards)
    else:
        board_codes, board_names, board_rps = kept_codes, kept_names, kept_rps

    # 成分股也只保留通过 P0 过滤后的板块
    if dropped:
        kept_set = set(kept_codes)
        before = len(ts_codes)
        ts_codes = _filter_stocks_by_kept_boards(ts_codes, kept_set)
        logger.info(f"P0 过滤: 成分股 {before} → {len(ts_codes)}")

    return {
        'board_codes': board_codes,
        'board_names': board_names,
        'board_rps': board_rps,
        'ts_codes': ts_codes,
        'stock_map': stock_map,
        'p0_dropped': dropped,  # 调试/审计用，外部不依赖
    }


def _filter_stocks_by_kept_boards(ts_codes, kept_board_codes):
    """成分股只在"通过 P0 过滤后的板块"内才保留。"""
    if not ts_codes or not kept_board_codes:
        return ts_codes
    conn = pymysql.connect(**get_db_config())
    try:
        ph = ','.join(['%s'] * len(ts_codes))
        pb = ','.join(['%s'] * len(kept_board_codes))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT ts_code FROM board_concept_cons
            WHERE ts_code IN ({ph}) AND board_code IN ({pb})
              AND (is_latest=1 OR is_latest IS NULL)
        """, list(ts_codes) + list(kept_board_codes))
        kept = {r[0] for r in cur.fetchall()}
        cur.close()
        return [c for c in ts_codes if c in kept]
    finally:
        conn.close()

def get_climbing_board_stocks(top_n=3, as_of_date=None):
    """获取周板RPS排名持续提升的板块的成分股，作为 Top5 的补充。

    流程:
      1. 从 get_board_rps() 获取全量板块 RPS
      2. 从 get_rank_slope_map() 获取全量 rank 序列
      3. 筛选通过 is_rank_climbing() 的板块
      4. 从通过的板块中按 RPS 降序取 Top N
      5. 对 Top N 加广度检查，失败则回退到原始 Top N
      6. 获取成分股（同 get_top_board_stocks 的过滤逻辑）

    Returns:
        dict: {board_codes, board_names, board_rps, ts_codes, stock_map}
        无爬升板块时返回空结构
    """
    board_df = get_board_rps(as_of_date=as_of_date, use_weekly=True)
    if board_df.empty:
        logger.warning("爬升板块: 无 RPS 数据")
        return {'board_codes': [], 'board_names': [], 'board_rps': [],
                'ts_codes': [], 'stock_map': {}}

    slope_series = get_rank_slope_map(as_of_date=as_of_date)
    if not slope_series:
        logger.warning("爬升板块: 无 rank 序列数据")
        return {'board_codes': [], 'board_names': [], 'board_rps': [],
                'ts_codes': [], 'stock_map': {}}

    # 遍历全量板块，筛选通过 is_rank_climbing 的板块
    climbing_boards = []
    for _, row in board_df.iterrows():
        bc = row['board_code']
        ok, info = is_rank_climbing(bc, slope_series)
        if ok:
            delta = info.get('delta', 0)
            rank_series = info.get('ranks', [])
            climbing_boards.append({
                'board_code': bc,
                'board_name': row['board_name'],
                'rps': row['rps'],
                'rank': row['rank'],
                'climbing_score': delta,
                'rank_series': rank_series,
            })

    if not climbing_boards:
        logger.info("爬升板块: 无板块通过 is_rank_climbing")
        return {'board_codes': [], 'board_names': [], 'board_rps': [],
                'ts_codes': [], 'stock_map': {}}

    # 从通过检查的板块中按 RPS 降序取 Top N
    climbing_sorted = sorted(climbing_boards, key=lambda x: x['climbing_score'], reverse=True)
    raw_top = climbing_sorted[:top_n]

    logger.info("爬升板块: 总计 %d 个板块通过爬升检查, 按爬升幅度取Top%d",
                len(climbing_boards), top_n)
    for cb in raw_top:
        logger.info("  [爬升候选] %s(%s) RPS=%.1f rank=%d 爬升=%d 序列=%s",
                    cb['board_name'], cb['board_code'], cb['rps'], cb['rank'],
                    cb['climbing_score'], cb['rank_series'])

    # 广度检查（同 P0 过滤逻辑），失败则回退
    climb_codes = [cb['board_code'] for cb in raw_top]
    climb_names = [cb['board_name'] for cb in raw_top]
    climb_rps = [cb['rps'] for cb in raw_top]
    breadth_map = get_board_breadth(climb_codes, as_of_date=as_of_date)

    kept_codes, kept_names, kept_rps = [], [], []
    for bc, bn, br in zip(climb_codes, climb_names, climb_rps):
        upshare = breadth_map.get(bc)
        ok_breadth = (upshare is None) or (upshare >= BREADTH_MIN_UPSHARE)
        if ok_breadth:
            kept_codes.append(bc)
            kept_names.append(bn)
            kept_rps.append(br)
        else:
            logger.info("  [爬升广度剔除] %s(%s) upshare=%s",
                        bn, bc, f"{upshare:.1%}" if upshare is not None else 'N/A')

    # 如果广度过滤后为空，回退到原始爬升 Top N
    if not kept_codes and climb_codes:
        kept_codes = climb_codes
        kept_names = climb_names
        kept_rps = climb_rps
        logger.warning("爬升板块广度过滤后全剔除，回退到原始 Top%d", top_n)

    # 获取成分股（同 get_top_board_stocks 的查询和过滤逻辑）
    conn = pymysql.connect(**get_db_config())
    try:
        ph = ','.join(['%s'] * len(kept_codes))
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT bcc.ts_code, bcc.stock_name
            FROM board_concept_cons bcc
            WHERE bcc.board_code IN ({ph})
              AND (bcc.is_latest = 1 OR bcc.is_latest IS NULL)
        """, kept_codes)
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
        if code.startswith('688') or code.startswith('8') or \
           code.startswith('4') or code.startswith('9'):
            continue
        stock_map[code] = name or code
        ts_codes.append(code)

    # 剔除 > 200 元的高价股
    if ts_codes:
        try:
            conn2 = pymysql.connect(**get_db_config())
            cur2 = conn2.cursor()
            ph = ','.join(['%s'] * len(ts_codes))
            cur2.execute(
                f"SELECT ts_code FROM daily_price WHERE ts_code IN ({ph}) "
                f"AND trade_date=(SELECT MAX(trade_date) FROM daily_price) "
                f"AND close > 200",
                ts_codes
            )
            high_price_codes = {r[0] for r in cur2.fetchall()}
            cur2.close()
            conn2.close()
            if high_price_codes:
                ts_codes = [c for c in ts_codes if c not in high_price_codes]
                logger.info("  爬升板块剔除高价股(>200元): %d只", len(high_price_codes))
        except Exception:
            pass

    logger.info("爬升板块 Top%d: %s | 成分股: %d只",
                len(kept_codes), kept_names, len(ts_codes))

    return {
        'board_codes': kept_codes,
        'board_names': kept_names,
        'board_rps': kept_rps,
        'ts_codes': ts_codes,
        'stock_map': stock_map,
    }


def board_scan_recommend(top_n=3, as_of_date=None, use_weekly=True, climbing_boards=3):
    """板RPS周线 → Top5板块成分股 + 爬升板块成分股 → 合并ML排序 → TopN

    Args:
        climbing_boards: 爬升板块数（0=关闭爬升补充）
    """
    model_ver = 'V11.0(板RPS周线)' if use_weekly else 'V11.0(板RPS60)'

    # 1. 现有 Top5 路径
    try:
        top5_candidates = get_top_board_stocks(as_of_date=as_of_date, use_weekly=use_weekly)
    except Exception as e:
        logger.warning("Board RPS 失败: %s", e)
        return None

    if not top5_candidates['ts_codes']:
        logger.warning("Board RPS 候选池为空")
        return None

    # 2. 爬升板块路径（新增）
    climb_candidates = None
    if climbing_boards > 0:
        try:
            climb_candidates = get_climbing_board_stocks(top_n=climbing_boards, as_of_date=as_of_date)
        except Exception as e:
            logger.warning("爬升板块扫描失败: %s，跳过爬升补充", e)

    # 3. 合并候选池（去重，保留 top5 优先）
    all_ts_codes = list(top5_candidates['ts_codes'])
    all_stock_map = dict(top5_candidates['stock_map'])
    climb_added = 0
    if climb_candidates and climb_candidates['ts_codes']:
        for code in climb_candidates['ts_codes']:
            if code not in all_stock_map:
                all_ts_codes.append(code)
                if code in climb_candidates['stock_map']:
                    all_stock_map[code] = climb_candidates['stock_map'][code]
                climb_added += 1

    logger.info("候选池来源: Top5板块 %d只 + 爬升板块 %d只 = 合计%d只",
                len(top5_candidates['ts_codes']), climb_added, len(all_ts_codes))

    all_codes = all_ts_codes
    logger.info(f"ML排序: {len(all_codes)}只候选股")

    from ml_predict import predict_batch
    predictions = predict_batch(all_codes, as_of_date=as_of_date)
    if not predictions:
        logger.warning("ML 预测返回空")
        return None

    ranked = sorted(
        [(code, pred) for code, pred in predictions.items()
         if code in all_stock_map],
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
            name = all_stock_map.get(code, code)
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


# ============================================================================
# P0 板块RPS主线识别（2026-06-20 补充）
# 来源: 头条文章《靠板块RPS强弱榜单锁定市场主线》4 条筛选标准
# 目的: 不只看静态 rank，看"名次变动趋势 + 板块内部涨跌结构"
# 不动 get_board_rps / board_scan_recommend 既有逻辑，仅在 get_top_board_stocks 末尾追加两道过滤
# ============================================================================

def save_board_rps_history(as_of_date=None):
    """把"截止 as_of_date 当周"的 RPS 全量结果写入 board_rps_history 表。

    写入策略：先 DELETE 当周已存在记录，再 INSERT 最新结果，保证幂等。
    供 cron 每天/周跑一次即可（rank 历史序列由表自然累积）。
    """
    board_df = get_board_rps(as_of_date=as_of_date, use_weekly=True)
    if board_df.empty:
        logger.warning("save_board_rps_history: 无 RPS 数据")
        return 0

    # 找到对应周（用 max_date 反推 ISO year/week）
    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        max_date = as_of_date or _get_max_date(conn)
        cur.execute("""
            SELECT YEAR(%s), WEEK(%s, 3)
        """, (max_date, max_date))
        y, w = cur.fetchone()
        # 取当周任意一个交易日作为 trade_date
        cur.execute("""
            SELECT MAX(trade_date) FROM board_concept_hist
            WHERE YEARWEEK(trade_date, 3) = YEARWEEK(%s, 3)
        """, (max_date,))
        week_trade_date = cur.fetchone()[0]

        # 幂等：先删
        cur.execute("DELETE FROM board_rps_history WHERE year=%s AND week=%s", (y, w))

        rows = [
            (int(y), int(w), week_trade_date, r['board_code'], r['board_name'],
             float(r['cum_return']), float(r['rps']), int(r['rank']))
            for _, r in board_df.iterrows()
        ]
        cur.executemany("""
            INSERT INTO board_rps_history
                (year, week, trade_date, board_code, board_name, cum_return, rps, rank_num)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, rows)
        conn.commit()
        cur.close()
        logger.info("save_board_rps_history: year=%s week=%s 写入 %d 条", y, w, len(rows))
        return len(rows)
    finally:
        conn.close()


def get_rank_slope_map(as_of_date=None, lookback_weeks=None):
    """取最近 N 周每个板块的 rank 序列。

    Returns:
        dict: {board_code: [rank_t, rank_t-1, ..., rank_t-N+1]}
              序列按时间从早到晚排列（最右是最新一周的 rank）
    """
    if lookback_weeks is None:
        lookback_weeks = RANK_SLOPE_LOOKBACK

    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        max_date = as_of_date or _get_max_date(conn)
        cur.execute("SELECT YEAR(%s), WEEK(%s, 3)", (max_date, max_date))
        y, w = cur.fetchone()
        # 找最近 N 周 (year, week) 对
        cur.execute("""
            SELECT DISTINCT year, week
            FROM board_rps_history
            WHERE (year < %s) OR (year = %s AND week <= %s)
            ORDER BY year DESC, week DESC
            LIMIT %s
        """, (y, y, w, lookback_weeks))
        weeks = list(reversed(cur.fetchall()))  # 早→晚
        if not weeks:
            return {}

        # 用 tuple 列表展开成 ((y1,w1), (y2,w2), ...)
        ph = ','.join(['(%s,%s)'] * len(weeks))
        flat = [v for pair in weeks for v in pair]
        cur.execute(f"""
            SELECT year, week, board_code, rank_num
            FROM board_rps_history
            WHERE (year, week) IN ({ph})
            ORDER BY year ASC, week ASC
        """, flat)
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    # 按板块聚合 rank 序列
    series = {}
    for yr, wk, bcode, rk in rows:
        series.setdefault(bcode, []).append(int(rk))
    return series


def is_rank_climbing(board_code, series, lookback=None):
    """判断板块 rank 序列是否"爬升或持平"。

    规则（最近 N 周）：
      rank 序列 [r_{t-N+1}, ..., r_{t-1}, r_t]
      delta = r_{t-N+1} - r_t  (早→晚，rank 数字变小 = 爬升)
      delta >= RANK_SLOPE_MIN_DELTA（即 ≥ -1）→ 通过
      即允许 1 名小波动，但不可断崖式下滑

    Returns:
        bool, dict（含 slope 详情，便于日志）
    """
    if lookback is None:
        lookback = RANK_SLOPE_LOOKBACK
    ranks = series.get(board_code, [])
    if len(ranks) < 2:
        # 数据不足时保守放行（避免新板块被错杀）
        return True, {"reason": "数据不足(<2周),放行", "ranks": ranks}
    use = ranks[-lookback:] if len(ranks) >= lookback else ranks
    delta = use[0] - use[-1]  # 正=爬升
    passed = delta >= RANK_SLOPE_MIN_DELTA
    return passed, {"delta": delta, "ranks": use, "passed": passed}


def get_board_breadth(board_codes, as_of_date=None, lookback_days=None):
    """对每个板块计算"成分股近 N 日上涨家数占比"。

    上涨定义: daily_price.pct_chg > 0 的成分股数 / 总成分股数
    阈值: BREADTH_MIN_UPSHARE (默认 70%)

    Returns:
        dict: {board_code: upshare (float)}  缺失或无成分股则返回 None
    """
    if lookback_days is None:
        lookback_days = BREADTH_LOOKBACK_DAYS
    if not board_codes:
        return {}

    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        max_date = as_of_date or _get_max_date(conn)
        # 取最近 N 个交易日
        cur.execute("""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= %s
            ORDER BY trade_date DESC LIMIT %s
        """, (max_date, lookback_days))
        days = [r[0] for r in cur.fetchall()]
        if not days:
            return {bc: None for bc in board_codes}

        ph_d = ','.join(['%s'] * len(days))
        ph_b = ','.join(['%s'] * len(board_codes))
        # 一次 SQL: 对每个 (board, day) 算成分股上涨占比，再按 board 平均
        cur.execute(f"""
            SELECT bcc.board_code,
                   AVG(CASE WHEN dp.pct_chg > 0 THEN 1 ELSE 0 END) AS upshare
            FROM board_concept_cons bcc
            JOIN daily_price dp
              ON dp.ts_code = bcc.ts_code
             AND dp.trade_date IN ({ph_d})
            WHERE bcc.board_code IN ({ph_b})
              AND (bcc.is_latest = 1 OR bcc.is_latest IS NULL)
              AND dp.pct_chg IS NOT NULL
            GROUP BY bcc.board_code
        """, days + list(board_codes))
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    return {bc: float(upshare) if upshare is not None else None for bc, upshare in rows}


# ============================================================================
# 个股 RPS 止损（2026-06-20 补充）
# 规则: 当前 20 日累计收益在历史 250 日里 RPS < 20 → 资金退潮, 立即止损
# 优於纯价格止损: 价格跌往往是结果, RPS 跌是因 → 更早撤退
# ============================================================================

RPS_STOP_LOOKBACK = 20          # 当下累计收益窗口
RPS_STOP_HISTORY = 250          # 历史回看天数
RPS_STOP_THRESHOLD = 20.0       # RPS < 20 视为止损触发 (2026-06-13: 从 15 上调到 20，过滤楚江 5/21 假信号)
RPS_STOP_MIN_HOLD_DAYS = 2      # 持仓 < 2 天不触发 RPS 止损（避免买入次日波动）


def compute_stock_rps(ts_code, as_of_date=None, lookback=None, history=None):
    """计算个股在 as_of_date 当日的 RPS（百分位排名）。

    RPS = (过去 250 日所有 20 日累计收益 ≤ 当前 20 日累计收益) / 总窗口数 × 100

    Args:
        ts_code: 股票代码
        as_of_date: 截止日期 (str 或 date)，None=最新
        lookback: 当下累计窗口天数 (默认 20)
        history:  历史回看天数 (默认 250)

    Returns:
        (cum_return, rps, lower_band)  其中 lower_band 是历史 lookback 窗口的下沿 (25 分位)
        数据不足返回 (None, None, None)
    """
    if lookback is None:
        lookback = RPS_STOP_LOOKBACK
    if history is None:
        history = RPS_STOP_HISTORY

    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        max_date = as_of_date or cur.execute("SELECT MAX(trade_date) FROM daily_price") \
                   or _get_max_date(conn)
        if isinstance(max_date, int):
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            max_date = cur.fetchone()[0]

        # 取历史 N 日 pct_chg
        cur.execute("""
            SELECT pct_chg FROM daily_price
            WHERE ts_code=%s AND trade_date<=%s
            ORDER BY trade_date DESC LIMIT %s
        """, (ts_code, max_date, history))
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    rets = [float(r[0]) for r in rows if r[0] is not None]
    if len(rets) < lookback + 5:  # 至少 lookback+5 天数据
        return None, None, None

    # 当前 lookback 日累计收益
    cum_now = sum(rets[:lookback])

    # 历史滚动 lookback 窗口的累计收益分布
    rolling = []
    for i in range(len(rets) - lookback + 1):
        rolling.append(sum(rets[i:i + lookback]))

    # RPS: 当前 cum_now 在历史 rolling 里的百分位
    rank = sum(1 for x in rolling if x <= cum_now)
    rps = rank / len(rolling) * 100

    # 下沿: rolling 的 25 分位 (历史偏弱区间的上限)
    rolling_sorted = sorted(rolling)
    lower_band = rolling_sorted[len(rolling_sorted) // 4]

    return cum_now, rps, lower_band


def check_rps_stop(ts_code, as_of_date=None, days_held=0):
    """判断个股是否触发 RPS 止损。

    触发条件（任一满足）:
      1. RPS < RPS_STOP_THRESHOLD (默认 15)
      2. cum_now < lower_band (20 日累计收益跌破历史下沿)

    Args:
        ts_code: 股票代码
        as_of_date: 截止日期
        days_held: 已持仓天数 (< RPS_STOP_MIN_HOLD_DAYS 直接放行，避免买入次日波动误杀)

    Returns:
        (should_stop: bool, detail: dict)
    """
    if days_held < RPS_STOP_MIN_HOLD_DAYS:
        return False, {
            "reason": f"持仓仅{days_held}天(<{RPS_STOP_MIN_HOLD_DAYS}天), 跳过 RPS 止损",
            "should_stop": False,
        }

    cum, rps, lb = compute_stock_rps(ts_code, as_of_date=as_of_date)
    if cum is None:
        return False, {"reason": "数据不足, 跳过 RPS 止损"}

    reasons = []
    if rps < RPS_STOP_THRESHOLD:
        reasons.append(f"RPS={rps:.1f}<{RPS_STOP_THRESHOLD:.0f}")
    if cum < lb:
        reasons.append(f"20日累计{cum:+.2f}%<下沿{lb:+.2f}%")

    should = bool(reasons)
    return should, {
        "should_stop": should,
        "cum_20d": cum,
        "rps": rps,
        "lower_band": lb,
        "reasons": reasons,
        "reason_text": "RPS止损: " + "; ".join(reasons) if reasons else "未触发",
    }
