# Phase 4: ML 模型迭代规范化 — 执行计划

## 问题陈述

当前 ML 训练存在五个独立脚本（`ml_train_v6.py` ~ `ml_train_v6_5.py`），各自有独立入口和保存逻辑，没有统一调用方式。模型文件 11 个、特征配置 7 套混在 `data/` 中，包含大量已废弃的 V4 及更早版本（合计约 4MB 无引用文件）。`ml_predict.py` 有双重缓存层（模块级全局变量 + model_loader.py 的 lru_cache），冗余且不一致。模型监控只有训练时的 IC 记录，没有部署后的预测准确率追踪和劣化检测。

---

## Plan 1: 训练流水线统一 (REQ-10)

**目标：** 统一训练入口，标准化模型文件输出，解决版本混乱。

### 现状

| 文件 | 模型类型 | 输出文件名 |
|------|---------|-----------|
| `ml_train_v6.py` | LightGBM 单模型回归 | `ml_stock_model_v6.pkl` |
| `ml_train_v6_2.py` | LambdaRank 5-ensemble | `ml_stock_model_v6_2.pkl` |
| `ml_train_v6_3.py` | LambdaRank 5-ensemble | `ml_stock_model_v6_3.pkl` |
| `ml_train_v6_4.py` | LambdaRank 5-ensemble | `ml_stock_model_v6_4.pkl` |
| `ml_train_v6_5.py` | LambdaRank 5-ensemble | `ml_stock_model_v6_5.pkl` |

每个脚本都定义了自己的 `append_monitor_record()`（5 份重复代码），`MODEL_PATH` / `FEATURE_CONFIG_PATH` 硬编码在文件头。

### 任务 1.1: 创建统一训练入口 `scripts/train_model.py`

**涉及文件：**

- 新建 `scripts/train_model.py`
- 现有训练脚本不做修改（保持各自可独立执行）

**设计：**

```python
# scripts/train_model.py
"""
统一 ML 训练入口

用法:
    python3 scripts/train_model.py            # 默认训练最新版本 (v6.5)
    python3 scripts/train_model.py v6.5       # 指定版本
    python3 scripts/train_model.py v6         # 训练 v6
    python3 scripts/train_model.py --list     # 列出可用版本
    python3 scripts/train_model.py v6.5 --quick  # 快速模式（较少数据/轮次，仅调试用）
"""
```

实现方式：用 `importlib` 动态导入对应版本的训练脚本 `ml_train_v6_5.py` 的 `main()`。不做代码搬移，不做重构。

```python
_MODULE_MAP = {
    "v6": "ml_train_v6",
    "v6.2": "ml_train_v6_2",
    "v6.3": "ml_train_v6_3",
    "v6.4": "ml_train_v6_4",
    "v6.5": "ml_train_v6_5",
}
```

关键设计原则：

- 各训练脚本文件不做修改 — 入口只做代理调用
- `--quick` 模式通过环境变量 `TRAIN_QUICK=1` 传递给被调用脚本（各训练脚本自行决定如何缩短：减少数据量或减少轮次）
- 这样后续 v6.6 只需要加脚本 + 注册到 `_MODULE_MAP`，入口不用改
- 训练完成后打印 summary（耗时、Rank IC、模型路径）

**成功标准：** 上述四条 `python3 scripts/train_model.py` 命令都能正常运行，`--list` 输出所有注册版本。

### 任务 1.2: 标准化模型输出路径和监控记录

**涉及文件：**

- 每个训练脚本 `ml_train_v6*.py` 的头部的路径常量
- `model_monitor_history.json` schema（确认一致性）

**问题：** 当前各脚本的 `MODEL_PATH` 和 `FEATURE_CONFIG_PATH` 都是硬编码的，格式一致但分散。`append_monitor_record()` 函数在每个脚本里重复定义。

**改动 1 — 让每个脚本的 `MONITOR_HISTORY_PATH` 指向同一个文件：**

当前所有脚本已经指向 `os.path.join(DATA_DIR, 'model_monitor_history.json')`，路径一致但代码重复。本次不消除重复（保持 surgical），只确认所有版本输出 schema 一致。检查 v6 的 monitor record vs v6.5 的，确认字段时间戳格式一致。

**改动 2 — 版本注册表：**

创建 `data/model_registry.json`，记录当前有效的模型版本列表和状态：

```json
{
  "latest_version": "v6.5",
  "previous_version": "v6.4",
  "best_version": "v6.2",
  "available_versions": ["v6", "v6.2", "v6.3", "v6.4", "v6.5"],
  "updated_at": "2026-05-05T10:00:00",
  "history": [
    {"version": "v6.5", "trained_at": "2026-05-04T19:01:55", "final_rank_ic": 0.055, "model_file": "ml_stock_model_v6_5.pkl"},
    {"version": "v6.4", "trained_at": "2026-05-04T18:42:00", "final_rank_ic": 0.055, "model_file": "ml_stock_model_v6_4.pkl"},
    ...
  ]
}
```

- `scripts/train_model.py` 运行完训练后，调用 `update_registry(version, metrics)` 写入该文件
- `best_version` 取 `history` 中 `final_rank_ic` 最高者（仅对比 ensemble 版本 v6.2+，排除 v6 单模型以避免不公平比较）
- 提供一个函数 `get_latest_version()` 供 `ml_predict.py` 和外部调用方使用

**成功标准：** `train_model.py v6` 执行后，`data/model_registry.json` 正确更新；`--list` 输出注册表内容。

---

## Plan 2: 废弃模型清理 (REQ-12)

**目标：** 删除不再使用的旧模型，清理文件冗余。

### 任务 2.1: 移除废弃模型文件

**需要移除的 7 个 .pkl 文件（移至 `archive/`）：**

| 文件 | 大小 | 现状 |
|------|------|------|
| `data/ml_stock_model.pkl` | 951K | 基础 v1，已不再使用 |
| `data/ml_stock_model_v3.pkl` | ~200K | V3 旧版 |
| `data/ml_stock_model_v4_bull.pkl` | 1.0M | V4 牛市子模型，策略已下线 |
| `data/ml_stock_model_v4_bear.pkl` | 1.0M | V4 熊市子模型，策略已下线 |
| `data/ml_stock_model_v4_sideways.pkl` | 1.0M | V4 震荡市子模型，策略已下线 |
| `data/ml_stock_model_ridge.pkl` | 9.4K | Ridge 备选模型，已不启用 |
| `data/ml_bear_model.pkl` | 27K | 熊市专用模型，策略已下线 |

**需要移除的 3 个 feature_config（移至 `archive/`）：**

| 文件 | 说明 |
|------|------|
| `data/feature_config_v3.json` | V3 配置 |
| `data/feature_config_v4.json` | V4 配置 |
| `data/feature_config_ridge.json` | Ridge 配置 |

**涉及文件：**

- `quant_app/utils/model_loader.py` — 从 `_MODEL_REGISTRY` 移除 `v3` 和 `v4_sideways` 条目
- `ml_predict.py` — 无需改动（`_load_model()` 只加载 v6+，不会触及这些旧文件）
- 上述文件移至 `archive/`（不是物理删除，遵循"不删除文件"原则）
- 更新 `CLAUDE.md` 中的 AI 辅助提示（如果有引用旧模型路径）

**操作步骤：**

1. 确认 `ml_predict.py` 中没有 import 或引用这些废弃版本的代码路径
2. `mv data/ml_stock_model.pkl archive/`
3. `mv data/ml_stock_model_v3.pkl archive/`
4. `mv data/ml_stock_model_v4_bull.pkl archive/`
5. `mv data/ml_stock_model_v4_bear.pkl archive/`
6. `mv data/ml_stock_model_v4_sideways.pkl archive/`
7. `mv data/ml_stock_model_ridge.pkl archive/`
8. `mv data/ml_bear_model.pkl archive/`
9. `mv data/feature_config_v3.json archive/`
10. `mv data/feature_config_v4.json archive/`
11. `mv data/feature_config_ridge.json archive/`
12. 从 `model_loader.py` 的 `_MODEL_REGISTRY` 中移除 `v3` 和 `v4_sideways`

**成功标准：** `data/` 下只保留 `ml_stock_model_v6*.pkl` 和 `feature_config_v6*.json`，以及 `model_registry.json` 和 `model_monitor_history.json`。`model_loader.py` 中的注册表只包含 v6+ 版本。

### 任务 2.2: 创建 `scripts/cleanup_models.py`

**周期性清理脚本，保留 N 个最新版本。** 供计划任务调用。

```bash
# 保留最近 2 个正式版本
python3 scripts/cleanup_models.py --keep 2

# 查看哪些会被清理（不实际执行）
python3 scripts/cleanup_models.py --dry-run
```

**逻辑：**

1. 读取 `data/model_registry.json` 获取已注册的版本列表
2. 按 `trained_at` 降序排列，保留前 N 个版本（默认 2）
3. 将被淘汰版本对应的 `.pkl` 和 `feature_config_*.json` 文件移至 `archive/`
4. 更新 `model_loader.py` 中的 `_MODEL_REGISTRY`（移除条目）
5. 更新 `model_registry.json` 中的 `available_versions`
6. 打印清理报告

**版本定义：** 所谓"版本"的正则匹配是 `v\d+(_\d+)?$`（匹配 v6、v6_2、v6.5），不匹配 v3、v4_bull 等旧版。

**注意：** v6 单模型（1.0M）与 v6.2+ ensemble（5.1M）在能力上有代差。保留策略：如果 v6 被选中淘汰但 v6.2+ 有更优版本，v6 可以优先被淘汰。看 `-keep` 参数，只按 `trained_at` 时间排序，不对版本进行加权。

**设计决策：** 不与 `model_loader.py` 的文件系统交互直接写注册表文件。用 Edit 工具修改 `_MODEL_REGISTRY` 的 Python 源码过于脆弱，改为让 `cleanup_models.py` 通过重写 `model_registry.json` 来"宣告"清理结果，然后由 `model_loader.py` 在下次启动时读取该注册表来决定加载哪些模型。

但 `model_loader.py` 当前是硬编码字典，没有从 JSON 读取。因此需要：

**改动 — model_loader.py 增加注册表感知能力：**

```python
_MODEL_REGISTRY = {}  # 启动时从 registry.json 构建

def _build_registry():
    """从 data/model_registry.json 构建模型注册表"""
    registry_path = MODELS_DIR / "model_registry.json"
    if registry_path.exists():
        try:
            with open(registry_path) as f:
                data = json.load(f)
            for ver in data.get("available_versions", []):
                _MODEL_REGISTRY[ver] = MODELS_DIR / f"ml_stock_model_{ver.replace('.', '_')}.pkl"
            return
        except Exception:
            pass
    # 回退：从文件系统扫描
    for pkl in MODELS_DIR.glob("ml_stock_model_v*.pkl"):
        ...  # 推断 version 名称
```

或更简单的方案：保留 `_MODEL_REGISTRY` 静态字典，`cleanup_models.py` 直接编辑该字典输出到文件头部附近的注释占位。这个方案容易破坏文件结构。

**选定方案：** 让 `model_loader.py` 支持从 `model_registry.json` 加载注册表。如果 `model_registry.json` 不存在或加载失败，fallback 到原有静态字典。这样 `cleanup_models.py` 只写 JSON 文件，不碰 Python 代码。

**涉及文件：**

- 新建 `scripts/cleanup_models.py`
- 修改 `quant_app/utils/model_loader.py`（增加 JSON 注册表加载逻辑）

### 任务 2.3: 统一 ml_predict.py 缓存 — 移除模块级全局变量

**问题：** `ml_predict.py` 中有两层缓存：

1. 模块级全局变量 `_v6_bundle`, `_v6_2_bundle`, ..., `_v6_5_bundle`（5 个变量）
2. `_model_lock` 线程锁保护
3. model_loader.py 的 `@lru_cache(maxsize=4)` 已经是独立、线程安全（CPython GIL 下 lru_cache 原子操作）的缓存

全局变量层完全冗余。`model_loader` 的 lru_cache 已经缓存了最近 4 次不同的 `load_model()` 调用结果（命中可直接返回），而且 `_load_model()` 的调用模式决定了并发场景下同一版本的并发调用很少发生。

**方案：**

将 `_load_model(version)` 简化为直接调用 `model_loader.load_model()`，移除所有全局变量 `_v6_bundle` ... `_v6_5_bundle` 和 `_model_lock`，以及对应的 if-elif 分支：

**Before (55 行):**
```python
_v6_bundle = None
_v6_2_bundle = None
...
_model_lock = threading.Lock()

def _load_model(version="v6"):
    with _model_lock:
        if version == "v6.5":
            global _v6_5_bundle
            if _v6_5_bundle is None:
                _v6_5_bundle = load_model("v6.5")
                ...
            return _v6_5_bundle
        if version == "v6.4":
            ...
        ...  # 5 个版本各 10-15 行分支
```

**After (15 行):**
```python
def _load_model(version="v6"):
    """加载指定版本的模型（线程安全，由 model_loader 的 lru_cache 保证）"""
    bundle = load_model(version)
    if bundle is None:
        return None
    ic = bundle.get('final_rank_ic', 'N/A')
    n_models = bundle.get('ensemble_n_models', 1)
    ver = bundle.get('version', version)
    logger.info(f"模型已加载: {version} ({n_models}个子模型, rank_ic={ic})")
    return bundle
```

同时移除顶部 5 行全局变量声明和 `import threading`（如果 threading 只有这里用）。

**注意：** `_load_model()` 除了 ml_predict.py 自己内部的 `predict_batch()` 和 `ml_enhanced_score()` 调用外，还会被 `app_core.py` 间接调用（通过 import），需要 grep 确认。

**涉及文件：**

- `ml_predict.py`（核心修改）
- 确认 `app_core.py`、`ml_predict_v3.py` 等文件是否有直接引用这些全局变量

**成功标准：** `python3 run_three_strategies.py` 正常执行，`ml_enhanced_score()` 正常返回结果，确认 model_loader 的 lru_cache 生效。

---

## Plan 3: 模型性能监控 (REQ-11)

**目标：** 追踪模型部署后的预测准确率，检测劣化并预警。

### 任务 3.1: 每日预测追踪

**涉及文件：**

- 新建 `data/prediction_tracking.json`（记录结构见下）
- 修改 `ml_predict.py` 的 `predict_batch()` 函数，加入记录逻辑

**结构设计：**

```json
[
  {
    "prediction_date": "2026-05-05",
    "version": "v6.5",
    "model_label": "V6.5集成(IC=0.055)",
    "stocks_count": 4610,
    "predicted_at": "2026-05-05T09:30:00",
    "results_updated_at": "2026-05-08T17:00:00",
    "actuals_summary": {
      "mean_actual_return_3d": 0.012,
      "std_actual_return_3d": 0.045,
      "top100_mean_return": 0.035,
      "top100_win_rate": 0.62,
      "bottom100_mean_return": -0.018,
      "spearman_corr": 0.12
    }
  }
]
```

**数据流：**

1. `predict_batch()` 在完成批量预测后，将预测结果以临时文件 `data/predictions_{date}_{version}.json` 保存原始记录（ts_code -> predicted_return）
2. 交易日收盘 3 天后（预测期 3 天），由 `scripts/check_model_perf.py` 加载该记录、计算实际收益、写入 `prediction_tracking.json`
3. 原始预测记录文件在完成统计后删除（避免积累）

**实现注意：**

- `predict_batch()` 是纯函数，不应该有写入文件的副作用。改为在 `ml_enhanced_score()` 中（调用 `predict_batch` 的地方）保存预测记录，因为 `ml_enhanced_score()` 是策略扫描流程的一部分。
- 或者：提供一个独立函数 `save_prediction_snapshot(ts_codes, predictions, version)`，由策略扫描流程在调用完 `ml_enhanced_score()` 后调用。

**具体改动：**

在 `ml_predict.py` 末尾新增函数：

```python
def save_prediction_snapshot(ts_codes, predictions, version="v6.5"):
    """保存预测快照，供后续实际收益对比"""
    today = datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(DATA_DIR, f'predictions_{today}_{version.replace(".", "_")}.json')
    records = []
    for code in ts_codes:
        pred = predictions.get(code, {})
        records.append({
            'ts_code': code,
            'predicted_return': float(pred.get('predicted_return', 0)),
            'probability': float(pred.get('probability', 0.5)),
            'is_likely_up': bool(pred.get('is_likely_up', False)),
        })
    data = {
        'prediction_date': today,
        'version': version,
        'stocks_count': len(records),
        'predicted_at': datetime.now().isoformat(),
        'records': records,
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    return path
```

**成功标准：** 运行 `python3 run_three_strategies.py` 后，`data/` 下出现 `predictions_2026-05-05_v6_5.json` 文件。

### 任务 3.2: 劣化检测

**问题：** 当前 `model_monitor_history.json` 仅记录训练时的 IC。训练 IC 高不代表部署后预测准确。需要两个层面的检测：

1. **训练 IC vs 实际预测的 Rank Correlation** — 如果实际预测排序能力远低于训练时报告的 IC，说明过拟合或市场风格切换
2. **部署后 IC 趋势** — 连续 3 个交易日的实际 IC 低于基线（取最近 20 个交易日均值）则应告警

**实现：**

在 `scripts/check_model_perf.py` 中实现（见任务 3.3），核心逻辑：

```python
def detect_degradation(tracking_data, model_version="v6"):
    """检测模型劣化"""
    records = [r for r in tracking_data if r["version"] == model_version]
    if len(records) < 5:
        return None  # 数据不足
    
    recent = records[-5:]
    baseline = records[:-5]
    recent_ic = np.mean([r["actuals_summary"]["spearman_corr"] for r in recent])
    baseline_ic = np.mean([r["actuals_summary"]["spearman_corr"] for r in baseline])
    
    if baseline_ic > 0.05 and recent_ic < baseline_ic * 0.5:
        return {
            "status": "WARN",
            "message": f"模型 {model_version} 实际 IC 从 {baseline_ic:.3f} 降至 {recent_ic:.3f}（-{(1-recent_ic/baseline_ic)*100:.0f}%）",
            "recent_ic": recent_ic,
            "baseline_ic": baseline_ic,
        }
    return {
        "status": "OK",
        "message": f"模型 {model_version} 实际 IC {recent_ic:.3f}，基线 {baseline_ic:.3f}",
    }
```

阈值：`spearman_corr` 低于基线 50% 触发 WARN。

**注意：** 劣化检测需要积累至少 ~10 个交易日的数据才能有效工作，上线初期不会有告警。建议在 `check_model_perf.py` 中明确打印 "数据不足(N天)，需要至少5天" 的提示。

### 任务 3.3: 创建 `scripts/check_model_perf.py`

**诊断脚本，读取监控历史和预测追踪数据，输出模型性能报告。**

**功能：**

1. **读取 `model_monitor_history.json`** — 输出每个版本的训练 IC 趋势
2. **读取 `data/predictions_*.json` + 从 MySQL 取 3 日实际收益** — 计算实际 IC
3. **劣化检测** — 输出每个版本的当前健康状态
4. **汇总报告** — 

```bash
python3 scripts/check_model_perf.py

# 输出示例:
# ========================================
# ML 模型性能报告 (2026-05-05)
# ========================================
# 
# 训练监控:
#   v6.2: RankIC=0.043 (2026-05-03), 最新训练IC→已用7天
#   v6.5: RankIC=0.055 (2026-05-04), 最新训练IC→已用1天
#
# 预测追踪 (实际收益验证):
#   数据: 0 个交易日 (尚未积累)
#   需至少 5 天数据才能评估劣化
#
# 模型文件:
#   当前: v6.5 (5.1MB)
#   备用: v6.4, v6.3, v6.2, v6
#   已归档: v3, v4_bull, v4_bear, v4_sideways, ridge, base
# 
# 推荐操作: 无
```

**实现细节：**

- 读取 `prediction_tracking.json` 获取历史实际 IC
- 如果有未处理的 `predictions_*.json` 文件（即 3 天前预测但未统计实际收益的），连接 MySQL 计算实际收益并更新
- `check_model_perf.py` 设计为幂等的 — 多次运行不会产生重复记录
- 报告内容输出到 stdout，也以 JSON 格式写入 `data/perf_report_latest.json` 供其他工具读取

**计算实际收益的 SQL：**

```sql
SELECT ts_code,
       (close - LAG(close, 1) OVER (PARTITION BY ts_code ORDER BY trade_date)) / LAG(close, 1) OVER (PARTITION BY ts_code ORDER BY trade_date) AS ret_3d
FROM daily_price
WHERE trade_date >= DATE_ADD('{prediction_date}', INTERVAL 1 DAY)
  AND trade_date <= DATE_ADD('{prediction_date}', INTERVAL 3 DAY)
```

实际实现时，对每个 `predicted_at` 日期的预测，查找该日期后面第 3 个交易日（考虑跳空）的涨跌幅作为实际收益。

**涉及文件：**

- 新建 `scripts/check_model_perf.py`
- 新建 `data/prediction_tracking.json`（初始为空数组 `[]`）
- `ml_predict.py`（添加 `save_prediction_snapshot()` 函数，约 30 行）

**成功标准：**

1. `python3 scripts/check_model_perf.py` 正常运行并输出报告
2. 报告中的"预测追踪"部分显示正确的数据状态
3. 如果已有旧 `predictions_*.json` 文件且对应日期已过 3 天，它们应被处理并移动到 `archive/`

---

## 执行顺序

```
Plan 1 (训练入口统一)
  └── 任务 1.1: scripts/train_model.py
  └── 任务 1.2: 版本注册表 + model_registry.json

Plan 2 (模型清理)
  └── 任务 2.1: 手动清理废弃模型（移至 archive/）
  └── 任务 2.2: scripts/cleanup_models.py + model_loader.py 注册表感知
  └── 任务 2.3: ml_predict.py 缓存简化
       └── 必须先于 Plan 3 的 3.1（因为 3.1 要在 ml_predict.py 加代码）

Plan 3 (性能监控)
  └── 任务 3.1: ml_predict.py 加 save_prediction_snapshot()
  └── 任务 3.3: scripts/check_model_perf.py
  └── 任务 3.2: 劣化检测（集成在 3.3 中）
```

**依赖关系：**
- 任务 2.1 不依赖任何前置，可先做
- 任务 2.3 需要先确认 `app_core.py` 无全局变量依赖，再做简化
- 任务 3.1 需要 `ml_predict.py` 修改后的稳定缓存，所以最好在 2.3 之后
- 任务 3.2/3.3 依赖于 3.1 的预测快照数据积累

**推荐执行顺序：** 2.1 → 1.1 → 1.2 → 2.2 → 2.3 → 3.1 → 3.3（含 3.2）

---

## 涉及文件汇总

| 文件 | 操作 | Plan |
|------|------|------|
| `scripts/train_model.py` | 新建 | 1.1 |
| `data/model_registry.json` | 新建 | 1.2 |
| `ml_train_v6.py` | 不移除，保留独立可执行 | 1.x |
| `ml_train_v6_5.py` | 不移除，保留独立可执行 | 1.x |
| `archive/ml_stock_model.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_stock_model_v3.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_stock_model_v4_bull.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_stock_model_v4_bear.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_stock_model_v4_sideways.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_stock_model_ridge.pkl` | 从 data/ 移入 | 2.1 |
| `archive/ml_bear_model.pkl` | 从 data/ 移入 | 2.1 |
| `archive/feature_config_v3.json` | 从 data/ 移入 | 2.1 |
| `archive/feature_config_v4.json` | 从 data/ 移入 | 2.1 |
| `archive/feature_config_ridge.json` | 从 data/ 移入 | 2.1 |
| `scripts/cleanup_models.py` | 新建 | 2.2 |
| `quant_app/utils/model_loader.py` | 修改（注册表感知） | 2.2 |
| `ml_predict.py` | 修改（简化缓存） | 2.3 |
| `ml_predict.py` | 修改（加 save_prediction_snapshot） | 3.1 |
| `data/prediction_tracking.json` | 新建 | 3.2 |
| `scripts/check_model_perf.py` | 新建 | 3.3 |

---

## 边界/回滚说明

- **不做的事：** 不重构 `ml_train_v6*.py` 的内部逻辑，不合并特征构建代码，不重写推理管道
- **回滚：** 所有废弃模型文件是 mv 到 archive/ 不是 rm，回滚只需 mv 回 `data/`。`model_registry.json` 可手动修正。`ml_predict.py` 的缓存简化是纯删代码，git checkout 即可恢复
- **安全：** `cleanup_models.py` 的 `--dry-run` 模式确保不会误删当前在用模型
- **数据警告：** 劣化检测需要至少 5 个交易日的数据，上线首周不会有有效检测。`check_model_perf.py` 在数据不足时应给出明确提示，不产生误报
