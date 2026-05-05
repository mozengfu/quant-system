# Concerns

> Identified during codebase mapping 2026-05-05. File paths use backtick format.

---

## 1. Dead/Unused Code

### `app_server.py` -- A 3489-line standalone monolith NOT in the running path

`app_server.py` (141KB, 3489 lines) is a completely standalone FastAPI application that:

- Creates its own `app = FastAPI()` at line 867
- Defines 40+ `@app.get` / `@app.post` routes (auth, analysis, signals, admin, etc.)
- Contains its own `get_stock_realtime()` at line 42 (duplicate of `realtime_service.py`)
- Contains its own `hash_pw()` / `verify_pw()` using **SHA256** (not bcrypt) at lines 946-957
- Contains its own `_load_sessions()` / `_save_sessions()` persistence logic
- Re-implements all business logic inline

The actual entry point (`app.py`) imports `app` from `app_api.py`, which uses `quant_app/routes/*` modules. `app_server.py` is never imported. This is a massive amount of dead code with its own auth system -- confusing at best, a security risk if someone accidentally runs it.

### `app_thin.py` -- Points to a DIFFERENT project

`app_thin.py` imports from `workspace-stock-analyzer/quant_app` (a separate project entirely). It appears to be a leftover wrapper from a previous architecture. Also dead code.

---

## 2. Two Coexisting Authentication Systems

### `app_server.py` uses SHA256

```python
# app_server.py:949
h = hashlib.sha256((salt + password).encode()).hexdigest()
```

### `quant_app/utils/auth.py` uses bcrypt (with SHA256 fallback)

```python
# quant_app/utils/auth.py:12
return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
```

`quant_app/routes/auth.py` imports from `quant_app/utils/auth.py` (bcrypt), and this is the auth system used by the running app (`app_api.py`). But `app_server.py` has its own SHA256-only implementation.

**Risk**: If someone unknowingly runs `app_server.py` and registers users, those password hashes would be SHA256-only, fundamentally weaker than bcrypt. The two auth systems write to the same `data/users.json` file but use incompatible hash formats.

---

## 3. Security

### 3.1 SSL Verification Disabled in 5 Files

HTTPS requests skip certificate validation in multiple locations:

| File | Line(s) | Target |
|------|---------|--------|
| `quant_app/services/notification_service.py` | 95-96 | Feishu webhook |
| `scripts/alicloud_api.py` | 15-16 | AliCloud API |
| `scripts/eastmoney_api.py` | 16-17 | EastMoney API |
| `scripts/backtest_v4_factors.py` | 28-29 | Market data |
| `scripts/backtest_v6_3_fast.py` | 432 | Market data |

This exposes HTTPS traffic to MITM attacks. For Feishu webhook in particular, an attacker could inject malicious notifications.

### 3.2 CORS Wide Open (in `app_server.py`)

```python
# app_server.py:875
app.add_middleware(CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
```

Wildcard origin + credentials is an invalid configuration in browsers (browsers reject it), but it signals insecure intent.

### 3.3 Session Tokens Stored in Plaintext JSON

Session tokens (32-char hex secrets) are persisted unencrypted in `data/sessions.json`. Anyone with filesystem access to the data directory can impersonate any active session.

### 3.4 No Rate Limiting on Auth Endpoints

Login (`/api/auth/login`), registration (`/api/auth/register`), forgot-password, and reset-password endpoints have zero rate limiting. Brute force / credential stuffing is trivially possible.

### 3.5 No CAPTCHA

No CAPTCHA/reCAPTCHA/hCaptcha on any public form (register, login, forgot-password).

### 3.6 `.env` Read Directly by Scripts

Several scripts bypass `os.environ` and read `.env` as a raw file:

- `backtest_buy_conditions.py` (line 14: `open('.env')`)
- `ml_train_bear.py` (line 21: `open('.env')`)
- `backtest_alpha.py` (line 13: `open('.env')`)

This is fragile -- fails if CWD is not the project root.

### 3.7 Sensitive Data in Exception Messages

Several log statements include sensitive data:

```python
# app_server.py:72
logger.warning(f"实时行情失败 {code}: {e}")
```

This pattern exposes internal details (data values, variable state) in logs.

---

## 4. Code Duplication

### 4.1 Two Independent Quote Caches

Both `quant_app/services/realtime_service.py` and `quant_app/services/market_service.py` implement their own `_quote_cache` dictionary with nearly identical logic:

- `realtime_service.py`: `_cache`, `_get_cache()`, `_set_cache()`, TTL 30s
- `market_service.py`: `_quote_cache`, `_get_cached()`, `_set_cache()`, TTL 30s

`market_service.py` imports `get_stock_quote` from `realtime_service.py` (renamed to `get_stock_realtime`), which already handles caching. The local `_quote_cache` in `market_service.py` appears to be dead (no callers or also duplicating the same work).

### 4.2 Three `get_stock_realtime` Implementations

1. `quant_app/services/realtime_service.py::get_stock_quote()` -- canonical (cached, 3-source fallback chain)
2. `app_server.py::get_stock_realtime()` -- standalone, AliCloud-only (no fallback chain, no cache)
3. `quant_app/services/market_service.py::get_stock_realtime` -- just an alias imported from realtime_service

Only one is used by the running app. The duplication in `app_server.py` is dead code (see concern #1).

---

## 5. Monolithic Files and Technical Debt

### 5.1 `app_server.py` -- 3489 lines, 141KB

Despite being dead code, this file represents the worst-case architecture. It contains:

- Route definitions (40+ routes)
- Auth logic (SHA256 hashing, session management)
- Data persistence (read/write JSON files)
- Market data fetching (Tushare, AliCloud)
- Stock analysis (RPS, strategy scoring, backtesting)
- HTML content (inline Jinja2 templates as f-strings, e.g. line 1802)
- Fully embedded pages as Python strings

This is the archetype of the "big ball of mud" pattern that the modular `quant_app/` structure was meant to replace.

### 5.2 `quant_app/services/strategy_service.py` -- 1949 lines, 80KB

The largest actively-used file. Contains all strategy logic (C3.0 V3, V4 combo, technical scanning, bottom breakout, MA pullback) with heavy nesting and indentation in branches like `analyze_stock()`.

### 5.3 `quant_app/routes/strategy.py` -- 1921 lines, 75KB

The largest route file. Most routes have minimal business logic (they delegate to services), but several inline large SQL queries and processing loops.

### 5.4 `quant_app/services/market_service.py` -- 776 lines, 30KB

Grew beyond its original purpose. Contains market data access, RPS calculation, position sync, AND technical buy/sell signal generation. Could be split.

---

## 6. Error Handling Issues

### 6.1 Bare `except:` Clauses

Found in `app_server.py` at lines 323, 956, 1191, 1265, 1277, 1341, 2500. Bare `except:` catches even `KeyboardInterrupt` and `SystemExit`, making the server hard to kill and potentially masking fatal errors.

### 6.2 Broad `except Exception` Throughout

Virtually every `try` block uses `except Exception as e:` with `logger.warning()`. This swallows:

- Programming errors (NameError, TypeError, AttributeError)
- Memory errors
- Minor recoverable failures that should be distinguishable from critical ones

### 6.3 NameError as Control Flow in `strategy_service.py`

```python
# strategy_service.py:232-241 (~10 lines)
try: ma5
except NameError: ma5 = ma10 = ma20 = price; ma_trend = "震荡"; rps = 50
try: rps
except NameError: rps = 50
try: ma_trend
except NameError: ma_trend = "震荡"
```

Using `NameError` to check if a variable was assigned inside previous `try` blocks is fragile and unusual. If any variable name is misspelled, it silently defaults rather than failing with a clear error.

### 6.4 Mixed Return Types

Many functions return either a dict or `None` (or `False`), requiring callers to check return values. Inconsistent -- some return empty dicts, some return `None`, some return string `"error"`. Example:

```python
# app_server.py get_stock_realtime: returns dict or None
# app_server.py get_recent_trade_dates: returns list or None
```

---

## 7. ML Model Proliferation

### 7.1 Five Versions of `ml_train_v6` Created in 2 Days

| File | Size | Date |
|------|------|------|
| `ml_train_v6.py` | 37KB | May 4 |
| `ml_train_v6_2.py` | 43KB | May 4 |
| `ml_train_v6_3.py` | 49KB | May 4 |
| `ml_train_v6_4.py` | 54KB | May 4 |
| `ml_train_v6_5.py` | 52KB | May 4 |

These are separate files with progressively larger code sizes. With no consolidation or cleanup, it is unclear which version is current or how they differ.

### 7.2 12 ML Model `.pkl` Files Accumulated

```
data/ml_stock_model.pkl
data/ml_stock_model_v3.pkl
data/ml_stock_model_v4_bull.pkl
data/ml_stock_model_v4_bear.pkl
data/ml_stock_model_v4_sideways.pkl
data/ml_stock_model_v6.pkl
data/ml_stock_model_v6_2.pkl
data/ml_stock_model_v6_3.pkl
data/ml_stock_model_v6_4.pkl
data/ml_stock_model_v6_5.pkl
data/ml_stock_model_ridge.pkl
data/ml_bear_model.pkl
```

Each model is 3-15MB. Without version tracking, it is impossible to know which model corresponds to which training script without manual inspection.

---

## 8. Data File Accumulation

88 files in `data/` totaling 49MB. Notable:

- 21+ backtest result JSON files (comparisons, versioned results, parameter searches)
- `access_log.json` at 168KB -- file-based access log that is also synced to MySQL (dual persistence)
- 70 JSON files total

The `.gitignore` excludes `data/*.json`, but these files consume disk space on the server.

---

## 9. Missing Test Coverage

The `tests/` directory contains only an empty `__init__.py`. Zero unit tests exist for:

- Auth logic (hash/verify, session management)
- Strategy scoring (the core business logic)
- ML training pipeline
- Market data fallback chain
- Route handlers
- Technical indicator calculations

Any refactoring or change requires manual verification in production.

---

## 10. Concurrency and Async Issues

### 10.1 Sync I/O in Async Routes

`app_server.py` (and the production `quant_app/routes/*.py`) use `async def` routes but the handler functions call synchronous I/O directly: `urlopen`, `pymysql` queries, pandas operations, file I/O. Only `run_in_executor` is used in the positions endpoint (line 1999). All other synchronous calls block the FastAPI event loop, consuming worker threads and reducing throughput under load.

### 10.2 Concurrent JSON File Writes

Threading locks exist in `quant_app/utils/persistence.py`, but `app_server.py` has its own `_save_sessions()` without locks, risking corrupted `sessions.json` under concurrent requests.

---

## 11. Missing Standard Security Headers

No evidence of:

- Content-Security-Policy header
- X-Content-Type-Options
- X-Frame-Options
- Strict-Transport-Security

The global exception handler in `app_api.py` returns `status_code=500` without security headers.

---

## 12. Shell Scripts in `scripts/`

Several `.sh` files (`auto_scan.sh`, `auto_refresh_data.sh`) appear to be automation wrappers. Their error handling relies on exit codes from Python scripts, with no retry or alerting logic.

---

## 13. `quant_app/utils/indicators.py` vs `quant_app/services/technical_service.py`

Two modules providing technical indicator calculations with overlapping functionality:

- `quant_app/utils/indicators.py` -- Pure Python indicators (EMA, MACD, KDJ, BOLL, ATR)
- `quant_app/services/technical_service.py` -- Also exports `calculate_macd`, `calculate_kdj`, `calculate_bollinger_bands`, `calculate_atr`, plus `calculate_rsi`

Both are imported by different callers. Some files import from `indicators`, others from `technical_service`. There is a risk of inconsistent implementations if both are updated separately.

---

## Summary of Critical Concerns

| Priority | Concern | Impact |
|----------|---------|--------|
| P0 | `app_server.py` is a dead-code monolith with its own SHA256 auth | Security, confusion, maintenance burden |
| P0 | Zero unit tests | Every change is a risk |
| P1 | No rate limiting on auth endpoints | Brute force vulnerability |
| P1 | SSL verification disabled in 5 locations | MITM on API calls |
| P1 | No CAPTCHA on forms | Automated abuse |
| P2 | NameError-as-control-flow in `strategy_service.py` | Silent logic errors |
| P2 | Two quote cache implementations | Wasteful, potentially stale data |
| P2 | ML model proliferation (12 .pkl files) | Maintenance, disk waste |
| P2 | Sync I/O in async routes | Performance degradation under load |
| P3 | Bare `except:` clauses | Hard to kill, masked errors |
| P3 | Mixed return types from functions | Bug-prone callers |
| P3 | 88 data files at 49MB | Server disk usage |
