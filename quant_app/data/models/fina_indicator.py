"""fina_indicator 表 ORM 模型。"""
from decimal import Decimal

from sqlalchemy import DECIMAL, Date, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class FinaIndicator(Base):
    __tablename__ = "fina_indicator"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    end_date: Mapped[str] = mapped_column(Date, primary_key=True)
    roe: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    yoy_sales: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    grossprofit_margin: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    netprofit_margin: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    eps: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    update_time: Mapped[str | None] = mapped_column(DateTime)
