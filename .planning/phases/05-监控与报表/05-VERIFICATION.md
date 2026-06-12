---
phase: 05-监控与报表
verified: 2026-06-12T16:50:00Z
status: gaps_found
score: 6/7 must-haves verified
re_verification: false
---

# Phase 5: 监控与报表 — 验证报告

**阶段目标：** 交易绩效可视化，关键指标一目了然。
**REQ 覆盖：** REQ-13（交易记录可视化）、REQ-14（绩效看板）
**验证状态：** gaps_found
**分数：** 6/7 must-haves 通过

## 总体评估

后端数据层和 API 层完整：`data/nav_history.json` 244 行（约 4 个月历史）、`/api/sim/nav_history` 和 `/api/sim/performance_summary` 端点工作正常、`feishu_alerts.py` 已加入净值追踪/月度收益/回撤预警 3 段增强。**但前端"绩效看板"面板未实现**：sidebar 缺按钮、HTML 缺 `panel-perf` div、`app.js` 缺 `loadPerformanceSummary` 函数（仅留有空的 section 注释）。这是一个后端就绪但 UI 未接管的半成品状态。

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | 每日净值快照自动保存 | ✓ VERIFIED | `sim_trading.py:_record_nav_snapshot()` line 1508-1525；daily_scan 末尾 line 1453 调用 |
| 2 | NAV 历史可通过 API 查询 | ✓ VERIFIED | `dashboard.py:47-58` `GET /api/sim/nav_history` 返回 history + count |
| 3 | 绩效汇总 API 计算总收益/年化/夏普/最大回撤/月度 | ✓ VERIFIED | `dashboard.py:59-115` `GET /api/sim/performance_summary` 含 total_return/annual_return/sharpe/max_drawdown/trade_count/monthly_returns/nav_dates/nav_values |
| 4 | 飞书收盘日报含净值追踪 | ✓ VERIFIED | `feishu_alerts.py:342-389` 净值追踪段（当前净值/最大回撤/较上日/较 5 日前） |
| 5 | 飞书收盘日报含月度收益 | ✓ VERIFIED | `feishu_alerts.py:376-388` 月度收益段（按 YYYY-MM 聚合，🟢/🔴 标识） |
| 6 | 飞书收盘日报含回撤预警 | ✓ VERIFIED | `feishu_alerts.py:390-393` 回撤预警段（>10% 触发 ⚠️⚠️） |
| 7 | 前端"绩效看板"面板可显示 8 张指标卡片 | ✗ FAILED | `templates/index.html` 第 57-66 行 sidebar 无"📈 绩效看板"按钮；HTML 主体无 `id="panel-perf"`；`static/js/app.js` 第 933 行只有空 section 注释，无 `loadPerformanceSummary` 实现 |

**Score:** 6/7 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `sim_trading.py:_record_nav_snapshot` | 净值快照函数 | ✓ VERIFIED | line 1508-1525，含去重（同 date 覆盖） |
| `data/nav_history.json` | 净值历史 | ✓ VERIFIED | 244 行，时间跨度 2026-05-05 至约 2026-09-04（~4 个月）；含 date/total_value/cash/holdings_value/profit_pct/max_drawdown/peak_value |
| `quant_app/routes/dashboard.py:/api/sim/nav_history` | NAV API | ✓ VERIFIED | line 47-58 |
| `quant_app/routes/dashboard.py:/api/sim/performance_summary` | 绩效 API | ✓ VERIFIED | line 59-115，纯 Python 实现 std（不依赖 numpy） |
| `scripts/feishu_alerts.py:净值追踪` | 飞书净值段 | ✓ VERIFIED | line 342-373 |
| `scripts/feishu_alerts.py:月度收益` | 飞书月度段 | ✓ VERIFIED | line 376-388 |
| `scripts/feishu_alerts.py:回撤预警` | 飞书预警段 | ✓ VERIFIED | line 390-393 |
| `templates/index.html:panel-perf` | 前端绩效面板 | ✗ MISSING | sidebar 无按钮、HTML 缺 div、app.js 缺函数 |
| `static/js/app.js:loadPerformanceSummary` | 前端加载函数 | ✗ MISSING | 仅留 `// ==================== 绩效看板 ====================` 空注释（line 933），无函数体 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `sim_trading.py:daily_scan()` | `_record_nav_snapshot()` | 函数末尾调用 | ✓ WIRED | line 1453（实际是 line 1433 附近 `sync_positions_to_json()` 之后 `7. 记录净值快照 _record_nav_snapshot()`） |
| `sim_trading.py:_record_nav_snapshot` | `data/nav_history.json` | `json.dump` | ✓ WIRED | line 1523-1524 |
| `dashboard.py:get_nav_history` | `data/nav_history.json` | `open + json.load` | ✓ WIRED | line 53-55 |
| `dashboard.py:get_performance_summary` | `data/nav_history.json` | `open + json.load` | ✓ WIRED | line 64-65 |
| `dashboard.py:performance_summary` | 计算 annual_return | 复利年化公式 | ✓ VERIFIED | line 78 `((1 + total_return / 100) ** (365 / days) - 1) * 100` |
| `dashboard.py:performance_summary` | 计算 sharpe_ratio | 年化夏普 | ✓ VERIFIED | line 88 `(avg_ret / std) * (252**0.5) if std > 0 else 0` |
| `dashboard.py:performance_summary` | 计算 monthly_returns | 月度聚合 | ✓ VERIFIED | line 91-99 |
| `feishu_alerts.py:send_daily_report` | `data/nav_history.json` | `open + json.load` | ✓ WIRED | line 343-345 |
| `index.html` | `/api/sim/performance_summary` | `fetch` 调用 | ✗ NOT_WIRED | 前端未调用此 API |

### Requirements Coverage

| Requirement | Status | Blocking Issue |
|-------------|--------|----------------|
| REQ-13 交易记录可视化 | ✓ SATISFIED | NAV 历史 + 飞书日报三段增强（净值/月度/回撤）均完整 |
| REQ-14 绩效看板 | ⚠️ PARTIAL | 后端 API 完整；前端面板未实现，用户无法在 Web 端看到 8 张指标卡片 |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `static/js/app.js` | 933 | 空 section header `// ==================== 绩效看板 ====================` 注释下方无函数 | 🛑 Blocker | 用户在 UI 看不到绩效数据；API 已就绪但未接线 |
| `templates/index.html` | 57-66 | sidebar 缺 "📈 绩效看板" 按钮 | 🛑 Blocker | 用户入口缺失 |
| `templates/index.html` | 73-215 | 缺 `id="panel-perf"` div | 🛑 Blocker | 即使加上按钮也无面板可显示 |

### Human Verification Required

1. **API 实际可访问性** — 后端 API 完整存在，但前端未接线。需确认：用户当前是否通过其他途径（直接 curl、Postman、Feishu 日报）访问绩效数据？该需求是否实际有用户在使用？
2. **飞书日报增强的视觉确认** — `feishu_alerts.py:342-393` 三段增强是否在最近一次收盘推送中正常展示？需要看 `logs/feishu_alerts.log` 或实际飞书消息。
3. **nav_history.json 数据连续性** — 244 行数据，但日期范围需要核对是否覆盖连续交易日，有无缺失。

### Gaps Summary

**1 个显著缺口**（REQ-14 半成品）：

**前端"绩效看板"面板缺失**：
- `templates/index.html` sidebar 缺"📈 绩效看板"按钮（约 1 行 HTML）
- 缺 `<div id="panel-perf">` 面板（约 40 行 HTML）
- 缺 `loadPerformanceSummary()` JS 函数（约 60 行 JS）
- `static/js/app.js` line 933 留有空的 section header 注释，说明**开发者已规划位置但未实现函数体**

**修复路径**（明确）：
1. 在 `index.html` line 66 之后插入 `<button onclick="loadPerformanceSummary(); showPanel('perf',event)">📈 绩效看板</button>`
2. 在 `index.html` line 215 之后插入 `<div id="panel-perf" class="panel">...</div>`
3. 在 `app.js` line 933 之后插入 `loadPerformanceSummary()` 函数（约 60 行）
4. 在 `showPanel()` 函数（约 `app.js:135`）末尾加 `if (name === 'perf') loadPerformanceSummary();`

**2 个非阻塞观察**：
- `data/nav_history.json` 244 行数据完整，API 响应正常
- 飞书日报三段增强（净值/月度/回撤）实现完整，可在 `send_daily_report` 触发时验证

---

_Verified: 2026-06-12T16:50:00Z_
_Verifier: gsd-verifier_
