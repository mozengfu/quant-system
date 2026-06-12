# 实盘交易部署指南

## 架构总览

```
macOS (你的开发机)                         Windows VM/机器
┌─────────────────────────┐               ┌────────────────────────────────┐
│                          │ ────────────→ │  Port 1430                     │
│    scan / monitor / ping │ ←──────────── │  ├─ 中信证券交易               │
│                          │   JSON 响应    │  └─ 招商证券交易               │
│  TRADE_MODE=live         │               │                                │
│  REMOTE_TRADER_HOST=X.X  │               │  Windows 10/11                 │
└─────────────────────────┘               └────────────────────────────────┘
```

## 一、macOS 端（已完成）

### 文件结构

```
quant_app/trading/                  # 交易执行层
  executor.py                       # AbstractTradeExecutor + factory
  config.py                         # 配置（TRADE_MODE, 券商参数, 安全控制）
  orders.py                         # 数据类（Order/Position/Balance）
  modes/
    sim_executor.py                 # 模拟盘执行器（MySQL）
  risk/
    pre_trade_check.py              # 8项下单前安全检查

scripts/
  live_trading_scheduler.py         # 统一交易调度器
  setup_windows_trader.ps1          # Windows 端部署脚本
```

### 调度器命令

```bash
# 模拟盘（默认）
python3 scripts/live_trading_scheduler.py init    # 初始化模拟账户
python3 scripts/live_trading_scheduler.py scan    # 盘后选股+买卖
python3 scripts/live_trading_scheduler.py monitor # 盘中持仓监控
python3 scripts/live_trading_scheduler.py status  # 账户状态
python3 scripts/live_trading_scheduler.py sync    # 持仓同步JSON

# 实盘（连接远程 Windows）
TRADE_MODE=live \
REMOTE_TRADER_HOST=192.168.1.100 \
ENABLE_REAL_TRADING=false \
python3 scripts/live_trading_scheduler.py ping    # 健康检查

TRADE_MODE=live \
REMOTE_TRADER_HOST=192.168.1.100 \
ENABLE_REAL_TRADING=false \
python3 scripts/live_trading_scheduler.py status  # 账户状态

# 实盘（开启交易 — 先 dry-run 1周确认无误后）
TRADE_MODE=live \
REMOTE_TRADER_HOST=192.168.1.100 \
ENABLE_REAL_TRADING=true \
python3 scripts/live_trading_scheduler.py scan    # 真实下单
```

### 三档推进模式

| 阶段 | `TRADE_MODE` | `ENABLE_REAL_TRADING` | 行为 |
|------|-------------|----------------------|------|
| **Phase A** (现状) | `sim` | `false` | MySQL 模拟盘，原有逻辑不变 |
| **Phase B** (Windows 就绪后) | `live` | `false` | **dry-run**：连接远程 server，检查通过但不下单 |
| **Phase C** (dry-run 1周后) | `live` | `true` | **实盘**：真实下单交易 |

### 安全机制

| 检查项 | 阈值 | 违规时 |
|--------|------|--------|
| 交易时间 | 仅 9:30-11:30 / 13:00-14:57 | 拒绝下单 |
| 价格偏差 | 下单价 vs 市价 < 1% | 拒绝下单 |
| 单日熔断 | 累计亏损 > -5% | 暂停所有交易至次日 |
| 单笔限额 | < 50,000 元 | 拒绝下单 |
| 重复下单 | 同一股票 60s 内 | 拒绝下单 |
| 安全开关 | ENABLE_REAL_TRADING=true | 拒绝所有下单 |

---

## 二、Windows 端部署

### 前置条件

- Windows 10/11

### 一键部署

在 Windows VM/机器上运行（以管理员身份）：

```powershell
# 方法1：使用部署脚本
powershell -ExecutionPolicy Bypass -File scripts/setup_windows_trader.ps1

# 方法2：手动操作
```

### 启动交易服务端

```batch
# 2. 双击 start_trader_server.bat（由部署脚本生成）
#    或手动运行：
```

服务端启动后默认监听 `0.0.0.0:1430`。

### 验证

在 macOS 上运行：

```bash
REMOTE_TRADER_HOST=<Windows_IP> \
TRADE_MODE=live \
ENABLE_REAL_TRADING=false \
python3 scripts/live_trading_scheduler.py ping
```

预期输出：
```
  ✅ 状态: ok
  📝 信息: 连接正常，持仓0只
```

---

## 三、上线检查清单

### 每日检查

- [ ] `live_trading_scheduler.py ping` 返回 ok

### 上线前（dry-run 1周）

- [ ] 每天跑 `live_trading_scheduler.py scan`（dry-run）
- [ ] 对比日志中的买卖信号是否合理
- [ ] 确认 pre_trade_check 无异常拒绝
- [ ] 检查 Windows 端连接稳定性（无断连）

### 正式上线

- [ ] 注释掉旧 crontab `sim_trading.py scan`
- [ ] 启用新 crontab `live_trading_scheduler.py scan`
- [ ] `ENABLE_REAL_TRADING=true`
- [ ] 首日手动盯盘，确认下单正确
- [ ] 开启飞书告警：每笔实盘成交推送

### 回退方案

```bash
# 恢复模拟盘（秒级回退）
export TRADE_MODE=sim
python3 scripts/live_trading_scheduler.py scan  # 切回 MySQL 模拟
# 恢复旧 crontab
crontab -e  # 取消注释 sim_trading.py scan 行
```

---

## 四、故障处理

| 问题 | 原因 | 解决 |
|------|------|------|
| `ping` 返回 timeout | 防火墙阻拦端口 1430 | Windows 防火墙放行 1430 端口 |
| 下单被 rejected | pre_trade_check 拒绝 | 检查日志 `trade_risk_checks` 表具体原因 |
| 股票买不进 | 涨停/停牌 | 已内置涨停检查，正常跳过 |

---

## 五、MySQL 实盘表

```sql
real_orders       -- 实盘订单记录（所有实盘下单写入）
trade_risk_checks -- 风控检查审计日志（每笔检查写入）
```

在 `live_trading_scheduler.py init` 时自动创建。
