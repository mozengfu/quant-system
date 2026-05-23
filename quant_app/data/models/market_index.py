"""market_index_daily 表 ORM 模型。"""
from decimal import Decimal

from sqlalchemy import DECIMAL, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class MarketIndexDaily(Base):
    __tablename__ = "market_index_daily"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[str] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    high: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    low: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    close: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    pre_close: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    change_pct: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
