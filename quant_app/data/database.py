"""SQLAlchemy 数据库引擎和会话管理。

与 quant-system 现有的 pymysql 连接并存，新代码推荐使用此模块。

用法:
    from quant_app.data.database import with_session, SessionLocal

    # 上下文管理器方式
    with with_session() as sess:
        rows = sess.query(DailyPrice).filter(...).all()

    # 手动管理方式
    sess = SessionLocal()
    try:
        ...
    finally:
        sess.close()
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from quant_app.utils.config import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_SOCKET,
    MYSQL_USER,
)


def _build_url() -> str:
    """构建 SQLAlchemy DSN。"""
    if MYSQL_SOCKET:
        return (
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
            f"@/{MYSQL_DATABASE}"
            f"?unix_socket={MYSQL_SOCKET}&charset=utf8mb4"
        )
    return f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"


engine = create_engine(
    _build_url(),
    pool_size=20,
    max_overflow=30,
    pool_recycle=3600,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class with_session:
    """上下文管理器，自动管理会话生命周期。

    用法:
        with with_session() as sess:
            rows = sess.query(DailyPrice).filter(...).all()
    """

    def __enter__(self):
        self.sess = SessionLocal()
        return self.sess

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.sess.rollback()
        self.sess.close()
        return False
