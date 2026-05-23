"""moneyflow_daily 表 ORM 模型。"""
from decimal import Decimal

from sqlalchemy import DECIMAL, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class MoneyflowDaily(Base):
    __tablename__ = "moneyflow_daily"

    ts_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    trade_date: Mapped[str] = mapped_column(Date, primary_key=True)
    buy_sm_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    sell_sm_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    buy_md_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    sell_md_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    buy_lg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    sell_lg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    buy_elg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    sell_elg_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    net_mf_amount: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
    main_net: Mapped[Decimal | None] = mapped_column(DECIMAL(20, 2))
