"""配置模块 — 统一 Config 单例

用法:
    from quant_app.utils.config import config
    db_params = config.mysql.get_connection_params()
    engine = create_engine(config.mysql.url)
    webhook = config.notification.feishu_webhook

旧式模块级常量仍可用作向后兼容别名。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(dotenv_path=ROOT / ".env")


class MySQLConfig:
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    database = os.getenv("MYSQL_DATABASE", "quant_db")
    socket = os.getenv("MYSQL_SOCKET", "/tmp/mysql.sock")

    @property
    def url(self) -> str:
        return f"mysql+pymysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"

    @property
    def url_with_socket(self) -> str:
        return f"mysql+pymysql://{self.user}:{self.password}@localhost/{self.database}?unix_socket={self.socket}"

    def get_connection_params(self, **kwargs):
        """pymysql 连接参数字典（兼容旧 get_db_config()）"""
        params = {
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "connect_timeout": 5,
        }
        if self.socket:
            params["unix_socket"] = self.socket
        else:
            params["host"] = self.host
            params["port"] = self.port
        params.update(kwargs)
        return params


class TushareConfig:
    token = os.getenv("TUSHARE_TOKEN", "")


class NotificationConfig:
    feishu_webhook = os.getenv("FEISHU_WEBHOOK", "")
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "")
    smtp_host = os.getenv("SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    sms_access_key = os.getenv("ALIYUN_SMS_ACCESS_KEY", "")
    sms_access_secret = os.getenv("ALIYUN_SMS_ACCESS_SECRET", "")
    sms_sign_name = os.getenv("ALIYUN_SMS_SIGN_NAME", "智能量化")
    sms_template_code = os.getenv("ALIYUN_SMS_TEMPLATE_CODE", "SMS_xxx")


class AliyunMarketConfig:
    host = "http://alirmcom2.market.alicloudapi.com"
    app_code = os.getenv("ALIYUN_APP_CODE", "")


class Config:
    mysql = MySQLConfig()
    tushare = TushareConfig()
    notification = NotificationConfig()
    aliyun_market = AliyunMarketConfig()
    log_level = os.getenv("LOG_LEVEL", "INFO")
    data_dir = ROOT / "data"


config = Config()

# ========== 向后兼容别名（旧代码仍可使用这些模块级名称） ==========

BASE_DIR = ROOT
DATA_DIR = config.data_dir

# MySQL
MYSQL_HOST = config.mysql.host
MYSQL_PORT = config.mysql.port
MYSQL_USER = config.mysql.user
MYSQL_PASSWORD = config.mysql.password
MYSQL_DATABASE = config.mysql.database
MYSQL_SOCKET = config.mysql.socket

# Tushare
TUSHARE_TOKEN = config.tushare.token

# 东方财富实时行情
EASTMONEY_HOST = "http://push2.eastmoney.com"

# 阿里云行情
ALIYUN_HOST = config.aliyun_market.host
ALIYUN_CODE = config.aliyun_market.app_code

# 通知
FEISHU_WEBHOOK = config.notification.feishu_webhook
WECOM_WEBHOOK = config.notification.wecom_webhook
SMTP_HOST = config.notification.smtp_host
SMTP_PORT = config.notification.smtp_port
SMTP_USER = config.notification.smtp_user
SMTP_PASS = config.notification.smtp_pass
ALIYUN_SMS_ACCESS_KEY = config.notification.sms_access_key
ALIYUN_SMS_ACCESS_SECRET = config.notification.sms_access_secret
ALIYUN_SMS_SIGN_NAME = config.notification.sms_sign_name
ALIYUN_SMS_TEMPLATE_CODE = config.notification.sms_template_code

# 数据文件路径
USERS_FILE = DATA_DIR / "users.json"
PENDING_USERS_FILE = DATA_DIR / "pending_users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
ACCESS_LOG_FILE = DATA_DIR / "access_log.json"
POSITIONS_FILE = DATA_DIR / "positions.json"
STOCKS_FILE = DATA_DIR / "stocks.json"
SIGNALS_FILE = DATA_DIR / "signals.json"
TRACK_FILE = DATA_DIR / "track" / "recommendations.json"
RESET_TOKENS_FILE = DATA_DIR / "reset_tokens.json"

# 确保数据目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "track").mkdir(parents=True, exist_ok=True)


def get_db_config(**kwargs):
    """pymysql 连接参数字典（向后兼容旧代码）"""
    return config.mysql.get_connection_params(**kwargs)


class db_connection:
    """pymysql 连接上下文管理器，自动关闭连接。

    用法:
        from quant_app.utils.config import db_connection
        with db_connection() as conn:
            cur = conn.cursor()
            cur.execute(...)
    """

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self.conn = None

    def __enter__(self):
        import pymysql
        self.conn = pymysql.connect(**get_db_config(**self._kwargs))
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()
        return False
