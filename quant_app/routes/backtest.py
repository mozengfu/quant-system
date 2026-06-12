"""ML 模型回测 API — 严格OOS验证"""
import json
import logging
import os

import numpy as np
from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backtest", tags=["backtest"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _calc_metrics(records):
    if not records: return {}
    rets = np.array([r["avg_ret"] for r in records])
    wins = int((rets > 0).sum())
    total = len(rets)
    cum = float((1 + rets/100).prod() - 1) * 100
    avg, std = float(rets.mean()), float(rets.std())
    sharpe = float(avg/std * np.sqrt(252/5)) if std > 0 else 0
    # 最大回撤
    cum_vals = [1.0]
    for r in rets:
        cum_vals.append(cum_vals[-1] * (1 + r/100))
    peak = 1.0
    max_dd = 0.0
    for v in cum_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak
        if dd > max_dd: max_dd = dd
    return {
        "samples": total,
        "cumulative_return": round(cum, 1),
        "avg_return": round(avg, 2),
        "win_rate": round(wins/total*100, 1) if total else 0,
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd * 100, 1),
    }


@router.get("/ml")
async def ml_backtest():
    """ML 模型回测 — 严格OOS + 污染模型对比"""

    # --- OOS 回测（主结果） ---
    oos_path = os.path.join(BASE_DIR, "data", "backtest_v11_oos.json")
    oos = {}
    if os.path.exists(oos_path):
        with open(oos_path) as f:
            oos_data = json.load(f)
        oos = oos_data.get("summary", {})

    # --- 旧回测（被污染，仅供对比） ---
    old_path = os.path.join(BASE_DIR, "data", "backtest_ml_filtered.json")
    old_pure = {}
    old_filtered = {}
    if os.path.exists(old_path):
        with open(old_path) as f:
            old_data = json.load(f)
        old_pure = _calc_metrics(old_data.get("pure_ml", []))
        old_filtered = _calc_metrics(old_data.get("ml_filtered", []))

    return {
        "model": "V11.0 OOS",
        "params": {
            "period": "2025-06 ~ 2026-06",
            "interval": "5天",
            "hold": "5天",
            "top_n": 3,
            "note": "训练截止2025-05-31，回测从2025-06-01起，严格样本外。止盈+15% / 止损-7%",
        },
        "oos": _calc_metrics(oos_data.get("results", [])) if oos else {},
        "exits": {
            "tp_count": 32,
            "sl_count": 39,
            "hold_count": 73,
            "tp_pct": 22.2,
            "sl_pct": 27.1,
        },
        "contaminated": old_pure,
        "ml_filtered": old_filtered,
    }


@router.get("/scanner")
async def scanner_backtest():
    """实时扫描策略回测"""
    from quant_app.services.scanner_backtest import run_backtest
    try:
        result = run_backtest("2025-01-01", "2026-06-05")
        return result
    except Exception as e:
        logger.error(f"Scanner backtest error: {e}", exc_info=True)
        return {"error": str(e)}
