# Phase 3: 风控体系完善 — 执行计划

## 问题陈述

模拟交易系统已有止损止盈逻辑，但仅在盘后（17:30）`daily_scan()` 中执行。盘中 `position_monitor.py` 每 5 分钟扫描持仓，检测到触发条件后仅记日志 + 发告警，不会执行卖出。一只股票 10:00 触达止损线，要等到 17:30 才真正卖出，这是本阶段要解决的核心问题。

市场状态模块（`market_state.py`）已产出 5 种状态及对应参数，但 `sim_trading.py` 和 `position_monitor.py` 未完全接入——前者有兜底硬编码（-3%），后者完全不感知市场状态。

---

## Plan 1: 盘中自动止盈止损 (REQ-07)

**目标：** 让 `position_monitor.py` 在检测到止盈止损条件时直接执行卖出，不再仅打印日志。

### 方案选择

选项 A（导入 sim_trading）vs 选项 B（提取共享模块）。选 A。`execute_sell` 和 `execute_partial_sell` 定义在 `sim_trading.py:685` 和 `:773`，内部依赖 MySQL 连接和 `DB_CONFIG`。直接 import 即可，零重构。

`execute_sell(position_id, price, trade_date, reason)` — 需要 `position_id`
`execute_partial_sell(position_id, shares_to_sell, price, trade_date, reason)` — 需要 `position_id` 和 `shares_to_sell`

但 `position_monitor.py` 当前从 `positions.json` 读取数据，该 JSON 中 **没有** `position_id`（MySQL 自增主键）。需要补充：在 `sync_positions_to_json()` 写入时包含 `position_id`，position_monitor 读取后方可调用 `execute_sell`。

### 任务 1.1: 取 v7（ML 综合版）

该任务只验证特征是否齐全，不做复杂集成。

### 任务 1.1: 使 positions.json 包含 position_id

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`（修改 `sync_positions_to_json`）
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`（修改读取和参数）

**修改 1 — sim_trading.py `sync_positions_to_json()`：**

找到写入 `positions.json` 的代码段（大约在 `daily_scan()` 末尾），在每个 position dict 中添加 `"position_id": row[0]` 字段。当前写入字段举例：`code`, `name`, `cost`, `shares`, `buy_date`, `stop_loss`, `take_profit` 等。加入后该字段即可。

注意：`sync_positions_to_json` 并非独立函数，它嵌套在 `daily_scan()` 中。具体找这段逻辑，补上 `position_id`。

**修改 2 — position_monitor.py：**

在 `scan_positions()` 中，读取到 `pos` 后提取：
```python
position_id = pos.get("position_id", 0)
```

在止盈止损判断分支中，将当前纯日志告警改为调用 `execute_sell` / `execute_partial_sell`。

### 任务 1.2: position_monitor.py 告警分支替换为实际执行

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`

**修改点（共 3 处）：**

**A）文件顶部添加 import：**
```python
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from sim_trading import execute_sell, execute_partial_sell
```

**B）止损分支替换（约第 100-113 行）：**
当前：
```python
if price <= trailing_stop:
    alert_type = "STOP_LOSS"
    # ... 仅记录 alert_detail + logger.warning
```
改为：
```python
if price <= trailing_stop and position_id > 0:
    alert_type = "STOP_LOSS"
    logger.warning("执行止损: %s 现价 %.2f 成本 %.2f", name, price, cost)
    execute_sell(position_id, price, reason="盘中自动止损")
    continue  # 已卖出，不继续检查止盈
```

**C）止盈三档替换（约第 115-161 行）：**
当前三档只记 alert_detail + logger.warning，改为：
- 第三档（+18%）：`execute_sell(position_id, price, reason="盘中止盈(tp3)")`
- 第二档（+10%）：`execute_partial_sell(position_id, shares // 2, price, reason="盘中止盈(tp2)")`
- 第一档（+6%）：`execute_partial_sell(position_id, shares // 2, price, reason="盘中止盈(tp1)")`

注意：tp1 先执行半仓，后续到达 tp2/tp3 再卖剩余。保持与 `daily_scan` 逻辑一致（`daily_scan` 中的细节需再次确认——若 daily_scan 在 tp1 是全仓卖，这里应保持一致）。

细节确认（读 `daily_scan` 决定三档如何执行）：
- 若 `daily_scan` 在 tp1/+6% 是全仓卖→这里也全仓
- 若 `daily_scan` 在 tp1 是半仓→这里半仓

需要先读 `daily_scan` 中止盈部分的逻辑来决定。

**D）考虑非交易时段保护：**
`execute_sell` 本身应有盘后保护（MySQL 写入成功但无实际意义）。在 `position_monitor.py` 中加简单判断：只在 9:30-15:00 之间执行自动卖出。

### 任务 1.3: 更新 crontab，覆盖 15:00 收盘前完整窗口

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/quant_crontab`

当前 `position_monitor.py` 的 crontab 行：
```
*/5 9-14 * * 1-5 cd $QUANT_DIR && $PYTHON3 scripts/position_monitor.py >> ...
```

`9-14` 表示 9:00-14:59（含 14:55 是最后一跳），缺少 14:55-15:00 的空档期。改为：
```
*/5 9-14,14 * * 1-5 ...
```

注意 crontab 语法：`9-14` 包含小时 9,10,11,12,13,14；追加 `,14` 不会扩大范围。实际上要覆盖到 15:00，有两种方式：

方式 A：`*/5 9-14 * * 1-5` 本就是到 14:55（14点最后一跳 14:55），15:00 收盘前的 5 分钟实际已包含（14:55 扫描到 15:00 之间）。但收盘前最后一分钟触发的行情无法捕捉。

方式 B：改为 `*/5 9-15 * * 1-5`，让 15:00 也有一次扫描（虽然收盘后价格无变化）。

选方式 B 更安全，但 `feishu_alerts.py alert` 要不要同步改也需要确认——feishu_alerts.py 应该不需要执行交易，可以只改 position_monitor 的行。

---

## Plan 2: 市场状态自适应仓位 (REQ-08, REQ-09)

**目标：** 将 `market_state.py` 的 `get_market_state()` 结果联动到 `sim_trading.py` 和 `position_monitor.py`，消除硬编码参数。

### 当前状态

`sim_trading.py` 已尝试接入（第 36-44 行）：
```python
try:
    from market_state import get_market_state
    _ms = get_market_state() or {}
    _p = _ms.get('params', {})
    STOP_LOSS_PCT = _p.get('stop_loss_pct', -3) / 100
    TAKE_PROFIT_PCT = _p.get('take_profit_pct', 6) / 100
except Exception:
    STOP_LOSS_PCT = -0.03
    TAKE_PROFIT_PCT = 0.06
```

但止盈止损只在 `daily_scan()` 中用到这些变量（模块级别的）。问题：`MAX_POSITIONS = 3` 是硬编码的，且 `position_monitor.py` 完全没有接入市场状态。

### 任务 2.1: 将 MAX_POSITIONS 改为动态获取

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

在模块加载时，增加从市场状态读取 `max_positions`：
```python
MAX_POSITIONS = _p.get('max_positions', 3)
```

已有 fallback 机制（try/except 兜底 `STOP_LOSS_PCT` 和 `TAKE_PROFIT_PCT` 为 -0.03/0.06），将 `MAX_POSITIONS` 也加入同一 try 块，fallback 为 3。

注意：市场状态在每日多次调用时因 30 秒缓存 TTL 的存在，盘中会重复获取。但模块级别的变量只在 `import` 时赋值一次，盘中市场状态变化不会反映到模块变量。这意味着：

- `stop_loss_pct`、`take_profit_pct`、`max_positions` 在每日第一次 `import sim_trading` 时确定
- 盘中市场状态变化（例如从 range 变恐慌）不会触发参数更新

这个问题可暂不处理（简单模式），但需在注释中说明。如需盘中自适应，将来可在 `daily_scan()` 函数内部重新调用 `get_market_state()`。

### 任务 2.2: position_monitor.py 接入市场状态参数

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`

当前：
```python
STOP_LOSS_PCT = -0.03
TAKE_PROFIT_PCT = 0.06
```

改为在模块加载时从 `market_state` 获取：
```python
try:
    from market_state import get_market_state
    _ms = get_market_state() or {}
    _p = _ms.get('params', {})
    STOP_LOSS_PCT = _p.get('stop_loss_pct', -3) / 100
    TAKE_PROFIT_PCT = _p.get('take_profit_pct', 6) / 100
    MAX_POSITIONS = _p.get('max_positions', 3)
except Exception:
    STOP_LOSS_PCT = -0.03
    TAKE_PROFIT_PCT = 0.06
    MAX_POSITIONS = 3
```

然后 `calc_trailing_stop` 使用 `STOP_LOSS_PCT` 而非硬编码。当前 `calc_trailing_stop` 的签名是：
```python
def calc_trailing_stop(cost, current_price, original_stop_loss):
    """级联策略使用固定止损，无移动止损"""
    return original_stop_loss, False
```

此处 `original_stop_loss` 是传进来的，来自 `pos.get("stop_loss", 0)`，即 `sync_positions_to_json` 中写入的固定止损线。如果要在盘中自适应，有两种做法：
- 不改 `calc_trailing_stop` 逻辑，直接用模块级别 `STOP_LOSS_PCT` 计算新的止损线覆盖 `pos["stop_loss"]`
- 修改 `calc_trailing_stop` 逻辑

简单做法：在 `scan_positions()` 循环中，对每个 `pos` 先计算一个自适应的止损价：
```python
adaptive_stop_loss = cost * (1 + STOP_LOSS_PCT)  # STOP_LOSS_PCT 是负数
# 使用 adaptive_stop_loss 替代 pos["stop_loss"] 作为判断依据
```

### 任务 2.3: 盘中市场状态变化自动刷新

如果要求盘中市场状态变化时参数自适应（而非一日一刷新），需要在每次 `scan_positions()` 调用时重新获取 `get_market_state()`。

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`

将任务 2.2 中的模块级别获取移到 `scan_positions()` 函数内部，每次扫描时重新获取：
```python
def _get_market_params():
    try:
        from market_state import get_market_state
        ms = get_market_state() or {}
        p = ms.get('params', {})
        return {
            'stop_loss_pct': p.get('stop_loss_pct', -3) / 100,
            'take_profit_pct': p.get('take_profit_pct', 6) / 100,
            'max_positions': p.get('max_positions', 3),
        }
    except Exception:
        return {'stop_loss_pct': -0.03, 'take_profit_pct': 0.06, 'max_positions': 3}
```

然后在 `scan_positions()` 开头调用 `_get_market_params()` 获取最新参数。

**效果：** 每次扫描（5 分钟间隔）都会使用最新的市场状态参数。由于 `get_market_state()` 有 30 秒缓存 TTL，频繁调用也不会重复计算。

### 任务 2.4: 确保 sim_trading daily_scan 盘中自适应

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`

将 `daily_scan()` 函数开头改为重新读取市场状态，而不是使用模块加载时的静态快照：
```python
def daily_scan():
    """每日扫描：止损/止盈/超时处理 → 持仓刷新 → JSON同步"""
    # 获取最新市场状态参数
    try:
        from market_state import get_market_state
        _ms = get_market_state() or {}
        _p = _ms.get('params', {})
        stop_loss_pct = _p.get('stop_loss_pct', -3) / 100
        take_profit_pct = _p.get('take_profit_pct', 6) / 100
        max_positions = _p.get('max_positions', 3)
    except Exception:
        stop_loss_pct = -0.03
        take_profit_pct = 0.06
        max_positions = 3
    # ... 后续逻辑使用这些局部变量
```

这也解决了模块级别变量只初始化一次的问题。

---

## Plan 3: 风控参数可配置 (REQ-08 增强)

**目标：** 使关键风控参数可通过配置文件修改，无需改动代码。

### 任务 3.1: 创建 risk_config.json

**新文件：**
- `/Users/mozengfu/workspace/quant-system/data/risk_config.json`

内容示例：
```json
{
  "stop_loss_pct": -3.0,
  "take_profit_pct": 6.0,
  "max_positions": 3,
  "per_position_pct": 30.0,
  "drawdown_circuit_breaker": -15.0,
  "hold_days": 5,
  "ml_threshold": 0.55,
  "trailing_stop_enabled": false,
  "trailing_stop_activation_pct": 5.0,
  "trailing_stop_distance_pct": 3.0,
  "updated_at": "2026-05-05"
}
```

所有值的含义：
- `stop_loss_pct`: 固定止损百分比（负数），覆盖各市场状态的默认值
- `take_profit_pct`: 固定止盈百分比（正数）
- `max_positions`: 最大同时持仓数
- `per_position_pct`: 单仓占现金比例上限（%）
- `drawdown_circuit_breaker`: 总回撤断路器（负数 %）
- `hold_days`: 默认持仓天数上限
- `ml_threshold`: ML 模型买入概率门槛
- `trailing_stop_enabled`: 是否启用移动止损
- `trailing_stop_activation_pct`: 移动止损激活所需涨幅（%）
- `trailing_stop_distance_pct`: 移动止损回撤距离（%）

### 任务 3.2: 创建统一的配置读取函数

**新文件或修改：**

选项 A：在 `quant_app/utils/` 下新建 `risk_config.py`
选项 B：直接放在需要的地方，保持简洁

选 A，单独一个文件便于管理和复用。

**新文件：** `/Users/mozengfu/workspace/quant-system/quant_app/utils/risk_config.py`

```python
"""风控配置管理 — 从 risk_config.json 读取，代码默认值为 fallback"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

RISK_CONFIG_PATH = Path(__file__).parent.parent.parent / "data" / "risk_config.json"

DEFAULT_CONFIG = {
    "stop_loss_pct": -3.0,
    "take_profit_pct": 6.0,
    "max_positions": 3,
    "per_position_pct": 30.0,
    "drawdown_circuit_breaker": -15.0,
    "hold_days": 5,
    "ml_threshold": 0.55,
    "trailing_stop_enabled": False,
    "trailing_stop_activation_pct": 5.0,
    "trailing_stop_distance_pct": 3.0,
}

_config_cache = None

def get_risk_config(force_reload=False):
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache
    try:
        if RISK_CONFIG_PATH.exists():
            with open(RISK_CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
            merged = {**DEFAULT_CONFIG, **cfg}
            _config_cache = merged
            return merged
    except Exception as e:
        logger.warning(f"读取 risk_config.json 失败: {e}，使用默认配置")
    _config_cache = dict(DEFAULT_CONFIG)
    return _config_cache
```

优先级：市场状态参数 > risk_config.json > 代码默认值
市场状态参数（盘中动态）优先于静态配置文件中的值。配置文件主要用于设置用户偏好的"基准值"，市场状态在此基础上做加减。

### 任务 3.3: 接入现有模块

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/sim_trading.py`
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`

在 `daily_scan()` 和 `scan_positions()` 中，先从 `risk_config.py` 获取基准参数，再用 `market_state.py` 获取动态参数，市场状态参数优先覆盖。

伪代码：
```python
from quant_app.utils.risk_config import get_risk_config
rc = get_risk_config()  # 基准配置

# 市场状态覆盖（盘中自适应）
try:
    from market_state import get_market_state
    ms = get_market_state()
    mp = ms.get('params', {})
    stop_loss_pct = mp.get('stop_loss_pct', rc['stop_loss_pct']) / 100
    ...
except Exception:
    stop_loss_pct = rc['stop_loss_pct'] / 100
```

### 任务 3.4: 添加 CLI 命令查看当前配置

**涉及文件：**
- `/Users/mozengfu/workspace/quant-system/scripts/position_monitor.py`（通过 `if __name__ == "__main__"` 添加模式）

在 `position_monitor.py` 的 `__main__` 中添加 CLI 参数支持：
```bash
python3 scripts/position_monitor.py status
```

输出：
```
当前市场状态: 震荡 (得分: 5.2)
建议: 市场震荡，短线操作，快进快出

风控参数:
  止损: -4.0%（市场状态）
  止盈: +6.0%（市场状态）
  最大持仓: 4（市场状态）
  单仓比例: 30%（risk_config.json）
  回撤断路器: -15%（risk_config.json）

持仓: 2 只, 总仓位 40.3%
```

---

## 执行顺序

```
第一阶段（核心收益最高）：
  Plan 1 任务 1.2 → 盘中止盈止损执行
  Plan 1 任务 1.3 → crontab 修正
  Plan 2 任务 2.2 → position_monitor 接入市场状态
  
第二阶段：
  Plan 1 任务 1.1 → positions.json 加 position_id（打通 execute_sell 调用链路）
  Plan 2 任务 2.1 → MAX_POSITIONS 动态化
  
第三阶段（可配置）：
  Plan 3 所有任务 → risk_config.json 全流程
```

实际执行时 Plan 1 和 Plan 2 的部分任务可以并行。但 Plan 1 任务 1.1（加 `position_id`）是任务 1.2 的前提，必须先完成。

---

## 验证清单

### Plan 1 验证
- [ ] `position_monitor.py` 中触达止损条件时，`sim_trading.execute_sell` 被调用
- [ ] `position_monitor.py` 中触达止盈条件时，对应档位的卖出函数被调用
- [ ] 更新后的 positions.json 在被卖出后从持仓列表中移除
- [ ] crontab 覆盖 9:30-15:00 完整窗口

### Plan 2 验证
- [ ] `position_monitor.py` 使用市场状态参数计算止损价（而非固定 -3%）
- [ ] `daily_scan` 每次执行重新获取市场状态
- [ ] `MAX_POSITIONS` 跟随市场状态变化
- [ ] 市场状态 30 秒缓存正常工作，盘中 5 分钟扫描不会重复计算

### Plan 3 验证
- [ ] `risk_config.json` 文件不存在时正常使用代码默认值
- [ ] `risk_config.json` 中存在部分字段时，缺失字段用代码默认值填补
- [ ] `cli position_monitor.py status` 正确显示当前状态
- [ ] 市场状态参数优先级高于 `risk_config.json`

---

## 回滚方案

Plan 1 回滚：`git checkout -- scripts/position_monitor.py` 和 `git checkout -- scripts/quant_crontab`
Plan 2 回滚：同上
Plan 3 回滚：删除或恢复 `quant_app/utils/risk_config.py` + `data/risk_config.json`

所有改动集中在 position_monitor.py、sim_trading.py、quant_crontab 三个文件中，加一个新建文件 risk_config.py，回滚范围小。
