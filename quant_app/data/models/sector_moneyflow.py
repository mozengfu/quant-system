"""sector_moneyflow 表 ORM 模型。"""
from decimal import Decimal

from sqlalchemy import DECIMAL, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class SectorMoneyflow(Base):
    __tablename__ = "sector_moneyflow"

    trade_date: Mapped[str] = mapped_column(Date, primary_key=True)
    sector_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    net_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    buy_elg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    sell_elg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    pct_change: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
