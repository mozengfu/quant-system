"""市场状态判定服务 — 从根 market_state 重导出，逐步迁移到此模块"""

# 当前从根目录 market_state.py 重导出，避免双向依赖
# 未来应将 market_state.py 的实现移入此模块，根文件改为 thin re-export
from market_state import get_market_state  # noqa: E402

__all__ = ["get_market_state"]
