---
phase: 01-策略打磨与回测体系
verified: 2026-06-12T16:50:00Z
status: gaps_found
score: 5/7 must-haves verified
re_verification: false
---

# Phase 1: 策略打磨与回测体系 — 验证报告

**阶段目标：** V4 组合策略有系统化的回测验证和参数优化能力。
**REQ 覆盖：** REQ-01（V4 组合策略持续验证）、REQ-02（参数优化工具）、REQ-03（多策略对比回测）
**验证状态：** gaps_found
**分数：** 5/7 must-haves 通过

## 与规划文件的偏差

规划文档（01-PLAN.md）指定的所有新增文件均使用 `scripts/run_backtest.py` / `scripts/backtest_metrics.py` / `scripts/optimize_v4_params.py` / `scripts/analyze_params.py` / `scripts/compare_strategies.py` 这五个具体文件名。**实际代码库中没有这五个文件存在**。原计划假设的命名约定未执行，开发过程改用了不同的命名和目录结构。

| 规划文件 | 实际替代实现 | 状态 |
|---------|-------------|------|
| `scripts/run_backtest.py` | `scripts/backtest_run.py`（227 行）+ 多个 `run_backtest_v*.py` | 文件名变更但意图达成 |
| `scripts/backtest_metrics.py` | `quant_app/backtest/utils.py`（176 行）含 `backtest_stats()` 和 `format_backtest_table()` | 重构为包内模块 |
| `scripts/optimize_v4_params.py` | `scripts/backtest_param_scan.py`（343 行） | 文件名变更 |
| `scripts/analyze_params.py` | 与 `backtest_param_scan.py` 合并 | 独立分析脚本缺失 |
| `scripts/compare_strategies.py` | `scripts/backtest_all_strategies.py`（223 行） | 文件名变更 |
| `data/params_scan_v4.json` | `data/backtest_compare/grid_search_*.csv` 等多份扫描结果 | 格式从 JSON 改为 CSV |
| `data/strategy_comparison.json` | `data/backtest_all_strategies.json` | 存在 |

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 可一键运行 V4 回测并输出收益/风险指标 | ✓ VERIFIED | `scripts/backtest_run.py` 提供 CLI（`--start/--end/--top-n/--hold-days/--pool`），封装 `BacktestEngine`；多个 `run_backtest_v*.py` 覆盖 V4/V11 变体 |
| 2 | 输出 JSON/CSV 标准指标（总收益、胜率、盈亏比、夏普、最大回撤、交易记录） | ✓ VERIFIED | `data/backtest_result.json` 752 字节包含 `total_return/win_rate/profit_loss_ratio/sharpe/max_drawdown/n_trades/nav_values`；`quant_app/backtest/utils.py:backtest_stats()` 是统一的指标计算函数 |
| 3 | 参数扫描能自动寻找最优参数组合 | ✓ VERIFIED | `scripts/backtest_param_scan.py` 在内存中批量测试 HOLD_DAYS=[1,3,5] × TOP_N=10 组合；`data/backtest_compare/grid_search_*.csv` 是扫描结果（每行一组参数 + 14 项指标） |
| 4 | 多策略能在同一条件下对比 | ✓ VERIFIED | `scripts/backtest_all_strategies.py` 注释 "对比6种方案"；`data/backtest_all_strategies.json` 38KB；`run_backtest_v4_pool.py` 和 `run_backtest_v11.py` 共用 DB_CONFIG/START_DATE/END_DATE |
| 5 | 标准化指标计算可独立 import 并复用 | ✓ VERIFIED | `quant_app/backtest/utils.py:backtest_stats(results)` / `format_backtest_table(all_stats)` 公开函数；`run_backtest_v4_pool.py` 等 5 个脚本均 `from quant_app.backtest.utils import backtest_stats, ...` |
| 6 | 推荐参数可直接用于实际选股 | ✗ FAILED | 缺失独立的 `analyze_params.py` 分析器脚本，无 `data/params_optimization_report.txt` 报告文件；`data/backtest_compare/` 目录下仅有原始 CSV 扫描结果，无推荐参数报告 |
| 7 | 废弃脚本已标记，不再混用 | ✓ VERIFIED | 旧版 V4 脚本（`backtest_v4_combo.py`、`backtest_v41_vs_ml.py`、`backtest_bottom_awakening.py`）均已移至 `archive/scripts/`；运行入口已统一通过 `scripts/backtest_run.py` 和 `run_backtest_v*.py` |

**Score:** 5/7 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `scripts/backtest_run.py` | 统一回测 CLI | ✓ VERIFIED | 227 行，argparse + BacktestEngine，标准指标 |
| `quant_app/backtest/utils.py` | 标准化指标 | ✓ VERIFIED | 176 行，`backtest_stats()` / `format_backtest_table()` |
| `quant_app/backtest/engine.py` | 回测引擎 | ✓ VERIFIED | 294 行，`BacktestEngine` 类 |
| `scripts/backtest_param_scan.py` | 参数扫描 | ✓ VERIFIED | 343 行，矩阵扫描 |
| `scripts/backtest_all_strategies.py` | 多策略对比 | ✓ VERIFIED | 223 行，6 种方案对比 |
| `scripts/analyze_params.py` | 参数分析器 | ✗ MISSING | 缺失独立分析脚本和推荐报告输出 |
| `data/backtest_result.json` | V4 回测结果 | ✓ VERIFIED | 752 字节，标准字段齐全 |
| `data/backtest_compare/` | 扫描结果目录 | ✓ VERIFIED | 9 份 CSV/JSON 扫描结果，含 grid_search |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `backtest_run.py` | `quant_app.backtest.engine.BacktestEngine` | `from quant_app.backtest.engine import BacktestEngine` | ✓ WIRED | line 15 |
| `backtest_param_scan.py` | `quant_app.backtest.utils` | `import` | ⚠️ PARTIAL | 使用 `pymysql` + 内存计算，未复用 `backtest_stats()` |
| `backtest_all_strategies.py` | DB | `pymysql.connect` | ✓ WIRED | `get_db_config()` + `get_trade_dates()` + `_v4_score_single()` |
| `run_backtest_v4_pool.py` | `quant_app.backtest.utils` | `from quant_app.backtest.utils import backtest_stats, ...` | ✓ WIRED | line 21 |

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| REQ-01 V4 组合策略持续验证 | ✓ SATISFIED | 多份 backtest_result.json / backtest_v4_pool.json / backtest_walkforward_v11.json |
| REQ-02 参数优化工具 | ⚠️ PARTIAL | 扫描器存在但缺少分析器和推荐报告 |
| REQ-03 多策略对比回测 | ✓ SATISFIED | backtest_all_strategies.py + 6-way 对比输出 |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| 无显著反模式 | — | — | — | 所有核心回测脚本均实际执行计算，无 TODO/stub/占位实现 |

### Human Verification Required

1. **回测结果合理性** — `data/backtest_result.json` 中 `total_return=606.95, max_drawdown=42.54, n_trades=24` 数字是否准确反映 V4 策略表现。
2. **参数扫描覆盖** — `data/backtest_compare/grid_search_*.csv` 中扫描维度（`stop_loss_pct/take_profit/time_stop_days/high_conf_pos_pct`）是否覆盖了 V4 关键参数空间。
3. **6 策略对比** — `data/backtest_all_strategies.json` 中 6 个策略的对比口径是否真正"同条件"（相同交易日、相同初始资金、相同手续费）。

### Gaps Summary

**2 个非阻塞性缺口**：

1. **参数分析器缺失**（REQ-02 部分）— `scripts/analyze_params.py` 不存在；`data/params_optimization_report.txt` 不存在。`scripts/backtest_param_scan.py` 只做扫描不做分析。修复：补一个分析脚本读取 CSV 扫描结果，输出 Top 5 推荐参数和单参数敏感性说明。
2. **文件名偏离规划**（流程性）— 5 个规划文件名均未按计划使用。这是 5 月以来多次重构的副作用，不是功能缺失。

**3 个值得注意的架构演进**（不是缺口，是优势）：
- 实际系统将回测核心重构到 `quant_app/backtest/` 包中（vs 计划的 `scripts/` 散落），更符合工程组织
- 实际有 6 个 `run_backtest_v*.py` 变体（v4_pool、v4_pool_filter、v11、v11_atr、v11_full、v11_walkforward）覆盖 V4/V11 两条主轴，胜过计划假设的"一个 V4 入口"
- 实际有 Walk-Forward 无泄漏回测变体（`run_backtest_v11_walkforward.py`），是计划外的健壮性增强

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier_
