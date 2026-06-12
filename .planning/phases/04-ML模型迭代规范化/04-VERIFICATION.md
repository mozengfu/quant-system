---
phase: 04-ML模型迭代规范化
verified: 2026-06-12T16:50:00Z
status: gaps_found
score: 4/9 must-haves verified
re_verification: false
---

# Phase 4: ML 模型迭代规范化 — 验证报告

**阶段目标：** ML 训练流程有规范化的版本管理和性能监控。
**REQ 覆盖：** REQ-10（ML 训练流水线规范化）、REQ-11（模型性能监控）、REQ-12（模型清理）
**验证状态：** gaps_found
**分数：** 4/9 must-haves 通过

## 总体评估

ML 流水线的"使用层"完整（`_save_prediction_snapshot` 工作、模型文件 v6→v11 系列齐全、`model_monitor_history.json` 持续记录、`lru_cache` 生效），但规划中的"规范化文件"大部分缺失：未生成 `model_registry.json`、未提供 `train_model.py` 统一入口、未提供 `cleanup_models.py` 清理脚本、未提供 `check_model_perf.py` 性能报告、未生成 `prediction_tracking.json` 聚合追踪、`ml_predict.py` 的全局变量缓存也未按计划简化。系统当前直接调用 `ml_train_v11_0.py` 等具体脚本。

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 训练有标准入口，输出稳定的模型文件 | ⚠️ PARTIAL | 当前直接调用 `ml_train_v11_0.py` 等具体脚本；规划要求的 `scripts/train_model.py` 统一入口**缺失** |
| 2 | 模型加载有 lru_cache | ✓ VERIFIED | `quant_app/utils/model_loader.py:load_model()` line 60 含 `@lru_cache(maxsize=4)` |
| 3 | 训练监控有 history 记录 | ✓ VERIFIED | `data/model_monitor_history.json` 2696 行；`data/model_monitor_history_v11_2.json` 存在 |
| 4 | 预测快照按日保存 | ✓ VERIFIED | `ml_predict.py:_save_prediction_snapshot()` line 2234-2265；`data/predictions_20260512_v8.0.json` 等 10+ 份快照存在 |
| 5 | 部署后预测准确率可跟踪对比 | ✗ FAILED | `data/prediction_tracking.json` 不存在；无聚合 IC 趋势文件；`check_model_perf.py` 不存在 |
| 6 | 模型劣化可自动检测 | ✗ FAILED | 无劣化检测逻辑（计划中应在 `check_model_perf.py` 中实现的 `detect_degradation()`） |
| 7 | 旧模型定期清理 | ⚠️ PARTIAL | `cleanup_models.py` 缺失；`data/` 下仍有大量 .pkl 文件（含多个 _bad/_oos/_backup/.bak），未按 v\d+ 正则清理到 archive |
| 8 | 废弃模型文件已从 data/ 移走 | ⚠️ PARTIAL | v3/v4_bull/bear/sideways/ridge 已在 archive 路径中（git 历史中），但 `data/` 目录当前仍存有 20+ 个 v6-v11 .pkl 文件，模型加载器注册表也包含 v6-v11 所有版本 |
| 9 | `ml_predict.py` 缓存已简化（移除模块级全局变量） | ✗ FAILED | line 55-61 仍声明 `_v6_bundle, _v6_2_bundle, ..., _v6_7_bundle, _model_lock` 7 个全局变量；line 74-76 仍用 `with _model_lock` + 复杂 if-elif 分支 |

**Score:** 4/9 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `scripts/train_model.py` | 统一训练入口 | ✗ MISSING | 缺失；当前用 `ml_train_v11_0.py` 等直接调用 |
| `scripts/cleanup_models.py` | 模型清理脚本 | ✗ MISSING | 缺失；data/ 下仍有冗余文件 |
| `scripts/check_model_perf.py` | 性能报告 | ✗ MISSING | 缺失；无劣化检测、无汇总报告 |
| `data/model_registry.json` | 版本注册表 | ✗ MISSING | `_MODEL_REGISTRY` 是硬编码 dict（model_loader.py:13-37），未从 JSON 加载 |
| `data/prediction_tracking.json` | 预测追踪聚合 | ✗ MISSING | 只有分散的 `predictions_YYYYMMDD_vX.X.json` 快照，无聚合 |
| `data/model_monitor_history.json` | 训练历史 | ✓ VERIFIED | 2696 行；记录训练 IC 时间序列 |
| `ml_predict.py:_save_prediction_snapshot()` | 快照保存 | ✓ VERIFIED | line 2234-2265；每日运行后生成 `predictions_*.json` |
| `quant_app/utils/model_loader.py` | 模型加载 | ✓ VERIFIED | 67 行；`@lru_cache(maxsize=4)` + `_MODEL_REGISTRY` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `ml_predict.py:_load_model` | `model_loader.load_model` | `from quant_app.utils.model_loader import load_model` | ✓ WIRED | lru_cache 在底层生效 |
| `ml_predict.py:ml_enhanced_score` | `_save_prediction_snapshot` | 函数末尾调用 | ✓ WIRED | line 2230 `ml_enhanced_score` 末尾触发 |
| `ml_predict.py:_load_model` | 移除模块级变量 | 计划要求 | ✗ NOT_WIRED | line 55-61 仍保留 7 个全局变量 |
| `model_loader.py:_MODEL_REGISTRY` | `data/model_registry.json` | 计划要求的动态加载 | ✗ NOT_WIRED | 注册表是硬编码 dict |
| `data/predictions_*.json` | 聚合到 `prediction_tracking.json` | 计划要求的 3 天后处理 | ✗ NOT_WIRED | 没有任何处理脚本读取快照计算实际收益 |

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| REQ-10 ML 训练流水线规范化 | ⚠️ PARTIAL | lru_cache 与训练历史完整，但缺统一训练入口、版本注册表 JSON |
| REQ-11 模型性能监控 | ✗ BLOCKED | 训练 IC 有记录但部署后实际预测准确率追踪缺失；无劣化检测 |
| REQ-12 模型清理 | ⚠️ PARTIAL | 旧 v3/v4 系列已不存在，但 v6-v11 全量 20+ 文件仍堆积在 data/，无保留 N 版本的清理脚本 |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `ml_predict.py` | 55-61 | 模块级 7 个 `_v6_X_bundle` 全局变量 | ⚠️ Warning | 与 plan Task 2.3 目标相反；冗余缓存层（lru_cache 已足够） |
| `data/` | — | 20+ .pkl 文件（含 _bad/_oos/_backup/.bak 多份） | ⚠️ Warning | 无定期清理；占用磁盘且增加选择成本 |
| `ml_predict.py` | 68, 74-220 | `_model_lock` 锁 + 7 路 if-elif 分支 | ⚠️ Warning | 与 plan Task 2.3 目标相反；lru_cache 已线程安全 |

### Human Verification Required

1. **预测快照是否被消费** — 10+ 份 `predictions_*.json` 文件存在，但 `prediction_tracking.json` 不存在。需确认：这些快照是临时调试产物，还是有未提交的脚本在定期处理它们。
2. **模型文件堆积是否影响推理** — `data/ml_stock_model_v11_0.pkl` + `*_bad.pkl` + `*_oos*.pkl` + `*_retrain_backup.pkl` 共 7 个 v11.0 相关文件。推理时是否真的只用其中一个？注册表里也只注册 v11.0（未注册 _bad/_oos 子变体），似乎无影响，但需确认。
3. **训练历史的两份文件** — `data/model_monitor_history.json` 和 `data/model_monitor_history_v11_2.json` 并存，是否应为单一文件？
4. **v6 训练脚本** — 规划中提到 `ml_train_v6.py` ~ `ml_train_v6_5.py` 5 个独立脚本；实际仓库 `ls ml_train_v6*.py` 无结果（v6 系列被 v8/v9/v10/v11 替代）。这是计划外演进还是未迁移？

### Gaps Summary

**5 个规划要求完全未实现**（REQs 实质性缺口）：

1. **`scripts/train_model.py` 统一训练入口缺失**（REQ-10）— 5 个规划任务的核心交付物未创建。修复：创建入口脚本并注册现有 `ml_train_v11_0.py`、`ml_train_v11_2.py` 等。
2. **`data/model_registry.json` 与注册表感知加载缺失**（REQ-10）— 当前 `_MODEL_REGISTRY` 是硬编码 19 项；应改为启动时从 JSON 加载 + 失败 fallback。
3. **`scripts/check_model_perf.py` 与 `data/prediction_tracking.json` 缺失**（REQ-11）— 性能监控的整个聚合层未实现。
4. **`scripts/cleanup_models.py` 缺失**（REQ-12）— 无保留 N 版本的自动清理；data/ 下文件堆积。
5. **`ml_predict.py` 缓存未简化**（plan Task 2.3）— 7 个模块级全局变量 + lock + 7 路分支仍在。

**2 个改进但非阻塞**：
- `model_monitor_history.json` 2696 行有数据
- `predictions_*.json` 10+ 份快照有数据（但无消费者）

**0 个训练入口崩溃**（实际训练仍能跑）

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier_
