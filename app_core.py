#!/usr/bin/env python3
"""
智能量化系统 v2.0 - Tushare数据源
"""
import os, sys, json, time, logging, uuid
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from datetime import datetime, timedelta
from pathlib import Path

# 加载环境变量
from dotenv import load_dotenv
load_dotenv()

from quant_app.utils.config import get_db_config
from quant_app.services.notification_service import send_sms, send_email, send_feishu

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


# ========== 东方财富实时行情 ==========
EASTMONEY_HOST = "http://push2.eastmoney.com"


# ========== 行情数据（从 market_service 导入） ==========
from quant_app.services.market_service import (
    _code_to_secid, _try_tencent, get_stock_realtime,
    get_tushare_pro, get_recent_trade_dates, get_recent_trade_dates_fallback,
    get_latest_rps_from_db, calculate_rps,
    sync_positions, add_to_positions,
    get_stock_history_from_db, get_technical_buy_sell_signals,
)

# ========== Tushare 配置 ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# ========== 策略选股（从 strategy_service 导入） ==========
from quant_app.services.strategy_service import (
    analyze_stock, get_block_stocks, score_stock_c30,
    detect_macd_crossover,
    scan_daily_pool, strategy_scan,
    scan_daily_pool_technical, scan_concept_trend, get_hot_concepts,
    scan_daily_pool_bottom_breakout, scan_daily_pool_ma_pullback,
)

# ========== 回测（从 backtest_service 导入） ==========
from quant_app.services.backtest_service import (
    backtest_stock_enhanced, backtest_stock, backtest_stock_v4,
)

# ========== 技术指标工具函数（纯Python实现，无需talib） ==========

from quant_app.utils.indicators import calculate_ema, calculate_macd, calculate_kdj, calculate_bollinger_bands, calculate_atr


# ========== 板块常量 ==========
ALL_BLOCKS = [
    "半导体", "IT设备", "互联网", "软件服务", "通信设备", "元器件",
    "中成药", "化学制药", "生物制药", "医疗保健", "医药商业", "农药化肥",
    "白酒", "啤酒", "红黄酒", "食品", "软饮料", "纺织", "酒店餐饮", "家居用品", "家用电器",
    "汽车整车", "汽车配件", "汽车服务", "专用机械", "工程机械", "电气设备", "机床制造",
    "全国地产", "区域地产", "房产服务", "建筑工程", "装修装饰", "其他建材",
    "银行", "证券", "保险", "多元金融",
    "化工原料", "化纤", "化工机械", "小金属", "广告包装", "影视音像"
]

# ========== 订单生成 ==========
def generate_order_id():
    return datetime.now().strftime("%Y%m%d%H%M%S") + str(uuid.uuid4())[:4]



# ========== 认证和会话管理（供 API 层使用） ==========
from quant_app.utils.persistence import (
    load_users, save_users, _load_sessions, _save_sessions,
    get_client_ip, _classify_module, _write_log_mysql, save_access_log,
    load_track_data, save_track_data, record_recommendation, update_stock_results,
    load_pending_users, save_pending_users,
    save_reset_tokens, load_reset_tokens,
    get_signals_path, read_signals, write_signals,
    get_positions_data,
)

def get_current_user(token: str = None):
    if not token:
        return None
    # 使用 routes.auth 的内存 SESSIONS（延迟 import 避免循环引用）
    from quant_app.routes.auth import SESSIONS
    if token in SESSIONS:
        return SESSIONS[token].get("username")
    return None

def require_auth(token: str = None):
    user = get_current_user(token)
    if not user:
        raise Exception("未登录")
    return user
