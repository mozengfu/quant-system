"""board_concept 和 board_industry 表 ORM 模型。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from quant_app.data.database import Base


class BoardConcept(Base):
    __tablename__ = "board_concept"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))


class BoardIndustry(Base):
    __tablename__ = "board_industry"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(50))
