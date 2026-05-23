"""stock_info 表 ORM 模型。"""
from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class StockInfo(Base):
    __tablename__ = "stock_info"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    code: Mapped[str | None] = mapped_column(String(6))
    name: Mapped[str | None] = mapped_column(String(50))
    market: Mapped[str | None] = mapped_column(String(4))
    industry: Mapped[str | None] = mapped_column(String(50))
    list_date: Mapped[str | None] = mapped_column(String(10))
    is_st: Mapped[int | None] = mapped_column(SmallInteger, default=0)
