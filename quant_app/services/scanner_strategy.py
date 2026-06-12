"""
实时扫描策略 v8 - 高胜率版
"""
import json
import logging
from datetime import datetime
from pathlib import Path

import yaml

from quant_app.services.realtime_scanner import scan_stocks
from quant_app.utils.config import config

logger = logging.getLogger(__name__)
SIGNAL_FILE = "scanner_signals.json"

def _load_config():
    cfg_path = Path(__file__).parent.parent.parent / "config" / "scanner_config.yaml"
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except:
        return {
            "capital": {"total": 100000, "scanner_ratio": 0.5},
            "position": {"max_positions": 3, "weight_per_stock": 0.33},
            "entry_filter": {"min_score": 65},
        }

def _get_account_balance():
    """获取QMT实时账户总资产，30秒缓存"""
    import time
    now = time.time()
    cache = getattr(_get_account_balance, "_cache", None)
    if cache and now - cache["ts"] < 30:
        return cache["total_asset"]
    try:
        import requests
        r = requests.get("http://192.168.10.25:1430/balance", timeout=3)
        data = r.json()
        total = float(data.get("total_asset", 0))
        _get_account_balance._cache = {"ts": now, "total_asset": total}
        return total
    except Exception:
        # 降级：使用配置文件中的初始资金
        return _load_config().get("capital", {}).get("total", 100000)

def get_capital_ratio():
    return _load_config().get("capital", {}).get("scanner_ratio", 0.5)

def get_total_capital():
    """动态获取：账户当前总资产"""
    return _get_account_balance()

def get_scanner_capital():
    """动态获取：总资产 × Scanner比例"""
    ratio = get_capital_ratio()
    return _get_account_balance() * ratio

def get_v11_capital():
    """动态获取：总资产 × ML比例"""
    ratio = 1 - get_capital_ratio()
    return _get_account_balance() * ratio

def generate_signals() -> dict:
    cfg = _load_config()
    result = scan_stocks()

    buy_threshold = cfg.get("entry_filter", {}).get("min_score", 65)
    max_positions = cfg.get("position", {}).get("max_positions", 3)

    signals = []
    for s in result.get("signals", []):
        level = s.get("level", "NEUTRAL")
        score = s.get("score", 0)
        if level in ("STRONG_BUY", "BUY") and score >= buy_threshold:
            signals.append({
                "ts_code": s["code"], "price": s["price"], "score": score,
                "level": level, "weight": 0.33,
                "factors": s.get("factors", {}), "pct_chg": s.get("pctChg", 0),
            })

    signals = sorted(signals, key=lambda x: x["score"], reverse=True)[:max_positions]
    if signals:
        weight_each = min(0.33, 1.0 / len(signals))
        for s in signals:
            s["weight"] = weight_each

    output = {
        "ts": datetime.now().isoformat(), "strategy": "scanner",
        "total_capital": get_total_capital(), "scanner_capital": get_scanner_capital(),
        "capital_ratio": get_capital_ratio(),
        "market": result.get("market", {}),
        "total_scanned": result.get("total_scanned", 0),
        "signals": signals,
    }

    signal_path = config.data_dir / SIGNAL_FILE
    signal_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"扫描策略v8: {len(signals)}个信号")
    return output

def get_current_signals() -> dict:
    signal_path = config.data_dir / SIGNAL_FILE
    if signal_path.exists():
        try:
            return json.loads(signal_path.read_text(encoding="utf-8"))
        except:
            pass
    return {"ts": "", "signals": [], "strategy": "scanner", "capital_ratio": get_capital_ratio()}
