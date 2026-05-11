"""
quant_app - 量化系统主包

重构后的模块化结构:
- quant_app.utils.config: 配置管理
- quant_app.utils.auth: 认证和授权
- quant_app.services.stock_data_service: 股票数据服务
- quant_app.services.notification_service: 通知服务
- quant_app.services.technical_service: 技术指标计算
- quant_app.routes: API路由
"""

from quant_app.utils.config import (
    BASE_DIR, DATA_DIR,
    ALIYUN_HOST, ALIYUN_CODE,
    FEISHU_WEBHOOK,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS,
    ALIYUN_SMS_ACCESS_KEY, ALIYUN_SMS_ACCESS_SECRET,
    TUSHARE_TOKEN,
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
)

from quant_app.utils.auth import hash_pw, verify_pw, make_token
from quant_app.services.notification_service import send_sms, send_email, send_feishu
__all__ = [
    # config
    'BASE_DIR', 'DATA_DIR', 'ALIYUN_HOST', 'ALIYUN_CODE', 'FEISHU_WEBHOOK',
    'SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASS',
    'ALIYUN_SMS_ACCESS_KEY', 'ALIYUN_SMS_ACCESS_SECRET',
    'TUSHARE_TOKEN', 'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD',
    'MYSQL_DATABASE',
    # auth
    'hash_pw', 'verify_pw', 'make_token',
    # services
    'send_sms', 'send_email', 'send_feishu',
]
