"""
风控参数配置管理
优先级链：市场状态参数 > risk_config.json > 代码默认值
"""
import json
import os


def load_risk_config():
    """加载 risk_config.json"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "risk_config.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"circuit_breaker": {"max_drawdown_pct": -15, "enabled": True}}


def get_circuit_breaker_config():
    config = load_risk_config()
    return config.get("circuit_breaker", {})


def get_position_sizing_config():
    config = load_risk_config()
    return config.get("position_sizing", {})


def get_market_state_override(state):
    """获取特定市场状态的参数覆盖"""
    config = load_risk_config()
    overrides = config.get("market_state_overrides", {})
    return overrides.get(state) if state in overrides else None
