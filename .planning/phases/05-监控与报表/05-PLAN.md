# Phase 5: 监控与报表 — 执行计划

## 问题陈述

现有 Web 仪表盘有 7 个面板（持仓、分析、策略、ML、模拟交易、回测、信号），全部使用**纯表格和文本**展示数据，无任何图表。模拟交易面板只显示当前快照（资金、持仓、最近交易），没有历史净值曲线和绩效指标。飞书日报只有逐笔持仓盈亏汇总，缺少月度表现和净值趋势。

用户通过模拟交易面板看到的只是"今天赚了多少"，无法回答三个核心问题：

1. **历史表现如何？** — 净值怎么变化的？收益曲线是平滑上升还是剧烈波动？
2. **风险指标怎样？** — 夏普比率多少？最大回撤发生在什么时候？
3. **月度稳定性如何？** — 哪个月份赚钱？哪个月份亏钱？收益是否均匀？

---

## Plan 1: 净值历史追踪 (REQ-13)

**目标：** 建立每日净值快照体系，为图表和绩效计算提供数据基础。

### 现状

`sim_trading.py` 的 `daily_scan()` 最后一步调用 `update_account_value()`，该函数计算总价值、盈亏、最大回撤并写入 `sim_account` 表。但只保留当前快照，没有历史序列，无法绘制净值曲线。

### 任务 1.1: daily_scan() 追加净值快照

**涉及文件：**

- `scripts/sim_trading.py`（约 +25 行，在 `update_account_value()` 调用的末尾或之后追加）

**改动：**

在 `daily_scan()` 函数末尾（第 1209 行 `update_account_value()` 之后，第 1212 行 `sync_positions_to_json()` 之前），追加以下逻辑：

```python
# 5.1 保存净值快照（供绩效看板使用）
_save_nav_snapshot(total_value=account_data_after_update, cash=..., holdings_value=...)
```

新建辅助函数 `_save_nav_snapshot()`：

```python
def _save_nav_snapshot(total_value, cash, holdings_value):
    """Append a daily NAV snapshot to data/nav_history.json"""
    import json
    from pathlib import Path
    nav_path = Path(__file__).parent.parent / "data" / "nav_history.json"
    
    # Re-read account for latest values (after update_account_value)
    account = get_account()
    if not account:
        return
    
    snapshot = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_value": float(account["total_value"]),
        "cash": float(account["cash"]),
        "holdings_value": float(account["total_value"]) - float(account["cash"]),
        "profit_pct": round(float(account["profit_pct"]) * 100, 2),
        "max_drawdown": round(float(account["max_drawdown"]) * 100, 2),
        "peak_value": float(account["peak_value"]),
    }
    
    # Load existing, append, save
    history = []
    if nav_path.exists():
        with open(nav_path) as f:
            history = json.load(f)
    
    # Avoid duplicate entries for the same date (idempotent)
    history = [h for h in history if h["date"] != snapshot["date"]]
    history.append(snapshot)
    history.sort(key=lambda x: x["date"])
    
    with open(nav_path, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    
    logger.info("📊 净值快照已保存: %s total=%.2f pct=%.2f%%", 
                snapshot["date"], snapshot["total_value"], snapshot["profit_pct"])
```

**设计要点：**

- 去重：同一天多次运行 `daily_scan()` 不会产生重复记录（用 date 字段判断，后运行覆盖先运行）
- 每次手动运行也记录 — 不只在 crontab 调度下工作
- 文件不存在时自动初始化
- 数据格式固定，后续可被任意消费方读取

**数据格式示例：**

```json
[
  {"date": "2026-04-28", "total_value": 100500.00, "cash": 85000.00, "holdings_value": 15500.00, "profit_pct": 0.50, "max_drawdown": 0.00, "peak_value": 100500.00},
  {"date": "2026-04-29", "total_value": 101200.00, "cash": 70000.00, "holdings_value": 31200.00, "profit_pct": 1.20, "max_drawdown": 0.00, "peak_value": 101200.00},
  {"date": "2026-04-30", "total_value": 100800.00, "cash": 70000.00, "holdings_value": 30800.00, "profit_pct": 0.80, "max_drawdown": 0.40, "peak_value": 101200.00}
]
```

**成功标准：** `python3 scripts/sim_trading.py scan` 执行后，`data/nav_history.json` 存在且有正确格式的数据。连续运行两次不会产生重复日期记录。

### 任务 1.2: API 端点提供净值历史

**涉及文件：**

- `quant_app/routes/dashboard.py`（+约 25 行，新增路由）

**改动：** 在 dashboard 路由文件中新增一个端点，返回 NAV 历史 JSON：

```python
@router.get("/api/sim/nav_history")
async def get_nav_history(request: FastAPIRequest, token: str = Cookie(None)):
    """返回模拟账户的净值历史"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    
    nav_path = DATA_DIR / "nav_history.json"
    if not nav_path.exists():
        return {"history": [], "count": 0}
    
    with open(nav_path) as f:
        history = json.load(f)
    
    return {"history": history, "count": len(history)}
```

**设计要点：**

- 路径复用 `DATA_DIR`（已在文件顶部的 `BASE_DIR / "data"`）
- 返回对象带 `count` 字段方便前端判断是否有数据
- 空文件 / 不存在时优雅处理

**成功标准：** `curl http://localhost:5001/api/sim/nav_history` 返回包含历史记录的 JSON 数组。

---

## Plan 2: 绩效看板页面 (REQ-14)

**目标：** 在仪表盘新增"绩效看板"面板，展示累计收益、年化收益、夏普比率、最大回撤、月度收益表等核心指标。不使用图表库，纯卡片 + 表格。

### 现状

模拟交易面板（`#panel-sim`）只显示当前快照的三个数字（可用资金、持仓市值、浮动盈亏）和持仓表格。没有任何衍生指标（夏普、年化收益、盈亏比、月度收益）。胜率追踪面板（`#panel-track`）有独立的推荐胜率统计，但与模拟交易账户数据不关联。

### 任务 2.1: 绩效汇总 API 端点

**涉及文件：**

- `quant_app/routes/dashboard.py`（+约 60 行，新增路由）

**改动：** 新增端点 `/api/sim/performance_summary`，从 NAV 历史 + sim_account 表计算衍生指标：

```python
@router.get("/api/sim/performance_summary")
async def get_performance_summary(request: FastAPIRequest, token: str = Cookie(None)):
    """返回模拟账户绩效汇总指标"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    
    # 1. 读取 NAV 历史
    nav_path = DATA_DIR / "nav_history.json"
    if not nav_path.exists():
        return {"error": "无净值历史数据"}
    
    with open(nav_path) as f:
        history = json.load(f)
    
    if len(history) < 2:
        return {"error": "净值数据不足（至少需要 2 个交易日）"}
    
    # 2. 读取当前账户快照
    from sim_trading import get_account
    account = get_account()
    if not account:
        return {"error": "无账户数据"}
    
    # 3. 计算衍生指标
    first_val = history[0]["total_value"]
    last_val = history[-1]["total_value"]
    total_return_pct = (last_val - first_val) / first_val * 100
    
    # 交易日数（自然日用于年化估算）
    try:
        first_date = datetime.strptime(history[0]["date"], "%Y-%m-%d")
        last_date = datetime.strptime(history[-1]["date"], "%Y-%m-%d")
        days = (last_date - first_date).days or 1
        years = days / 365.0
        annual_return = ((1 + total_return_pct / 100) ** (1 / years) - 1) * 100
    except Exception:
        annual_return = 0
        years = 0
    
    # 日收益率序列 → 夏普比率
    daily_returns = []
    for i in range(1, len(history)):
        prev = history[i-1]["total_value"]
        curr = history[i]["total_value"]
        if prev > 0:
            daily_returns.append((curr - prev) / prev)
    
    sharpe = 0
    if len(daily_returns) > 1:
        avg_ret = np.mean(daily_returns) if 'numpy' in dir() else (sum(daily_returns) / len(daily_returns))
        std_ret = (sum((r - avg_ret)**2 for r in daily_returns) / (len(daily_returns)-1)) ** 0.5
        if std_ret > 0:
            sharpe = (avg_ret / std_ret) * (252 ** 0.5)  # 年化夏普
    
    # 最大回撤（取 NAV 历史中的最大值）
    max_dd = float(account.get("max_drawdown", 0)) * 100
    
    # 月度收益表
    monthly = {}
    for snap in history:
        d = snap["date"][:7]  # "2026-04"
        if d not in monthly:
            monthly[d] = {"start": snap["total_value"], "end": snap["total_value"]}
        else:
            monthly[d]["end"] = snap["total_value"]
    monthly_returns = []
    for month, vals in sorted(monthly.items()):
        if vals["start"] > 0:
            ret = (vals["end"] - vals["start"]) / vals["start"] * 100
        else:
            ret = 0
        monthly_returns.append({"month": month, "return_pct": round(ret, 2)})
    
    # 盈亏比
    win_count = int(account.get("win_count", 0))
    trade_count = int(account.get("trade_count", 0))
    loss_count = trade_count - win_count
    profit_factor = 0
    if loss_count > 0 and win_count > 0:
        profit_factor = round(win_count / loss_count, 2)
    
    return {
        "total_return_pct": round(total_return_pct, 2),
        "annual_return_pct": round(annual_return, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate": round(float(account.get("win_rate", 0)) * 100, 2),
        "profit_factor": profit_factor,
        "trade_count": trade_count,
        "current_cash": float(account.get("cash", 0)),
        "holding_value": float(account.get("total_value", 0)) - float(account.get("cash", 0)),
        "total_value": float(account.get("total_value", 0)),
        "nav_days": len(history),
        "monthly_returns": monthly_returns,
        "last_update": history[-1]["date"] if history else "",
    }
```

**注意：** 如果 `numpy` 未导入，用纯 Python 替代标准差计算（上述 `avg_ret` 计算用纯 Python 的列表求和代替了 numpy）。确认 dashboard.py 顶部是否已 `import numpy as np`，如无则用纯 Python 实现，不添加 numpy 依赖。

**成功标准：** `curl http://localhost:5001/api/sim/performance_summary` 返回包含所有关键指标的 JSON。

### 任务 2.2: 前端绩效看板面板

**涉及文件：**

- `templates/index.html`（+约 70 行 HTML + 60 行 JS，surgical 增补）

**改动 A — 侧边栏添加按钮（约 +1 行）：**

在第 314 行附近（侧边栏按钮组），在"📊 胜率追踪"按钮之后追加：

```html
<button onclick="showPanel('perf',event)" aria-label="绩效看板">📈 绩效看板</button>
```

**改动 B — 新增面板 HTML（约 +40 行）：**

在 `#panel-sim`（模拟交易面板）之后、`#panel-ai_sim` 之前插入新面板：

```html
<!-- 绩效看板 -->
<div id="panel-perf" class="panel">
    <h3 class="section-title">📈 绩效看板 — 模拟账户</h3>
    <div style="margin-bottom:16px; display:flex; gap:8px;">
        <button class="refresh-btn" onclick="loadPerformanceSummary()">🔄 刷新</button>
    </div>
    <div id="perfSummary">
        <div class="loading"><span class="spinner"></span>正在加载绩效数据...</div>
    </div>
    <div id="perfMonthly" style="margin-top:16px;"></div>
    <div id="perfNavNote" style="margin-top:16px; color:#8892b0; font-size:12px;"></div>
</div>
```

**改动 C — JS 加载函数（约 +60 行）：**

在 JS 部分（约第 1811 行附近），`loadSimAccount()` 函数之后新增：

```javascript
// ==================== 绩效看板 ====================
async function loadPerformanceSummary() {
    const el = document.getElementById('perfSummary');
    const monthlyEl = document.getElementById('perfMonthly');
    const noteEl = document.getElementById('perfNavNote');
    el.innerHTML = getLoadingHTML('正在加载绩效指标...');
    monthlyEl.innerHTML = '';
    noteEl.innerHTML = '';
    try {
        const res = await fetch('/api/sim/performance_summary', {credentials: 'include'});
        const d = await res.json();
        if (d.error) {
            el.innerHTML = `<div class="error">${h(d.error)}</div>`;
            return;
        }
        // 指标卡片
        const cards = [
            {label:'累计收益率', value: d.total_return_pct + '%', color: d.total_return_pct >= 0 ? '#ef5350' : '#66bb6a'},
            {label:'年化收益率', value: d.annual_return_pct + '%', color: d.annual_return_pct >= 0 ? '#ef5350' : '#66bb6a'},
            {label:'夏普比率', value: d.sharpe_ratio, color: d.sharpe_ratio >= 1 ? '#4fc3f7' : '#ffa726'},
            {label:'最大回撤', value: '-' + d.max_drawdown_pct + '%', color: '#ef5350'},
            {label:'胜率', value: d.win_rate + '%', color: d.win_rate >= 50 ? '#66bb6a' : '#ef5350'},
            {label:'盈亏比', value: d.profit_factor, color: d.profit_factor >= 1 ? '#66bb6a' : '#ef5350'},
            {label:'交易次数', value: d.trade_count, color: '#8892b0'},
            {label:'净值天数', value: d.nav_days + '天', color: '#8892b0'},
        ];
        let html = '<div style="display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap;">';
        for (const c of cards) {
            html += `<div style="background:rgba(255,255,255,0.04); border-radius:8px; padding:14px 18px; border:1px solid rgba(255,255,255,0.08); min-width:120px; text-align:center;">
                <div style="color:#8892b0;font-size:12px;margin-bottom:4px;">${c.label}</div>
                <div style="font-size:22px;font-weight:bold;color:${c.color};">${c.value}</div>
            </div>`;
        }
        html += '</div>';
        html += '<div style="display:flex; gap:16px; flex-wrap:wrap; margin-bottom:12px;">';
        html += `<div style="color:#8892b0;font-size:12px;">可用资金: <span style="color:#e6f1ff;font-weight:bold;">${(d.current_cash||0).toFixed(2)}</span> 元</div>`;
        html += `<div style="color:#8892b0;font-size:12px;">持仓市值: <span style="color:#e6f1ff;font-weight:bold;">${(d.holding_value||0).toFixed(2)}</span> 元</div>`;
        html += `<div style="color:#8892b0;font-size:12px;">账户总值: <span style="color:#e6f1ff;font-weight:bold;">${(d.total_value||0).toFixed(2)}</span> 元</div>`;
        html += `<div style="color:#8892b0;font-size:12px;">数据更新: ${h(d.last_update||'')}</div>`;
        html += '</div>';
        el.innerHTML = html;

        // 月度收益表
        if (d.monthly_returns && d.monthly_returns.length > 0) {
            let mhtml = '<h4 style="margin-bottom:8px;">月度收益</h4>';
            mhtml += '<table style="width:auto;font-size:13px;"><thead><tr><th style="padding:4px 16px 4px 0;text-align:left;">月份</th><th style="padding:4px 16px;text-align:right;">收益率</th></tr></thead><tbody>';
            for (const m of d.monthly_returns) {
                const mc = m.return_pct >= 0 ? '#ef5350' : '#66bb6a';
                const arrow = m.return_pct >= 0 ? '▲' : '▼';
                mhtml += `<tr><td style="padding:4px 16px 4px 0;">${m.month}</td><td style="padding:4px 16px;text-align:right;color:${mc};">${arrow} ${m.return_pct.toFixed(2)}%</td></tr>`;
            }
            mhtml += '</tbody></table>';
            monthlyEl.innerHTML = mhtml;
        }

        noteEl.innerHTML = `<span style="color:#8892b0;">夏普比率≥1 表示风险调整后收益良好。盈亏比≥1 表示平均盈利大于平均亏损。最大回撤越低越好。</span>`;
    } catch(e) {
        el.innerHTML = `<div class="error">加载绩效看板失败: ${h(e.message)}</div>`;
    }
}
```

**改动 D — 面板加载钩子：** 在 `showPanel()` 函数中（约第 770 行），`panel.classList.add('active')` 之后，增加 `if (name === 'perf') loadPerformanceSummary();` 的调用。

**成功标准：** 点击侧边栏"📈 绩效看板"按钮，面板展示 8 张指标卡片、资金余额行、月度收益表。夏普比率、年化收益率等衍生指标正确计算。

---

## Plan 3: 飞书日报增强 (REQ-13 enhancement)

**目标：** 增强收盘日报的内容，加入净值趋势、月度表现对比和回撤预警。

### 现状

`send_daily_report()` 函数（`scripts/feishu_alerts.py` 第 310 行）目前只输出：

```
📋 收盘日报（2026-05-05）
━━━━━━━━━━━━━━━━━━━━━━━━━━
  🟢 中信证券（SH600030）
     成本: 22.50  收盘: 23.80
     当日: +3.20%  累计: +5.78%
     盈亏: +650.00元
  ...
━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 当日盈亏汇总: 🟢 盈利 +1250.00元
```

缺少：净值趋势、月度表现、回撤预警、账户总值。

### 任务 3.1: 日报加入净值趋势和月度表现

**涉及文件：**

- `scripts/feishu_alerts.py`（约 +40 行，在 `send_daily_report()` 中增强）

**改动：** 在 `send_daily_report()` 函数中，读取 `data/nav_history.json`，在消息末尾追加：

```python
# 追加净值趋势和月度表现
nav_path = BASE_DIR / "data" / "nav_history.json"
if nav_path.exists():
    with open(nav_path) as f:
        nav_history = json.load(f)
    if len(nav_history) >= 2:
        nav_history.sort(key=lambda x: x["date"])
        latest = nav_history[-1]
        prev = nav_history[-2] if len(nav_history) >= 2 else None
        week_ago = nav_history[-min(5, len(nav_history))] if len(nav_history) >= 5 else nav_history[0]
        
        msg += "\n\n" + "━" * 28
        msg += "\n📊 净值追踪"
        msg += f"\n  当前净值: {latest['total_value']:.2f}元 ({latest['profit_pct']:+.2f}%)"
        msg += f"\n  最大回撤: {latest['max_drawdown']:.2f}%"
        if prev:
            day_chg = latest['total_value'] - prev['total_value']
            msg += f"\n  较上日: {day_chg:+.2f}元"
        if week_ago and week_ago != latest:
            week_chg = latest['total_value'] - week_ago['total_value']
            week_pct = (week_chg / week_ago['total_value']) * 100
            msg += f"\n  较5日前: {week_chg:+.2f}元 ({week_pct:+.2f}%)"
        
        # 月度收益
        monthly = {}
        for snap in nav_history:
            month = snap["date"][:7]
            if month not in monthly:
                monthly[month] = {"start": snap["total_value"], "end": snap["total_value"]}
            else:
                monthly[month]["end"] = snap["total_value"]
        
        msg += "\n\n📅 月度收益"
        for month, vals in sorted(monthly.items()):
            ret = (vals["end"] - vals["start"]) / vals["start"] * 100
            arrow = "🟢" if ret >= 0 else "🔴"
            msg += f"\n  {month} {arrow} {ret:+.2f}%"
```

**成功标准：** 收盘飞书日报包含净值追踪区块（当前净值、最大回撤、较上日变化、较 5 日前变化）和月度收益列表。

### 任务 3.2: 回撤预警

**涉及文件：**

- `scripts/feishu_alerts.py`（约 +15 行，在日报末尾追加预警）

**改动：** 读取 NAV 历史判断当前是否处于回撤状态，如回撤超过阈值则追加预警：

```python
# 回撤预警
if len(nav_history) >= 3:
    peak_30d = max(h["total_value"] for h in nav_history[-30:]) if len(nav_history) >= 30 else max(h["total_value"] for h in nav_history)
    current_val = nav_history[-1]["total_value"]
    dd_from_peak = (peak_30d - current_val) / peak_30d * 100
    current_dd = nav_history[-1]["max_drawdown"]
    
    if dd_from_peak > 5:
        msg += f"\n\n⚠️ 回撤预警"
        msg += f"\n  近30日峰值: {peak_30d:.2f}元"
        msg += f"\n  当前回撤: {dd_from_peak:.2f}%"
        msg += f"\n  历史最大回撤: {current_dd:.2f}%"
        if dd_from_peak > 10:
            msg += "\n  ⚠️⚠️ 回撤超过10%，建议检查策略运行状态"
```

**预警阈值设计：**

| 回撤幅度 | 预警级别 | 颜色/符号 |
|---------|---------|----------|
| 5% - 10% | 注意 | ⚠️ |
| > 10% | 警告 | ⚠️⚠️ |

**成功标准：** 当净值从近期高点回撤超过 5% 时，日报末尾追加回撤预警区块。

---

## 执行顺序

```
Plan 1 (净值历史)
  └── 任务 1.1: sim_trading.py 追加 _save_nav_snapshot()
  └── 任务 1.2: dashboard.py 新增 /api/sim/nav_history

Plan 2 (绩效看板)
  └── 任务 2.1: dashboard.py 新增 /api/sim/performance_summary
       └── 依赖 1.2 的 nav_history 数据源
  └── 任务 2.2: index.html 新增面板 HTML + JS
       └── 依赖 2.1 的 API

Plan 3 (日报增强)
  └── 任务 3.1: feishu_alerts.py 增强日报
       └── 依赖 1.1 的 nav_history.json 文件
  └── 任务 3.2: feishu_alerts.py 回撤预警
       └── 依赖 3.1
```

**推荐执行顺序：** 1.1 → 1.2 → 2.1 → 2.2 → 3.1 → 3.2

**依赖关系：**
- Plan 2 依赖 Plan 1 的 `nav_history.json` 数据源（但不阻塞 — 在没有数据时 API 返回 `{"error": "无净值历史数据"}`，前端显示提示）
- Plan 3 依赖 Plan 1 的 `nav_history.json` 文件（`nav_history.json` 不存在时静态跳过，不影响日报原有内容）

---

## 涉及文件汇总

| 文件 | 操作 | Plan | 预估改动量 |
|------|------|------|-----------|
| `scripts/sim_trading.py` | 修改（追加 `_save_nav_snapshot` + 调用） | 1.1 | +30 行 |
| `data/nav_history.json` | 新建（由 1.1 自动生成） | 1.1 | — |
| `quant_app/routes/dashboard.py` | 修改（新增 `/api/sim/nav_history`） | 1.2 | +25 行 |
| `quant_app/routes/dashboard.py` | 修改（新增 `/api/sim/performance_summary`） | 2.1 | +60 行 |
| `templates/index.html` | 修改（侧边栏 + 面板 HTML + JS） | 2.2 | ~130 行 |
| `scripts/feishu_alerts.py` | 修改（日报增强 + 回撤预警） | 3.1, 3.2 | +55 行 |

---

## 回滚说明

- **任务 1.1**：移除 `_save_nav_snapshot()` 函数和 `daily_scan()` 中的调用即可
- **任务 1.2 / 2.1**：移除路由函数，`git checkout` 恢复 `dashboard.py`
- **任务 2.2**：移除面板 HTML 和 JS 函数，`git checkout` 恢复 `index.html`
- **任务 3.1 / 3.2**：移除新增代码块，`git checkout` 恢复 `feishu_alerts.py`
- **数据**：`data/nav_history.json` 可安全删除，系统不会报错（各消费方都做了文件不存在检查）

## 不做的事

- 不引入 ECharts / Chart.js 等图表库 — v1 纯卡片 + 表格
- 不修改现有模拟交易面板的功能或布局
- 不重构 `update_account_value()` 的内部逻辑
- 不迁移 `send_daily_report()` 的持仓数据源（保持从 `positions.json` 读取）
- 不添加持仓盈亏分布图、资金曲线图等可视化（留待 v2 图表化），不加 K 线图
- 不修改数据库中 `sim_account` 表的 schema
