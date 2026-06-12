---
audit: v1-5phases-comprehensive
verified: 2026-06-12T16:50:00Z
status: gaps_found
overall_score: 27/39 must-haves verified (69%)
phases:
  - {id: 01, score: 5/7, status: gaps_found}
  - {id: 02, score: 6/8, status: gaps_found}
  - {id: 03, score: 6/8, status: gaps_found}
  - {id: 04, score: 4/9, status: gaps_found}
  - {id: 05, score: 6/7, status: gaps_found}
---

# 全面审核报告 — v1 五阶段验证

**审核范围：** `.planning/phases/01..05/` 全部 5 个阶段的 PLAN.md 规划与实际代码库
**审核基准：** Goal-backward verification — 检查"目标是否达成"而非"任务是否标记完成"
**审核范围：** 实际工作目录代码（包含未提交修改）
**总体评分：** 27/39 must-haves 验证通过（69%）

## TL;DR

v1 五阶段规划**核心目标基本达成**，系统已经在生产环境实际运行（CLAUDE.md 与 STATE.md 均明确说明）。但与 5 月初编制的 PLAN.md 相比，实际实现存在**显著偏差**，分为三类：

1. **架构升级替代**（非缺口）：position_monitor.py 的盘中执行被 live_trading_scheduler.py 替代，是更好的实现
2. **文件命名/路径漂移**（5 月以来多次重构副作用）：核心能力有，但文件名偏离规划
3. **规划要求未实现**（真正缺口）：5 个规划交付物完全缺失，主要集中在 Phase 4（ML 规范化）和 Phase 5（前端绩效面板）

## 五阶段评分总览

| 阶段 | REQ | 规划目标 | 实际达成 | 缺口 |
|------|-----|---------|---------|------|
| 01 策略打磨与回测体系 | REQ-01, 02, 03 | 一键 V4 回测+参数优化+多策略对比 | ✓ 完整 | 独立参数分析脚本缺失；5 个规划文件名未使用 |
| 02 模拟交易全自动化 | REQ-04, 05, 06 | 选股/下单/调仓自动 | ✓ 核心完整 | position_monitor.py 被新架构替代（不算缺口）；实盘 stop_loss=0 数据完整性问题 |
| 03 风控体系完善 | REQ-07, 08, 09 | 止损止盈/仓位/市场状态 | ✓ 完整 | crontab 14:50-15:00 窗口缺失；position_monitor 未接入 market_state |
| 04 ML 模型迭代规范化 | REQ-10, 11, 12 | 训练规范/性能监控/清理 | ⚠️ 4/9 验证 | 5 个规划交付物完全缺失（train_model.py / cleanup_models.py / check_model_perf.py / model_registry.json / prediction_tracking.json） |
| 05 监控与报表 | REQ-13, 14 | NAV 追踪 + 绩效看板 | ⚠️ 后端就绪前端未做 | 前端"绩效看板"面板缺失（按钮/div/JS 函数全无） |

## 阶段详细报告

详细报告见各阶段的 VERIFICATION.md：
- [Phase 1: 策略打磨与回测体系](/.planning/phases/01-策略打磨与回测体系/01-VERIFICATION.md)
- [Phase 2: 模拟交易全自动化](/.planning/phases/02-模拟交易全自动化/02-VERIFICATION.md)
- [Phase 3: 风控体系完善](/.planning/phases/03-风控体系完善/03-VERIFICATION.md)
- [Phase 4: ML 模型迭代规范化](/.planning/phases/04-ML模型迭代规范化/04-VERIFICATION.md)
- [Phase 5: 监控与报表](/.planning/phases/05-监控与报表/05-VERIFICATION.md)

## 关键发现排序（按严重性）

### 🛑 Blocker（影响核心需求交付）

1. **Phase 5 前端绩效面板完全缺失** — 后端 API 与 nav_history.json 244 行数据完整，但 `templates/index.html` 缺按钮/div、`static/js/app.js:933` 仅留空 section 注释。用户在 Web 端看不到 8 张绩效卡片。修复路径明确（4 步骤，约 100 行代码）。
2. **Phase 4 五个规划交付物完全缺失** — `train_model.py` / `cleanup_models.py` / `check_model_perf.py` / `model_registry.json` / `prediction_tracking.json` 全部不存在。REQ-10/11/12 中 REQ-11（性能监控）实际未工作；REQ-10/12 仅部分达成。

### ⚠️ Warning（影响风控有效性或监控完整性）

3. **`data/positions.json` 中 2 个持仓 `stop_loss: 0.0`** — 实盘持仓同步路径未带止损价。已被 `live_trading_scheduler.py:685` 硬性 -5% 兜底覆盖，但与 sim_trading 计算预期不一致。
4. **crontab 14:50-15:00 收盘前窗口未覆盖** — `*/10 9-14` 实际配置，14:50 是最后一次扫描；14:50-15:00 极端行情无保护。
5. **`ml_predict.py` 缓存未按计划简化**（line 55-68, 74-220）— 7 个模块级全局变量 + lock + 7 路 if-elif 分支仍存在；`@lru_cache` 已在底层生效，全局变量层冗余。
6. **Phase 1 独立参数分析器缺失** — `scripts/analyze_params.py` 与 `data/params_optimization_report.txt` 缺失；`backtest_param_scan.py` 只扫描不分析。
7. **Phase 3 `position_monitor.py` 未接入 market_state** — 该脚本被 `live_trading_scheduler.py` 架构替代，但代码中确实无 `market_state` 引用。

### ℹ️ Info（值得注意的架构演进）

8. **plan 文件命名全面漂移** — 5 个规划文件名（`run_backtest.py`、`backtest_metrics.py` 等）均未使用；实际采用了 `backtest_run.py`、`quant_app/backtest/utils.py`、`backtest_param_scan.py` 等。这是 5 月以来多次重构的副作用。
9. **`position_monitor.py` 架构替代** — 计划中"position_monitor 改造为盘中自动执行"未执行；实际由 `live_trading_scheduler.py monitor`（54KB）承担，含 ATR 动态止损、硬性兜底、移动止盈、V11 盘中择时入场，能力超越原计划。
10. **ML v6 系列被 v11 替代** — 规划中提到 `ml_train_v6.py` ~ `ml_train_v6_5.py`；实际仓库 `ml_train_v11_0.py` / `ml_train_v11_2.py` / `ml_train_v11_3.py`。注册表覆盖 v6-v11 全 17 个版本。

## 与 STATE.md（2026-05-05 状态）的对比

| STATE.md 标记 | 实际情况 | 差距 |
|--------------|---------|------|
| Phase 1: 统一回测入口 + 参数优化 + 多策略对比 | 后端完整，前端分析器缺失 | 部分达成 |
| Phase 2: 自动止盈/止损/超时 + 数据源统一 + 健康检查 | 全部实现 | ✓ 达成 |
| Phase 3: 盘中自动执行 + 市场状态自适应 + 风控配置模块 | 三层基础完整 + 14:50-15:00 窗口缺失 | 大部分达成 |
| Phase 4: 统一训练入口 + 模型清理 + 性能监控 | 训练历史 + 快照有；5 个规划交付物缺失 | 大部分未达成 |
| Phase 5: 净值历史 + 绩效看板 + 飞书日报增强 | 后端完整；前端绩效面板缺失 | 大部分达成 |

## 建议的下一步行动

按 ROI 排序：

1. **完成 Phase 5 前端绩效面板**（约 100 行代码）— 后端就绪，纯前端补完，立即可见效果
2. **修复 `positions.json` 中 stop_loss=0**（约 30 行）— 排查 sim_positions 写入路径，确保买入时计算并持久化止损价
3. **扩展 crontab 至 9-15**（单行修改）— `*/10 9-14` 改 `*/10 9-15`
4. **实现 Phase 4 缺失的 5 个文件**（约 600 行）— `train_model.py` / `cleanup_models.py` / `check_model_perf.py` / `model_registry.json` / `prediction_tracking.json`
5. **简化 ml_predict.py 缓存**（约 30 行）— 移除 7 个全局变量
6. **补 Phase 1 analyze_params.py**（约 80 行）

## 验证方法学说明

- 工具：纯静态分析（grep / wc / sed / cat）+ 关键文件 syntax check
- 未实际运行回测或模拟交易（不在 gsd-verifier 范围）
- 数据文件（nav_history.json / positions.json / predictions_*.json）已检查存在性和内容合理性
- 未检查的：实际模型预测准确率、实际交易盈亏、实际 crontab 触发结果

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier (全面审核)_
_Verification artifacts_: `.planning/phases/{01..05}/*-VERIFICATION.md` (5 份)
