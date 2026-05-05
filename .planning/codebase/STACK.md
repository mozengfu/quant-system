# STACK.md — Tech Stack

## Languages

- **Python** 3.12+ — primary application language (all backend logic, scripts, ML)
- **JavaScript** (ES6) — browser-side frontend logic (in `templates/*.html`, inline `<script>`)
- **HTML** (Jinja2 templates) — server-rendered pages in `templates/`
- **CSS** — custom stylesheets in `static/`
- **Bash** — shell scripts for deployment, sync, and crontab automation (`scripts/auto_scan.sh`, `sync-to-server.sh`, `setup.sh`)
- **SQL** — embedded in Python via pymysql string queries (no ORM)

## Runtime

- **macOS Framework Build** — `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3` (development)
- **Ubuntu/Debian** — system Python 3 via apt (production on Alibaba Cloud ECS)
- **No containerization** — runs directly via `python3 app.py`; no Dockerfile

## Frameworks

- **FastAPI** 0.115.0 — web framework, serves both API endpoints and HTML pages
- **Uvicorn** 0.30.0[standard] — ASGI server, binds `0.0.0.0:5001`
- **Jinja2** 3.1.4 — server-side HTML templating (login, dashboard, strategy pages)
- **Starlette** (FastAPI dependency) — exception handling, middleware, static file mounting

## Key Dependencies

### ML & Data Science

| Package | Version | Purpose |
|---------|---------|---------|
| `lightgbm` | latest | ML model training + inference (gradient boosting) |
| `pandas` | 2.2.2 | DataFrame processing, feature engineering, SQL I/O |
| `numpy` | latest | numerical computing (via pandas implicitly) |
| `scipy` | latest | Spearman rank correlation for model evaluation |
| `joblib` | latest | model serialization (.pkl file save/load) |

### Database

| Package | Version | Purpose |
|---------|---------|---------|
| `pymysql` | 1.1.1 | MySQL client, raw SQL execution (no ORM) |
| `DBUtils` | >=3.1.0 | Connection pooling for MySQL |

### External Data APIs

| Package | Version | Purpose |
|---------|---------|---------|
| `tushare` | 1.4.7 | Chinese A-share market data (daily prices, fundamentals, financial indicators, moneyflow, index data) |
| `python-dotenv` | 1.0.1 | `.env` file loading |

### Security & Auth

| Package | Version | Purpose |
|---------|---------|---------|
| `cryptography` | 42.0.8 | Cryptographic operations |
| `bcrypt` | >=4.0.1 | Password hashing (user auth) |
| `python-multipart` | 0.0.9 | Form data parsing (login forms) |
| `secrets` | stdlib | Session token generation |
| `hashlib` | stdlib | SHA-256 (legacy password format compat) |

### HTTP / Network

| Package | Purpose |
|---------|---------|
| `urllib.request` | stdlib | In-house HTTP calls (market data, webhooks, SMS API) — no `requests` package used |
| `ssl` | stdlib | Custom SSL context (Feishu webhook, AliCloud SMS) |

## Configuration

- **Environment variables** in `quant-system/.env` (loaded by `python-dotenv` via `quant_app/utils/config.py`)
- **JSON data files** in `quant-system/data/` — users, sessions, positions, signals, backtest results, stock pools, feature configs, model monitor history
- **Crontab** at `scripts/quant_crontab` — all scheduled tasks

## Platform Requirements

- **Development**: macOS with Python 3.12+ (Framework build), MySQL server running locally with `quant_db` database
- **Production**: Alibaba Cloud ECS (Ubuntu), Python 3.x, MySQL, SSH key-based sync from dev machine
- **MySQL** 8.x — local socket (`/tmp/mysql.sock`) for dev, TCP for production
- **No containerization**, no CI/CD pipeline, no Docker — deployment is manual `scp` + `pkill` + `nohup` restart
- **No ORM** — all database access through raw pymysql cursors with hand-written SQL
- **No testing framework** — `tests/` directory exists but is empty (no pytest, no unittest usage in the codebase)
