# 设计方案 — 7 个待修复架构问题

## 1. N+1 查询重构（`scan_daily_pool` ~2700 SQL/次）

**问题**: `scan_daily_pool()` 对 300 候选股票逐股循环，每只跑 9 次 SQL。

**方案**: 批量化改造

1. 在 `strategy_service.py` 中新增 3 个批量化函数：
   - `batch_dragon_tiger_bonus(conn, ts_codes, trade_date)` → `{ts_code: bonus}`
   - `batch_holder_bonus(conn, ts_codes)` → `{ts_code: bonus}`
   - `batch_mainforce_scores(conn, ts_codes, trade_date)` → `{ts_code: score}`
2. 每个函数用 `WHERE ts_code IN (...)` 一次查出所有数据，在内存中建 dict
3. 修改 `scan_daily_pool()` 中 P1 增强循环（行 1322-1392）：循环前先调用 3 个 batch 函数，循环内查 dict
4. 旧逐股函数保留不动（其他地方可能还在用）

**影响文件**: `quant_app/services/strategy_service.py`
**估算**: ~100 行新增，零风险（旧函数不改动）

---

## 2. 持仓数据统一真实来源

**问题**: `add_to_positions()` 写入 JSON，`position_monitor.py` 读 MySQL，两边静默漂移。

**方案**: MySQL 作为唯一真实来源，JSON 仅做缓存

1. 修改 `add_to_positions()`（`market_service.py:214`）：**先写 MySQL `positions` 表**，再写 JSON
2. 修改 `sync_positions()`（`market_service.py:190`）：改为 **MySQL → JSON** 单向同步（现在是 JSON→JSON）
3. `get_positions_data()`（`persistence.py:475`）：MySQL 为主不变，JSON fallback 保留
4. `position_monitor.py`：继续读 MySQL 不变

**影响文件**: `quant_app/services/market_service.py`, `quant_app/utils/persistence.py`
**估算**: ~50 行改动，需确认 `positions` 表 schema

---

## 3. 回测 SQL 逻辑抽取共享库

**问题**: 6 个脚本各自硬编码了取交易日/候选池/算前向收益的 SQL。

**方案**: 在 `quant_app/backtest/utils.py` 中抽取共享函数

```python
def get_trade_dates(conn, start_date, end_date) -> list[str]
def get_candidate_pool(conn, trade_date, limit=500) -> list[str]
def compute_pool_forward_returns(conn, ts_codes, buy_date, hold_days=5) -> dict
def backtest_stats(results: list[dict]) -> dict
```

6 个回测脚本统一 import 这些函数，去掉重复 SQL。

**影响文件**:
- 新建 `quant_app/backtest/utils.py`
- 修改 `run_backtest_v4_pool.py`, `_pool_filter.py`, `_v11.py`, `run_ml_quality_*.py`
**估算**: ~80 行共享代码 + 每个脚本减 ~30 行

---

## 4. `sync_akshare.py` f-string SQL 加固

**问题**: 表名通过 f-string 拼入 SQL，虽当前来自硬编码列表，模式危险。

**方案**: 加白名单映射，拒绝未知表名

```python
# 文件顶部
_TABLE_WHITELIST = {
    "concept": "board_concept",
    "industry": "board_industry",
    "daily_price": "daily_price",
    # ... 按需添加
}

def _safe_table(name):
    tbl = _TABLE_WHITELIST.get(name)
    if not tbl:
        raise ValueError(f"禁止的表名: {name}")
    return tbl
```

所有 `f"... {table} ..."` 改为 `f"... {_safe_table(table)} ..."`。

**影响文件**: `scripts/sync_akshare.py`
**估算**: ~15 行新增，逐处替换 f-string 中的变量

---

## 5. 特征构建链重复加载（`ml_predict.py`）

**问题**: v11.0 推理链：v11_features → v8.0 → v6.3 → v6.2 → v6，每层都从 MySQL 重载 base 数据。

**方案**: 提取基础数据至调用方，通过上下文传递

```
方案 A（推荐 — 改动小）:
  新建 _load_base_feature_data(conn, ts_codes, as_of_date) 
  → 返回 dict of DataFrames: {daily_price, moneyflow, market_index, margin, ...}
  各 _build_features_for_stocks_v* 函数增加可选参数 base_data=None
  如果传了 base_data，跳过 reload 直接用

方案 B（彻底 — 重构整个管线）:
  将特征构建改为 pipeline 模式：
  PipelineStage(name, depends_on=[...], fn=...)
  按依赖拓扑排序，每层只算增量特征
```

建议先方案 A：对现有代码侵入最小。方案 B 需要完整理解所有特征依赖关系。

**影响文件**: `ml_predict.py`, `scripts/predict_v11.py`, `quant_app/features/v11_features.py`
**估算**: 方案 A ~80 行，方案 B ~300 行

---

## 6. `iterrows()` + 逐股 N+1 向量化

**问题**: 3 处 perf 热点

| 位置 | 问题 |
|------|------|
| `strategy_service.py:2512` | `for _, row in df.iterrows()` 做 V4 评分 |
| `ml_predict.py:607` | 逐股查 margin_daily |
| `ml_predict.py:958` | 逐股查 margin_daily |

**方案**:
- **V4 评分**: 将 `_v4_score_single(row)` 改为向量化版本 `_v4_score_vectorized(df)` → `pd.Series`，用 pandas 列运算替代行循环（分数逻辑基于 close/pct_chg/volume_ratio/rps 等列，完全可向量化）
- **margin 查询**: 改成 `WHERE ts_code IN (...)` 一次查出所有，`groupby('ts_code')` 索引

**影响文件**: `strategy_service.py`, `ml_predict.py`
**估算**: ~60 行

---

## 7. CSRF 保护

**问题**: 所有 POST/PUT/DELETE 端点无 CSRF 防护。

**方案**: 自定义请求头方案（对 API 应用最简单，配合已有的 `SameSite=Strict` Cookie）

1. 在 `app_api.py` 中添加中间件，要求所有 POST/PUT/DELETE 请求携带自定义头 `X-CSRF-Protection: 1`
2. 登录/注册端点白名单例外
3. 前端 `fetch` 调用统一加 header

```python
@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE"):
        # 跳过登录/注册
        if request.url.path not in ("/api/auth/login", "/api/auth/register"):
            if request.headers.get("X-CSRF-Protection") != "1":
                return JSONResponse(status_code=403, content={"error": "CSRF 验证失败"})
    return await call_next(request)
```

前端改动：所有 `fetch()` 调用加 `"X-CSRF-Protection": "1"` 头。

**影响文件**: `app_api.py` + 前端 JS（`index.html`, `admin.html` 等）
**估算**: ~15 行后端 + 前端逐处加 header

---

## 优先级建议

| 优先级 | 方案 | 工作量 | 影响 |
|--------|------|--------|------|
| P0 | 7. CSRF 保护 | 极小（~15 行） | 安全短板 |
| P1 | 1. N+1 重构 | 中等（~100 行） | 性能大幅提升 |
| P1 | 6. iterrows 向量化 | 小（~60 行） | 性能提升 |
| P2 | 2. 持仓统一 | 小（~50 行） | 数据一致性 |
| P2 | 4. SQL 白名单加固 | 极小（~15 行） | 安全加固 |
| P3 | 3. 回测共享库 | 中等（~80 行） | 可维护性 |
| P3 | 5. 特征链优化 | 中-大 | 性能 + 可维护 |
