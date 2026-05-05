# 代码库问题清单

> 2026-05-05 代码库映射时识别。文件路径使用反引号格式。

---

## 1. 死代码/未使用代码

### `app_server.py` —— 一个 3489 行的独立单体，不在运行路径中

`app_server.py`（141KB，3489 行）是一个完全独立的 FastAPI 应用，它：

- 在第 867 行创建了自己的 `app = FastAPI()`
- 定义了 40+ 个 `@app.get` / `@app.post` 路由（认证、分析、信号、管理等）
- 在第 42 行包含自己的 `get_stock_realtime()`（与 `realtime_service.py` 重复）
- 在第 946-957 行包含使用 **SHA256**（非 bcrypt）的自己的 `hash_pw()` / `verify_pw()`
- 包含自己的 `_load_sessions()` / `_save_sessions()` 持久化逻辑
- 内联重新实现了所有业务逻辑

实际入口点（`app.py`）从 `app_api.py` 导入 `app`，后者使用 `quant_app/routes/*` 模块。`app_server.py` 从未被导入。这是大量的死代码，带有自己的认证系统 —— 往好里说是困惑，往坏里说如果有人意外运行它就是一个安全风险。

### `app_thin.py` —— 指向一个不同的项目

`app_thin.py` 从 `workspace-stock-analyzer/quant_app`（一个完全独立项目）导入。看起来是之前架构的遗留包装器。也是死代码。

---

## 2. 两套并存的认证系统

### `app_server.py` 使用 SHA256

```python
# app_server.py:949
h = hashlib.sha256((salt + password).encode()).hexdigest()
```

### `quant_app/utils/auth.py` 使用 bcrypt（带 SHA256 回退）

```python
# quant_app/utils/auth.py:12
return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
```

`quant_app/routes/auth.py` 从 `quant_app/utils/auth.py`（bcrypt）导入，这是运行中的应用（`app_api.py`）使用的认证系统。但 `app_server.py` 有自己的纯 SHA256 实现。

**风险**：如果有人不知情地运行 `app_server.py` 并注册用户，那些密码哈希将是纯 SHA256，从根本上弱于 bcrypt。两套认证系统写入同一个 `data/users.json` 文件但使用不兼容的哈希格式。

---

## 3. 安全问题

### 3.1 SSL 验证在 5 个文件中被禁用

HTTPS 请求在多个位置跳过了证书验证：

| 文件 | 行号 | 目标 |
|------|---------|--------|
| `quant_app/services/notification_service.py` | 95-96 | 飞书 webhook |
| `scripts/alicloud_api.py` | 15-16 | 阿里云 API |
| `scripts/eastmoney_api.py` | 16-17 | 东方财富 API |
| `scripts/backtest_v4_factors.py` | 28-29 | 行情数据 |
| `scripts/backtest_v6_3_fast.py` | 432 | 行情数据 |

这使 HTTPS 流量暴露于 MITM 攻击。特别是飞书 webhook，攻击者可注入恶意通知。

### 3.2 CORS 全开（在 `app_server.py` 中）

```python
# app_server.py:875
app.add_middleware(CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
```

通配符来源 + 凭据在浏览器中是无效配置（浏览器会拒绝），但它表明了不安全的意图。

### 3.3 会话令牌以明文 JSON 存储

会话令牌（32 字符十六进制密钥）未加密地持久化在 `data/sessions.json` 中。任何对 data 目录有文件系统访问权限的人都可以冒充任何活跃会话。

### 3.4 认证端点无速率限制

登录（`/api/auth/login`）、注册（`/api/auth/register`）、忘记密码和重置密码端点完全没有速率限制。暴力破解/撞库攻击轻而易举。

### 3.5 无验证码

任何公共表单（注册、登录、忘记密码）上都没有 CAPTCHA/reCAPTCHA/hCaptcha。

### 3.6 脚本直接读取 `.env`

几个脚本绕过 `os.environ`，将 `.env` 作为原始文件读取：

- `backtest_buy_conditions.py`（第 14 行：`open('.env')`）
- `ml_train_bear.py`（第 21 行：`open('.env')`）
- `backtest_alpha.py`（第 13 行：`open('.env')`）

这很脆弱 —— 如果 CWD 不是项目根目录就会失败。

### 3.7 异常消息中包含敏感数据

几条日志语句包含敏感数据：

```python
# app_server.py:72
logger.warning(f"实时行情失败 {code}: {e}")
```

这种模式将内部细节（数据值、变量状态）暴露在日志中。

---

## 4. 代码重复

### 4.1 两个独立的行情缓存

`quant_app/services/realtime_service.py` 和 `quant_app/services/market_service.py` 都实现了自己的 `_quote_cache` 字典，逻辑几乎相同：

- `realtime_service.py`：`_cache`、`_get_cache()`、`_set_cache()`，TTL 30s
- `market_service.py`：`_quote_cache`、`_get_cached()`、`_set_cache()`，TTL 30s

`market_service.py` 从 `realtime_service.py` 导入 `get_stock_quote`（重命名为 `get_stock_realtime`），这已经处理了缓存。`market_service.py` 中的本地 `_quote_cache` 看起来是死代码（没有调用者或也在重复相同的工作）。

### 4.2 三个 `get_stock_realtime` 实现

1. `quant_app/services/realtime_service.py::get_stock_quote()` —— 规范实现（缓存，3 源降级链路）
2. `app_server.py::get_stock_realtime()` —— 独立实现，仅阿里云（无降级链路，无缓存）
3. `quant_app/services/market_service.py::get_stock_realtime` —— 只是从 realtime_service 导入的别名

只有一个被运行中的应用使用。`app_server.py` 中的重复是死代码（见问题 #1）。

---

## 5. 单体文件和技债

### 5.1 `app_server.py` —— 3489 行，141KB

尽管是死代码，这个文件代表了最差的架构。它包含：
- 路由定义（40+ 个路由）
- 认证逻辑（SHA256 哈希，会话管理）
- 数据持久化（读写 JSON 文件）
- 行情数据获取（Tushare、阿里云）
- 股票分析（RPS、策略评分、回测）
- HTML 内容（内联 Jinja2 模板作为 f-string，如第 1802 行）
- 完全嵌入的页面作为 Python 字符串

这是模块化 `quant_app/` 结构本应取代的"大泥球"模式的原型。

### 5.2 `quant_app/services/strategy_service.py` —— 1949 行，80KB

最大的活跃使用文件。包含所有策略逻辑（C3.0 V3、V4 组合、技术扫描、底部突破、MA 回调），在 `analyze_stock()` 等分支中存在大量嵌套和缩进。

### 5.3 `quant_app/routes/strategy.py` —— 1921 行，75KB

最大的路由文件。大多数路由有最小化的业务逻辑（它们委托给服务），但有几个内联了大量 SQL 查询和处理循环。

### 5.4 `quant_app/services/market_service.py` —— 776 行，30KB

已增长超出其原始目的。包含行情数据访问、RPS 计算、持仓同步 AND 技术买卖信号生成。可以拆分。

---

## 6. 错误处理问题

### 6.1 裸 `except:` 子句

在 `app_server.py` 的第 323、956、1191、1265、1277、1341、2500 行发现。裸 `except:` 甚至会捕获 `KeyboardInterrupt` 和 `SystemExit`，使服务器难以杀死，并可能掩盖致命错误。

### 6.2 整个代码库中宽泛的 `except Exception`

几乎所有 `try` 块都使用 `except Exception as e:` 加 `logger.warning()`。这会吞噬：
- 编程错误（NameError、TypeError、AttributeError）
- 内存错误
- 本应可区分的轻微可恢复故障与关键故障

### 6.3 `strategy_service.py` 中将 NameError 用作控制流

```python
# strategy_service.py:232-241（约 10 行）
try: ma5
except NameError: ma5 = ma10 = ma20 = price; ma_trend = "震荡"; rps = 50
try: rps
except NameError: rps = 50
try: ma_trend
except NameError: ma_trend = "震荡"
```

使用 `NameError` 检查变量是否在前面的 `try` 块中被赋值是脆弱且不寻常的。如果有任何变量名拼写错误，它会静默地使用默认值而不是以清晰错误失败。

### 6.4 混合返回类型

许多函数返回字典或 `None`（或 `False`），需要调用者检查返回值。不一致 —— 有些返回空字典，有些返回 `None`，有些返回字符串 `"error"`。示例：

```python
# app_server.py get_stock_realtime：返回 dict 或 None
# app_server.py get_recent_trade_dates：返回 list 或 None
```

---

## 7. ML 模型泛滥

### 7.1 2 天内创建了 5 个版本的 `ml_train_v6`

| 文件 | 大小 | 日期 |
|------|------|------|
| `ml_train_v6.py` | 37KB | 5月4日 |
| `ml_train_v6_2.py` | 43KB | 5月4日 |
| `ml_train_v6_3.py` | 49KB | 5月4日 |
| `ml_train_v6_4.py` | 54KB | 5月4日 |
| `ml_train_v6_5.py` | 52KB | 5月4日 |

这些是独立的文件，代码量逐渐增大。没有整合或清理，不清楚哪个版本是当前的，也不清楚它们之间的差异。

### 7.2 累积了 12 个 ML 模型 `.pkl` 文件

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

每个模型 3-15MB。没有版本跟踪，不手动检查就无法知道哪个模型对应哪个训练脚本。

---

## 8. 数据文件积累

`data/` 中有 88 个文件，总计 49MB。值得注意的：
- 21+ 个回测结果 JSON 文件（对比、版本化结果、参数搜索）
- `access_log.json` 达 168KB —— 基于文件的访问日志，同时也同步到 MySQL（双重持久化）
- 共 70 个 JSON 文件

`.gitignore` 排除了 `data/*.json`，但这些文件消耗服务器磁盘空间。

---

## 9. 缺少测试覆盖

`tests/` 目录只包含一个空的 `__init__.py`。以下内容完全没有单元测试：
- 认证逻辑（哈希/验证、会话管理）
- 策略评分（核心业务逻辑）
- ML 训练流水线
- 行情数据降级链路
- 路由处理器
- 技术指标计算

任何重构或变更都需要在生产环境中手动验证。

---

## 10. 并发和异步问题

### 10.1 异步路由中的同步 I/O

`app_server.py`（和生产环境的 `quant_app/routes/*.py`）使用 `async def` 路由，但处理器函数直接调用同步 I/O：`urlopen`、`pymysql` 查询、pandas 操作、文件 I/O。仅持仓端点在 `run_in_executor` 中执行（第 1999 行）。所有其他同步调用阻塞 FastAPI 事件循环，消耗工作线程并降低负载下的吞吐量。

### 10.2 并发的 JSON 文件写入

`quant_app/utils/persistence.py` 中存在线程锁，但 `app_server.py` 有自己的不带锁的 `_save_sessions()`，在并发请求下有损坏 `sessions.json` 的风险。

---

## 11. 缺少安全响应头

没有发现以下内容：
- Content-Security-Policy 头
- X-Content-Type-Options
- X-Frame-Options
- Strict-Transport-Security

`app_api.py` 中的全局异常处理器返回 `status_code=500` 时不带安全头。

---

## 12. `scripts/` 中的 Shell 脚本

几个 `.sh` 文件（`auto_scan.sh`、`auto_refresh_data.sh`）似乎是自动化包装器。它们的错误处理依赖 Python 脚本的退出码，没有重试或告警逻辑。

---

## 13. `quant_app/utils/indicators.py` 与 `quant_app/services/technical_service.py`

两个模块提供功能重叠的技术指标计算：
- `quant_app/utils/indicators.py` —— 纯 Python 指标（EMA、MACD、KDJ、BOLL、ATR）
- `quant_app/services/technical_service.py` —— 也导出 `calculate_macd`、`calculate_kdj`、`calculate_bollinger_bands`、`calculate_atr`，外加 `calculate_rsi`

两者被不同的调用者导入。如果两者分别更新，存在实现不一致的风险。

---

## 关键问题总结

| 优先级 | 问题 | 影响 |
|----------|---------|--------|
| P0 | `app_server.py` 是带有自己 SHA256 认证的死代码单体 | 安全、混淆、维护负担 |
| P0 | 零单元测试 | 每次变更有风险 |
| P1 | 认证端点无速率限制 | 暴力破解漏洞 |
| P1 | SSL 验证在 5 个位置禁用 | API 调用的 MITM 风险 |
| P1 | 表单上无验证码 | 自动化滥用 |
| P2 | `strategy_service.py` 中将 NameError 用作控制流 | 静默逻辑错误 |
| P2 | 两个行情缓存实现 | 浪费，可能的数据陈旧 |
| P2 | ML 模型泛滥（12 个 .pkl 文件） | 维护、磁盘浪费 |
| P2 | 异步路由中的同步 I/O | 负载下性能下降 |
| P3 | 裸 `except:` 子句 | 难以杀死，掩盖错误 |
| P3 | 函数混合返回类型 | 调用者容易出错 |
| P3 | 88 个数据文件共 49MB | 服务器磁盘使用 |
