"""
策略选股相关 API 路由 — 分析 / 情绪 / 板块
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException
from fastapi import Request as FastAPIRequest

from quant_app.routes.auth import get_current_user
from quant_app.services.market_service import (
    get_recent_trade_dates,
    get_tushare_pro,
)
from quant_app.services.strategy_service import (
    ALL_BLOCKS,
    analyze_stock,
)
from quant_app.utils.persistence import (
    get_client_ip,
    save_access_log,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["strategy"])


# ========== 个股分析 ==========


@router.get("/api/analysis/{market}/{code}")
def analyze(market: str, code: str, request: FastAPIRequest, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"分析个股 {market.upper()}{code}")
    return analyze_stock(code, market)


def _get_limit_pct(ts_code):
    """根据 ts_code 前缀判断涨跌幅限制"""
    if ts_code.startswith("688") or ts_code.startswith("689"):
        return 20.0
    if ts_code.startswith("30"):
        return 20.0
    if ts_code.startswith("8") or ts_code.startswith("4"):
        return 30.0
    return 10.0


@router.get("/api/sentiment")
def get_sentiment(token: str = Cookie(None)):
    """市场情绪接口 - 涨停/跌停/涨跌家数比"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        pro = get_tushare_pro()
        today = get_recent_trade_dates(1)
        if not today:
            return {"error": "无法获取今日交易日"}
        trade_date = today[-1]
        df = pro.daily(trade_date=trade_date)
        if df is None or len(df) == 0:
            return {"error": f"今日({trade_date})无交易数据"}
        rise_count = int((df.apply(lambda r: r["pct_chg"] >= _get_limit_pct(r["ts_code"]), axis=1)).sum())
        fall_count = int((df.apply(lambda r: r["pct_chg"] <= -_get_limit_pct(r["ts_code"]), axis=1)).sum())
        up_count = int((df["pct_chg"] > 0).sum())
        down_count = int((df["pct_chg"] < 0).sum())
        len(df)
        rise_ratio = up_count / max(down_count, 1)
        if rise_ratio >= 2:
            sentiment = "极度乐观"
        elif rise_ratio >= 1.2:
            sentiment = "偏乐观"
        elif rise_ratio >= 0.8:
            sentiment = "中性"
        elif rise_ratio >= 0.5:
            sentiment = "偏悲观"
        else:
            sentiment = "极度悲观"
        return {
            "trade_date": trade_date,
            "涨停家数": rise_count,
            "跌停家数": fall_count,
            "上涨家数": up_count,
            "下跌家数": down_count,
            "涨跌比": round(rise_ratio, 2),
            "市场情绪": sentiment,
            "备注": "涨停/跌停按各板块涨跌幅限制计算",
        }
    except Exception as e:
        logger.error(f"市场情绪获取失败: {e}")
        return {"error": str(e)}


@router.get("/api/blocks")
def get_blocks(token: str = Cookie(None)):
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    return {"blocks": ALL_BLOCKS}


@router.get("/api/strategy/compare")
def strategy_compare(token: str = Cookie(None)):
    """策略对比：V11 vs 板RPS实时 交易表现"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    import pymysql
    from quant_app.utils.config import get_db_config

    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()
    try:
        # 按策略汇总
        cur.execute("""
            SELECT
                strategy,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status='已平仓' THEN 1 ELSE 0 END) AS closed,
                SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins,
                ROUND(AVG(pnl_pct)*100, 2) AS avg_return_pct,
                ROUND(SUM(pnl), 2) AS total_pnl,
                ROUND(MAX(pnl_pct)*100, 2) AS best_trade_pct,
                ROUND(MIN(pnl_pct)*100, 2) AS worst_trade_pct,
                ROUND(AVG(hold_days), 1) AS avg_hold_days
            FROM strategy_trade_log
            GROUP BY strategy
            ORDER BY total_pnl DESC
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]

        # 持仓中（未平仓）
        cur.execute("""
            SELECT strategy, COUNT(*) AS holding
            FROM strategy_trade_log
            WHERE status='持有'
            GROUP BY strategy
        """)
        holding = {r[0]: r[1] for r in cur.fetchall()}

        strategies = []
        for row in rows:
            s = dict(zip(cols, row))
            s['holding'] = holding.get(s['strategy'], 0)
            # 胜率
            s['win_rate'] = round(s['wins'] / s['closed'] * 100, 1) if s['closed'] > 0 else 0
            # 胜者全拿建议
            strategies.append(s)

        # 最佳策略
        best = max(strategies, key=lambda x: x['total_pnl']) if strategies else None

        cur.close()
        conn.close()

        return {
            'strategies': strategies,
            'best_strategy': best['strategy'] if best else None,
            'best_total_pnl': best['total_pnl'] if best else 0,
        }
    except Exception as e:
        cur.close()
        conn.close()
        return {'error': str(e)}
