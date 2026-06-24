# AGENTS.md — 量化交易系统操作手册

> 给 AI Agent 和运维人员的系统操作指南。涵盖架构、管线、组件关系、常见问题排查。

---

## 1. 系统架构概览

```
┌──────────────────────────────────────────────────────────┐
│                    Mac (192.168.10.30)                    │
│                                                          │
│  app.py + app_api.py     FastAPI (:5001) + WebSocket     │
│  market_monitor.py       30s 轮询 → data/market_state    │
│  live_trading_scheduler  交易调度器 (scan/monitor/等)     │
│  ml_predict.py           ML 推理引擎 (V11)               │
│  frontend/               Vue 3 + Vite SPA                │
│  scripts/                独立脚本 (训练/同步/诊断)       │
│  MySQL :3306/quant_db    核心数据库                       │
│                                                          │
├──────────────────────────┬───────────────────────────────┤
│  192.168.10.25 (QMT)     │  192.168.10.39 (训练机)       │
│  HTTP 桥 :1430           │  ml_train_v11_2.py 全量训练   │
│  国信 iQuant 策略平台    │  32GB RAM / 16GB VRAM         │
│  账户 170000758981 实盘  │  .pkl 传回 Mac data/          │
└──────────────────────────┴───────────────────────────────┘
```

**核心数据流**:
```
盘后: MySQL daily_price → ml_predict → 候选股 → sim_signals(待执行)
盘中: QMT 快照 → 板RPS实时扫描 → 因子评分 → ML排序 → 买入
      QMT 持仓 → 止损/止盈/超时检查 → 卖出
```

---

## 2. 生产交易管线

### 2.1 盘后扫描 — cmd_scan (17:30 cron)

```
板RPS周线(836板块) → Top5板块 → 成分股(332只)
  → ML(V11.2)排序 → Top3候选 → sim_signals(待执行)
  → 飞书通知候选列表
  → stockpool.json 同步到 QMT (766只)
```

**关键文件**: `scripts/live_trading_scheduler.py` 函数 `cmd_scan()`

**依赖**: 必须先跑 `update_daily_price_cron.py` (17:00) 导入当日行情。

**候选数**: 固定 Top3 (`ML_CANDIDATES_COUNT = 3`),不随可用 slot 变动。

### 2.2 盘中监控 — cmd_monitor (每分钟 9:15-15:00, cron `* 9-15 * * 1-5`)

三条线并行执行:

```
① 持仓监控 (止盈止损)
   遍历 QMT 实盘持仓:
     - T+1 检查: 今日买入跳过卖出
     - 止损: min(ATR动态, 固定-7%) 的硬兜底线 → 市价卖
     - 峰值止盈(分级):
        峰值>8% 且 <20% → 回落到剩8%锁利
        峰值≥20%         → peak-2×ATR 动态trailing
     - 兜底止盈: 峰值3~8% + 今日到过3%线 + 现价在成本~3%之间 → 卖
     - 超时卖出: ≥5天且盈利<3%
     - 强制平仓: >8天

② V11择时入场 (ML候选)
   读取 sim_signals(待执行, 今日) → ml_prob 降序
   逐个检查实时条件: 涨跌幅-5%~+5%, 非涨停, 分时回踩/放量
   满足则买入(预算 = 全部可用cash / slot数)

③ 板RPS实时扫描 (盘中实时)
   板RPS周线 + 实时因子评分(量能/动量/趋势/流动性/RSI/布林/盘口/日内/资金/指数)
   综合分 ≥ 60 + ML概率 ≥ 0.3 → 预算够就买
```

**关键文件**: `scripts/live_trading_scheduler.py` 函数: `cmd_monitor()` → `_monitor_v11_entry()` + `_monitor_board_rps_entry()`

### 2.3 风控过滤

| 条件 | 行为 |
|---|---|
| 恐慌状态 (跌>2.5%+涨跌比<0.3) | 阻断所有买入, max_pos=1 |
| 恐慌清仓 (跌>3.5%) | 全部市价卖出 + 飞书告警 |
| 逆市 (跌1~2%) | 仓位减半, 提高选股门槛 |
| 14:55 后 | 不开新仓 (尾盘信号质量差) |
| 市场阻断 (涨跌比<0.3/连跌/北向流出>100亿) | 阻断 |

---

## 3. 核心文件一览

### 交易调度器

| 文件 | 作用 | 入口函数 |
|---|---|---|
| `scripts/live_trading_scheduler.py` | **主力入口**, 所有交易操作 | `cmd_scan/monitor/morning/status/init/sync/ping/keepalive` |
| `scripts/feishu_alerts.py` | 飞书告警 + 心跳检查 | `alert/morning/daily/check_position_alerts` |
| `scripts/sim_trading.py` | 信号记录 + 交易日工具 + 模拟交易 | `record_signal/execute_buy/execute_sell` |
| `scripts/position_monitor.py` | 风控扫描(备用, 已集成到 monitor) | — |
| `scripts/market_monitor.py` | 大盘行情守护 (30s) | 写 `data/market_state.json` |

### ML 引擎

| 文件 | 作用 |
|---|---|
| `ml_predict.py` | V11 推理引擎 (131维特征, 18子模型集成) |
| `quant_app/utils/model_loader.py` | 模型注册表, `load_model(version)` 统一加载 |
| `quant_app/services/board_rps_scanner.py` | 板RPS周线筛选 + ML排序 + 实时因子评分 |

### 交易执行

| 文件 | 作用 |
|---|---|
| `quant_app/trading/executor.py` | 抽象基类 + 工厂 `create_executor(mode)` |
| `quant_app/trading/modes/remote_executor.py` | 生产执行器 (HTTP→QMT:1430) |
| `quant_app/trading/modes/sim_executor.py` | 模拟执行器 |
| `quant_app/trading/config.py` | 交易配置单例 (TRADE_MODE/安全开关) |
| `quant_app/services/qmt_adapter.py` | QMT HTTP 协议适配 |

### 数据库 & 配置

| 文件 | 作用 |
|---|---|
| `quant_app/utils/config.py` | Config 单例, 命名空间分组 |
| `quant_app/data/database.py` | SQLAlchemy 引擎 + `with_session()` |
| `.env` | 敏感参数 (被 gitignore), 参照 `.env.example` |
| `config/scanner_config.yaml` | 扫描策略参数 |

### 通知

| 文件 | 作用 |
|---|---|
| `quant_app/services/notification_service.py` | 飞书/企微/邮件/短信 4 通道 |

---

## 4. 数据库核心表

| 表 | 说明 | 关键字段 |
|---|---|---|
| `daily_price` | 日线行情 (ts_code, trade_date, open/high/low/close/vol/ma/rps等) | `ts_code, trade_date` |
| `stock_info` | 股票基本信息 | `ts_code, name, industry` |
| `sim_signals` | 买卖信号 (待执行/已执行/已平仓/已过期) | `ts_code, status, signal_date` |
| `sim_positions` | 持仓记录 (HOLD/SOLD) | `ts_code, status, buy_date` |
| `strategy_trade_log` | 策略交易日志 | `ts_code, strategy, status` |
| `market_state` | 市场状态数据 | `state, state_name` |
| `trade_cal` | 交易日历 | `trade_date, is_open` |

**sim_signals 状态流转**:
```
待执行 → 已执行 (monitor买入后)
待执行 → 已过期 (次日 scan 清理)
已执行(买入候选) + 已平仓(卖出) = 完整交易记录
```

**唯一约束**: `uk_sim_signals_executed(ts_code, active_executed_date)` — 同一天同一只股只能有一条已执行记录。

---

## 5. 定时任务 (crontab)

| 时间 | 任务 | 说明 |
|---|---|---|
| `* 9-15 * * 1-5` | `live_trading_scheduler.py monitor` | **主力**: 盘中持仓监控+板RPS实时扫描 (每分钟) |
| `*/5 9-15 * * 1-5` | `feishu_alerts.py alert` | 飞书告警 + 心跳检测 |
| `0 9 * * 1-5` | `feishu_alerts.py morning` | 开盘预警 |
| `5 15 * * 1-5` | `feishu_alerts.py daily` | 盘后汇总 |
| `0 17 * * 1-5` | `update_daily_price_cron.py` | **盘后行情导入 (先决条件)** |
| `30 17 * * 1-5` | `live_trading_scheduler.py scan` | **盘后选股 (ML候选)** |
| `0 3 1 * *` | `cron_retrain.py` | 每月 1 号模型重训 |

**migrations/ 目录**: schema 变更按 `NNN_desc.sql` 编号留档。

---

## 6. 常用操作命令

### 交易操作

```bash
# 盘后扫描 (17:30, 生成次日 ML 候选)
python3 scripts/live_trading_scheduler.py scan

# 盘中监控 (crontab 自动跑, 也可手动验证)
python3 scripts/live_trading_scheduler.py monitor

# 查看实盘状态
python3 scripts/live_trading_scheduler.py status

# 健康检查 (QMT 连接)
python3 scripts/live_trading_scheduler.py ping

# 保活 (防止 QMT 锁屏)
python3 scripts/live_trading_scheduler.py keepalive
```

### 数据维护

```bash
# 盘后行情导入 (必须先于 scan)
python3 scripts/update_daily_price_cron.py

# 板块数据同步
python3 scripts/sync_tushare_boards.py

# 财务指标同步
python3 scripts/sync_fina_indicator.py

# 资金流向同步
python3 scripts/backfill_moneyflow.py
```

### ML 训练

```bash
# Mac 快速重训 (Top500)
python3 scripts/retrain_v11_fast.py

# Windows 训练机全量训练
ssh quant@192.168.10.39
cd C:\Users\quant\quant-system
python ml_train_v11_2.py
# 训练完传回 Mac data/
```

### 诊断

```bash
# 检查 daily_price 最新交易日
mysql -h 127.0.0.1 -u root -proot123 -e \
  "SELECT MAX(trade_date) FROM quant_db.daily_price"

# 检查待执行信号
mysql -h 127.0.0.1 -u root -proot123 -e \
  "SELECT * FROM quant_db.sim_signals WHERE status='待执行'"

# 查看持仓
mysql -h 127.0.0.1 -u root -proot123 -e \
  "SELECT * FROM quant_db.sim_positions WHERE status='HOLD'"

# 查看 monitor 日志
tail -30 /Users/mozengfu/workspace/quant-system/logs/live_trading_monitor.log

# 查看 scan 日志
tail -30 /Users/mozengfu/workspace/quant-system/logs/live_trading.log

# 查看心跳
cat /Users/mozengfu/workspace/quant-system/data/monitor_heartbeat.txt
```

---

## 7. 常见问题排查

### 7.1 心跳告警

**症状**: 飞书收到 "监控心跳异常"。

**排查**:
```
1. cat data/monitor_heartbeat.txt  # 查看最后心跳时间
2. tail -5 logs/live_trading_monitor.log  # 看 monitor 是否运行
3. crontab -l | grep monitor  # 确认 cron 任务存在
4. python3 scripts/live_trading_scheduler.py ping  # 测试 QMT 连接
```

**常见原因**:
- Mac 夜间睡眠, 09:00 唤醒后 cron 延迟 (正常, 等几分钟)
- `import datetime` 缺失导致心跳写入 NameError (已修复 2026-06-18)
- QMT 远程服务不可达 (检查 192.168.10.25:1430)

### 7.2 Monitor 运行时无买入行为

**症状**: monitor 正常跑但不买。

**排查**:
```
1. 检查 V11 候选:
   mysql -e "SELECT * FROM sim_signals WHERE status='待执行'"
2. 检查 板RPS实时日志:
   grep "无触发买入条件" logs/live_trading_monitor.log
3. 检查预算:
   grep "可用资金不足\|资金不足" logs/live_trading_monitor.log
```

**常见原因**:
- 候选信号不存在 (scan 未跑或 17:30 断网)
- 预算买不起 1 手 (shares < 100, 已全部现金分配给已有持仓)
- 持仓已满 (ML max=1, Scanner max=2)
- 14:55 后尾盘 cutoff (已修复 2026-06-18)
- 午后市场阻断触发

### 7.3 账户线空但 sim_positions 仍有数据

**症状**: `status` 显示持仓已清, 但 `sim_positions` 仍是 HOLD。

**原因**: `executor.sell()` 发单到 QMT 后不更新 sim_positions。sim_positions 是**异步同步**的, 依赖 QMT 成交回推。不影响交易逻辑 (QMT 以实际持仓为准)。

### 7.4 Scan 返回 ML=0

**原因排查**:
```
1. 检查网络: python3 -c "import requests; print(requests.get('https://api.waditu.com',timeout=5))"
2. 检查 daily_price 最新日期: SELECT MAX(trade_date) FROM daily_price
3. 检查预算: 可用现金是否足够买 100 股候选股
4. 检查候选池: grep "ML排序" 看候选数量
```

### 7.5 盘后数据未更新

**症状**: `daily_price` 最新日期停留在前一天。

**原因**: `update_daily_price_cron.py` (17:00 cron) 因网络/QMT 断连失败。

**解决**: 手动跑 `python3 scripts/update_daily_price_cron.py`, 然后重跑 scan。

### 7.6 飞书告警重复触发

**机制**: 每日去重, 复用 `data/alert_state.json` 中的日期标记。
- `monitor_heartbeat` key — 心跳告警 (每日 1 次)
- 其他告警按类型去重

---

## 8. 最近系统状态 (2026-06-18 收盘)

### 当前持仓

| 股票 | 成本 | 现价 | 盈亏 | 状态 |
|---|---|---|---|---|
| 002171 楚江新材 | 12.49 | ~15.38 | +23.1% | 正常持有 |
| 002213 大为股份 | 39.75 | ~40.07 | +0.8% | T+1 (6/18买入) |
| 002515 金字火腿 | 9.53 | ~9.63 | +1.0% | T+1 (6/18买入) |

### 今日 ML 候选 (待执行, 明早 monitor 评估)

| 股票 | 价格 | 份额 | ML概率 |
|---|---|---|---|
| 301511 德福科技 | 160.90 | 100 | 0.998 |
| 600110 诺德股份 | 17.57 | 1200 | 0.988 |
| 301366 一博科技 | 55.82 | 300 | 0.985 |

### 最近修复清单

| 日期 | Commit | 修复 |
|---|---|---|
| 06-18 | 9099b98 | ML 候选固定 Top3 |
| 06-18 | 6d7af0e | 取消预算分配比例 |
| 06-18 | e3804f3 | `_classify_holds_by_strategy` 复用 `_classify_single_hold` |
| 06-18 | 8699be2 | V11 无信号时加日志 |
| 06-18 | b3f5ce9 | 兜底止盈加 `today_high >= trigger` 检查 |
| 06-18 | 932d420 | 文件顶部加 `import datetime` |
| 06-18 | eb3bb3a | 5 项买入规则加固 (cleanup日期过滤/14:55 cutoff/ml_prob阈值/涨停分板块/预算buffer) |
| 06-17 | f979f4b | 心跳绝对路径 + IntegrityError 防护 + strategy 列扩50 |
| 06-17 | 0348b49 | scan 清理旧信号 + Path导入修复 |
| 06-17 | 589fbde | feishu_alerts 心跳检查 |
| 06-17 | b367b1f | T+1 检查 + 兜底止盈 `>=` |
| 06-17 | 4d69f39 | 3 个 bug: 缩进/第二档ATR trailing/兜底浮盈保护 |

---

## 9. 版本演进 (V11.0 生产版本)

```
V11.0(板RPS周线) — 生产 (2026-06-13)
├── 特征: 131维 (原V11 117维 + 板RPS指标)
├── 模型: 18子模型 LGB LambdaRank 集成
├── IC: 0.139
├── 选股: 板RPS周线 → Top5板块 → 成分股 → ML排序 → Top3候选
├── 成交: 盘中择时入场 (非开盘直接买)
├── 止盈:
│   ├── 峰值>8%: 第一档剩8%锁利
│   ├── 峰值≥20%: 第二档 peak-2×ATR 动态trailing
│   └── 峰值3~8%: 兜底止盈 +3% (需今天到过3%线)
├── 止损: min(ATR×2, 固定-7%)
├── 实时扫描: 板RPS盘中重算, 因子评分, ML排序, 综合分≥60买入
└── 分类: _classify_single_hold() 统一判断 ML/Scanner
```

---

## 10. 开发纪律与编码规范

### 10.1 工作流程纪律

每次修改必须按以下顺序执行, 不可跳过:

```
┌─────────────────┐
│  1. 先读后改     │  ← 编辑文件前必须已经读取过该文件
│                 │     涉及数据/模型的修改要先检查当前值
├─────────────────┤
│  2. 理解再动手   │  ← 不猜测代码行为, 先查日志/数据/commit历史
│                 │     涉及取消/禁用/删除的, 先确认有没有依赖方
├─────────────────┤
│  3. 最小改动     │  ← 只改问题所在的行, 不重构无关代码
│                 │     不改缩进/import顺序/注释风格等周边内容
│                 │     不改不属于本次任务的文件, 除非必须
├─────────────────┤
│  4. 验证         │  ← AST 解析校验 (python3 -c "import ast; ast.parse(...)")
│                 │     逻辑模拟验证 (模拟输入输出, 验证新旧行为差异)
│                 │     系统状态验证 (python3 scripts/live_trading_scheduler.py status)
├─────────────────┤
│  5. 提交         │  ← 原子提交: 一个 commit 只做一件事
│                 │     提交信息必须描述 "为什么改" 而非 "改了什么"
│                 │     commit message 格式: <type>(<scope>): <subject>
│                 │     类型: fix/feat/chore/docs/refactor/test
├─────────────────┤
│  6. 记录         │  ← 飞书告警等通知先查原因, 再修, 再记录
│                 │     MEMORY.md 记录关键决策和修复
└─────────────────┘
```

### 10.2 文件修改守则

| 规则 | 说明 |
|---|---|
| **不擅删根目录脚本** | `app.py`, `ml_predict.py`, `market_monitor.py`, `live_trading_scheduler.py`, `scripts/` 等独立脚本是生产主力, 不要擅自删除。新代码优先放 `quant_app/`。 |
| **不回退用户改动** | 工作目录中用户已做但未提交的改动, 除非用户明确要求, 否则不碰。 |
| **不擅自格式化** | 不改文件缩进/import 顺序/空行排版, 除非问题就是缩进错误。 |
| **不批量 rename** | 函数名/变量名/文件名的全局重命名必须逐个确认引用链, 否则漏了就炸。 |
| **不引入新依赖** | 除非用户主动要求, 不 `pip install` 新包。现有环境没有的库, 先确认功能是否能用现有方法实现。 |
| **不多文件同时操作** | 一次只改一个文件, 改完验证再改下一个。复杂改动先分批次提交。 |

### 10.3 代码质量标准

**错误处理**:
```python
# ✅ 好: 明确区分可预期异常和意外异常
try:
    result = do_something()
except SpecificKnownError as e:
    logger.warning("预期内失败, 可降级: %s", e)
    return fallback_value
except Exception as e:
    logger.error("意外异常, 需要排查: %s", e)
    raise  # 不知道怎么办的时候就抛出去

# ❌ 差: 吞掉所有异常
try:
    do_something()
except:
    pass
```

**日志级别规范**:

| 级别 | 适用场景 |
|---|---|
| `logger.info()` | 正常业务流转, cycle 开始/结束, 买卖操作 |
| `logger.warning()` | 预期内的降级/跳过/失败, 不影响主线 |
| `logger.error()` | 意外失败, 需要人工关注但不至于崩溃 |
| `logger.critical()` | 系统需要立即停机的严重错误 (恐慌清仓等) |
| `logger.debug()` | 开发期临时调试用, 上线前应删除或改为 info |

**代码注释**:
```python
# ✅ 好: 解释 "为什么这么写", 附修复日期/原因
# 修复: 主人 2026-06-17 反馈, 旧版没检查当前价是否高于成本,
#   导致 600183 在 -0.55% 浮亏位被错误触发卖出。
#   加 price > cost_price 后: 仅在浮盈时才允许触发。
if price <= trigger_price and price >= cost_price:

# ❌ 差: 解释 "写的是什么" (代码本身已经表达了)
# 如果价格小于等于触发价, 卖出
```

### 10.4 数据库操作规范

```sql
-- schema 变更必须留 migration 文件
-- 文件: migrations/NNN_desc.sql
ALTER TABLE sim_signals MODIFY strategy VARCHAR(50) DEFAULT NULL;

-- 查询先 EXPLAIN, 确认走索引
EXPLAIN SELECT * FROM sim_signals WHERE status='待执行' AND DATE(created_at)=CURDATE();

-- 清数据用 DELETE WHERE, 不用 TRUNCATE (保留自增ID连续性)
-- 大批量清洗数据时, 先 SELECT COUNT(*) 确认影响行数
```

### 10.5 验证清单

每次修改完成后, 按此清单逐项确认:

- [ ] AST 解析通过 (`import ast; ast.parse(open(file).read())`)
- [ ] 修改过的文件能正常 import (`python3 -c "from file import ..."`)
- [ ] 系统状态正常 (`python3 scripts/live_trading_scheduler.py status`)
- [ ] 心跳正常 (`cat data/monitor_heartbeat.txt`)
- [ ] 数据库连接正常 (`mysql -h 127.0.0.1 -u root -proot123 -e "SELECT 1"`)
- [ ] 模拟验证: 旧场景不再触发 + 正确场景仍然触发
- [ ] 提交信息描述完整: 为什么改 + 改了什么 + 影响范围

### 10.6 沟通规则

- **收到告警先查原因, 再修, 再回报告警后续处理**
- **不做预期外的操作**: 禁用/删除/清零/重置 操作必须先确认没有依赖方
- **涉及 QMT 健康检查/系统状态/连通性验证时，**无论在哪个会话中，都必须先调用 `qmt-health-check` skill**（已安装在 `.agents/skills/qmt-health-check/`），按 9 步检查流程执行
- **不改配置不改数据不重启的"三不"原则**: 线上操作前先想想这三样动没动
- **复杂修改先出方案, 用户确认后再动手**
- **保持 git 历史清晰**: 不要混杂无关改动到一个 commit

---

## 11. 注意事项

1. **crontab vs launchd**: macOS crontab 在睡眠时不执行。唤醒后缺的会补跑, 但可能有 1-2 个 cycle 的延迟。如需更可靠的定时, 改用 `launchctl`。
2. **`.env` 被 gitignore**: 环境变量改完 `.env` 后不需要 commit, 但要确认 `app.py` 重启。
3. **QMT 地址 (192.168.10.25:1430)**: 局域网内, 断连时脚本静默降级 (不抛异常)。重连后自动恢复。
4. **`migrations/` 变更**: 数据库 schema 改动后, 在 `migrations/` 写 `NNN_desc.sql` 留档。
5. **模型文件大 (~160MB)**: `data/ml_stock_model_v11_0.pkl` 不会 push 到 GitHub。训练机传回 .pkl 后本地放 `data/` 即可。
6. **端午休市 (2026-06-19)**: 候选和数据保留到 6/22(周一)开盘。
7. **健康检查用 skill**: **无论在哪条会话线程中**，涉及 QMT 连通性/系统状态/健康检查时，信息会自动加载 `qmt-health-check` skill，按 9 步流程执行。

## 不准虛報

- **沒驗證通過 = 沒完成**：任何功能改完必須有可重現的證據（日志輸出、查詢結果、文件時間戳），不能只憑幾句代碼就說"好了"
- **狀態必須可驗證**：說某個進程在跑、某個文件已更新、某個接口返回正確，都必須有對應的命令輸出作證
- **不猜測**：不確定的事說不確定，不編造"看起來好了"的結論
- **不混淆概念**：別把回退方案當正常方案匯報，要清楚說明"這是降級方案/臨時方案/永久方案"
- **用戶指出的問題先承認再修正**：用戶說不對就是不對，不要繞圈子解釋
