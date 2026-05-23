# -*- coding: utf-8 -*-
"""
FastAPI 路由层 - 从 app_core 导入核心函数
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Request as FastAPIRequest, Cookie, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from quant_app.utils.config import get_db_config

# 基础路径和日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# 导入核心业务函数
from app_core import (
    # 通知服务
    send_sms, send_email, send_feishu,
    # 股票数据
    get_stock_realtime, get_tushare_pro, get_recent_trade_dates,
    get_recent_trade_dates_fallback, get_latest_rps_from_db, calculate_rps,
    sync_positions, add_to_positions,
    # 分析和策略
    analyze_stock, get_block_stocks, score_stock_c30,
    detect_macd_crossover, scan_daily_pool, strategy_scan,
    # 技术指标
    calculate_ema, calculate_macd, calculate_kdj, calculate_bollinger_bands, calculate_atr,
    # 深度技术选股
    scan_daily_pool_technical, scan_concept_trend, get_hot_concepts,
    # 买卖点
    get_stock_history_from_db, get_technical_buy_sell_signals,
    # 回测
    backtest_stock_enhanced, backtest_stock, backtest_stock_v4,
    get_client_ip, _classify_module, _write_log_mysql, save_access_log,
    # 追踪
    load_track_data, save_track_data, record_recommendation, update_stock_results,
    # 信号
    get_signals_path, read_signals, write_signals,
    # 板块常量
    ALL_BLOCKS,
    generate_order_id,
)

# ========== 路由模块导入 ==========
from quant_app.routes.pages import router as pages_router
from quant_app.routes.auth import router as auth_router
from quant_app.routes.admin import router as admin_router
from quant_app.routes.strategy import router as strategy_router
from quant_app.routes.signals import router as signals_router
from quant_app.routes.scanning import router as scanning_router
from quant_app.routes.recommend import router as recommend_router
from quant_app.routes.market import router as market_router
from quant_app.routes.dashboard import router as dashboard_router

# ========== FastAPI 应用 ==========
app = FastAPI(title="智能量化系统", version="2.0.0")
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'https://lh.mozengfu.com.cn').split(',')
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True, allow_methods=["GET","POST","PUT","DELETE"], allow_headers=["*"])

# 全局异常处理：防止错误详情泄露给客户端
from starlette.responses import JSONResponse as StarletteJSONResponse
@app.exception_handler(Exception)
async def global_exception_handler(request: FastAPIRequest, exc: Exception):
    logger.error(f"未处理异常: {exc}", exc_info=True)
    return StarletteJSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试"},
    )

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# 注入 DATA_DIR 到 app_core（app_core 中的策略函数需要）
import app_core
app_core.DATA_DIR = str(DATA_DIR)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")))

# ========== 注册子路由 ==========
app.include_router(pages_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(strategy_router)
app.include_router(signals_router)
app.include_router(scanning_router)
app.include_router(recommend_router)
app.include_router(market_router)
app.include_router(dashboard_router)

ACCESS_LOG_FILE = DATA_DIR / "access_log.json"
USERS_FILE = DATA_DIR / "users.json"
PENDING_USERS_FILE = DATA_DIR / "pending_users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
TRACK_FILE = DATA_DIR / "track" / "recommendations.json"
RESET_TOKENS_FILE = DATA_DIR / "reset_tokens.json"

# 确保数据目录存在
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "track").mkdir(parents=True, exist_ok=True)

# ========== 启动时自动迁移日志 ==========
@app.on_event("startup")
async def startup_auto_import_logs():
    """系统启动时自动从 access_log.json 导入历史数据到 MySQL"""
    try:
        if not ACCESS_LOG_FILE.exists():
            logger.info("启动检查：无 access_log.json 文件，跳过迁移")
            return
        
        logs = json.loads(ACCESS_LOG_FILE.read_text())
        if not logs:
            logger.info("启动检查：access_log.json 为空，跳过迁移")
            return
        
        import pymysql
        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM system_logs")
        existing = cursor.fetchone()[0]
        
        if existing > 0:
            cursor.execute("SELECT MAX(timestamp) FROM system_logs")
            last_imported = cursor.fetchone()[0]
            if last_imported:
                new_logs = [l for l in logs if l.get("timestamp", "") > str(last_imported)]
            else:
                new_logs = logs
        else:
            new_logs = logs
        
        if not new_logs:
            logger.info(f"启动检查：无新日志需要导入（已有{existing}条）")
            conn.close()
            return
        
        imported = 0
        for log in new_logs:
            try:
                module = _classify_module(log.get("action", ""))
                cursor.execute(
                    "INSERT INTO system_logs (username, ip, action, module, timestamp) VALUES (%s, %s, %s, %s, %s)",
                    (log.get("username", "unknown"), log.get("ip", "unknown"),
                     log.get("action", "unknown"), module, log.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                )
                imported += 1
            except Exception as _e:
                logger.error(f"Error in app_api.py: {_e}")
        
        conn.commit()
        conn.close()
        logger.info(f"启动自动导入完成：{imported} 条日志已迁移到 MySQL（总计{existing + imported}条）")
    except Exception as e:
        logger.warning(f"启动自动导入失败: {e}")

