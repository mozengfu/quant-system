"""
板块特征共享模块 — 从 daily_price + board_industry_cons 聚合

数据覆盖 2023-01 ~ 至今（808个交易日），不依赖 sector_moneyflow。

提供:
  - build_sector_daily(): 每日板块级别聚合 (收益/资金流/宽度)
  - build_market_breadth_daily(): 每日全市场宽度
  - get_sector_features_at(): 指定日期的板块特征快照
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 缓存
_sector_daily_cache = None
_breadth_daily_cache = None
_board_map_cache = None
_cache_conn_id = None


def _load_raw_data(conn):
    """一次性加载所有基础数据到内存"""
    logger.info("Loading daily_price + board_cons for sector features...")

    # 板块-股票映射
    cons = pd.read_sql("""
        SELECT ic.board_code, bi.board_name, ic.ts_code
        FROM board_industry_cons ic
        JOIN board_industry bi ON ic.board_code = bi.board_code
        WHERE ic.is_latest = 1
    """, conn)
    logger.info(f"  Board mapping: {len(cons)} rows, {cons.board_code.nunique()} boards")

    # daily_price (仅取需要的列)
    dp = pd.read_sql("""
        SELECT ts_code, trade_date, close, pct_chg, vol, amount, turnover_rate, ma20
        FROM daily_price
        ORDER BY ts_code, trade_date
    """, conn, parse_dates=['trade_date'])
    for col in ['close', 'pct_chg', 'vol', 'amount', 'turnover_rate', 'ma20']:
        dp[col] = dp[col].astype(float)
    logger.info(f"  daily_price: {len(dp)} rows, {dp.ts_code.nunique()} stocks")

    return dp, cons


def build_sector_daily(conn) -> pd.DataFrame:
    """构建每日板块聚合数据

    Returns:
        DataFrame: trade_date, board_code, board_name,
                   sector_ret (成分股平均收益),
                   sector_up_ratio (上涨比例),
                   sector_above_ma20 (站上MA20比例),
                   sector_amount (总成交额),
                   sector_vol_ratio (放量股比例),
                   sector_ret_std (成分股收益标准差)
    """
    global _sector_daily_cache
    if _sector_daily_cache is not None:
        return _sector_daily_cache

    dp, cons = _load_raw_data(conn)

    logger.info("Aggregating sector daily features...")
    df = dp.merge(cons[['ts_code', 'board_code', 'board_name']], on='ts_code', how='inner')

    # 按板块+日期聚合
    sector_daily = df.groupby(['board_code', 'board_name', 'trade_date']).agg(
        sector_ret=('pct_chg', 'mean'),
        sector_up_ratio=('pct_chg', lambda x: (x > 0).mean()),
        sector_above_ma20=('close', lambda x: (x > df.loc[x.index, 'ma20']).mean()
                           if 'ma20' in df.columns else 0.5),
        sector_amount=('amount', 'sum'),
        sector_vol_ratio=('vol', lambda x: (
            x / x.rolling(20, min_periods=10).mean().shift(1)
        ).gt(1.5).mean() if len(x) > 10 else 0.1),
        sector_ret_std=('pct_chg', 'std'),
        sector_stock_count=('ts_code', 'nunique'),
    ).reset_index()

    sector_daily = sector_daily.fillna(0)
    _sector_daily_cache = sector_daily
    logger.info(f"  Sector daily: {len(sector_daily)} rows, {sector_daily.board_code.nunique()} boards")
    return sector_daily


def build_market_breadth_daily(conn) -> pd.DataFrame:
    """构建每日全市场宽度

    Returns:
        DataFrame: trade_date, mkt_up_ratio, mkt_above_ma20,
                   mkt_amount (总成交额), mkt_vol (总波动)
    """
    global _breadth_daily_cache
    if _breadth_daily_cache is not None:
        return _breadth_daily_cache

    logger.info("Aggregating market breadth daily...")
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date,
               AVG(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END) as up_ratio,
               AVG(CASE WHEN close>ma20 THEN 1 ELSE 0 END) as above_ma20,
               SUM(amount) as total_amount,
               STDDEV(pct_chg) as ret_std
        FROM daily_price
        GROUP BY trade_date ORDER BY trade_date
    """)
    rows = cur.fetchall()
    breadth = pd.DataFrame(rows, columns=['trade_date', 'mkt_up_ratio', 'mkt_above_ma20',
                                           'mkt_amount', 'mkt_vol'])
    for col in ['mkt_up_ratio', 'mkt_above_ma20', 'mkt_amount', 'mkt_vol']:
        breadth[col] = breadth[col].astype(float)
    breadth = breadth.fillna(0)
    _breadth_daily_cache = breadth
    logger.info(f"  Market breadth: {len(breadth)} days")
    return breadth


def get_sector_features_at(sector_daily: pd.DataFrame, as_of_date: str) -> dict:
    """获取指定日期的板块横截面特征

    Returns:
        dict: {
            'sector_ret_std': 板块收益标准差,
            'sector_ret_range': 板块收益极差,
            'sector_up_ratio': 板块上涨比例,
            'sector_net_flow': 成交额加权净流入(近似),
            'sector_inflow_ratio': 板块上涨比例,
            'sector_elg_net': 0 (无资金流数据时填0),
        }
    """
    day = sector_daily[sector_daily['trade_date'] == as_of_date]
    if len(day) < 5:
        return {
            'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5,
            'sector_net_flow': 0, 'sector_inflow_ratio': 0.5, 'sector_elg_net': 0,
        }

    rets = day['sector_ret'].dropna().values
    return {
        'sector_ret_std': float(np.std(rets)),
        'sector_ret_range': float(np.ptp(rets)) if len(rets) > 1 else 0,
        'sector_up_ratio': float(np.mean(rets > 0)),
        'sector_net_flow': float(day['sector_amount'].sum()),
        'sector_inflow_ratio': float(np.mean(rets > 0)),
        'sector_elg_net': 0,
    }


def build_sector_features_for_heat(conn, sector_daily: pd.DataFrame = None) -> pd.DataFrame:
    """为 Layer 2 构建板块特征数据（从 daily_price 聚合）

    不依赖 sector_moneyflow，完全从 daily_price 聚合。
    覆盖全部交易日。

    Returns:
        DataFrame: trade_date, board_code, board_name, + 25维板块特征
    """
    if sector_daily is None:
        sector_daily = build_sector_daily(conn)

    sd = sector_daily.sort_values(['board_code', 'trade_date']).copy()

    result_rows = []

    for bd_code, group in sd.groupby('board_code'):
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue

        bd_name = group['board_name'].iloc[0]
        rets = group['sector_ret'].values
        amounts = group['sector_amount'].values
        trade_dates = group['trade_date'].values

        for i in range(20, len(group) - 3):
            row = {
                'trade_date': trade_dates[i],
                'board_code': bd_code,
                'board_name': bd_name,
                # 动量
                'sector_ret_1d': float(rets[i]),
                'sector_ret_3d': float(np.mean(rets[i-2:i+1])),
                'sector_ret_5d': float(np.mean(rets[i-4:i+1])),
                'sector_ret_10d': float(np.mean(rets[i-9:i+1])),
                'sector_ret_20d': float(np.mean(rets[i-19:i+1])),
                # 资金流 (用成交额变化代理)
                'sector_net_1d': float(amounts[i]),
                'sector_net_3d': float(np.sum(amounts[i-2:i+1])),
                'sector_net_5d': float(np.sum(amounts[i-4:i+1])),
                'sector_elg_net_1d': 0,
                'sector_elg_net_3d': 0,
                'sector_flow_accel': float(np.mean(amounts[i-2:i+1]) - np.mean(amounts[i-9:i+1])),
                # 波动
                'sector_vol_5d': float(np.std(rets[i-4:i+1])),
                'sector_vol_20d': float(np.std(rets[i-19:i+1])),
                # 持续性
                'sector_up_days_5d': int(np.sum(rets[i-4:i+1] > 0)),
                'sector_consecutive_up': int(_count_consecutive_up(rets[:i+1])),
                'sector_net_inflow_days': int(np.sum(np.diff(amounts[max(0,i-5):i+1]) > 0)),
                # 加速度
                'sector_ret_accel': float(np.mean(rets[i-4:i+1]) - np.mean(rets[i-9:i+1])),
                'sector_ret_vol_ratio': (_safe_div(np.mean(rets[i-4:i+1]), np.std(rets[i-4:i+1]))),
                # 资金流/收益比
                'sector_flow_yield': _safe_div(np.sum(amounts[i-4:i+1]), abs(np.mean(rets[i-4:i+1]))),
                # 占位（横截面计算）
                'sector_mom_rank': 0,
                'sector_flow_rank': 0,
                'sector_rel_strength': 0,
                'sector_trend_score': 0,
                # 标签：未来3日平均收益
                'future_ret_3d': float(np.mean(rets[i+1:i+4])) if i+4 < len(rets) else np.nan,
            }
            result_rows.append(row)

    result = pd.DataFrame(result_rows)
    result = result.dropna(subset=['future_ret_3d'])

    # qcut 10级标签 (按日横截面)
    result['label'] = result.groupby('trade_date')['future_ret_3d'].transform(
        lambda x: pd.qcut(x, q=10, labels=False, duplicates='drop')
    )
    result = result.dropna(subset=['label'])
    result['label'] = result['label'].astype(int)

    logger.info(f"  Sector heat features: {len(result)} rows, {result.board_code.nunique()} boards")
    return result


def _count_consecutive_up(arr):
    """计算连续上涨天数"""
    count = 0
    for v in reversed(arr):
        if v > 0:
            count += 1
        else:
            break
    return count


def _safe_div(a, b):
    return float(a / b) if b != 0 else 0.0
