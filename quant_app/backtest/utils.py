#!/usr/bin/env python3
"""
回测共享工具函数 — 统一 6 个回测脚本中的重复 SQL 逻辑

用法:
    from quant_app.backtest.utils import (
        get_trade_dates, get_candidate_pool,
        compute_pool_forward_returns, backtest_stats,
        create_backtest_engine
    )
"""

import logging

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

from quant_app.utils.config import config

logger = logging.getLogger(__name__)


def create_backtest_engine(pool_size=5):
    """创建统一的回测用 SQLAlchemy 引擎"""
    params = config.mysql.get_connection_params()
    url = config.mysql.url
    return create_engine(url, pool_pre_ping=True, pool_size=pool_size)


def get_trade_dates(engine, start_date, end_date, date_col="trade_date", table="daily_price"):
    """获取日期范围内的所有交易日（已排序）

    Args:
        engine: SQLAlchemy 引擎
        start_date: 起始日期 "YYYY-MM-DD"
        end_date: 结束日期 "YYYY-MM-DD"
        date_col: 日期列名
        table: 表名

    Returns:
        list[str]: 排序后的交易日列表
    """
    sql = f"""
        SELECT DISTINCT {date_col}
        FROM {table}
        WHERE {date_col} >= %(start)s AND {date_col} <= %(end)s
        ORDER BY {date_col}
    """
    df = pd.read_sql(sql, engine, params={"start": start_date, "end": end_date})
    return df[date_col].astype(str).tolist()


def get_prev_trade_date(engine, trade_date, table="daily_price", date_col="trade_date"):
    """获取指定日期之前的最近交易日"""
    sql = f"SELECT MAX({date_col}) FROM {table} WHERE {date_col} < %(d)s"
    df = pd.read_sql(sql, engine, params={"d": trade_date})
    val = df.iloc[0, 0]
    if hasattr(val, 'strftime'):
        return val.strftime('%Y-%m-%d')
    return str(val)


def get_candidate_pool(engine, trade_date, limit=500, table="daily_price"):
    """获取成交额 Top N 候选池（参数化查询）

    Args:
        engine: SQLAlchemy 引擎
        trade_date: 交易日 "YYYY-MM-DD"
        limit: 候选池大小

    Returns:
        list[str]: ts_code 列表
    """
    sql = """
        SELECT ts_code FROM {table}
        WHERE trade_date = %(d)s
          AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200
        ORDER BY amount DESC LIMIT %(lim)s
    """
    df = pd.read_sql(sql.format(table=table), engine, params={"d": trade_date, "lim": limit})
    return df['ts_code'].tolist()


def compute_pool_forward_returns(conn_or_engine, ts_codes, buy_date, hold_days=5, table="daily_price"):
    """计算候选池前向收益（个股持仓期内累计收益）

    Args:
        conn_or_engine: DB 连接或引擎
        ts_codes: 股票代码列表
        buy_date: 买入日期 "YYYY-MM-DD"
        hold_days: 持仓天数

    Returns:
        dict[str, float]: {ts_code: 总收益%}
    """
    if not ts_codes:
        return {}
    placeholders = ','.join(['%s'] * len(ts_codes))
    sql = f"""
        SELECT ts_code, pct_chg FROM {table}
        WHERE ts_code IN ({placeholders})
          AND trade_date > %s
        ORDER BY ts_code, trade_date
    """
    params = ts_codes + [buy_date]
    if hasattr(conn_or_engine, 'cursor'):
        # pymysql 连接
        cur = conn_or_engine.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        fwd = pd.DataFrame(rows, columns=['ts_code', 'pct_chg'])
    else:
        # SQLAlchemy 引擎
        fwd = pd.read_sql(sql, conn_or_engine, params=params)

    fwd_rets = {}
    for tc in ts_codes:
        ts_fwd = fwd[fwd['ts_code'] == tc]['pct_chg'].values[:hold_days] / 100.0
        ts_fwd = ts_fwd[~np.isnan(ts_fwd)]
        if len(ts_fwd) >= 2:
            fwd_rets[tc] = float((1 + ts_fwd).prod() - 1) * 100
    return fwd_rets


def backtest_stats(results):
    """计算回测统计指标

    Args:
        results: list[dict], 每个元素包含 'avg_ret' (float, 收益%)

    Returns:
        dict: {cum_return, win_rate, sharpe, max_drawdown, avg_return, count}
    """
    if not results:
        return {"cum_return": 0, "win_rate": 0, "sharpe": 0,
                "max_drawdown": 0, "avg_return": 0, "count": 0}

    rets = np.array([r['avg_ret'] for r in results])
    cum = float((1 + rets / 100).prod() - 1) * 100
    wins = int((rets > 0).sum())
    total = len(rets)
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg / std * np.sqrt(252 / max(len(results), 1))) if std > 1e-8 else 0
    dd = float(min(0, (rets / 100).min()))

    return {
        "cum_return": round(cum, 2),
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(dd * 100, 2),
        "avg_return": round(avg, 2),
        "count": total,
    }


def format_backtest_table(all_stats):
    """格式化输出回测对比表

    Args:
        all_stats: list of (name, stats_dict)
    """
    header = f"{'策略':<25} {'累积收益':>10} {'胜率':>8} {'夏普':>8} {'最大回撤':>8} {'均值':>7} {'样本':>6}"
    sep = '-' * 72
    lines = [header, sep]
    for name, s in all_stats:
        lines.append(
            f"{name:<25} {s['cum_return']:>+10.2f}% "
            f"{s['win_rate']:>7.1f}% {s['sharpe']:>8.2f} "
            f"{s['max_drawdown']:>8.2f}% {s['avg_return']:>+6.2f}% {s['count']:>6d}"
        )
    return '\n'.join(lines)
