"""
交易数据模型 — Order / Position / Balance
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class Order:
    """订单数据"""
    order_id: str
    ts_code: str
    name: str
    action: Literal["BUY", "SELL"]
    price: float
    quantity: int
    amount: float
    status: Literal["pending", "filled", "partial", "canceled", "rejected"] = "pending"
    filled_quantity: int = 0
    filled_amount: float = 0.0
    reason: str = ""
    created_at: datetime = None
    updated_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = self.created_at


@dataclass
class Position:
    """持仓数据"""
    position_id: int = 0
    ts_code: str = ""
    name: str = ""
    market: str = ""
    quantity: int = 0
    cost_price: float = 0.0
    total_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    buy_date: str = ""
    ml_prob: float | None = None
    strategy: str | None = None


@dataclass
class Balance:
    """账户资金"""
    total_asset: float = 0.0
    available: float = 0.0
    market_value: float = 0.0
    frozen: float = 0.0
    initial_capital: float = 100000.0
    profit_loss: float = 0.0
    profit_pct: float = 0.0
    max_drawdown: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    win_rate: float = 0.0


@dataclass
class TradeRecord:
    """交易记录"""
    id: int = 0
    ts_code: str = ""
    stock_name: str = ""
    market: str = ""
    action: Literal["BUY", "SELL"] = "BUY"
    price: float = 0.0
    shares: int = 0
    amount: float = 0.0
    trade_date: str = ""
    profit_loss: float | None = None
    profit_pct: float | None = None
    reason: str = ""


@dataclass
class SignalRecord:
    """信号记录"""
    id: int = 0
    signal_type: str = ""
    ts_code: str = ""
    stock_name: str = ""
    price: float = 0.0
    shares: int = 0
    strategy: str | None = None
    ml_prob: float | None = None
    market_state: str | None = None
    reason: str = ""
    signal_date: str = ""
    status: str = "已执行"
