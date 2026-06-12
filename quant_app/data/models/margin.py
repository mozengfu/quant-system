"""margin_daily 表 ORM 模型。"""

from decimal import Decimal

from sqlalchemy import DECIMAL, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class MarginDaily(Base):
    __tablename__ = "margin_daily"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[str] = mapped_column(Date, primary_key=True)
    rzye: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    rqye: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    rzmre: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    rqyl: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    rqmcl: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
