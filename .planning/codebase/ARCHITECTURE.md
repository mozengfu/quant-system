# ARCHITECTURE.md — System Architecture

## Pattern Overview

This is a **traditional server-rendered web application** with a **monolithic backend** and **standalone scheduled scripts**. It is NOT a microservices or event-driven architecture. The system follows a classic three-tier web pattern (presentation -> business logic -> data), overlaid with a batch-oriented ML pipeline and a cron-driven automation layer.

There is no dependency injection, no service registry, no message queue, and no containerization. The architecture is pragmatic and straightforward: a single FastAPI process serves all HTTP traffic, a set of standalone Python scripts handle offline computation, and JSON files plus MySQL provide persistence.

Two code structures coexist within the same process:

- **Legacy monolithic** (`app_core.py` ~97 lines of imports + `app_api.py` ~163 lines) — these files are thin re-export layers. The real bulk was in `app_server.py` (3489 lines, the original monolithic core); `app_core.py` currently acts as a facade that imports everything from `quant_app/` services and re-exports them so that `app_api.py` and route modules can access them without breaking import chains.
- **Refactored modular** (`quant_app/` package, ~8700 lines across 16 files) — services broken into separate modules by domain (market data, strategy, backtest, realtime quotes, technical indicators, notifications).

## Layers

```
+------------------------------------------------------------------+
|                        PRESENTATION LAYER                         |
|  Jinja2 HTML templates (templates/)                              |
|  Static assets (static/)                                         |
|  Browser-side JS (inline <script> in .html files)                |
+------------------------------------------------------------------+
        |  FastAPI route handlers return HTML or JSON
        v
+------------------------------------------------------------------+
|                     API / ROUTE LAYER                              |
|  app_api.py          -- FastAPI app creation, middleware, routes   |
|  quant_app/routes/   -- 6 route modules (pages, auth, admin,      |
|                          strategy, market, dashboard)             |
+------------------------------------------------------------------+
        |  Routes import business functions from app_core
        v
+------------------------------------------------------------------+
|                     BUSINESS LOGIC LAYER                           |
|  app_core.py         -- Re-export facade (imports from quant_app) |
|  quant_app/services/ -- 6 service modules                         |
|    market_service.py     -- Market data, RPS, trade dates         |
|    realtime_service.py   -- Real-time quote fallback chain        |
|    strategy_service.py   -- Stock scoring, scanning, analysis     |
|    backtest_service.py   -- Historical backtesting                |
|    technical_service.py  -- Technical indicators (thin wrapper)   |
|    notification_service.py -- SMS, Email, Feishu alerts           |
|  Standalone modules:                                              |
|    market_state.py         -- Market regime classification         |
|    sector_rotation.py      -- Sector rotation analysis             |
|    alpha_filter.py         -- Alpha signal filtering               |
+------------------------------------------------------------------+
        |  Services read/write data
        v
+------------------------------------------------------------------+
|                       DATA LAYER                                   |
|  MySQL (quant_db)   -- All structured data                        |
|     daily_price, stock_info, moneyflow_daily, fina_indicator,     |
|     sim_account/sim_trades/sim_positions, alpha_signals, ...      |
|  JSON files (data/) -- Non-relational state                       |
|     users.json, sessions.json, positions.json, signals.json,      |
|     backtest results, stock pools, ML model .pkl files            |
|  ML Model files (data/) -- LightGBM .pkl bundles                  |
|     ml_stock_model_v6*.pkl, feature_config_v*.json                |
+------------------------------------------------------------------+
|                                                                   |
|  CRON / SCRIPT LAYER (separate processes, same codebase)          |
|  scripts/ -- 50+ standalone Python scripts                        |
|     feishu_alerts.py     -- Pre-market / intraday / post-market   |
|     sim_trading.py       -- Simulated trading engine              |
|     morning_briefing.py  -- Daily briefing generation             |
|     update_daily_price_cron.py -- Tushare -> MySQL daily import   |
|     backfill_tushare.py  -- Historical data backfill              |
|  Top-level ML scripts:                                            |
|     ml_predict.py        -- ML inference (LightGBM prediction)    |
|     ml_train_v6*.py      -- Model training (v6 series)            |
+------------------------------------------------------------------+
```

## Data Flow

### Main request flow (HTTP API)

```
Browser / curl
    |
    v
FastAPI (uvicorn on 0.0.0.0:5001)
    |
    v
Route handler (quant_app/routes/*.py or app_api.py inline routes)
    |
    v
Business function (from app_core -> quant_app/services/*)
    |
    +---> MySQL (pymysql raw queries)  ---> Tushare Pro API
    |                                          |
    |    OR                                    v
    |                                      MySQL (daily_price)
    |
    +---> Real-time quote chain:
            In-memory cache (30s TTL)
            -> Tencent Finance (qt.gtimg.cn, 3s timeout)
            -> EastMoney (push2.eastmoney.com, 3s timeout)
            -> AliCloud Market (alirmcom2, 3s timeout)
    |
    +---> JSON file read/write (data/*.json)
    |
    v
JSON response (API) or Jinja2-rendered HTML (page)
```

### ML pipeline flow

```
Tushare Pro API (historical data)
    |
    v
MySQL (daily_price, moneyflow_daily, fina_indicator, market_index_daily)
    |
    v
ml_train_v6.py (feature engineering -> LightGBM training -> evaluation)
    |
    v
data/ml_stock_model_v6*.pkl  +  data/feature_config_v*.json
    |
    v
ml_predict.py (load model -> build features -> predict -> rank)
    |
    +---> Used by strategy_service.py (ML score blending)
    +---> Used by scripts/sim_trading.py (trade signal generation)
    +---> Used by scripts/daily_ml_predict.py (batch prediction)
```

### Quote fallback chain (in `quant_app/services/realtime_service.py`)

```
get_stock_quote(code, market)
    |
    v
_cached = _get_cache(key, 30)  -- return if fresh
    |
    | miss
    v
_try_tencent(code)  -- qt.gtimg.cn, 3s timeout, GBK decode
    | fail
    v
_try_eastmoney(code) -- push2.eastmoney.com, 3s timeout, JSON
    | fail
    v
_try_aliyun(code)   -- alirmcom2.market.alicloudapi.com, 3s timeout
    | fail
    v
return None (all sources exhausted)
```

### Stock selection pipeline (V4 combo strategy)

```
MySQL daily_price (latest trade date)
    |
    v
SQL filtering: price > 5, pct_chg > 1%, turnover > 5%, volume_ratio > 1.2
    |
    v
Technical screening: MA5 > MA10 > MA20 (upward alignment)
    |
    v
Mainforce scoring (scripts/mainforce_scoring.py): capital flow analysis
    |
    v
ML score blending (ml_predict.py): 3-day return probability
    |
    v
Enhanced score = rule_score * ML_probability  -> ranked output
    |
    v
Top 5 candidates -> Feishu push (feishu_alerts.py morning)
```

## Key Abstractions

### app_core.py — the facade

`app_core.py` is a thin (~97 lines) re-export module. Its entire purpose is to provide a single import target for the legacy route code. It imports all business functions from `quant_app/services/` and `quant_app/utils/` and makes them available as `from app_core import analyze_stock, strategy_scan, ...`.

New code should import directly from `quant_app/` modules, not from `app_core`. The facade exists only for backward compatibility with routes that were written before the modular refactor.

### quant_app.utils.config — central configuration

All environment variables, file paths, and constants are loaded once in `quant_app/utils/config.py`. Every other module imports from here rather than reading `os.environ` directly or duplicating path constants. The `get_db_config()` function provides the MySQL connection dict, selecting between Unix socket (local dev) and TCP (production).

### quant_app.utils.persistence — JSON file I/O

All JSON file reads and writes are centralized in `persistence.py` with thread-safe atomic writes (temp file + `os.replace()`). This prevents file corruption from concurrent request handlers.

### quant_app.utils.model_loader — ML model caching

Model loading is centralized in `model_loader.py` with `@lru_cache` decorator, ensuring each model version is loaded from disk only once. `ml_predict.py` adds its own threading layer on top for thread safety.

### market_state.py — market regime adapter

`market_state.py` reads index trend + breadth + volatility + volume data, classifies the market into one of five states, and returns a parameter dict (`stop_loss_pct`, `take_profit_pct`, `max_positions`, `ml_threshold`). Other modules (strategy_service, sim_trading, feishu_alerts) call `get_market_state()` to adapt their behavior to current conditions.

## Scheduled Task Architecture

All scheduled tasks run as **separate Python processes** launched by the system crontab, configured in `scripts/quant_crontab`. They share the same codebase and database as the web app but run independently.

```
Crontab schedule (trading days only):
 09:00     feishu_alerts.py morning       -- pre-market push
 09:30-14:30  every 30min  auto_refresh_data.sh -- data refresh
 09:30-15:00  every 5min   feishu_alerts.py alert + position_monitor.py
 15:05     feishu_alerts.py daily         -- closing report
 17:00     update_daily_price_cron.py     -- Tushare daily data import
 17:30     sim_trading.py scan            -- end-of-day simulated trading
 17:45     run_three_strategies.py        -- V4 combo strategy scan
```

Each script is self-contained: it sets its own `sys.path`, imports its dependencies directly, and writes to its own log file under `logs/`.

## Error Handling Strategy

- **HTTP layer**: Global exception handler in `app_api.py` catches all unhandled exceptions and returns `{"detail": "服务器内部错误，请稍后重试"}` with status 500. Error details are logged but never leaked to the client.
- **Service layer**: Functions wrap external calls in try/except with `logger.warning()` and return `None` or empty structures on failure. No exceptions propagate from data source failures into the HTTP layer.
- **JSON persistence**: Atomic writes prevent file corruption. Thread locks prevent concurrent write races.
- **Quote fallback**: Three-tier retry with 3s timeout each, in-memory cache reduces external call frequency.
- **Network calls**: `_retry_urlopen()` implements exponential backoff (3 retries, 0.5s/1s/1.5s delays).
- **No circuit breakers, no health checks, no structured error codes**.

## Concurrency Model

- **Single process**: Uvicorn runs a single worker (no `--workers` flag). This avoids Python GIL contention and keeps the in-memory caches (quote cache, model cache, session cache) simple.
- **Thread-safe caches**: Quote caches use `threading.Lock`, model loading uses `threading.Lock`, JSON writes use `threading.RLock`. Session state in `auth.py` uses `threading.Lock`.
- **No async I/O in business logic**: All database and network calls in services are synchronous (`pymysql`, `urllib.request`). FastAPI route handlers are `async def` wrappers around sync calls — there is no actual async/await deeper than the route layer.

## Deprecated / Dead Code

- `app_server.py` (3489 lines) — the original monolithic core, no longer imported by anything. Likely the ancestor of the current modular code.
- `app.py.fixed`, `app.py.full` (235KB, 306KB) — backup/copy files, not actively used.
- `archive/` directory — 17 archived scripts (old ML training versions, backtest backups, research scripts). Kept for reference but not imported.
- `index.html` (60KB, top-level) — appears to be a standalone HTML file, not part of the FastAPI template system.
- `static.backup_20260427_220128/` — backup of static assets.
