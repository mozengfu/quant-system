"""daily_price 表 ORM 模型。"""

from decimal import Decimal

from sqlalchemy import DECIMAL, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class DailyPrice(Base):
    __tablename__ = "daily_price"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[str] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    high: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    low: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    close: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    pre_close: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    vol: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    pct_chg: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    turnover_rate: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    volume_ratio: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 4))
    rps_20: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    ma5: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    ma10: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
    ma20: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2))
