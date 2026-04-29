# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

智能量化系统 v2.0 — A Chinese A-share quantitative trading system with ML-enhanced stock screening, real-time monitoring, simulated trading, and multi-channel alerting.

**Tech Stack**: Python 3.12+, FastAPI, MySQL (pymysql), LightGBM, pandas, Tushare Pro, Jinja2

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Start web server (FastAPI on port 5001)
python3 app.py

# ML model training (latest version)
python3 ml_train_v6.py

# Run all three strategies + print TOP5
python3 run_three_strategies.py

# Market state detection
python3 market_state.py

# Simulated trading scan
python3 scripts/sim_trading.py scan

# Morning briefing (Feishu push)
python3 scripts/morning_briefing.py

# Feishu alerts (morning/alert/daily)
python3 scripts/feishu_alerts.py morning
python3 scripts/feishu_alerts.py daily

# Data backfill from Tushare to MySQL
python3 scripts/backfill_tushare.py
python3 scripts/update_daily_price_cron.py

# Alpha signal integration
python3 alpha_signal_integration.py
python3 alpha_filter.py

# Debug/analysis scripts
python3 check_features.py          # ML feature distribution
python3 check_label_dist.py        # Label distribution
python3 debug_prediction.py        # Debug prediction output
python3 backtest_alpha.py          # Backtest alpha strategy
```

## Architecture

```
quant-system/
├── app.py                 # Entry point — starts uvicorn on 0.0.0.0:5001
├── app_api.py             # FastAPI routes (~190KB, defines all HTTP endpoints)
├── app_core.py            # Core business logic (~170KB, monolithic legacy module)
├── app_thin.py            # Lightweight variant
├── quant_app/             # Refactored modular package
│   ├── utils/
│   │   ├── config.py      # Configuration (reads .env, path constants)
│   │   └── auth.py        # bcrypt/sha256 auth, session token generation
│   ├── services/
│   │   ├── stock_data_service.py   # Tushare Pro + AliCloud realtime API
│   │   ├── technical_service.py    # MA, MACD, KDJ, BOLL, ATR calculations
│   │   └── notification_service.py # Feishu, QQ email, AliCloud SMS
│   └── main.py            # Package exports
├── scripts/               # Cron jobs and automation
│   ├── quant_crontab      # Crontab configuration (all schedule tasks)
│   ├── sim_trading.py     # Simulated trading engine (MySQL-backed)
│   ├── feishu_alerts.py   # Feishu notification bot (pre-market, intraday, post-market)
│   ├── morning_briefing.py # 6:30 daily briefing
│   ├── position_monitor.py# Intraday position monitoring
│   ├── auto_scan.sh       # Automated stock scan via API
│   ├── auto_refresh_data.sh # Refresh stock pool data
│   ├── update_daily_price_cron.py # Daily Tushare → MySQL import
│   ├── backfill_tushare.py # Historical data backfill
│   ├── backtest_combo_v*.py  # Backtesting scripts (v1-v6)
│   └── mainforce_scoring.py  # Main force capital flow scoring
├── ml_predict.py          # ML inference module (~64KB, LightGBM prediction)
├── ml_predict_v3.py       # V3 ML inference variant
├── ml_train.py            # Base ML training script
├── ml_train_v6.py         # Latest ML training (v6, ~31KB)
├── ml_train_bear.py       # Bear market specialized model
├── market_state.py        # Market regime detection (trend_up/down/range/panic/overheated)
├── sector_rotation.py     # Sector rotation analysis
├── ai_sim_trading.py      # AI-powered simulated trading (LLM-based)
├── alpha_filter.py        # Alpha signal filtering
├── alpha_signal_integration.py # Alpha signal integration
├── backfill_alpha_history.py   # Historical alpha backfill
├── templates/             # Jinja2 HTML templates (login, dashboard, strategy, etc.)
├── static/                # Static assets (CSS, icons, manifest)
├── data/                  # JSON data files (users, positions, backtest results, configs)
└── logs/                  # Application logs (error, monitor, sync, etc.)
```

## Key Architecture Patterns

### Two code structures coexist
- **Legacy monolithic**: `app_core.py` + `app_api.py` — the main app imports nearly everything from `app_core`
- **Refactored modular**: `quant_app/` package with separated utils/services/routes — gradually replacing monolithic code
- Both coexist; `app_api.py` imports from `app_core` directly, while `quant_app/` is used by some scripts

### Data flow
```
Tushare Pro API ──→ MySQL (daily_price / market_index_daily / stock_basic tables)
AliCloud/EastMoney ──→ Direct HTTP call → JSON response (realtime quotes)
                        ↓
          app_core.py / app_api.py (business logic + strategies)
                        ↓
         JSON files (data/*.json) + MySQL + Feishu/SMS/Email notifications
```

### Stock selection pipeline
1. **Rule-based filtering** — C3.0 V3 scoring system via `strategy_scan()`
2. **ML scoring** — LightGBM model predicts 3-day rise probability (22 features)
3. **Alpha integration** — Additional factor-based signals
4. **Enhanced score** = Rule score × ML probability → ranked output

### Market state machine
`market_state.py` reads index trend + breadth + volatility + volume → classifies as `trend_up / trend_down / range / panic / overheated` → adjusts strategy parameters (stop-loss, take-profit, max positions, ML threshold)

### Scheduled automation (crontab)
- Weekdays 9:00 — Pre-market candidate push (Feishu)
- Weekdays 9:30-15:00 (every 5 min) — Stop-loss/take-profit monitoring
- Weekdays 15:05 — Closing report
- Weekdays 17:00 — Daily price data import
- Weekdays 17:30 — Sim trading scan

## Environment
- `.env` file required with `TUSHARE_TOKEN`, `MYSQL_*`, `ALIYUN_APP_CODE`, `FEISHU_WEBHOOK`, `SMTP_*`, `ALIYUN_SMS_*`
- MySQL must be running locally with `quant_db` database
- Python 3.12+ recommended (macOS Framework build at `/Library/Frameworks/Python.framework/Versions/3.12/`)
