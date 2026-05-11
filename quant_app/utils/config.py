"""
配置模块 - 集中管理配置和常量
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ========== 应用基础路径 ==========
BASE_DIR = Path(__file__).parent.parent.parent  # quant-system/
DATA_DIR = BASE_DIR / "data"

# ========== 阿里云实时行情 ==========
ALIYUN_HOST = "http://alirmcom2.market.alicloudapi.com"
ALIYUN_CODE = os.environ.get('ALIYUN_APP_CODE', '')

# ========== 行情 API 地址 ==========
TENCENT_QUOTE_URL = "http://qt.gtimg.cn/q="
EASTMONEY_QUOTE_URL = "http://push2.eastmoney.com"
SINA_HQ_URL = "http://hq.sinajs.cn/list="
SINA_MIX_URL = "https://feed.mix.sina.com.cn/api/roll/get"

# ========== 飞书推送配置 ==========
FEISHU_WEBHOOK = os.environ.get('FEISHU_WEBHOOK', '')

# ========== 企业微信推送配置 ==========
WECOM_WEBHOOK = os.environ.get('WECOM_WEBHOOK', '')

# ========== QQ邮箱SMTP配置 ==========
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.qq.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')

# ========== 阿里云短信配置 ==========
ALIYUN_SMS_ACCESS_KEY = os.environ.get('ALIYUN_SMS_ACCESS_KEY', '')
ALIYUN_SMS_ACCESS_SECRET = os.environ.get('ALIYUN_SMS_ACCESS_SECRET', '')
ALIYUN_SMS_SIGN_NAME = os.environ.get('ALIYUN_SMS_SIGN_NAME', '智能量化')
ALIYUN_SMS_TEMPLATE_CODE = os.environ.get('ALIYUN_SMS_TEMPLATE_CODE', 'SMS_xxx')

# ========== Tushare 配置 ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# ========== MySQL 数据库配置 ==========
MYSQL_HOST = os.environ.get('MYSQL_HOST', '127.0.0.1')
MYSQL_PORT = int(os.environ.get('MYSQL_PORT', '3306'))
MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '')
MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'quant_db')
MYSQL_SOCKET = os.environ.get('MYSQL_SOCKET', '/tmp/mysql.sock')

def get_db_config(**kwargs):
    """获取 MySQL 连接配置字典

    根据环境变量自动选择 socket 或 TCP 连接。
    可通过 kwargs 覆盖或扩展配置（如 autocommit=True）。

    返回的 dict 可直接用于 pymysql.connect(**config)。
    """
    config = {
        'user': MYSQL_USER,
        'password': MYSQL_PASSWORD,
        'database': MYSQL_DATABASE,
        'charset': 'utf8mb4',
        'connect_timeout': 5,
    }
    if MYSQL_SOCKET:
        config['unix_socket'] = MYSQL_SOCKET
    else:
        config['host'] = MYSQL_HOST
        config['port'] = MYSQL_PORT
    config.update(kwargs)
    return config


# ========== 数据文件路径 ==========
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
