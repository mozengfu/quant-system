"""
管线聚合 API — 汇总市场状态/ML预测/交易信号/绩效到单一端点
"""
import json
import logging
from datetime import date

from fastapi import APIRouter, Cookie, HTTPException
from quant_app.routes.auth import get_current_user

from quant_app.services.scanner_strategy import generate_signals, get_current_signals
from quant_app.utils.config import config
from quant_app.utils.model_loader import load_model

UNAUTHORIZED = HTTPException(status_code=401, detail="未登录或会话已过期")


def _auth_guard(token: str) -> str:
    user = get_current_user(token)
    if not user:
        raise UNAUTHORIZED
    return user




logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

DATA_DIR = config.data_dir


def _read_json(filename: str):
    """安全读取 data/ 下的 JSON 文件"""
    path = DATA_DIR / filename
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 %s 失败: %s", filename, e)
        return {}


def _get_market_info():
    """从 market_state.json 聚合市场信息"""
    state = _read_json("market_state.json")
    # 兼容两种字段命名风格
    state_val = state.get("state_name") or state.get("state", "未知")
    idx = state.get("sh_price") or state.get("index")
    chg_pct = state.get("sh_pct") or state.get("change_pct")
    update_time = state.get("updated_at") or state.get("update_time")
    # 从 state 值推算仓位建议
    pos_map = {"panic": 0, "fear": 0, "fear_close": 0, "block": 30,
               "weak": 50, "normal": 80, "strong": 100, "bull": 100}
    position_ratio = state.get("position_ratio")
    if position_ratio is None:
        position_ratio = pos_map.get(state_val) or pos_map.get(state.get("state"), 100)
    return {
        "state": state_val,
        "state_name": state.get("state_name") or state.get("state", "未知"),
        "index": idx,
        "change_pct": chg_pct,
        "position_ratio": position_ratio,
        "update_time": update_time,
    }


def _get_ml_info():
    """聚合 ML 模型信息"""
    bundle = load_model("v11.0")
    info = {
        "model": "v11.0",
        "status": "normal",
    }
    if bundle:
        info["model_size_mb"] = round(bundle.get("model_size_mb", 0), 1)
        info["feature_count"] = len(bundle.get("feature_cols", []))
        info["model_count"] = len(bundle.get("models", []))
        rank_ic = bundle.get("rank_ic")
        if rank_ic is not None:
            info["rank_ic"] = round(rank_ic, 4)
    else:
        info["status"] = "unavailable"

    # 检查最近的预测文件
    today_str = date.today().strftime("%Y-%m-%d")
    pred_file = DATA_DIR / f"ml_preds_{today_str}.parquet"
    if pred_file.exists():
        info["last_predict"] = today_str
    else:
        info["last_predict"] = None

    return info


def _get_trading_info():
    """聚合交易信号信息"""
    # 从 sim_signals 表获取今日信号数
    try:
        import pymysql

        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()
        today_str = date.today().strftime("%Y-%m-%d")

        cur.execute(
            "SELECT COUNT(*) FROM sim_signals WHERE DATE(created_at) = %s",
            (today_str,)
        )
        signals_today = cur.fetchone()[0] or 0

        cur.execute("SELECT COUNT(*) FROM sim_positions WHERE status='HOLD'")
        positions = cur.fetchone()[0] or 0

        cur.close()
        conn.close()

        return {
            "signals_today": signals_today,
            "positions": positions,
            "executor": "QMT",
        }
    except Exception as e:
        logger.warning("获取交易信息失败: %s", e)
        return {"signals_today": 0, "positions": 0, "executor": "QMT"}



def _get_scanner_info():
    """获取实时扫描策略状态"""
    try:
        from quant_app.services.scanner_strategy import get_scanner_capital, get_total_capital, get_v11_capital
        signals = get_current_signals()
        return {
            "strategy": "scanner",
            "total_capital": get_total_capital(),
            "scanner_capital": get_scanner_capital(),
            "v11_capital": get_v11_capital(),
            "capital_ratio": signals.get("capital_ratio", 0.5),
            "total_scanned": signals.get("total_scanned", 0),
            "signals_count": len(signals.get("signals", [])),
            "top_signal": signals["signals"][0] if signals.get("signals") else None,
            "market_level": signals.get("market", {}).get("index_level", "?"),
            "ts": signals.get("ts", ""),
        }
    except Exception as e:
        return {"error": str(e), "signals_count": 0}


def _get_performance_info():
    """聚合绩效信息"""
    nav = _read_json("nav_history.json")
    perf = {}

    if nav:
        if isinstance(nav, list) and len(nav) > 0:
            last = nav[-1] if isinstance(nav[-1], dict) else {}
            perf["total_return"] = last.get("total_return_pct", last.get("return_pct"))
            perf["month_return"] = last.get("month_return_pct")
        elif isinstance(nav, dict):
            perf["total_return"] = nav.get("total_return_pct")

    mon = _read_json("model_monitor_history_v11_2.json")
    if mon:
        if isinstance(mon, list) and mon:
            latest = mon[-1]
            if isinstance(latest, dict):
                perf["rank_ic"] = latest.get("rank_ic")

    # 从 performance_summary API 读数据（通过 DB）
    try:
        import pymysql

        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cur = conn.cursor()
        cur.execute(
            "SELECT sharpe, win_rate, max_drawdown, total_return "
            "FROM nav_history ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            perf["sharpe"] = round(float(row[0]), 2) if row[0] else None
            win_rate = float(row[1]) if row[1] else None
            perf["win_rate"] = round(win_rate, 4) if win_rate else None
            perf["max_drawdown"] = round(float(row[2]), 4) if row[2] else None
            perf["total_return"] = round(float(row[3]), 4) if row[3] else None
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("获取绩效数据失败: %s", e)

    return perf



@router.get("/scanner/refresh")
async def refresh_scanner(token: str = Cookie(None)):
    user = _auth_guard(token)
    """手动刷新扫描策略信号"""
    result = generate_signals()
    return {"ok": True, "signals": len(result.get("signals", []))}

@router.get("/scanner/signals")
async def scanner_signals(token: str = Cookie(None)):
    user = _auth_guard(token)
    """获取扫描策略当前信号"""
    return get_current_signals()


@router.get("/status")
async def pipeline_status(token: str = Cookie(None)):
    user = _auth_guard(token)
    """聚合管线各阶段状态"""
    return {
        "market": _get_market_info(),
        "ml": _get_ml_info(),
        "trading": _get_trading_info(),
        "scanner": _get_scanner_info(),
        "performance": _get_performance_info(),
    }
