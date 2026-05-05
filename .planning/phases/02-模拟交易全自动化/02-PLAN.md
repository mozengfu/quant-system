# Phase 2 — 模拟交易全自动化

**目标：** 模拟交易从选股到调仓全流程自动运行，无需每日人工触发。

**包含需求：** REQ-04（自动选股扫描）, REQ-05（自动下单执行）, REQ-06（持仓自动调整）

---

## 当前状态与关键差距

### 现状摘要

| 维度 | 当前状态 | 差距 |
|------|---------|------|
| 选股扫描 | `sim_trading.py daily_scan()` 自动运行，但 `v4_scan()` 函数无 CLI 入口 | REQ-04 部分完成 |
| 止损 | `sim_trading.py` 自动执行 -3% 止损 | ✅ |
| 止盈 | 仅 advisory log，无自动执行 | REQ-05 缺失 |
| 超时卖出 | 仅 advisory log（>5天提示），无自动执行 | REQ-05 缺失 |
| 仓位管理 | `cash / available_slots` 硬编码平均分配 | REQ-06 缺失 |
| 回撤断路器 | 不存在 | REQ-06 缺失 |
| `sim_signals` 表 | 5 处 INSERT/SELECT/UPDATE，但无 CREATE TABLE | 静默失败 |
| 数据源 | `position_monitor.py` 和 `feishu_alerts.py` 读写 `data/positions.json`，`sim_trading.py` 读写 MySQL | 双数据源可漂移 |
| 告警阈值 | `feishu_alerts.py` 止损 -5%（与 sim_trading 的 -3% 不一致） | 阈值不统一 |
| `position_monitor.py` | 引用了 `tp1` / `tp2` 变量但未定义，会 NameError | bug |
| 实时行情 | `sim_trading.py` 自建腾讯财经调用，`position_monitor.py`/`feishu_alerts.py` 用 `alicloud_api.py` | 三个不同的行情源 |

---

## Plan 1: 模拟交易引擎完善 (REQ-05, REQ-06)

### Objective

`sim_trading.py` 是模拟交易的核心引擎，但存在几个关键缺口：止盈和超时只打印日志不执行、仓位只会平均分配、无回撤断路器、`sim_signals` 表缺少建表语句。本计划修复这些核心缺口，使引擎具备自动闭环能力。

### Tasks

#### Task 1.1: 创建 sim_signals 表

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

在 `create_tables()` 函数中（第 362 行 `sim_positions` 建表之后），添加 `sim_signals` 表的 CREATE TABLE：

```sql
CREATE TABLE IF NOT EXISTS sim_signals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    signal_type VARCHAR(20) NOT NULL COMMENT '信号类型: 买入/止损/止盈/超时',
    ts_code VARCHAR(20) NOT NULL,
    stock_name VARCHAR(50) NOT NULL,
    price DECIMAL(8,3) NOT NULL,
    shares INT NOT NULL DEFAULT 0,
    strategy VARCHAR(50) DEFAULT NULL COMMENT '策略来源',
    ml_prob DECIMAL(6,4) DEFAULT NULL,
    enhanced_score DECIMAL(8,2) DEFAULT NULL,
    market_state VARCHAR(20) DEFAULT NULL,
    reason VARCHAR(200) DEFAULT NULL,
    signal_date DATE NOT NULL,
    signal_time DATETIME NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT '已执行' COMMENT '已执行/持仓中/已平仓',
    close_price DECIMAL(8,3) DEFAULT NULL,
    close_date DATE DEFAULT NULL,
    pnl DECIMAL(10,2) DEFAULT NULL,
    pnl_pct DECIMAL(8,4) DEFAULT NULL,
    created_at DATETIME NOT NULL,
    INDEX idx_ts_code (ts_code),
    INDEX idx_signal_date (signal_date),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
```

注意：列定义必须与现有 `record_signal()` 和 `execute_sell()` 中的 INSERT/UPDATE 语句完全对齐。

**验证标准：**
- 运行 `python3 scripts/sim_trading.py init` 后，MySQL 中 `sim_signals` 表存在
- 已有的 `record_signal()` 和 `get_sim_account_info()` 功能正常执行，不再被异常捕获静默跳过

#### Task 1.2: 自动止盈和超时卖出

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

修改 `daily_scan()` 函数中第 894-914 行（现有的止盈/超时建议逻辑），将 `actions` 列表的 advisory log 替换为实际的 `execute_sell()` 调用：

**分级止盈规则（与现有阈值对齐）：**
- 现价 >= tp3_price（+18%）：自动清仓，reason="止盈清仓(+18%)"
- 现价 >= tp2_price（+10%）：自动卖出 1/3 持仓，reason="止盈减仓(+10%)"（注意：需要支持部分卖出）
- 现价 >= tp1_price（+6%）：自动卖出 1/3 持仓，reason="止盈减仓(+6%)"
- 以上优先级从高到低，触发高优先级后不再检查低优先级

**超时卖出规则（与现有阈值对齐）：**
- 持有天数 > 5 天且未触发上述止盈：自动清仓，reason="超时卖出(>5天)"
- 持有天数 > 5 天但已触发止盈减仓：在止盈减仓日志后附加超时提示（不重复卖出）

**支持部分卖出：** 当前 `execute_sell()` 只支持清仓。需要新增 `execute_partial_sell(position_id, shares, price, reason)` 函数，逻辑与 `execute_sell()` 一致但只卖指定股数：
- 减少持仓股数（shares）和总成本（total_cost = cost_price * 剩余股数）
- 资金按比例回退到账户
- 交易记录 action='SELL'，记录实际卖出的股数和盈亏

**验证标准：**
- `python3 scripts/sim_trading.py scan` 在持仓盈利 +6% 以上时自动触发减仓/清仓，不再只打印日志
- 日志中出现 `止盈减仓` / `止盈清仓` / `超时卖出` 关键字
- simulate 一个持有 6 天以上且未触发止盈的持仓，观察自动卖出

#### Task 1.3: 回撤断路器 + 可配置仓位管理

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

**A. 回撤断路器（在 `daily_scan()` 买入逻辑前插入）：**

在 `daily_scan()` 第 920 行（`# 3. ML策略选股买入`）之前，添加回撤检查：

```python
# 回撤断路器：总回撤超过 -15% 时暂停所有新买入
account = get_account()
if account:
    total_drawdown = float(account["profit_pct"])
    if total_drawdown < -0.15:
        logger.warning("回撤断路器触发: 总亏损 %.2f%% < -15%%，暂停新买入", total_drawdown * 100)
        # 跳过买入，但仍执行持仓刷新和账户更新
    else:
        # 正常买入逻辑
```

注意：回撤断路器和现有代码中的 `mkt_info['is_bear']` 是两层独立风控。熊市不建仓 + 回撤过大不建仓，两者或关系（任一条件触发就不买）。

**B. 可配置仓位管理：**

将硬编码的平均分配逻辑（第 964 行 `per_position = float(account["cash"]) / available_slots`）替换为可配置模式：

新增顶部配置参数：
```python
# 仓位管理
POSITION_SIZING_MODE = 'equal'       # 'equal' | 'weighted'
PER_POSITION_PCT = 0.30              # 单仓最大占比（30%现金）
```

- `equal` 模式：行为与当前一致，但增加 `PER_POSITION_PCT` 上限保护（单仓不超过现金的 30%）
- `weighted` 模式：按 ML 概率权重分配资金（概率越高仓位越大），当前阶段先不实现，保持 equal

修改买入逻辑：
```python
if POSITION_SIZING_MODE == 'equal':
    per_position = min(
        float(account["cash"]) / available_slots,
        float(account["cash"]) * PER_POSITION_PCT
    )
```

**验证标准：**
- 当 `profit_pct < -15%` 时，`daily_scan()` 跳过买入步骤，日志包含"回撤断路器"
- 单仓买入金额不超过现金的 30%
- 历史正常情况逻辑不变

### Success Criteria

- `python3 scripts/sim_trading.py init` 创建完整的 4 张表（含 sim_signals）
- `python3 scripts/sim_trading.py scan` 自动执行止损、止盈、超时卖出，不再只打印建议
- 回撤超过 -15% 时自动暂停新买入
- 单仓金额有上限保护

---

## Plan 2: 数据源统一与告警对齐 (REQ-04)

### Objective

当前 `position_monitor.py` 和 `feishu_alerts.py` 依赖 `data/positions.json`，而 `sim_trading.py` 使用 MySQL `sim_positions` 表。双数据源在独立更新时必然漂移。此外 `feishu_alerts.py` 使用 -5% 止损阈值与 sim_trading 的 -3% 不一致。本计划在不重写 position_monitor 的前提下统一数据源和对齐阈值。

### Tasks

#### Task 2.1: sim_trading 盘后扫描同步写入 positions.json

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

在 `daily_scan()` 函数末尾（`update_account_value()` 之后，第 1028 行），新增 `sync_positions_to_json()` 调用：

```python
# 6. 同步持仓到 positions.json（供 position_monitor / feishu_alerts 使用）
sync_positions_to_json()
```

新增函数 `sync_positions_to_json()`：
1. 从 MySQL `sim_positions` 表查询所有 status='HOLD' 的记录
2. 转换为 `data/positions.json` 的格式（与 position_monitor.py 读取的格式一致）
3. 写入 `data/positions.json`（覆盖写入）

转换映射：
```
sim_positions 字段 → positions.json 字段
ts_code          → code（去掉后缀，取纯数字部分）
market           → market（保持一致）
stock_name       → name
cost_price       → cost
shares           → shares
stop_loss        → stop_loss
take_profit      → take_profit
buy_date         → buy_date
current_price    → current_price（如有）
profit_loss      → float_pnl
profit_pct * 100 → float_pnl_pct
```

`data/positions.json` 的顶层结构保持现有格式：
```json
{
  "positions": [
    {
      "code": "000001",
      "market": "sz",
      "name": "平安银行",
      "cost": 12.50,
      "shares": 800,
      "stop_loss": 12.13,
      "take_profit": 13.25,
      "buy_date": "2026-05-04",
      "current_price": 12.80,
      "float_pnl": 240.0,
      "float_pnl_pct": 2.4,
      "day_pct": 1.2,
      "last_update": "2026-05-05 15:30:00"
    }
  ]
}
```

注意：此流程是"MySQL --> JSON"的单向同步，MySQL 是权威源。position_monitor.py 运行时会更新 positions.json 中的浮动盈亏字段，但不会写回 MySQL —— 这仍然存在盘中漂移风险，但比完全两个独立源要好。`daily_scan()` 每天盘后跑一次，覆盖 position_monitor 盘中写入的浮动盈亏，重置为 MySQL 的权威值。

**验证标准：**
- `python3 scripts/sim_trading.py scan` 执行后，`data/positions.json` 存在且内容与 MySQL sim_positions 一致
- `python3 scripts/position_monitor.py` 能正常读取后续写入的 positions.json

#### Task 2.2: 统一止损阈值为 -3%

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/feishu_alerts.py`

**A. `check_position_alerts()` 函数（第 166 行）：**

第 229 行止损判断 `if price <= stop_loss:` — 这里的 `stop_loss` 来自 positions.json 中的 `stop_loss` 字段。如果 Task 2.1 中同步的 stop_loss 已经使用 -3% 计算，则此处自动对齐。但仍然在函数顶部添加注释说明止损来源：

```python
# 注意：stop_loss 值来自 positions.json（由 sim_trading.py 按 -3% 计算写入），
# 此处不再覆盖计算。如果 positions.json 中没有 stop_loss 字段，则按 -3% 兜底。
```

同时在读取 pos 后添加兜底逻辑：
```python
if stop_loss <= 0:
    stop_loss = round(cost * 0.97, 2)  # 兜底 -3%
```

**B. `send_morning_alert()` 函数（第 31 行）：**

修改第 108-117 行的止损止盈参数获取逻辑。当前先从 market_state 获取，如果失败则兜底 -5%/+10%。改为：
- 签名：`-3%` 对应 `stop_loss_pct = -3`，`take_profit_pct = 10`
- 从 market_state 获取的 `stop_loss_pct` 如果为 -5，仍按 -5 使用（不同场景可以不同）
- 但兜底值从 `-5` 改为 `-3`，与 sim_trading 保持一致

具体改动：
```python
# 第 108 行：sl_pct = -5 → sl_pct = -3
# 第 109 行：tp_pct = 10 → tp_pct = 10 (不变)
```

**验证标准：**
- `feishu_alerts.py alert` 在持仓无 `stop_loss` 字段时按 -3% 计算止损
- `feishu_alerts.py morning` 的兜底止损从 -5% 变为 -3%

#### Task 2.3: 修复 position_monitor.py 中未定义变量

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`

当前 `scan_positions()` 函数（第 38 行）有以下 bug：

1. **第 132-157 行**：`tp1` 和 `tp2` 变量未定义就使用。只有 `tp_level`（对应 tp3）在第 116 行定义。需要添加：
```python
tp1 = cost * 1.06   # +6%
tp2 = cost * 1.10   # +10%
```

2. **止盈逻辑混乱**：当前止盈判断使用了 `TAKE_PROFIT_PCT = 0.06`（第 31 行），但三挡止盈应该有独立的阈值。修正为三个档位分别判断，与 sim_trading.py 的 +6%/+10%/+18% 对齐。

3. **判断顺序错误**：当前代码先判断 `price >= tp3`（第 132 行），然后 `elif price >= tp2`（第 146 行），然后 `elif price >= tp1`（第 155 行）。但 `elif` 顺序是对的——如果 `tp3 > tp2 > tp1`，先判断 tp3 没问题。主要问题是 `tp1` 和 `tp2` 变量不存在。

修正后的止盈判断逻辑（替换第 116-158 行）：
```python
# 分级止盈
tp1_price = cost * 1.06   # +6% 建议卖1/3
tp2_price = cost * 1.10   # +10% 建议再卖1/3
tp3_price = cost * 1.18   # +18% 建议清仓

if price >= tp3_price:
    alert_type = "TAKE_PROFIT_3"
    alert_msg = ...
elif price >= tp2_price:
    alert_type = "TAKE_PROFIT_2"
    alert_msg = ...
elif price >= tp1_price:
    alert_type = "TAKE_PROFIT_1"
    alert_msg = ...
```

**验证标准：**
- `python3 scripts/position_monitor.py` 在持仓盈利时不再报 NameError
- 三条止盈档位分别能正确触发

### Success Criteria

- `daily_scan()` 盘后运行后 positions.json 与 MySQL 内容一致
- 所有脚本的止损兜底阈值统一为 -3%
- `position_monitor.py` 止盈判断正常执行，无 NameError
- 飞书预警的止损判断与 sim_trading 的止损线一致

---

## Plan 3: 自动化编排 (REQ-04 集成)

### Objective

当前模拟交易和监控的定时任务分散在 crontab 中，缺少选股扫描入口，且没有异常后快速恢复的能力。本计划补齐这些短板，确保每日流水线无需人工介入。

### Tasks

#### Task 3.1: 新增 v4_scan CLI 入口

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

当前 `__main__`（第 1120 行）只支持 `init` / `scan` / `status` 三个动作。`v4_scan()` 函数存在于第 452 行但无法通过 CLI 调用。

在 `__main__` 的 `parser` 中添加 `v4_scan` action：
```python
parser.add_argument("action", choices=["init", "scan", "v4_scan", "status"],
                    help="init=建表初始化, scan=每日扫描, v4_scan=V4候选扫描, status=账户状态")
```

添加对应分支：
```python
elif args.action == "v4_scan":
    candidates = v4_scan(top_n=5)
    for c in candidates:
        logger.info("V4候选: %s(%s) 主力评分=%.0f ML=%.2f",
                    c["name"], c["ts_code"], c["mainforce_score"], c.get("ml_prob", 0))
    print(json.dumps(candidates, ensure_ascii=False, indent=2, default=str))
```

**验证标准：**
- `python3 scripts/sim_trading.py v4_scan` 输出 JSON 格式的候选股列表
- 与现有 `v4_scan()` 函数行为一致，不影响逻辑

#### Task 3.2: 更新 crontab

**文件操作：** 修改 `/Users/mozengfu/workspace/quant-system/scripts/quant_crontab`

微调现有 crontab 时间线和注释：

**现有编排（盘后）:**
```
17:00  update_daily_price_cron.py（导入数据）
17:30  sim_trading.py scan（模拟交易扫描）
17:45  run_three_strategies.py（V4扫描）
```

**更新后的编排（保持现有时间，增加注释说明依赖关系）：**

```
# ========== 每日盘后流水线（依赖链）==========
# 17:00 Tushare数据导入（这一步必须先完成）
# 17:30 模拟交易扫描（依赖 17:00 的最新数据）
# 17:45 V4策略扫描（可选，不影响模拟交易）
```

具体改动：
1. 在 `# ========== 模拟交易 ==========` 区块上方添加依赖链注释
2. 当前时间点无需调整（17:00 → 17:30 → 17:45 已有 30 分钟间隔），但需要确保 17:30 的任务名从 `sim_trading.py scan` 更新日志标记
3. 在 `# ========== 盘中监控 ==========` 区块添加注释说明：position_monitor 每 5 分钟跑，读取 positions.json（盘后由 sim_trading scan 同步写入）
4. 移除 `auto_scan.sh` 的 crontab 条目（第 27-30 行）—— 该脚本调用 `/api/scan` 但 API 路由的核心逻辑已被 `sim_trading.py v4_scan()` 替代。或者保留但添加注释说明其仅用于前端界面刷新。

**验证标准：**
- crontab 文件语法正确
- 注释清晰地描述了每日流水线的依赖关系和执行顺序
- 所有定时任务的日志路径一致

#### Task 3.3: 每日流水线健康检查脚本

**文件操作：** 新增 `/Users/mozengfu/workspace/quant-system/scripts/check_pipeline.py`

快速诊断脚本，每天在模拟交易扫描后运行，验证流水线是否完整执行：

```python
"""
每日流水线健康检查
在 sim_trading scan 之后运行，验证模拟交易执行结果

用法: python3 scripts/check_pipeline.py
"""
```

检查内容：
1. `sim_signals` 表中当天是否有信号记录（`signal_date = today`）
2. `sim_trades` 表中当天是否有交易记录（买方或卖方）
3. `sim_account` 总资产相对于盘前是否有更新
4. `data/positions.json` 是否存在且与 MySQL sim_positions 一致（比较持仓数量）
5. 输出摘要：`[OK/FAIL] 模拟交易流水线: 信号N条, 交易N笔, 持仓N只, 总资产X.XX`

失败判定（任一条件触发）：
- 当天无信号记录
- `sim_account.profit_pct` 为盘前旧值（未更新）
- `data/positions.json` 不存在

将检查结果写入 `logs/pipeline_check.log`。

**不加入 crontab** —— 此脚本为按需诊断使用，主任在发现飞书日报异常时手动运行排查问题。

**验证标准：**
- `python3 scripts/check_pipeline.py` 在有数据时输出完整的健康检查摘要
- 在无数据时（如节假日、首次部署）优雅降级输出警告而非崩溃
- 日志文件有效

### Success Criteria

- `python3 scripts/sim_trading.py v4_scan` 可作为独立 CLI 调用
- crontab 注释清晰描述每日依赖链
- 流水线健康检查脚本可快速定位问题环节

---

## 执行顺序

```
Plan 1 (REQ-05, REQ-06) ──→ Plan 2 (REQ-04) ──→ Plan 3 (REQ-04)
   Task 1.1                    Task 2.1              Task 3.1
   Task 1.2                    Task 2.2              Task 3.2
   Task 1.3                    Task 2.3              Task 3.3
```

**严格顺序依赖：**
- Plan 1 必须先完成（核心交易引擎完善后才能谈数据源统一和编排）
- Plan 2 Task 2.1 依赖 Plan 1（需要 daily_scan 稳定运行后再加 JSON 同步）
- Plan 3 依赖 Plan 1 和 Plan 2（编排是在引擎和数据源都稳定后的上层优化）

**并行可能：**
- Plan 2 Task 2.2（阈值对齐）和 Task 2.3（bug 修复）不依赖 Plan 1，可在 Plan 1 之前或同期执行
- Plan 3 Task 3.3（健康检查）不依赖 Plan 2，可在 Plan 1 完成后执行

推荐执行路径：
```
time ──────────────────────────────────────────────►
Plan1 T1.1 ──→ T1.2 ──→ T1.3 ──→ Plan2 T2.1 ──→ Plan3 T3.1, T3.2, T3.3
                                    ↑
Plan2 T2.2 ──→ T2.3 ───────────────┘ (可并行)
```

---

## 涉及文件清单

### 新增文件
- `/Users/mozengfu/workspace/quant-system/scripts/check_pipeline.py` — 每日流水线健康检查（Plan 3, Task 3.3）

### 修改文件
- `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py` — 建表、自动止盈/超时、回撤断路器、仓位管理、JSON 同步、v4_scan CLI（Plan 1, Plan 2 Task 2.1, Plan 3 Task 3.1）
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py` — 修复未定义变量 tp1/tp2，对齐止盈档位（Plan 2 Task 2.3）
- `/Users/mozengfu/workspace/quant-system/scripts/feishu_alerts.py` — 止损兜底值对齐，stop_loss 兜底逻辑（Plan 2 Task 2.2）
- `/Users/mozengfu/workspace/quant-system/scripts/quant_crontab` — 依赖链注释、auto_scan.sh 标注（Plan 3 Task 3.2）

### 不修改
- `alicloud_api.py` — 虽然实时行情源不一致，但统一到 `realtime_service.py` 属于重构范围，本次不涉及
- `app_core.py` / `app_api.py` — 模拟交易的 Web API 路由不在此阶段范围
