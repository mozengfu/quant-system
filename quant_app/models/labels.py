"""
标签构建模块 — TopDown V1 三层标签体系

所有标签从 daily_price + market_index_daily 自构建，不依赖外部标签表。

- build_market_labels(): 大盘3分类标签 (bull/range/bear)
- build_sector_labels(): 板块热度 LambdaRank 标签 (10级)
- build_wave_labels(): 主升浪二分类标签
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def build_market_labels(conn, start_date='2024-01-01', end_date='2026-06-09'):
    """构建大盘方向标签 (3分类: 0=down, 1=range, 2=up)

    标签: 上证指数 3日 forward return
      - bull (2): > +0.5%
      - range (1): [-0.5%, +0.5%]
      - bear (0): < -0.5%

    Returns:
        DataFrame: trade_date, close, pct_chg, label, future_ret_3d
    """
    df = pd.read_sql("""
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])

    df = df.rename(columns={'close_price': 'close', 'change_pct': 'pct_chg'})
    df['close'] = df['close'].astype(float)
    df['pct_chg'] = df['pct_chg'].astype(float)

    # 3日 forward return
    future_rets = []
    for i in range(len(df)):
        if i + 3 < len(df):
            ret = (df['close'].iloc[i+3] / df['close'].iloc[i] - 1) * 100
        else:
            ret = np.nan
        future_rets.append(ret)
    df['future_ret_3d'] = future_rets

    # 3分类标签
    def _label(ret):
        if pd.isna(ret):
            return np.nan
        if ret < -0.5:
            return 0  # bear
        elif ret > 0.5:
            return 2  # bull
        else:
            return 1  # range

    df['label'] = df['future_ret_3d'].apply(_label)
    return df.dropna(subset=['label'])


def build_sector_labels(conn, start_date='2024-01-01', end_date='2026-06-09'):
    """构建板块热度标签 (LambdaRank 10级)

    板块定义: board_industry (申万行业分类)
    标签: 每个板块 T+1~T+3 的平均成分股收益，按日横截面 qcut 为 10 级

    Returns:
        DataFrame: trade_date, board_code, board_name, label, future_ret_3d
    """
    # 获取所有行业板块
    boards = pd.read_sql("SELECT board_code, board_name FROM board_industry", conn)
    board_codes = boards['board_code'].tolist()

    # 获取板块-成分股映射
    cons = pd.read_sql(f"""
        SELECT board_code, ts_code
        FROM board_industry_cons
        WHERE is_latest=1
          AND board_code IN ({','.join(['%s']*len(board_codes))})
    """, conn, params=tuple(board_codes))

    # 获取 daily_price 数据
    dp = pd.read_sql("""
        SELECT ts_code, trade_date, close, pre_close, pct_chg, amount
        FROM daily_price
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])

    dp['close'] = dp['close'].astype(float)
    dp['pct_chg'] = dp['pct_chg'].astype(float)

    # 计算每个板块每日的成分股平均收益
    dp_with_board = dp.merge(cons, on='ts_code', how='inner')

    # 板块日收益 = 成分股收益均值
    board_daily = dp_with_board.groupby(['board_code', 'trade_date'])['pct_chg'].mean().reset_index()
    board_daily.rename(columns={'pct_chg': 'board_return'}, inplace=True)

    # 计算未来3日累计收益作为标签
    results = []
    for bd_code in board_daily['board_code'].unique():
        bd_df = board_daily[board_daily['board_code'] == bd_code].sort_values('trade_date').copy()
        bd_df = bd_df.reset_index(drop=True)
        future_rets = []
        for i in range(len(bd_df)):
            if i + 3 < len(bd_df):
                ret = bd_df['board_return'].iloc[i+1:i+4].mean()
            else:
                ret = np.nan
            future_rets.append(ret)
        bd_df['future_ret_3d'] = future_rets
        results.append(bd_df)

    all_board = pd.concat(results, ignore_index=True)
    all_board = all_board.dropna(subset=['future_ret_3d'])

    # 按日横截面 qcut 为 10 级 (LambdaRank 标签, 0~9)
    all_board['label'] = all_board.groupby('trade_date')['future_ret_3d'].transform(
        lambda x: pd.qcut(x, q=10, labels=False, duplicates='drop')
    )

    # 合并板块名称
    all_board = all_board.merge(boards, on='board_code', how='left')

    return all_board.dropna(subset=['label'])


def build_wave_labels(conn, start_date='2024-01-01', end_date='2026-05-31'):
    """构建主升浪二分类标签 (向量化版本)

    标签=1 条件:
      1. 3日 forward return > 8%
      2. AND 3日 forward return 在当日横截面 top 15%
      3. AND 未来3天中至少1天成交量 > 1.5× 20日均量

    Returns:
        DataFrame: ts_code, trade_date, label, future_ret_3d, vol_confirm
    """
    dp = pd.read_sql("""
        SELECT ts_code, trade_date, close, pre_close, pct_chg, vol, amount, turnover_rate
        FROM daily_price
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY ts_code, trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])

    dp['close'] = dp['close'].astype(float)
    dp['pct_chg'] = dp['pct_chg'].astype(float)
    dp['vol'] = dp['vol'].astype(float)

    logger.info(f"  Loaded {len(dp)} rows, computing features...")

    # ── 向量化计算: 按股票分组 ──
    dp = dp.sort_values(['ts_code', 'trade_date'])

    # 20日均量
    dp['vol_ma20'] = dp.groupby('ts_code')['vol'].transform(
        lambda x: x.rolling(20, min_periods=10).mean()
    )

    # 3日 forward return (每只股票内 shift)
    dp['future_close'] = dp.groupby('ts_code')['close'].shift(-3)
    dp['future_ret_3d'] = (dp['future_close'] / dp['close'] - 1) * 100

    # 条件3: 当天成交量 > 1.5x 20日均量 (放量确认，无未来信息)
    dp['vol_confirm'] = dp['vol'] > 1.5 * dp['vol_ma20']

    # 条件1 & 2: 横截面top15% + 绝对收益>8%
    dp = dp.dropna(subset=['future_ret_3d', 'vol_ma20'])

    # 按日计算横截面阈值
    dp['ret_pct_85'] = dp.groupby('trade_date')['future_ret_3d'].transform(
        lambda x: x.quantile(0.85)
    )

    dp['label'] = (
        (dp['future_ret_3d'] > 8) &
        (dp['future_ret_3d'] >= dp['ret_pct_85']) &
        (dp['vol_confirm'])
    ).astype(int)

    result = dp[['ts_code', 'trade_date', 'future_ret_3d', 'vol_confirm', 'label']].copy()
    logger.info(f"  Wave labels: {len(result)} rows, positive rate={result['label'].mean():.4f}")
    return result
