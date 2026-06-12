# Code Review Report — quant-system

> 审核日期：2026-05-30 | 审核范围：全仓库 ~30K 行 Python + JS
> 审核人：Claude Code

---

## 🚨 Critical Issues (Fix Immediately)

### 1. 硬编码数据库凭据 (3 处)

**Severity**: Critical — 源码中的明文 root 密码

**文件**:
- `run_backtest_v4_pool.py:25-26`
- `run_backtest_v4_pool_filter.py:25-26`
- `scripts/daily_scan_report.py:7`

**代码**:
```python
# run_backtest_v4_pool.py
DB_CONFIG = {'user':'root','password':'root123','host':'127.0.0.1','port':3306,'database':'quant_db'}
ENGINE = create_engine('mysql+pymysql://root:root123@127.0.0.1:3306/quant_db?charset=utf8mb4', ...)
```

**影响**: 任何有仓库访问权限的人可直接连接生产数据库。这些是独立脚本（非 FastAPI），但风险相同。

**修复**: 改用 `from quant_app.utils.config import get_db_config` 或读取 `.env`。

---

### 2. XSS 漏洞 — log_analytics.html 无转义

**Severity**: Critical — 存储型 XSS，攻击者可注入任意 JS

**文件**: `templates/log_analytics.html:338-343`

**代码**:
```javascript
let html = '<table>...';
for (const log of logs) {
    html += `<tr><td>${log.username || '-'}</td><td>${log.ip || '-'}</td><td>${log.action || '-'}</td>...</tr>`;
}
document.getElementById('logTable').innerHTML = html;
```

**影响**: `username`、`ip`、`action`、`module` 均来自数据库，无任何转义直接拼入 `innerHTML`。攻击者注册用户名 `<img src=x onerror=alert(document.cookie)>`，管理员查看日志页面时 XSS 触发。

**修复**: 使用已有的 `escapeHtml()` 函数（`admin.html:58` 已定义但此文件未使用）：
```javascript
html += `<tr><td>${escapeHtml(log.username || '-')}</td>...`;
```

---

### 3. `/data` 静态挂载暴露所有数据文件

**Severity**: Critical — 信息泄露

**文件**: `app_api.py:77`

**代码**:
```python
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")
```

**影响**: 整个 `data/` 目录作为静态文件暴露，任何人可访问：
- `https://.../data/access_log.json` — 用户操作日志（含 IP、用户名、操作）
- `https://.../data/users.json` — 遗留用户数据（可能含哈希）
- `https://.../data/backtest_*.json` — 策略回测分析数据
- `https://.../data/predictions_*.json` — 每日预测结果

**修复**: 移除静态挂载，或加认证中间件限制访问。如果前端确实需要 `/data` 下的资源，建一个受控的路由代理。

---

### 4. N+1 查询 — scan_daily_pool / strategy_scan

**Severity**: Critical — 每次扫描约 2700 次 SQL 查询

**文件**: `quant_app/services/strategy_service.py:1322-1392`

**模式**:
```python
for c in candidates:  # ~300 只股票
    conn = pymysql.connect(**db_config)  # 每只股票新连接
    bonus = get_dragon_tiger_bonus(conn, ts_code, today)  # 2 SQL
    hb = get_holder_bonus(conn, ts_code)                    # 1 SQL
    mf = calculate_mainforce_score(ts_code, ...)            # 6 SQL
```

**影响**: 300 候选 × (1+2+1+6) ≈ 3000 SQL 连接+查询，每 30 分钟 cron 触发。DB 压力巨大。

**修复**: 批量化——`WHERE ts_code IN (...)` 一次性加载龙虎榜/持股/资金流数据，在内存中建 dict 索引。

---

### 5. `_load_best_model()` 无缓存

**Severity**: Critical — 每次 ML 推理重复获取锁+遍历降级链

**文件**: `ml_predict.py:197-238`

**代码**:
```python
def _load_best_model():
    bundle = _load_model("v11.0")
    if bundle: return bundle, "v11.0"
    bundle = _load_model("v10.0")
    if bundle: return bundle, "v10.0"
    # ... 共 13 个版本依次尝试 ...
```

**影响**: 每次 `generate_v4_ml_candidates()` / `predict_batch()` / `ml_enhanced_score()` / `scan_aimodel()` 调用都重新遍历降级链、获取锁。实际上同一个模型会连续成功，但锁和 13 次 try 开销每次都支付。

**修复**: 用 `@lru_cache(maxsize=1)` 或模块级 sentinel 缓存首次成功结果。

---

### 6. `predict_batch()` v10.0 用错特征构建函数

**Severity**: High — 静默预测退化

**文件**: `ml_predict.py:2079`

**代码**:
```python
elif version == "v10.0":
    feat_df = _build_features_for_stocks_v8_0(db_conn, ts_codes, as_of_date=as_of_date)  # BUG: 应调用 _v10_0
```

**影响**: v10.0 模型接收的是 v8.0 特征，缺失 26+ 个 v10.0 特有特征，预测质量降级。**生产管线不受影响**（`generate_v4_ml_candidates` 行 2625-2626 正确调用了 `_v10_0`），但 `predict_batch` 的直接调用者（`ml_enhanced_score` / `scan_daily_pool` 等）会用到错误特征。

**修复**: 改为 `_build_features_for_stocks_v10_0`。

---

### 7. 持仓数据分裂 — JSON vs MySQL 无唯一真实来源

**Severity**: High — 持仓数据静默漂移

**文件**:
- `market_service.py:190-211` — `sync_positions()` 写入 JSON
- `market_service.py:214` — `add_to_positions()` 写入 JSON（不写 MySQL）
- `persistence.py:475-548` — `get_positions_data()` 从 MySQL 读，JSON 做 fallback
- `position_monitor.py:14-39` — 从 MySQL `positions` 表读

**影响**: `add_to_positions()` 添加的持仓对 `position_monitor.py` 不可见（后者从 MySQL 读）。两套数据静默分歧，止盈止损可能漏执行。

**修复**: 统一为单一存储。建议 MySQL 作为唯一真实来源，`position_monitor.py` 保持从 MySQL 读取，`add_to_positions()` 改为写入 MySQL。

---

## ⚠️ High Priority Issues

### 8. 回测 SQL 逻辑重复 6 次

**文件**: `run_backtest_v4_pool.py`, `run_backtest_v4_pool_filter.py`, `run_backtest_v11.py`, `run_ml_quality_analysis.py`, `run_ml_quality_v2.py`, `run_ml_quality_fast.py`

**影响**: 约 60 行核心回测 SQL（取交易日历、取候选池、算前向收益）在 6 个文件中以细微不同的方式重复。改一处逻辑需同步改 6 处。

**建议**: 抽取为共享库函数，例如 `quant_app/backtest/utils.py`：`get_trade_dates()`, `get_candidate_pool()`, `compute_forward_return()`。

### 9. f-string SQL 表名拼接

**文件**: `scripts/sync_akshare.py:443,529,533,562,608,623,629`

**代码**:
```python
cursor.execute(f"ALTER TABLE quant_db.{tbl} ADD COLUMN is_latest BOOLEAN DEFAULT TRUE")
```

**影响**: 虽然当前 `tbl` 来自硬编码列表 `['board_concept', 'board_industry']`，但 f-string 拼接表名是 SQL 注入的高危模式。代码进化中可能引入用户控制输入。

**修复**: 如果表名必须动态，用白名单映射。

### 10. 特征构建链重复加载相同数据

**文件**: `ml_predict.py` 中 `_build_features_for_stocks_v6` → `v6_2` → `v6_3` → … → `v10_0`

**影响**: 每层构建函数都重新从 MySQL 加载 `daily_price`、`moneyflow_daily`、`market_index_daily` 等相同表格。例如 v11.0 推理链：`v11_features` → `_build_features_for_stocks_v8_0` → `_build_features_for_stocks_v6_3` → `_build_features_for_stocks_v6_2` → `_build_features_for_stocks_v6`，每层都 reload。

**修复**: 将基础数据提取到调用方，通过参数传递已加载的 DataFrame 给下游构建函数。

### 11. `iterrows()` 循环 + 逐股 SQL

**文件**: `strategy_service.py:2512`, `ml_predict.py:489-631, 607, 958`

**影响**: `for _, row in df.iterrows()` 是 pandas 最慢的迭代方式（每行构造 Series 对象）。`ml_predict.py:607` 在逐股循环内又做了 `pd.read_sql(... margin_daily ...)`，导致 N+1 隐藏在特征构建中。

**修复**: 用 `df.apply()` 或向量化操作替代 `iterrows()`；margin 查询提到循环外用 `WHERE ts_code IN (...)` 批量化。

### 12. `generate_v4_ml_candidates()` 函数过长 (~415 行)

**文件**: `strategy_service.py:2349-2759`

**影响**: 单个函数处理候选池构建、V4 评分（跳过）、ML 预测、市场自适应阈值、混合评分、主力资金查询、风控（禁用）、行业分散、日志。难以测试和维护。

**建议**: 拆分为：
- `_build_candidate_pool(conn, ...)` — SQL 候选池
- `_apply_ml_scoring(conn, candidates, ...)` — ML 预测 + 过滤
- `_apply_sector_diversification(candidates, ...)` — 行业分散
- 主函数只做协调

### 13. Cookie Secure 标志依赖环境变量

**文件**: `quant_app/routes/auth.py:340-341`

**代码**:
```python
is_secure = os.environ.get("ENV", "development") == "production"
resp.set_cookie(key="token", value=token, httponly=True, samesite="strict", max_age=86400*7, secure=is_secure)
```

**影响**: 非生产环境（`ENV` 未设或为 `development`）时 `secure=False`，session token 可通过 HTTP 明文传输，网络嗅探可劫持会话。

**修复**: 生产环境固定 `secure=True`，开发环境用 `localhost` 而不用 HTTP。

### 14. 密码重置 Token 在 URL 查询参数中

**文件**: `quant_app/routes/auth.py:384`

**代码**:
```python
reset_url = f"https://lh.mozengfu.com.cn/reset-password?token={reset_token}"
```

**影响**: Token 出现在浏览器历史、服务器访问日志、Referer 头中。1 小时有效期。

**修复**: 改用 POST-only 方式提交 token，或一次性链接+验证码组合。

### 15. `traceback.print_exc()` 在 4 个文件中

**文件**: `scanning.py:1223`, `strategy_service.py:1663`, `backtest_service.py:259,379`, `dashboard.py:570`

**影响**: 打印到 stderr（不是 logger），日志格式不统一，排查问题困难。

**修复**: 全部改为 `logger.exception("描述信息")`。

---

## 💡 Medium Priority Issues

### 16. 模块级 `PURE_ML_MODE` 在导入时评估

**文件**: `strategy_service.py:2149`

**代码**:
```python
PURE_ML_MODE = os.environ.get("PURE_ML", "0") == "1"
```

**影响**: 模块导入后环境变量变化不生效。被 7 个位置使用（行 2408/2472/2556/2610/2665/2681/2781）。当前通过 `start.sh` 在进程启动前设置，所以没问题，但限制了灵活性（不能通过 API 动态切换）。

**建议**: 如果确定是启动时配置，加上明确注释 `# 启动时确定，运行时不变`。

### 17. `strategy_scan()` SQL 中硬编码 `quant_db` 数据库名

**文件**: `strategy_service.py:1491,1519-1521`

**代码**:
```python
cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
FROM quant_db.daily_price d JOIN quant_db.stock_info s ...
```

**影响**: 数据库名在其他地方均通过连接默认指定，此处硬编码。改库名时此处会遗漏。

**修复**: 去掉 `quant_db.` 前缀，使用连接默认数据库。

### 18. `risk_config.json` 已定义但被 `PURE_ML=1` 旁路

**文件**: `data/risk_config.json`, `strategy_service.py:2715`

**影响**: 风控配置（熔断 -15%、仓位 30%、不同市场状态的仓位限制）通过 `data/risk_config.json` 定义，但 `PURE_ML=1` 时整段风控逻辑不执行，配置失效。如果有人切换回 `PURE_ML=0`，需要确认风控配置仍然适用。

### 19. SQLAlchemy ORM 已定义但未用于生产

**文件**: `quant_app/data/database.py` + `quant_app/data/models/` 9 张表

**影响**: 引擎配置了 `pool_size=20`、`max_overflow=30`，但所有生产代码都直接 pymysql 裸 SQL。ORM 代码约 500 行无人调用，存在腐化风险。同时也意味着错过了连接池复用带来的性能收益。

**建议**: 决定 ORM 的去留：要么移除未使用的代码，要么开始在新模块中使用。

### 20. `_v8_6` 特征构建器构建大量特征后丢弃

**文件**: `ml_predict.py:2024-2049`

**影响**: 调用 `_build_features_for_stocks_v6_3()` 计算 60+ 特征，然后只保留其中 52 个基础特征 + 10 个 rank 特征，其余全部丢弃。对 CPU 和 DB IO 都是浪费。

### 21. 访问日志迁移使用字符串时间戳比较

**文件**: `app_api.py:123-128`

**代码**:
```python
new_logs = [l for l in logs if l.get("timestamp", "") > str(last_imported)]
```

**影响**: 字符串字典序比较 vs 时间戳比较。如果格式不一致（如 `"2025-01-01 12:00:00"` vs `"2025-01-01T12:00:00"`），会漏掉或重复导入。

**修复**: 解析为 `datetime` 对象再比较。

### 22. 缺少 CSRF 保护

**文件**: 所有 POST/PUT/DELETE 端点（`auth.py` 登录/注册/改密，`admin.py` 审批，`signals.py` CRUD 等）

**影响**: 虽有 `samesite="strict"` 部分缓解，但对子域名攻击、浏览器扩展等场景不充分。

**建议**: 添加 CSRF token 中间件。

### 23. 全局异常捕获漏了特定错误处理

**文件**: `app_api.py:61-67`

**影响**: 全局 handler 捕获所有异常返回 500。但一些路由（`signals.py:115`、`admin.py:134`）又在 catch 块中把 `str(e)` 返回给客户端，绕过了全局 handler。错误信息可能泄露内部细节。

**修复**: 全局 handler 中统一处理，路由层不再手动 try-catch 返回 `str(e)`。

---

## ✅ Low Priority / Nice to Have

### 24. 函数缺少类型注解（~90%）

整个 `quant_app/` 包中仅有 `features/v11_features.py` 和 `data/database.py` 有较完整的类型注解。其余函数参数和返回类型均未标注。

### 25. 裸字典应改用数据类

持仓数据（6 个键）、候选股票数据（16 个键）在多处作为裸字典传递，键名拼写差异可能导致静默错误。

### 26. 回测脚本硬编码日期范围

6 个回测脚本各自硬编码了 `START_DATE, END_DATE`，应改为 CLI 参数或共享配置。

### 27. JSON 文件多进程无锁

`persistence.py` 使用 `threading.RLock()` 但 FastAPI 多 worker 时无进程间锁。`os.replace()` 本身原子，但读取可能拿到过期数据。

### 28. 重复的 `create_engine()` 调用

至少 9 处独立 `create_engine()`，池大小不一致（5 vs 20）。应统一到 `database.py` 的单例 engine。

---

## 📊 统计汇总

| 严重级别 | 数量 | 类别分布 |
|----------|------|----------|
| 🚨 Critical | 7 | 安全 3, 性能 2, Bug 1, 数据完整性 1 |
| ⚠️ High | 8 | 安全 3, 性能 2, 代码质量 2, 设计 1 |
| 💡 Medium | 8 | 代码质量 4, 设计 3, 配置 1 |
| ✅ Low | 5 | 类型安全 2, 代码重复 1, 配置 1, 并发 1 |
| **总计** | **28** | |

## 🎯 快速修复（高影响低投入）

1. **修复 `predict_batch()` v10.0 特征调用** — `ml_predict.py:2079`，改一行
2. **`log_analytics.html` 加 `escapeHtml()`** — 模板中加函数调用
3. **`_load_best_model()` 加 `lru_cache`** — 加装饰器
4. **移除 `/data` 静态挂载** — `app_api.py:77` 注释掉
5. **`traceback.print_exc()` → `logger.exception()`** — 改 4 处
6. **`strategy_service.py` 去掉 `quant_db.` 前缀** — 改 2 行

## 🏆 做得好的

- **密码哈希**: bcrypt + 旧 SHA256 自动升级（`utils/auth.py`）
- **登录限流**: 每 IP 60 秒内最多 5 次尝试（`routes/auth.py:27-42`）
- **全局异常处理**: 统一返回 500 不暴露详情（`app_api.py:61-67`）
- **Cookie 安全**: `httponly=True` + `samesite=strict` 已设置
- **行情降级**: 四级降级链路 + 缓存 TTL（`realtime_service.py`）
- **模型加载**: 线程安全锁 + 降级链容错（`ml_predict.py`）
- **JSON 原子写入**: 临时文件 + `os.replace()`（`persistence.py`）

## 📚 资源

- [OWASP SQL Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)
- [OWASP XSS Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)
- [OWASP CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
- [Python SQLAlchemy 连接池最佳实践](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [pandas 性能优化 — 避免 iterrows()](https://pandas.pydata.org/docs/user_guide/enhancingperf.html)
