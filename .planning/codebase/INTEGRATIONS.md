# INTEGRATIONS.md — External Integrations

## APIs & External Services

### Tushare Pro (Primary Data Source)

- **Type**: REST API via Python SDK (`tushare==1.4.7`)
- **Purpose**: Daily A-share price data, trade calendar, stock fundamentals (PE/PB/total_mv), financial indicators (yoy_sales), moneyflow (main_net), index data, stock list (`stock_basic`), RPS calculation
- **Authentication**: Token-based (`TUSHARE_TOKEN` in `.env`)
- **Usage Points**:
  - `quant_app/services/market_service.py:get_tushare_pro()` — singleton `pro_api()` instance
  - `scripts/update_daily_price_cron.py` — daily incremental price import (17:00 cron)
  - `scripts/backfill_tushare.py` — historical data backfill (batch), checkpointed via `progress.json`
  - `ml_train_v6.py` — loads daily_price, moneyflow_daily, market_index_daily, fina_indicator tables from MySQL (previously fetched from Tushare)
- **Limits**: Tushare Pro API rate limits; `backfill_tushare.py` implements batch + sleep logic

### Real-time Market Quotes (Three-tier Fallback)

Unified in `quant_app/services/realtime_service.py`. Call chain:

```
memory cache (30s TTL) → Tencent Finance (qt.gtimg.cn) → EastMoney (push2.eastmoney.com) → AliCloud Market (alirmcom2)
```

- **Tencent** (`_try_tencent`): `http://qt.gtimg.cn/q={market}{code}`, GBK encoding, 17-field quote. Primary source.
- **EastMoney** (`_try_eastmoney`): `http://push2.eastmoney.com/api/qt/stock/get`, JSON, 100x scaled integer fields. Fallback #1.
- **AliCloud Market** (`_try_aliyun`): `http://alirmcom2.market.alicloudapi.com/query/com`, APPCODE auth header. Fallback #2. Requires `ALIYUN_APP_CODE` env var.

Additional market data source:
- **Sina Finance** (`_fetch_market_breadth_from_sina`): `http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData` — market breadth (up/down count) for market state detection
- **Sina HQ** (`SINA_HQ_URL`): configured in `quant_app/utils/config.py` but not actively used in fallback chain
- **Sina Mix** (`SINA_MIX_URL`): configured but not actively used

### AliCloud SMS (Notifications)

- **Type**: Direct HTTP GET call to `dysmsapi.aliyuncs.com` (HMAC-SHA1 signed, no SDK)
- **Purpose**: Send SMS alerts for system events
- **Authentication**: `ALIYUN_SMS_ACCESS_KEY` + `ALIYUN_SMS_ACCESS_SECRET` from `.env`
- **Code**: `quant_app/services/notification_service.py:send_sms()`
- **Config**: `ALIYUN_SMS_SIGN_NAME`, `ALIYUN_SMS_TEMPLATE_CODE` in `.env`
- **Note**: Template code currently shows `SMS_xxx` placeholder — may not be functional

### Feishu / Lark Webhook (Primary Alert Channel)

- **Type**: Outbound HTTPS POST (JSON) to bot webhook URL
- **Purpose**: Morning briefing (9:00), stop-loss/take-profit alerts (every 5 min during trading), daily closing report (15:05), system status updates
- **Authentication**: Webhook URL embedded in `.env` (`FEISHU_WEBHOOK`) — no additional auth
- **Code**: `quant_app/services/notification_service.py:send_feishu()`
- **Usage**: `scripts/feishu_alerts.py` (morning/alert/daily commands), `scripts/morning_briefing.py`
- **Message format**: `{"msg_type": "text", "content": {"text": message}}`

### QQ Email SMTP (Notifications)

- **Type**: SMTP over SSL (port 465)
- **Purpose**: Email alerts for system events, user registration notifications
- **Authentication**: SMTP username/password from `.env` (`SMTP_USER`, `SMTP_PASS`)
- **Code**: `quant_app/services/notification_service.py:send_email()`
- **Config**: `SMTP_HOST=smtp.qq.com`, `SMTP_PORT=465` in `.env`

## Data Storage

### MySQL `quant_db`

- **Connection**: Raw `pymysql` — no ORM, no query builder
- **Pooling**: `DBUtils` connection pool (`get_db_config()` in `quant_app/utils/config.py`)
- **Transport**: Unix socket (`/tmp/mysql.sock`) on macOS dev, TCP on production
- **Key Tables** (determined from codebase queries):
  - `daily_price` — daily OHLCV data, MA/RPS columns, ~4000+ stocks
  - `stock_info` — stock metadata (name, industry, exchange)
  - `stock_basic` — Tushare stock listing data
  - `market_index_daily` — index OHLC (000001.SH, 399001.SZ, etc.)
  - `moneyflow_daily` — capital flow data (main_net, buy/sell by order size)
  - `fina_indicator` — financial indicators (yoy_sales, pe_ttm, pb, total_mv)
  - `sim_account`, `sim_trades`, `sim_positions`, `sim_signals` — simulated trading engine state
  - `alpha_signals` — alpha factor signal scores
  - `trade_cal` — Tushare trade calendar
  - `access_log` — API access logging

### JSON File Storage (`quant-system/data/`)

- **File-based persistence** for non-relational state (no database dependency for core config):
  - `users.json` — user accounts (username, hashed password, role)
  - `sessions.json` — active auth sessions
  - `positions.json` — real position tracking
  - `signals.json` — alert/signal state
  - `access_log.json` — request audit log
  - `pending_users.json` — registration queue
  - `reset_tokens.json` — password reset tokens
  - `ml_stock_model_v*.pkl` — LightGBM model bundles (binary, joblib format)
  - `feature_config_v*.json` — ML feature metadata
  - `backtest_*.json` — backtest result snapshots
  - `stock_pool*.json` — cached stock selection results
  - `recommend_cache.json`, `premarket_analysis.json` — API response cache
  - `track/recommendations.json` — recommendation tracking history
- **Write safety**: Atomic JSON writes via temp file + `os.replace()` in `quant_app/utils/persistence.py`

## Auth & Identity

- **Password hashing**: bcrypt via `bcrypt` package (`quant_app/utils/auth.py`). Legacy SHA-256 (`salt$hash`) with transparent upgrade-to-bcrypt on next login.
- **Session tokens**: `secrets.token_hex(32)` — stored in memory (`SESSIONS` dict in `quant_app/routes/auth.py`) and persisted to `data/sessions.json`
- **Auth flow**: Cookie-based (`token=<session_token>`). Login form POST -> verify password -> generate token -> set Cookie. Logout clears token.
- **Registration**: Two-step — user submits POST to `/api/register`, stored in `pending_users.json`; admin approves via `/api/admin/approve_user`
- **User roles**: `admin` / `user` — stored in `data/users.json`, checked in `admin.py` routes
- **No OAuth, no SSO, no LDAP**

## Monitoring

- **Logging**: Standard Python `logging` module — file handlers write to `quant-system/logs/`:
  - `app.log` — main application output (stdout/stderr of uvicorn)
  - `app.error.log` — error-level logs
  - `position_monitor.log` — intraday position scan results
  - `feishu_alerts.log` — Feishu notification delivery logs
  - `daily_import.log` — Tushare daily data import
  - `sim_trading.log` — simulated trading engine activity
  - `morning_briefing.log` — morning briefing output
  - `sync.log` — server sync operations
- **Alerting**: Feishu webhook for human-readable alerts (stop-loss, take-profit, daily summary); QQ email and AliCloud SMS as secondary channels
- **No external monitoring service** (no Datadog, Prometheus, Grafana, Sentry)

## CI/CD

- **None**. No GitHub Actions, GitLab CI, Jenkins, or any CI/CD pipeline.
- **Deployment**: Manual two-step process:
  1. `sync-to-server.sh` — `scp` from dev machine to Alibaba Cloud ECS (8.148.158.153)
  2. SSH `pkill -f "python3 app.py"` + `nohup python3 app.py &` restart
- **Sync**: `check-sync.sh` compares file timestamps to decide sync direction
- **Backup**: `setup.sh` and `deploy_manual.sh` are one-shot deployment scripts for fresh server install

## Environment Configuration

All secrets and runtime configuration via `quant-system/.env`:

| Variable | Purpose | Source |
|----------|---------|--------|
| `TUSHARE_TOKEN` | Tushare Pro API auth | `quant_app/utils/config.py` |
| `MYSQL_HOST` | MySQL host | `quant_app/utils/config.py` |
| `MYSQL_PORT` | MySQL port (3306) | `quant_app/utils/config.py` |
| `MYSQL_USER` | MySQL user (root) | `quant_app/utils/config.py` |
| `MYSQL_PASSWORD` | MySQL password | `quant_app/utils/config.py` |
| `MYSQL_DATABASE` | Database name (quant_db) | `quant_app/utils/config.py` |
| `MYSQL_SOCKET` | Unix socket path | `quant_app/utils/config.py` |
| `ALIYUN_APP_CODE` | AliCloud Market API key | `quant_app/utils/config.py` |
| `SMTP_HOST` | SMTP server (smtp.qq.com) | `quant_app/utils/config.py` |
| `SMTP_PORT` | SMTP port (465) | `quant_app/utils/config.py` |
| `SMTP_USER` | SMTP login (QQ email) | `quant_app/utils/config.py` |
| `SMTP_PASS` | SMTP password/app-code | `quant_app/utils/config.py` |
| `FEISHU_WEBHOOK` | Feishu bot webhook URL | `quant_app/utils/config.py` |
| `ALIYUN_SMS_ACCESS_KEY` | AliCloud SMS access key | `quant_app/utils/config.py` |
| `ALIYUN_SMS_ACCESS_SECRET` | AliCloud SMS secret | `quant_app/utils/config.py` |
| `ALIYUN_SMS_SIGN_NAME` | SMS sign name (default: 智能量化) | `quant_app/utils/config.py` |
| `ALIYUN_SMS_TEMPLATE_CODE` | SMS template code (default: SMS_xxx) | `quant_app/utils/config.py` |
| `CORS_ORIGINS` | CORS allowed origins (default: https://lh.mozengfu.com.cn) | `app_api.py` |

## Webhooks

- **Outbound** (only):
  - Feishu bot webhook (POST JSON to `FEISHU_WEBHOOK` URL) — all alerts go here
  - AliCloud SMS (GET signed request) — secondary alert channel
  - QQ email SMTP — tertiary alert channel
- **Inbound**: None. No external system sends webhooks to this application.
