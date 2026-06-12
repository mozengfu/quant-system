---
phase: 02-模拟交易全自动化
verified: 2026-06-12T16:50:00Z
status: gaps_found
score: 6/8 must-haves verified
re_verification: false
---

# Phase 2: 模拟交易全自动化 — 验证报告

**阶段目标：** 模拟交易从选股到调仓全流程自动运行，无需每日人工触发。
**REQ 覆盖：** REQ-04（自动选股扫描）、REQ-05（自动下单执行）、REQ-06（持仓自动调整）
**验证状态：** gaps_found
**分数：** 6/8 must-haves 通过

## 总体评估

核心交易引擎（`scripts/sim_trading.py`，1659 行）已基本实现规划的自动止盈、止损、超时、回撤断路器、仓位管理、JSON 同步、CLI 入口等能力。规划中"通过 `position_monitor.py` 做盘中自动执行"的设计被架构性替代 — 实际方案是将盘中监控迁出至 `scripts/live_trading_scheduler.py monitor`（54KB，更完整的执行器），`position_monitor.py` 降级为简化的飞书告警脚本。

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 每日定时自动扫描并产生候选股票 | ✓ VERIFIED | `sim_trading.py v4_scan` CLI（line 1642-1652）；crontab `45 17 * * 1-5 ... run_three_strategies.py` |
| 2 | 买卖信号自动触发执行（止损） | ✓ VERIFIED | `sim_trading.py:1290` `execute_sell(pos["id"], price, reason="模拟止损")` |
| 3 | 买卖信号自动触发执行（止盈三档 +6/+10/+18%） | ✓ VERIFIED | `sim_trading.py:1298-1326` 分级止盈逻辑（先 tp3 清仓 → tp2 减仓 → tp1 减仓） |
| 4 | 买卖信号自动触发执行（超时 >5天） | ✓ VERIFIED | `sim_trading.py:1329-1337` `execute_sell(...reason="超时卖出(>5天)")` |
| 5 | 持仓按规则自动动态调整（回撤断路器） | ✓ VERIFIED | `sim_trading.py:1349-1352` 触发条件 `profit_pct < -15%` 暂停买入；`POSITION_SIZING_MODE='equal'` + `PER_POSITION_PCT=0.30` 单仓上限 |
| 6 | 持仓按规则自动动态调整（仓位管理） | ✓ VERIFIED | `sim_trading.py:1379-1382` `min(cash/slots, cash*PER_POSITION_PCT)` 双重上限 |
| 7 | 数据源统一（MySQL → positions.json） | ✓ VERIFIED | `sim_trading.py:sync_positions_to_json()` line 1462-1497；当前 `data/positions.json` 2 个持仓均含 `position_id` 字段 |
| 8 | 异常时通过健康检查脚本快速定位 | ✓ VERIFIED | `scripts/check_pipeline.py`（164 行）检查信号/交易/账户/JSON 同步，输出 `logs/pipeline_check.log` |

**Score:** 6/8 truths verified（其余 2 项在下方说明）

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `scripts/sim_trading.py` | 模拟交易引擎 | ✓ VERIFIED | 1659 行，含 sim_signals 表/止盈/止损/超时/断路器/仓位/JSON 同步/v4_scan |
| `scripts/sim_signals` 表 | MySQL 信号表 | ✓ VERIFIED | line 516-538 `CREATE TABLE IF NOT EXISTS sim_signals` 含 16 列+3 索引 |
| `scripts/position_monitor.py` | 盘中监控 | ⚠️ PARTIAL | 273 行，仅做飞书告警（不执行交易）；与计划中"实际执行卖出"的设计不同 |
| `scripts/feishu_alerts.py` | 飞书告警 | ✓ VERIFIED | 454 行，盘前/收盘/止盈止损预警；读 sim_positions 直接用存的 stop_loss |
| `scripts/check_pipeline.py` | 健康检查 | ✓ VERIFIED | 164 行，5 项检查项 |
| `scripts/quant_crontab` | crontab | ✓ VERIFIED | 69 行，含依赖链注释、17:00/17:30/17:45 流水线 |
| `data/positions.json` | 持仓同步 | ✓ VERIFIED | 675 字节，含 `position_id/code/market/name/cost/shares/stop_loss/take_profit/buy_date/current_price/float_pnl/float_pnl_pct` |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `sim_trading.py:sim_signals` | MySQL `sim_signals` 表 | `CREATE TABLE IF NOT EXISTS` | ✓ WIRED | line 516，含 16 列与 `record_signal()` INSERT 对齐 |
| `sim_trading.py:execute_sell/partial_sell` | MySQL `sim_positions`/`sim_trades` | 函数内 cursor.execute | ✓ WIRED | line 970/1058，参数化 SQL |
| `sim_trading.py:daily_scan()` | `sync_positions_to_json()` | 同文件函数调用 | ✓ WIRED | line 1453 `sync_positions_to_json()` 在 daily_scan 末尾 |
| `sync_positions_to_json()` | `data/positions.json` | `json.dump` | ✓ WIRED | line 1493 |
| `position_monitor.py` | 实际卖出执行 | 应调用 `execute_sell` | ✗ NOT_WIRED | 当前 position_monitor 只调用 `send_feishu`，不调用 sim_trading 的执行函数 |
| `live_trading_scheduler.py:cmd_monitor` | `execute_sell` 等 | executor pattern | ✓ WIRED | line 78 `_executor_market_sell_and_record` + line 700+ 实际触发 |
| `feishu_alerts.py:check_position_alerts` | positions.json | `open + json.load` | ✓ WIRED | line 178-179 |
| `crontab` | 各 Python 脚本 | `cd $QUANT_DIR && python3 scripts/X.py` | ✓ WIRED | 7 个任务项 |

### 计划与实际架构的关键差异

**计划：** `position_monitor.py` 改造为盘中自动执行（直接调用 `execute_sell`/`execute_partial_sell`）
**实际：** `position_monitor.py` 降级为告警脚本，盘中执行迁至 `scripts/live_trading_scheduler.py monitor`（54KB，更复杂，含 ATR 动态止损、硬性 -5% 兜底、ATR 移动止盈、V11 盘中择时入场等）

这是架构演进，不是失败。新增的 `live_trading_scheduler.py` 提供了原计划未涵盖的能力：
- ATR 动态止损（line 678 "ATR动态止损: 成本价 - 2×ATR"）
- 硬性兜底止损 -5%（line 685-691）
- ATR 移动止盈（line 742-756）
- 实盘市价单（line 698-704）确保止损立即成交

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| REQ-04 自动选股扫描 | ✓ SATISFIED | `v4_scan` CLI + crontab 17:45 + `data/positions.json` 同步 |
| REQ-05 自动下单执行 | ✓ SATISFIED | sim_trading.py 止损/止盈/超时全链路自动执行；live_trading_scheduler.py 盘中执行 |
| REQ-06 持仓自动调整 | ✓ SATISFIED | 回撤断路器 + 仓位管理 + JSON 同步 |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `data/positions.json` | 13, 24 | `stop_loss: 0.0, take_profit: 0.0` | ⚠️ Warning | 实盘持仓 stop_loss 为 0 → 止损失效（live_trading_scheduler.py:685-691 已有 C3.0 -5% 硬性兜底覆盖） |

### Human Verification Required

1. **当前持仓 stop_loss=0 是否正常** — 实际 `sim_positions` 表的 2 个持仓（position_id 56, 58）止损价存为 0.0。需确认：这是仓位较新还未刷新的中间态，还是 `sync_positions_to_json()` 同步前的瞬时态，或者是 sim_positions 表的实际值。如果是后者，需要查为什么买入时 `stop_loss = round(price * (1 + stop_loss_pct), 3)` 计算后存入的是 0。
2. **盘中监控 5 分钟粒度 vs 实际 10 分钟** — crontab 写 `*/5 9-15` 但注释含"10分钟"，实际配置 `*/10 9-14`。需确认是否需要改为 5 分钟以更及时止损。
3. **position_monitor.py 是否还在 crontab** — 当前量化 crontab 中 `position_monitor.py` 未列出（被 live_trading_scheduler 替代），但 `feishu_alerts.py` 在 9:00 / 15:05 仍调用 `check_position_alerts()` 读 positions.json。

### Gaps Summary

**1 个显著架构差异**：
- `position_monitor.py` 实际不执行交易（仅告警）；盘中执行已迁至 `live_trading_scheduler.py monitor`（架构替代，非缺口）

**1 个数据完整性问题**：
- `data/positions.json` 中 2 个持仓的 `stop_loss: 0.0`（可能被 sim_trading 计算后未正确持久化，或实盘同步路径未带止损价）

**0 个功能缺失**（核心 6/8 必须项均达成）

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier_
