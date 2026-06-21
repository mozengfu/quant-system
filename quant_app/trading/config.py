"""
交易配置 — 读取 .env 中的 TRADE_MODE / 券商参数 / 安全控制参数
"""

import logging
import os

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


class TradingConfig:
    """交易配置单例"""

    # 交易模式
    trade_mode: str = os.getenv("TRADE_MODE", "sim")  # sim / live

    # 券商配置
    trade_broker: str = os.getenv("TRADE_BROKER", "ths")  # QMT直连
    trade_account_id: str = os.getenv("TRADE_ACCOUNT_ID", "")
    trade_password: str = os.getenv("TRADE_PASSWORD", "")
    trade_comm_password: str = os.getenv("TRADE_COMM_PASSWORD", "")

    # 实盘安全控制
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "-5"))
    max_single_order_amount: float = float(os.getenv("MAX_SINGLE_ORDER_AMOUNT", "50000"))
    max_position_pct: float = float(os.getenv("MAX_POSITION_PCT", "30"))
    price_deviation_pct: float = float(os.getenv("PRICE_DEVIATION_PCT", "1.0"))
    enable_real_trading: bool = os.getenv("ENABLE_REAL_TRADING", "false").lower() in ("true", "1", "yes")

    # 远程交易服务端配置（macOS + Windows VM 架构）
    remote_trader_host: str = os.getenv("REMOTE_TRADER_HOST", "")
    remote_trader_port: int = int(os.getenv("REMOTE_TRADER_PORT", "1430"))

    # 模拟盘默认参数
    initial_capital: float = float(os.getenv("SIM_INITIAL_CAPITAL", "100000"))
    max_positions: int = int(os.getenv("SIM_MAX_POSITIONS", "3"))

    @property
    def is_live(self) -> bool:
        """是否实盘模式"""
        return self.trade_mode == "live"

    @property
    def is_real_trading_enabled(self) -> bool:
        """实盘模式 + 安全开关已开启"""
        return self.is_live and self.enable_real_trading

    def validate(self) -> list[str]:
        """校验配置，返回所有错误信息列表"""
        errors = []
        if self.trade_mode not in ("sim", "live"):
            errors.append(f"TRADE_MODE 必须为 'sim' 或 'live'，当前: {self.trade_mode}")
        if self.is_live:
            if not self.trade_account_id:
                errors.append("实盘模式需要设置 TRADE_ACCOUNT_ID")
            if self.max_daily_loss_pct >= 0:
                errors.append("MAX_DAILY_LOSS_PCT 必须为负数")
            if self.max_single_order_amount <= 0:
                errors.append("MAX_SINGLE_ORDER_AMOUNT 必须 > 0")
            if self.max_position_pct <= 0 or self.max_position_pct > 100:
                errors.append("MAX_POSITION_PCT 必须为 1-100")
        return errors


trading_config = TradingConfig()
