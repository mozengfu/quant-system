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
