"""风控过滤模块。

生产管线中的风控规则（涨停追高/52周高位/异常放量/短期过热），
支持宽松降级模式。

移植自 quant_app/services/strategy_service.py (lines ~2430-2478)。
"""

import logging

logger = logging.getLogger(__name__)

# 弱市状态（启用额外过滤）
TIGHT_STATES = frozenset({"trend_down", "panic", "overheated"})


def apply_risk_filters(
    candidates: list[dict],
    risk_state: str = "range",
    tight_mode: bool | None = None,
) -> list[dict]:
    """对 ML 候选列表执行风控过滤。

    规则:
    1. 涨停追高（pct_chg > 9%，始终启用）
    2. 52 周高位（pos > 85%，仅弱势市场）
    3. 异常放量（pct_chg > 5% AND volume_ratio > 5，始终启用）
    4. 短期过热（RPS > 95 AND pct_chg > 4%，仅弱势市场）

    宽松降级：全部被过滤时只保留涨停追高过滤。

    Args:
        candidates: 候选列表，每个 dict 需包含
            ts_code, name, pct_chg, volume_ratio, rps_20,
            close, high_52w, low_52w
        risk_state: 市场状态（trend_up/range/trend_down/panic/overheated）
        tight_mode: 是否启用严格模式，不传则根据 risk_state 自动判断

    Returns:
        过滤后的候选列表
    """
    if tight_mode is None:
        tight_mode = risk_state in TIGHT_STATES

    passed = []
    for c in candidates:
        risks = []

        # 规则 1: 涨停追高（始终启用）
        if c.get("pct_chg", 0) > 9:
            risks.append("涨停追高")

        # 规则 2: 52 周高位（仅弱市启用）
        if tight_mode:
            h52 = c.get("high_52w", 0)
            l52 = c.get("low_52w", 0)
            if h52 > l52 > 0:
                pos = (c["close"] - l52) / (h52 - l52) * 100
                if pos > 85:
                    risks.append(f"52周高位({pos:.0f}%)")

        # 规则 3: 异常放量（始终启用）
        if c.get("pct_chg", 0) > 5 and c.get("volume_ratio", 0) > 5:
            risks.append("异常放量")

        # 规则 4: 短期过热（仅弱市启用）
        if tight_mode and c.get("rps_20", 0) > 95 and c.get("pct_chg", 0) > 4:
            risks.append("短期过热(RPS>95+涨4%)")

        if risks:
            c["risk_filtered"] = True
            c["risk_reason"] = "; ".join(risks)
            logger.info(
                "风控过滤[%s]: %s(%s) %s",
                risk_state,
                c["name"],
                c["ts_code"],
                "; ".join(risks),
            )
        else:
            passed.append(c)

    # 宽松降级：全部被过滤时只保留涨停追高
    if not passed:
        logger.warning(
            "所有候选被风控过滤，降级为宽松模式（仅保留涨停追高）",
        )
        passed = []
        for c in candidates:
            risks = []
            if c.get("pct_chg", 0) > 9:
                risks.append("涨停追高")
            if risks:
                c["risk_filtered"] = True
                c["risk_reason"] = "; ".join(risks)
            else:
                passed.append(c)
        if not passed:
            passed = candidates

    return passed
