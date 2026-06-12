"""
交易执行器抽象基类 + 工厂函数

定义 AbstractTradeExecutor 接口，所有具体实现（模拟盘/实盘）都继承此类。
"""

import logging
from abc import ABC, abstractmethod

from quant_app.trading.orders import Balance, Order, Position

logger = logging.getLogger(__name__)


class AbstractTradeExecutor(ABC):
    """交易执行器抽象 — 所有具体实现都实现这组接口"""

    @abstractmethod
    def buy(
        self,
        ts_code: str,
        name: str,
        market: str,
        price: float,
        quantity: int,
        strategy: str = None,
        ml_prob: float = None,
        enhanced_score: float = None,
        market_state: str = None,
        reason: str = None,
    ) -> Order | None:
        """执行买入"""
        ...

    @abstractmethod
    def sell(
        self,
        position_id: int,
        ts_code: str,
        price: float,
        quantity: int,
        reason: str = None,
    ) -> Order | None:
        """执行卖出（清仓）"""
        ...

    @abstractmethod
    def partial_sell(
        self,
        position_id: int,
        ts_code: str,
        price: float,
        quantity: int,
        reason: str = None,
    ) -> Order | None:
        """执行部分卖出"""
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """获取当前持仓列表"""
        ...

    @abstractmethod
    def get_balance(self) -> Balance | None:
        """获取账户资金信息"""
        ...

    @abstractmethod
    def get_orders(self, status: str = None) -> list[Order]:
        """获取订单列表"""
        ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """撤单"""
        ...


def create_executor(mode: str = None) -> AbstractTradeExecutor:
    """工厂方法：根据 TRADE_MODE 创建合适的执行器

    Args:
        mode: 'sim' 或 'live'，None 时从配置读取

    Returns:
        AbstractTradeExecutor 实例
    """
    if mode is None:
        from quant_app.trading.config import trading_config
        mode = trading_config.trade_mode

    if mode == "sim":
        from quant_app.trading.modes.sim_executor import SimExecutor
        return SimExecutor()
    elif mode == "live":
        from quant_app.trading.modes.remote_executor import RemoteTraderExecutor
        return RemoteTraderExecutor()
    else:
        raise ValueError(f"未知交易模式: {mode}，仅支持 'sim'/'live'")
