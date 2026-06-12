---
phase: 03-风控体系完善
verified: 2026-06-12T16:50:00Z
status: gaps_found
score: 6/8 must-haves verified
re_verification: false
---

# Phase 3: 风控体系完善 — 验证报告

**阶段目标：** 系统能在无人值守时自动控制风险。
**REQ 覆盖：** REQ-07（自动化止损止盈）、REQ-08（仓位管理）、REQ-09（市场状态判断）
**验证状态：** gaps_found
**分数：** 6/8 must-haves 通过

## 总体评估

风险控制的三层基础（市场状态识别 + 参数动态应用 + 配置文件）在 `market_state.py`、`sim_trading.py:get_market_params()`、`quant_app/utils/risk_config.py` 中完整实现。盘中自动止损止盈功能由 `live_trading_scheduler.py monitor` 承担（54KB，含 ATR 动态止损、硬性兜底、移动止盈），架构上比计划更先进。但计划中"`position_monitor.py` 接入市场状态"的任务没有执行（因为 position_monitor 已被新调度器替代）。

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 止损止盈 5 分钟内自动检测并执行 | ⚠️ PARTIAL | 实际由 `live_trading_scheduler.py monitor`（crontab `*/10 9-14`）执行，10 分钟间隔；含 ATR 动态止损、硬性 -5% 兜底、ATR 移动止盈 |
| 2 | 仓位根据市场状态自动调整 | ✓ VERIFIED | `sim_trading.py:get_market_params()` line 81-104 返回 `max_positions/per_position_pct/drawdown_circuit_breaker`，买入时按市场状态参数计算 |
| 3 | 市场状态切换时策略参数自适应 | ✓ VERIFIED | `market_state.py:get_market_state()` 5 种状态（trend_up/trend_down/range/panic/overheated），每种有独立 stop_loss_pct/take_profit_pct/max_positions/ml_threshold，`_get_strategy_params()` line 299-340 |
| 4 | 风控参数可通过配置文件修改 | ✓ VERIFIED | `data/risk_config.json` 28 行含 circuit_breaker/position_sizing/market_state_overrides 三段；`quant_app/utils/risk_config.py` 35 行提供 `load_risk_config()` 等函数 |
| 5 | 关键风控参数按"市场状态 > risk_config.json > 代码默认值"优先级 | ✓ VERIFIED | `sim_trading.py:get_market_params()` 先取 `_p.get('stop_loss_pct', -3)/100`（市场状态优先），失败时回退 -0.03（默认值） |
| 6 | 配置缺失时优雅降级 | ✓ VERIFIED | `risk_config.py:load_risk_config()` 文件不存在时返回 `{"circuit_breaker": {"max_drawdown_pct": -15, "enabled": True}}` |
| 7 | `position_monitor.py` 接入市场状态参数 | ✗ FAILED | 当前 `position_monitor.py`（273 行）完全不引用 `market_state`/`risk_config`；它是被新架构替代的简化告警脚本 |
| 8 | crontab 覆盖 9:30-15:00 完整窗口 | ⚠️ PARTIAL | 实际配置 `*/10 9-14`（最迟到 14:50），未覆盖 14:50-15:00 收盘前 10 分钟；comment 写 `9:30-15:00` 但 cron 行是 9-14 |

**Score:** 6/8 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `quant_app/utils/risk_config.py` | 配置加载器 | ✓ VERIFIED | 35 行，含 `load_risk_config()` / `get_circuit_breaker_config()` / `get_position_sizing_config()` / `get_market_state_override()` |
| `data/risk_config.json` | 配置文件 | ✓ VERIFIED | 28 行，三段结构：circuit_breaker/position_sizing/market_state_overrides |
| `market_state.py` | 市场状态识别 | ✓ VERIFIED | 355 行，5 种状态识别 + 参数返回 + 30 秒缓存 |
| `sim_trading.py:get_market_params()` | 动态参数获取 | ✓ VERIFIED | line 81-104，返回 state/state_name/stop_loss_pct/take_profit_pct/max_positions/ml_threshold/position_sizing_mode/per_position_pct/drawdown_circuit_breaker |
| `sim_trading.py:execute_sell/partial_sell` | 止损止盈执行 | ✓ VERIFIED | line 970/1058，含 partial sell 支持 |
| `live_trading_scheduler.py:cmd_monitor` | 盘中自动执行 | ✓ VERIFIED | 完整 ATR 动态止损 + 硬性兜底 + 移动止盈 + V11 盘中择时入场 |
| `position_monitor.py`（接入市场状态） | 计划要求 | ✗ MISSING | 当前是简化告警脚本，未接入 market_state |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `sim_trading.py:daily_scan()` | `get_market_params()` | 直接函数调用 | ✓ WIRED | line 91 `from market_state import get_market_state` |
| `sim_trading.py:execute_buy` | `_mp_exec['stop_loss_pct']` | 动态取参 | ✓ WIRED | line 938 `stop_loss = round(price * (1 + _mp_exec['stop_loss_pct']), 3)` |
| `sim_trading.py:回撤断路器` | `get_market_params()['drawdown_circuit_breaker']` | 局部变量 | ⚠️ PARTIAL | line 1349-1352 用的是模块级常量 `DRAWDOWN_CIRCUIT_BREAKER = -0.15`，未从 `get_market_params()` 取（虽然值相同） |
| `sim_trading.py:回测输出止损` | `get_market_params()` | 函数调用 | ✓ WIRED | line 430 `round(best.get('现价', 0) * (1 + get_market_params()['stop_loss_pct']), 2)` |
| `live_trading_scheduler.py:cmd_monitor` | executor | 同步/异步下单 | ✓ WIRED | line 700+ 实际触发 `_executor_market_sell_and_record` |
| `crontab` | `live_trading_scheduler.py monitor` | `*/10 9-14 * * 1-5` | ✓ WIRED | line ~85 `*/10 9-14 * * 1-5 cd $QUANT_DIR && $PYTHON3 scripts/live_trading_scheduler.py monitor` |

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| REQ-07 自动化止损止盈 | ⚠️ PARTIAL | 已由 live_trading_scheduler.py 实现（10分钟间隔），但 crontab 未覆盖 14:50-15:00 收盘前窗口 |
| REQ-08 仓位管理 | ✓ SATISFIED | 市场状态参数动态生效 + risk_config.json 可配 + 单仓 30% 上限 |
| REQ-09 市场状态判断 | ✓ SATISFIED | market_state.py 5 状态 + sim_trading.py 完整接入 |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|---------|
| `sim_trading.py` | 1349-1352 | 用模块级常量 `DRAWDOWN_CIRCUIT_BREAKER` 而非 `get_market_params()['drawdown_circuit_breaker']` | ℹ️ Info | 当前值与默认值一致（-0.15），不影响功能；但回撤阈值没走动态路径，与 plan 设计有偏差 |

### Human Verification Required

1. **crontab 9-14 vs 9-15** — 实际配置 `*/10 9-14` 意味着 14:50 是最后一次扫描，14:50-15:00 收盘前 10 分钟的极端行情无保护。需确认这是否是可接受的窗口。
2. **position_monitor.py 是否还需要保留** — 当前是 273 行的"告警脚本"，没有被 crontab 调度，但 `feishu_alerts.py` 仍会读 `data/positions.json` 触发告警。需确认这个分层设计是否符合预期。
3. **market_state 缓存 30 秒** — `market_state.py:_STATE_CACHE_TTL = 30`，但 `cmd_monitor` 每 10 分钟跑一次，缓存实际不影响。需确认 sim_trading.py 17:30 跑时是否能拿到当日最新状态。

### Gaps Summary

**2 个非阻塞性缺口**：
1. **`position_monitor.py` 未接入 market_state**（计划任务 2.2/2.3）— 该脚本被 `live_trading_scheduler.py` 架构替代，但代码中确实缺失市场状态引用。修复路径：要么删除该文件，要么补 market_state 集成并保留为告警脚本。
2. **crontab 14:50-15:00 窗口缺失**（计划任务 1.3）— 改为 `*/5 9-15` 即可，与 feishu_alerts 的 15:05 收盘推送对齐。

**2 个架构优势**（计划外）：
- `live_trading_scheduler.py` 提供了 ATR 动态止损、硬性 -5% 兜底、移动止盈等高级能力，超越原计划
- `data/risk_config.json` 的 `market_state_overrides` 段（panic → max_positions=1, stop_loss_pct=-2）实现了"特定状态强制覆盖"能力

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier_
