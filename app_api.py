"""
FastAPI 路由层 - 从 app_core 导入核心函数
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from quant_app.utils.config import get_db_config

# 基础路径和日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# 导入核心业务函数
from quant_app.routes.admin import router as admin_router
from quant_app.routes.auth import router as auth_router
from quant_app.routes.backtest import router as backtest_router
from quant_app.routes.dashboard import router as dashboard_router
from quant_app.routes.market import router as market_router

# ========== 路由模块导入 ==========
from quant_app.routes.pages import router as pages_router
from quant_app.routes.pipeline import router as pipeline_router
from quant_app.routes.pnl import router as pnl_router
from quant_app.routes.recommend import router as recommend_router
from quant_app.routes.scanning import router as scanning_router
from quant_app.routes.signals import router as signals_router
from quant_app.routes.strategy import router as strategy_router
from quant_app.routes.trading import router as trading_router
from quant_app.services.realtime_scanner import scan_stocks
from quant_app.utils.persistence import (
    _classify_module,
)

# ========== FastAPI 应用 ==========
app = FastAPI(title="智能量化系统", version="2.0.0")
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'https://lh.mozengfu.com.cn').split(',')
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_credentials=True, allow_methods=["GET","POST","PUT","DELETE"], allow_headers=["*"])

# CSRF 保护中间件（验证 POST/PUT/DELETE 请求携带自定义头）
from fastapi import Request
from starlette.responses import JSONResponse as StarletteJSONResponse


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE"):
        path = request.url.path
        # 登录/注册/登出 跳过 CSRF 检查（cookie 未就绪或无需防护）
        if path not in ("/api/auth/login", "/api/auth/register", "/api/auth/forgot-password", "/api/auth/reset-password", "/logout") and not path.startswith("/api/trading/"):
            csrf_header = request.headers.get("x-csrf-protection")
            if csrf_header != "1":
                logger.warning("CSRF 验证失败: method=%s path=%s", request.method, path)
                return JSONResponse(status_code=403, content={"error": "CSRF 验证失败，请刷新页面重试"})
    return await call_next(request)

# 全局异常处理：防止错误详情泄露给客户端
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
# 注意：data/ 目录已不再通过静态路由暴露（安全隐患：含 access_log.json 等敏感文件）
# 如需访问数据文件，通过受控的 API 路由代理

# ========== 注册子路由 ==========
app.include_router(pages_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(strategy_router)
app.include_router(signals_router)
app.include_router(scanning_router)
app.include_router(recommend_router)
app.include_router(backtest_router)
app.include_router(market_router)
app.include_router(dashboard_router)
app.include_router(trading_router)
app.include_router(pipeline_router)
app.include_router(pnl_router)

# ========== 前端 SPA (Vue 3) 静态文件挂载 ==========
_frontend_dist = BASE_DIR / "frontend" / "dist"
if _frontend_dist.is_dir():
    # 挂载静态资源路径（JS/CSS/图片）
    app.mount("/assets", StaticFiles(directory=str(_frontend_dist / "assets")), name="frontend_assets")
    logger.info("Vue SPA 静态资源已挂载: /assets (from %s)", _frontend_dist)
    
    # SPA 入口页路由（带 JWT 校验）
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi import Cookie as Fcookie
    
    @app.get("/", response_class=HTMLResponse)
    @app.get("/app", response_class=HTMLResponse)
    @app.get("/app/{path:path}", response_class=HTMLResponse)
    async def serve_spa(token: str = Fcookie(None), path: str = ""):
        # 如果请求 API 路径，跳过（由其他路由处理）
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        # 检查 JWT 登录状态
        from quant_app.routes.auth import SESSIONS
        if not token or token not in SESSIONS:
            return RedirectResponse(url="/login")
        html_content = (_frontend_dist / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(content=html_content)

# ========== WebSocket 事件总线 ==========
class PipelineEventBus:
    """简单的广播式事件总线，用于 WebSocket 推送管线状态变更"""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: dict):
        for q in self._subscribers:
            try:
                await q.put(event)
            except Exception:
                pass


pipeline_event_bus = PipelineEventBus()


@app.websocket("/api/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    q = pipeline_event_bus.subscribe()
    try:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
                await websocket.send_json(event)
            except TimeoutError:
                # 心跳保活（继续循环，不断连）
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        pipeline_event_bus.unsubscribe(q)

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
        from pymysql.err import OperationalError, IntegrityError

        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        conn.begin()

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
            logger.info("启动检查：无新日志需要导入（已有%s条）", existing)
            conn.rollback()
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
            except (OperationalError, IntegrityError) as _e:
                logger.error("日志迁移插入失败（事务回滚）: %s", _e)
                conn.rollback()
                break
            except Exception as _e:
                logger.error("日志迁移预期外异常: %s", _e)

        conn.commit()
        conn.close()
        logger.info("启动自动导入完成：%d 条日志已迁移到 MySQL（总计%d条）", imported, existing + imported)
    except Exception as e:
        logger.warning("启动自动导入失败: %s", e)

    # 后台预加载 ML 模型，避免首次请求冷启动 3-5s
    try:
        import threading
        def _warmup():
            try:
                from ml_predict import _load_best_model
                bundle, ver = _load_best_model()
                logger.info("ML模型预热完成: %s (%.0fMB)", ver, bundle.get("model_size_mb", 0) if isinstance(bundle, dict) else 0)
            except Exception as e:
                logger.warning("ML模型预热失败（不影响正常使用）: %s", e)
        threading.Thread(target=_warmup, daemon=True).start()
    except Exception:
        pass

    # 启动 market_state.json 文件监听，状态变更时推送到 WebSocket
    _market_file = DATA_DIR / "market_state.json"
    _market_last_mtime = _market_file.stat().st_mtime if _market_file.exists() else 0

    async def _watch_market_state():
        nonlocal _market_last_mtime
        while True:
            await asyncio.sleep(5)
            try:
                if _market_file.exists():
                    mtime = _market_file.stat().st_mtime
                    if mtime > _market_last_mtime:
                        _market_last_mtime = mtime
                        data = json.loads(_market_file.read_text(encoding="utf-8"))
                        await pipeline_event_bus.publish({
                            "type": "market_update",
                            **data,
                        })
            except Exception:
                pass

    asyncio.create_task(_watch_market_state())

    # 回填 sim_signals 中未导入的已执行交易到 qmt_trades
    try:
        from quant_app.trading.trade_recorder import backfill_from_signals
        count = backfill_from_signals()
        if count:
            logger.info("已回填 %d 条历史实盘交易到 qmt_trades", count)
    except Exception as e:
        logger.warning("回填历史交易失败: %s", e)

# ─── 实时选股扫描 ───
@app.get("/api/scanner/signals")
async def get_scanner_signals():
    """实时多因子选股扫描"""
    result = scan_stocks()
    return JSONResponse(result)

@app.get("/api/scanner/buy")
async def get_scanner_buy():
    """获取买入候选（score>=55）"""
    result = scan_stocks()
    buys = [s for s in result.get("signals", []) if s["level"] in ("STRONG_BUY", "BUY")]
    result["signals"] = buys
    result["total_scanned"] = len(buys)
    return JSONResponse(result)


