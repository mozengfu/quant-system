# Phase 1 — 策略打磨与回测体系

**目标：** V4 组合策略有系统化的回测验证和参数优化能力。

---

## Plan 1: 统一 V4 回测入口 (REQ-01)

### Objective

现有 V4 回测逻辑完整（`backtest_combo_v4.py`），但无统一入口 —— 需要记住脚本路径和参数格式，且每次回测后无标准化的指标摘要输出。本计划将 25+ 个散落脚本统一为一个单命令入口，输出标准化的收益/风险指标。

### Tasks

#### Task 1.1: 创建统一回测入口脚本

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/run_backtest.py`

将 `backtest_combo_v4.py` 的 `backtest_combo_v4()` 作为核心引擎，封装一个 CLI 入口，支持：
- 子命令 `v4`：运行 V4 组合回测
- 参数 `--start` / `--end`：回测区间（默认最近 6 个月）
- 参数 `--score`：主力评分门槛（默认 60）
- 参数 `--hold`：最大持仓天数（默认 7）
- 参数 `--positions`：最大持仓数（默认 5）
- 参数 `--output`：输出文件路径（默认 `data/backtest_result.json`）

**验证标准：**
- 执行 `python3 scripts/run_backtest.py v4 --start 20251001 --end 20260424` 输出完整回测结果
- JSON 输出包含 V4 已有的全部字段（总收益率、胜率、盈亏比、夏普比率、最大回撤、交易记录）

#### Task 1.2: 标准化指标计算与输出

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/backtest_metrics.py`

将 V4 回测中散落的指标计算逻辑抽取为独立模块（从 `backtest_combo_v4.py` 第 408-482 行提取）：

- `calc_win_rate(closed_trades)`：胜率计算
- `calc_sharpe(daily_values)`：夏普比率（年化 252 天）
- `calc_max_drawdown(daily_values)`：最大回撤
- `calc_profit_loss_ratio(win_trades, loss_trades)`：盈亏比
- `format_metrics_summary(result_dict)`：输出一行格式化的指标摘要

**验证标准：**
- `backtest_metrics.py` 可独立 import 并被 `run_backtest.py` 调用
- `format_metrics_summary()` 输出类似标准的单行摘要：`V4 | 总收益率+21.76% | 胜率45.2% | 盈亏比1.57 | 夏普1.79 | 最大回撤7.5%`

#### Task 1.3: 清理废弃同名脚本

**文件操作：** 标记废弃（不删除，加注释头说明）

- `/Users/mozengfu/workspace/quant-system/scripts/backtest_v4_combo.py` — 简化版 V4，已被 `backtest_combo_v4.py` 覆盖
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_v41_vs_ml.py` — 已有独立的 `backtest_v65_comparison.py` 覆盖
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_bottom_breakout.py` — 底部起步已下线
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_strong_active.py` — 强势活跃已下线

在以上文件顶部添加 `# DEPRECATED: 使用 scripts/run_backtest.py v4 替代` 注释，不删除任何代码。

**验证标准：**
- 4 个文件头部包含弃用注释
- `grep -rn "from.*backtest_v4_combo\|import.*backtest_v4_combo"` 无活跃引用

### Success Criteria

- `python3 scripts/run_backtest.py v4` 一个命令跑完回测并输出标准指标
- 结果 JSON 保存在 `data/backtest_result.json`，包含完整指标
- 废弃脚本已标记，不再混用

---

## Plan 2: 参数优化工具 (REQ-02)

### Objective

现有参数扫描零散且方式不同：`backtest_combo_v6_params.py` 只有 6 组硬编码参数，`backtest_param_scan.py` 针对不同策略。需要一个专门针对 V4 组合策略的参数网格扫描工具，能自动寻找最优参数组合。

### Tasks

#### Task 2.1: V4 参数网格扫描器

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/optimize_v4_params.py`

复用 `backtest_combo_v4.py` 的 `backtest_combo_v4()` 函数逻辑，但改造为支持参数网格循环：

扫描参数范围（可配置默认值）：

| 参数 | 扫描范围 | 说明 |
|------|---------|------|
| `min_score` | [40, 50, 60, 70, 80] | 主力评分门槛 |
| `max_hold_days` | [5, 7, 10, 14] | 最大持仓天数 |
| `max_positions` | [3, 5, 7] | 最大持仓数 |
| `initial_stop_pct` | [-3%, -5%, -7%] | 固定止损线 |
| `tp_tier_1` | [5%, 6%, 8%] | 第一段止盈 |
| `tp_tier_2` | [10%, 12%, 15%] | 第二段止盈 |
| `tp_tier_3` | [15%, 18%, 20%] | 清仓止盈 |

策略：
- 预加载所有参数组合到列表，逐个运行 `backtest_combo_v4()` 主流程
- 每个组合输出标准指标（复用 Plan 1 的 `backtest_metrics.py`）
- **避免全组合爆炸**：默认先固定 `max_positions=5`，扫描 `min_score x max_hold_days x initial_stop_pct` 三个维度（5x4x3=60 次），之后按需扩展

排序和评分：
- 综合评分 = `总收益率 * 0.3 + 胜率 * 0.15 + 夏普比率 * 0.25 + (1/最大回撤) * 0.15 + 盈亏比 * 0.15`
- 输出 Top 10 参数组合详细对比

**验证标准：**
- `python3 scripts/optimize_v4_params.py --scan` 输出参数扫描结果（约 60 次回测运行）
- JSON 保存在 `data/params_scan_v4.json`
- 打印 Top 5 最优参数组合

#### Task 2.2: 参数扫描结果分析器

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/analyze_params.py`

读取 `data/params_scan_v4.json`，提供：
- 单参数敏感性分析：固定其他参数，观察单个参数变化对收益/风险的影响
- 最佳参数推荐：按综合评分排序，输出推荐参数组合
- 输出文本格式的简要分析报告到 `data/params_optimization_report.txt`

**验证标准：**
- `python3 scripts/analyze_params.py` 读取已有扫描结果并输出分析
- 报告包含：推荐参数组合、参数敏感性说明、预期收益/风险范围

#### Task 2.3: 优化参数一键应用

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/run_backtest.py`

为 `run_backtest.py` 新增子命令：
- `run_backtest.py optimize` — 执行参数扫描（调用 Task 2.1）
- `run_backtest.py analyze` — 分析已有扫描结果（调用 Task 2.2）

**验证标准：**
- `python3 scripts/run_backtest.py optimize` 一键启动参数扫描
- `python3 scripts/run_backtest.py analyze` 输出分析结果

### Success Criteria

- 参数扫描一次运行覆盖 60+ 参数组合
- 输出 Top 5 最优参数组合及综合评分
- 单参数敏感性分析判断各参数对收益的边际影响
- 推荐参数可直接用于实际选股

---

## Plan 3: 多策略对比框架 (REQ-03)

### Objective

V4 组合是主策略，但需要在与 V4.1 评分、V6.5 ML 增强等同条件下横向对比，以量化评估各策略的优劣。现有的 `backtest_v65_comparison.py` 有对比概念但只对比 ML 相关变体，且没有统一的对比报告格式。

### Tasks

#### Task 3.1: 标准化对比评估器

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/compare_strategies.py`

定义标准化的策略对比流程：

1. 统一回测条件（交易日范围、初始资金、手续费、滑点）
2. 对 V4 组合策略使用 `backtest_combo_v4.py` 引擎
3. 对 V4.1 评分策略使用 `_v41_score()` 逻辑 + 信号触发回测
4. 对 V6.5 ML 策略使用 `ml_predict.py` 的模型预测 + 信号触发回测
5. 所有策略在完全相同的交易日区间、买卖规则下运行

对比指标（统一口径）：

| 指标 | 计算方式 |
|------|---------|
| 总收益率 | 最终市值/初始资金 - 1 |
| 年化收益率 | ((1+总收益率)^(252/交易日数) - 1) |
| 胜率 | 盈利交易/总交易 |
| 盈亏比 | 平均盈利/平均亏损 |
| 夏普比率 | (日收益率均值/日收益率标准差) * sqrt(252) |
| 最大回撤 | 峰值到谷值的最大跌幅 |
| 交易次数 | 总交易信号数 |
| 日均持仓 | 平均每日持仓股票数 |

支持子命令：
- `backtest`：运行对比回测
- `list`：列出已有对比结果

**验证标准：**
- `python3 scripts/compare_strategies.py backtest --strategies v4_combo,v41_scan` 运行两种策略对比
- JSON 输出到 `data/strategy_comparison.json`
- 打印对比表格

#### Task 3.2: 对比报告生成

**文件操作：** 在 `/Users/mozengfu/workspace/quant-system/scripts/compare_strategies.py` 内新增函数

生成可读的对比报告：
- 格式：文本表格（用 `--summary` 参数触发打印）
- 内容：策略名、总收益率、年化收益、胜率、盈亏比、夏普、最大回撤、交易次数
- 突出显示各项指标的最优值（用 `*` 标记）

**验证标准：**
- `python3 scripts/compare_strategies.py --summary` 打印清晰对比表
- 每行一个策略，每列一个指标

#### Task 3.3: 整合 V4 回测入口

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/run_backtest.py`

为 `run_backtest.py` 新增子命令：
- `run_backtest.py compare` — 运行多策略对比（调用 Task 3.1）
- `run_backtest.py compare --summary` — 展示已保存的对比结果

**验证标准：**
- `python3 scripts/run_backtest.py compare` 一键运行多策略对比
- 结果与独立运行 `compare_strategies.py` 一致

### Success Criteria

- V4 组合 vs V4.1 评分 vs V6.5 ML 三种策略在相同条件下可对比
- 对比报告包含完整指标且格式统一
- 对比运行时间和完整回测一致（不额外增加回测时间）

---

## 执行顺序

```
Plan 1 (REQ-01) ──→ Plan 2 (REQ-02) ──→ Plan 3 (REQ-03)
   Task 1.1           Task 2.1             Task 3.1
   Task 1.2           Task 2.2             Task 3.2
   Task 1.3           Task 2.3             Task 3.3
```

Plan 1 必须先完成（提供标准化指标和统一入口），Plan 2 和 Plan 3 可独立并行。

## 涉及文件清单

### 新增文件
- `/Users/mozengfu/workspace/quant-system/scripts/run_backtest.py` — 统一回测入口（Plan 1）
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_metrics.py` — 标准化指标计算（Plan 1）
- `/Users/mozengfu/workspace/quant-system/scripts/optimize_v4_params.py` — V4 参数扫描（Plan 2）
- `/Users/mozengfu/workspace/quant-system/scripts/analyze_params.py` — 参数分析器（Plan 2）
- `/Users/mozengfu/workspace/quant-system/scripts/compare_strategies.py` — 多策略对比（Plan 3）

### 修改文件
- `/Users/mozengfu/workspace/quant-system/scripts/run_backtest.py` — 逐步新增 optimize / compare 子命令（Plan 2, Plan 3）

### 标记文件（仅加弃用注释）
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_v4_combo.py`
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_v41_vs_ml.py`
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_bottom_breakout.py`
- `/Users/mozengfu/workspace/quant-system/scripts/backtest_strong_active.py`

### 输出文件
- `data/backtest_result.json` — V4 最新回测结果
- `data/params_scan_v4.json` — 参数扫描结果
- `data/params_optimization_report.txt` — 参数优化报告
- `data/strategy_comparison.json` — 多策略对比结果
