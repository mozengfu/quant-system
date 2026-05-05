# STRUCTURE.md — Directory Layout & Conventions

## Top-Level Layout

```
quant-system/
├── app.py                  # Entry point — starts uvicorn on 0.0.0.0:5001
├── app_api.py              # FastAPI app creation, middleware, route registration
├── app_core.py             # Re-export facade (imports from quant_app/ and re-exports)
├── app_server.py           # DEPRECATED — original monolithic core (3489 lines, unused)
├── app_thin.py             # Lightweight variant (23KB)
│
├── quant_app/              # Refactored modular package (~8700 lines)
│
├── scripts/                # Cron jobs, backtesting, data imports (~50 files)
├── templates/              # Jinja2 HTML templates (10 files)
├── static/                 # Static assets (CSS, icons, images)
├── data/                   # JSON state files, ML model .pkl files, feature configs
├── logs/                   # Runtime logs (app, import, monitoring)
│
├── archive/                # Deprecated scripts (old ML versions, strategy backups)
├── tests/                  # Empty (no test framework in use)
├── docs/                   # Documentation (small, 1-2 files)
│
├── ml_predict.py           # ML inference module (LightGBM prediction)
├── ml_train_v6*.py         # ML training scripts (v6/v6.2/v6.3/v6.4/v6.5)
├── market_state.py         # Market regime detection
├── sector_rotation.py      # Sector rotation analysis
├── alpha_filter.py         # Alpha signal filtering
├── alpha_signal_integration.py  # Alpha signal integration
├── ai_sim_trading.py       # AI-powered simulated trading
├── backtest_*.py           # Top-level backtest scripts
│
├── .env                    # Environment variables (NOT committed to git)
├── requirements.txt        # Python package dependencies
├── CLAUDE.md               # Project instructions for Claude Code
│
├── .planning/              # Architecture planning docs
│   └── codebase/
│       ├── STACK.md
│       ├── INTEGRATIONS.md
│       ├── ARCHITECTURE.md
│       └── STRUCTURE.md
│
├── data/                   # See detailed section below
├── logs/                   # See detailed section below
└── archive/                # Deprecated/backup scripts
```

## Directory Purposes

### `quant_app/` — Refactored Package (8700 lines)

The modular replacement for the original monolithic design. Broken into sub-packages by concern.

```
quant_app/
├── __init__.py              # Package marker
├── main.py                  # Re-exports for external consumers
│
├── utils/
│   ├── __init__.py
│   ├── config.py            # Central configuration (env vars, paths, DB config)
│   ├── auth.py              # Password hashing (bcrypt), session token generation
│   ├── persistence.py       # JSON file I/O (thread-safe, atomic writes)
│   ├── indicators.py        # Full-sequence technical indicators (EMA, MACD, KDJ, BOLL, ATR)
│   └── model_loader.py      # ML model loading with LRU cache
│
├── services/
│   ├── __init__.py
│   ├── realtime_service.py  # Real-time quote fallback chain (cache -> Tencent -> EastMoney -> AliCloud)
│   ├── market_service.py    # Market data (Tushare wrapper, trade dates, RPS calculation, history)
│   ├── strategy_service.py  # Stock scanning, scoring, analysis (C3.0 V3, V4 combo)
│   ├── backtest_service.py  # Historical backtesting (single stock)
│   ├── technical_service.py # Thin wrapper: delegates to indicators.py, returns last value
│   └── notification_service.py  # Multi-channel alerts (SMS, Email, Feishu)
│
├── routes/
│   ├── __init__.py
│   ├── pages.py             # Page routes (GET /login, /register, /admin, /market, etc.)
│   ├── auth.py              # Authentication routes (login, logout, register, reset password)
│   ├── admin.py             # Admin routes (access logs, user management)
│   ├── strategy.py          # Strategy routes (scan, analyze, buy/sell signals)
│   ├── market.py            # Market data routes (premarket, indices, sector rotation)
│   └── dashboard.py         # Dashboard routes (positions, backtest, tracking)
│
├── models/                  # Placeholder for future data models
│   └── __init__.py
│
└── data/                    # Placeholder for future data files
    └── track/
```

### `scripts/` — Automation Scripts (~50 files)

All scripts follow a basic pattern: set `sys.path`, import dependencies from `quant_app/` or `app_core`, and execute a `main()`-style function. They are designed to be run from crontab, not imported.

Key scripts by category:

**Notifications & Alerts:**
- `feishu_alerts.py` — Pre-market (9:00), intraday alert (every 5min), closing report (15:05)
- `morning_briefing.py` — 6:30 daily briefing generation

**Trading & Monitoring:**
- `sim_trading.py` — Simulated trading engine (MySQL-backed, ML-driven)
- `position_monitor.py` — Intraday position monitoring (stop-loss/take-profit checks)
- `add_position.py` / `sell_position.py` — Position management actions

**Data Import & Sync:**
- `update_daily_price_cron.py` — Daily Tushare -> MySQL incremental import
- `backfill_tushare.py` — Historical data batch backfill (checkpointed)
- `sync_fina_indicator.py` — Financial indicator sync
- `sync_mainforce_data.py` — Mainforce capital flow data sync
- `sync_akshare.py` — AKShare data sync (alternative data source)

**Backtesting:**
- `backtest_combo_v4.py` through `backtest_combo_v6_params.py` — Strategy backtest evolution
- `backtest_v41_vs_ml.py`, `backtest_v5_ml_real.py`, etc. — ML integration backtests
- `backtest_v65_*.py` — V6.5 series backtesting (comparison, optimization, portfolio)
- `backtest_fine_tune.py` / `backtest_param_scan.py` — Parameter optimization

**Analysis:**
- `mainforce_scoring.py` — Mainforce capital flow scoring (used by V4 combo strategy)
- `analyze_v4_factors_detail.py` — Detailed factor analysis
- `check_ml_style.py` / `ml_deep_analysis.py` — ML model analysis

**Utilities:**
- `calc_technical.py` — Technical indicator calculations
- `alicloud_api.py` — AliCloud market data API wrapper
- `eastmoney_api.py` — EastMoney data API wrapper
- `auto_refresh_data.sh` / `auto_scan.sh` — Shell script wrappers
- `quant_crontab` — Crontab configuration file (not a script)

### `templates/` — Jinja2 HTML (10 files)

Server-rendered HTML pages. Each file is a complete HTML document (not Jinja2 partials or blocks — the templates contain full `<html><head><body>` structure). Inline JavaScript handles all client-side interactivity via `fetch()` calls to FastAPI endpoints.

Key templates:
- `login.html` / `register.html` — Auth pages
- `index.html` (124KB) — Main dashboard (largest, most complex)
- `admin.html` — Admin panel
- `landing.html` — Landing page
- `market_analysis.html` — Market analysis page
- `ml_top15.html` — ML top 15 display
- `strategy_v41.html` — V4.1 strategy page
- `log_analytics.html` — Log analytics
- `market_analysis.html` — Market data view

### `data/` — JSON & Model Storage

Three categories of files coexist:

**Runtime State (JSON, read/write by app):**
- `users.json`, `pending_users.json` — User accounts
- `sessions.json` — Active auth sessions
- `positions.json` — Position tracking
- `signals.json` — Alert/signal state
- `access_log.json` — Request audit log (also writing to MySQL)
- `reset_tokens.json` — Password reset tokens
- `track/recommendations.json` — Recommendation tracking history

**Cached Results (JSON, periodically refreshed):**
- `stock_pool.json`, `stock_pool_bottom.json`, `stock_pool_strong.json` — Stock selection cache
- `premarket_analysis.json` — Pre-market analysis cache
- `recommend_cache.json` — Recommendation cache
- `concept_trend.json` — Concept trend data
- `sector_trend.json` — Sector rotation data

**Backtest Results (JSON, write-once):**
- `backtest_combo_v*.json` — Strategy backtest results
- `backtest_v65_*.json` — V6.5 series results
- `backtest_comparison.json`, `backtest_combo_comparison.json` — Cross-version comparisons

**ML Artifacts:**
- `ml_stock_model_v*.pkl` — LightGBM model bundles (joblib format, 1-15 MB each)
- `ml_stock_model_ridge.pkl` — Ridge regression model
- `ml_bear_model.pkl` — Bear market model
- `feature_config_v*.json` — Feature metadata for each model version
- `ml_preds_v6_3.parquet` / `ml_preds_v6_3_latest.parquet` — Prediction outputs (Parquet format)
- `model_monitor_history.json` — Model performance tracking

**User Data:**
- `holdings_mozengfu.json`, `trades_mozengfu.json` — User position data
- `admins.json` — Admin list

### `logs/` — Application Logs

- `app.log` / `app.error.log` — Main web app output
- `sync.log` — Server sync operations (largest, ~199KB)
- `server.log` — Server-side logs
- `feishu_alerts.log`, `morning_briefing.log` — Notification logs
- `position_monitor.log` — Intraday monitoring output
- `ai_sim.log` — AI simulated trading log
- `sim_trading.log` — Simulated trading engine log (in scripts/data/)
- `cron_daily.log` — Daily cron output (in scripts/data/)

### `archive/` — Deprecated Scripts (17 files)

Contains old ML training versions (v1-v5), abandoned backtest experiments, and strategy backups. Not imported by any active code. Kept for reference.

## Key File Locations

| Purpose | File Path |
|---------|-----------|
| Entry point | `app.py` |
| FastAPI app | `app_api.py` |
| Facade/re-export | `app_core.py` |
| Config | `quant_app/utils/config.py` |
| Auth | `quant_app/utils/auth.py` |
| Persistence | `quant_app/utils/persistence.py` |
| Technical indicators | `quant_app/utils/indicators.py` |
| Model loader | `quant_app/utils/model_loader.py` |
| Realtime quotes | `quant_app/services/realtime_service.py` |
| Market data | `quant_app/services/market_service.py` |
| Strategy/scanning | `quant_app/services/strategy_service.py` |
| Backtesting | `quant_app/services/backtest_service.py` |
| Notifications | `quant_app/services/notification_service.py` |
| Page routes | `quant_app/routes/pages.py` |
| Auth routes | `quant_app/routes/auth.py` |
| Admin routes | `quant_app/routes/admin.py` |
| Strategy routes | `quant_app/routes/strategy.py` |
| Market routes | `quant_app/routes/market.py` |
| Dashboard routes | `quant_app/routes/dashboard.py` |
| Market state | `market_state.py` |
| ML inference | `ml_predict.py` |
| ML training | `ml_train_v6*.py` (v6/v6.2/v6.3/v6.4/v6.5) |
| Crontab config | `scripts/quant_crontab` |
| Environment | `.env` (not committed) |

## Naming Conventions

- **Files**: `snake_case.py` for all Python files. Script names are descriptive: `update_daily_price_cron.py`, `backtest_combo_v4.py`.
- **Classes/Functions**: `snake_case` for functions and methods (`get_stock_realtime`, `strategy_scan`). Classes are rare in this codebase (only FastAPI route handlers use them implicitly via APIRouter).
- **Variables**: `snake_case` for Python, Chinese variable names in inline JavaScript (`名称`, `代码`, `现价`) for API responses that map directly to display fields.
- **Route paths**: `/api/resource/action` pattern (e.g., `/api/analysis/sz/000001`, `/api/combo_scan`).
- **JSON keys**: Chinese mixed with English (`"代码"`, `"名称"`, `"ts_code"`, `"close"`, `"ml概率"`).
- **Model versions**: `v6`, `v6.2`, `v6.3`, `v6.4`, `v6.5` — incremented via training script evolution. Each has a corresponding `feature_config_v*.json` and `ml_stock_model_v*.pkl`.
- **Database tables**: `snake_case` (`daily_price`, `stock_info`, `moneyflow_daily`).

## Code Style Observations

- **No type hints**: The codebase does not use Python type annotations in any module.
- **No dataclasses or Pydantic models**: Data is passed as plain dicts and lists. No request/response schemas.
- **Global mutable state**: Caches (`_quote_cache`, `_state_cache`, `_last_scan_results`) are module-level dicts with threading locks.
- **Import style**: Most modules use `from x import y` rather than `import x`. The import chain is deep: `routes -> app_core -> quant_app.services.* -> quant_app.utils.*`.
- **No ORM, no query builder**: All SQL queries are hand-written strings passed to pymysql cursors.

## Where to Add New Code

### New API endpoint

1. Add the route handler in the appropriate `quant_app/routes/*.py` file (strategy, market, dashboard, admin, or auth).
2. If the route needs business logic, add a function in the appropriate `quant_app/services/*.py` file.
3. Add the import to `app_core.py` if the function needs to be accessible from the facade (needed if other route modules import it via `app_core`).
4. Add the HTML template in `templates/` if serving a page.

### New ML model version

1. Create a new training script (e.g., `ml_train_v6_6.py`) based on the latest existing version.
2. Train outputs go to `data/ml_stock_model_v6_6.pkl` and `data/feature_config_v6_6.json`.
3. Register the model path in `quant_app/utils/model_loader.py`.
4. Update `ml_predict.py` to support the new version (add loading code in `_load_model()`).

### New scheduled job

1. Create the script in `scripts/`, following the existing pattern (sys.path setup, logging, DB config import).
2. Add the crontab entry to `scripts/quant_crontab`.
3. The script should be self-contained and import from `quant_app/` for shared services.

### New utility function

- Technical indicators: add to `quant_app/utils/indicators.py` (full sequence version).
- Database or file operations: add to `quant_app/utils/persistence.py` or the relevant service.
- Configuration: add env var + constant to `quant_app/utils/config.py`.

## Architecture Constraints to Be Aware Of

1. **No hot-reload in production**: `app.py` starts uvicorn with `reload=False`. Changes require process restart.
2. **No database migrations**: Schema changes are applied manually via SQL. No Alembic or migration tool.
3. **No async throughout stack**: FastAPI routes are `async def` but all business logic is synchronous. Concurrent requests are handled by uvicorn's thread pool, not asyncio.
4. **Single user assumption**: The auth system supports multiple users, but the UI and strategy assume a single primary user (mozengfu).
5. **Memory-bound ML models**: All model versions are loaded into memory at prediction time. ~54 MB for the latest v6.5 model.
6. **All JSON file I/O is single-threaded**: The `_write_lock` RLock in `persistence.py` serializes all writes, which is fine for the request volume but would bottleneck under heavy load.
