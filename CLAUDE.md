# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 量化交易系统

智能量化选股分析 + 实盘交易系统。v2.0，模块化重构后。

> **操作问题先查 `AGENTS.md`**（管线操作、数据库诊断、crash恢复流程）。**架构问题查本文档**（模块关系、数据流、设计模式）。

## 快速启动

```bash
# 安装
pip install -e . && pip install -r requirements.txt
cd frontend && npm install && cd ..

# 启动 Web 服务（端口 5001）
python3 app.py

# 前端开发服务器（端口 5173，/api 代理到 :5001）
cd frontend && npm run dev

# 前端生产构建
cd frontend && npm run build     # → frontend/dist/，FastAPI /app/ 路由自动 serve
```

## 常用命令

### 开发 & Lint
```bash
ruff check . && ruff check --fix .      # 检查 / 自动修复
ruff format . && ruff format --check .  # 格式化 / 验证
pytest -v                                # 全量测试
pytest -m "not slow"                     # 跳过慢测试（不加载模型/连DB）
pytest tests/test_inference.py -v        # 单测试文件
pytest tests/test_inference.py::test_xxx # 单用例
bash verify.sh                           # 自检脚本（环境+连接）
```

### 生产管线（交易调度）
```bash
# 盘后选股 (17:30)
python3 scripts/live_trading_scheduler.py scan
# 盘中监控 (每分钟 9:30-15:00)
python3 scripts/live_trading_scheduler.py monitor
# 早盘择时买入 (9:35)
python3 scripts/live_trading_scheduler.py morning
# 状态/同步/健康检查
python3 scripts/live_trading_scheduler.py status
python3 scripts/live_trading_scheduler.py sync     # 持仓同步到JSON
python3 scripts/live_trading_scheduler.py ping     # QMT 连接检查
python3 scripts/live_trading_scheduler.py keepalive
```

### ML 训练 & 预测
```bash
# Windows 全量训练（ssh quant@192.168.10.39）
python ml_train_v11_2.py      # V11.2 retrain (thin, ~900MB mem)
python ml_train_v11_3.py      # V11.3 实验 (10日标签)
# Mac 小样本训练
bash scripts/rolling_train.sh                    # 滚动训练（ML+TopDown）
python3 scripts/train_topdown.py                 # TopDown 三层训练
# 预测
python3 scripts/daily_ml_predict.py
python3 scripts/predict_v11.py
python3 ml_daily_top5.py                         # 每日 Top5 输出
```

### 回测
```bash
python3 run_backtest_v11_walkforward.py          # Walk-Forward（推荐，无数据泄露）
python3 run_backtest_v11_walkforward.py --n_folds 3
python3 scripts/backtest_pure_ml.py              # 纯ML+风控
python3 scripts/backtest_current_pipeline.py     # 当前管线
python3 scripts/backtest_scanner.py              # 实时扫描
python3 scripts/backtest_param_scan.py           # 参数扫描优化
python3 scripts/backtest_topdown.py              # TopDown
```

### 数据同步
```bash
python3 scripts/update_daily_price_cron.py       # 盘后行情
python3 scripts/backfill_moneyflow.py            # 资金流向
python3 scripts/backfill_margin.py               # 融资融券
python3 scripts/backfill_block_trade.py          # 大宗交易
python3 scripts/sync_tushare_boards.py            # 板块数据
bash scripts/auto_refresh_data.sh                # 全量刷新
```

### 告警 & 诊断
```bash
python3 scripts/feishu_alerts.py morning         # 开盘预警
python3 scripts/feishu_alerts.py daily           # 盘后汇总
python3 scripts/feishu_alerts.py alert           # 盘中告警+心跳
python3 scripts/validate_icir_fast.py            # IC/IR 快速验证
python3 scripts/diag_features.py                 # 特征诊断
python3 scripts/daily_health_check.py            # 开盘健康检查
```

### Alpha 信号
```bash
python3 alpha_filter.py                          # 新浪财经多频道提取
python3 alpha_signal_integration.py              # 注入 ML 预测
python3 ai_sim_trading.py daily                  # AI TOP5 推荐记录
python3 ai_sim_trading.py track                  # 后续收益追踪
python3 ai_sim_trading.py stats                  # 胜率统计
```

### 日内做T策略
```bash
python3 scripts/intraday_t_monitor.py --mode dryrun --once
python3 scripts/intraday_t_analysis.py --days 7 --tune --send
python3 scripts/intraday_t_backtest.py
```

## 管道健康与故障恢复

### 启动检查清单
```bash
# 1) Web 服务
pgrep -f "uvicorn.*5001" || echo "服务未启动"
# 2) 市场监控守护进程
cat data/market_state.json      # 应是最新时间
# 3) MySQL 连接
python3 -c "from quant_app.utils.config import config; print('OK')"
# 4) QMT HTTP 桥
python3 scripts/live_trading_scheduler.py ping
# 5) 模型加载
python3 -c "from quant_app.utils.model_loader import load_model; m=load_model('v11.0'); print(type(m).__name__)"
```

### 常见恢复操作
```bash
# QMT 失联 → 重启 QMT HTTP 服务（192.168.10.25）
#   schtasks /run /tn iquant_http
# 或 scp 更新文件后：
#   copy /Y C:\iquant_http_service.py C:\qmt_service\ && schtasks /run /tn iquant_http

# 模型降级 → 检查 data/ml_stock_model_*.pkl 存在性
#   ls -lh data/ml_stock_model_*.pkl

# crontab 不执行 → 检查日志
#   tail -50 logs/live_trading_monitor.log

# 市场状态异常 → 重启 market_monitor
#   bash scripts/start_monitor.sh

# sim_signals 信号错乱 → 检查 signal_date / created_at
#   SELECT signal_date, ts_code, action, signal_type FROM sim_signals ORDER BY id DESC LIMIT 20;
```

### 重要日志文件
```
logs/live_trading_monitor.log    # 盘中监控（最重要）
logs/feishu_alerts.log           # 飞书推送
logs/daily_import.log            # 盘后行情导入
logs/sim_trading.log             # 模拟交易
logs/retrain.log                 # 月度重训练
logs/intraday_t.log              # 日内做T
logs/backfill_*.log              # 数据回填
```

## 架构概览

### 双系统并线

代码经历了重构，目前并存两套结构：

**入口 (app.py):** `app.py → app_api.py` 挂载所有路由 + `quant_app.routes.*` 新路由。`app_core.py` 精简为仅加载 dotenv + 设置 sys.path（37行）。

**模块化 (quant_app/):** 按职责拆分的包结构，新代码优先写入此处。根目录的 `ml_predict.py`、`market_monitor.py`、`live_trading_scheduler.py` 等独立脚本仍在大量使用，**不要擅自删除这些模块**。

`app.py` 同时启动两种体系：在 `app_api.py` 中创建 FastAPI app，注册所有子路由，再挂载前端 SPA 和 WebSocket 端点。

**路由注册链**: 每个 `quant_app/routes/*.py` 导出独立的 `APIRouter(prefix="/api/xxx")`，`app_api.py` 通过 `app.include_router(xxx_router)` 统一注册。

### 前端架构

```
frontend/src/
├── main.js                 Vue 3 入口（Element Plus + Pinia + Router）
├── App.vue                 根组件
├── router/index.js         hash 路由（10 页面）
├── stores/                 Pinia 状态管理
│   ├── market.js           市场状态（WebSocket 实时更新）
│   ├── portfolio.js        持仓/收益
│   └── trading.js          交易面板状态
├── api/                    axios 请求封装
│   ├── index.js            通用请求 + 拦截器
│   ├── trading/market/ml/data.js  按模块拆分
├── utils/ws.js             useWebSocket() composable（5s 自动重连）
├── views/                  10 页面组件
└── components/             可复用组件
```

**技术栈**: Vue 3 (Composition API) + Pinia + Element Plus + Vue Router (hash mode) + Vite + axios

### 部署架构

```
Mac (192.168.10.30, 开发/DB/Web)
├── app.py + app_api.py                  FastAPI :5001 (uvicorn)
├── market_monitor.py                    守护进程 30s 轮询上证 → data/market_state.json
├── market_state.py                      市场状态判定逻辑
├── ml_predict.py                        ML 推理主引擎（V6~V11）
├── live_trading_scheduler.py            实盘交易调度器
│   └── HTTP POST 192.168.10.25:1430 (QMT HTTP 桥)
├── scripts/                             可执行脚本
├── data/                                运行时数据（.pkl/.json/.parquet/.csv）
├── quant_app/                           模块化 Python 包
├── frontend/                            Vue 3 + Vite SPA
└── templates + static                   旧版 Jinja2 前端

QMT (192.168.10.25, 国信iQuant, Windows)
├── iquant_http_service.py               Flask HTTP API (:1430)
├── qmt_strategy_v5.py                   主力策略（gbk 编码）
├── stockpool.json                        股票池 (60只, OOS-v2 Top10)
├── qmt_market.json / qmt_index.json     行情推流
└── 账户: 170000758981（实盘）

Windows训练机 (192.168.10.39, 32GB RAM + 16GB VRAM)
├── ml_train_v11_2.py                    全量训练（~13分钟）
└── 训练完将 .pkl 传回 Mac data/
```

### 项目目录

| 目录/文件 | 说明 |
|---|---|
| `app.py` / `app_api.py` | FastAPI 主入口 + 路由注册/中间件/WebSocket |
| `ml_predict.py` | ML 推理主引擎 |
| `market_state.py` | 市场状态判定 |
| `quant_app/` | 模块化包: routes/services/trading/backtest/features/data/risk/utils/models/pipeline |
| `quant_app/features/` | 特征构建统一入口 `build_features_for()`: v11/pattern/sector_relay/LHB/HSGT/research |
| `quant_app/models/` | 预测子模型: market_direction_v1~v3, sector_rotation_v1, sector_heat_v1, wave_catcher_v1, main_wave_detector_v1, topdown_pipeline |
| `quant_app/pipeline/` | 预测管线: topdown_predictor, v11_enhanced_predictor, v11_sector_predictor |
| `quant_app/risk/` | 风控: hot_money(游资), position_manager(仓位), sector(行业分散), filters(过滤管线) |
| `quant_app/backtest/` | 回测: engine.py(轻量), strategy_engine.py(完整), utils.py |
| `quant_app/routes/` | API 路由: trading/auth/scanning/backtest/admin/strategy/pipeline/pnl/market/dashboard/recommend/signals/pages |
| `quant_app/services/` | 业务服务: board_rps_scanner/realtime_scanner/realtime_service/strategy_service/notification_service 等 |
| `quant_app/trading/` | 交易: executor(工厂), config, orders, trade_recorder, modes/(remote/sim), risk/pre_trade_check |
| `quant_app/data/` | 数据库: database.py(SQLAlchemy), models/(ORM表), track/(持仓信号读写) |
| `quant_app/utils/` | 工具: config(配置单例), model_loader(模型注册表), auth/authz, indicators, persistence(缓存), json_encoder, risk_config |
| `scripts/` | 可执行脚本（按用途分组: trading/backtest/train/sync/diagnosis/feishu） |
| `data/` | 运行时数据: .pkl(模型), .json(状态), .parquet(预测), .csv(股票池) |
| `frontend/` | Vue 3 + Vite SPA |
| `config/` | scanner_config.yaml(扫描参数) |
| `tests/` | test_inference.py（覆盖较薄；改 ML 推理路径时优先手跑此文件） |
| `migrations/` | Schema 变更 SQL（编号管理；注意已有两个 `002_*` 前缀文件属历史遗留，新增续用下一个可用编号） |
| `AGENTS.md` | **AI Agent 操作手册** — 管线/DB诊断/crash恢复/常用SQL |
| `MEMORY.md` | 历史决策 + 版本演进 — 排查不明行为优先查阅 |

## 跨模块架构模式

### WebSocket 事件推送链

```
market_monitor.py（30s 轮询拉行情）
  └─ 写 data/market_state.json

app_api.py（startup 定时 5s 轮询）
  └─ PipelineEventBus.publish({"type": "market_update", ...})
      └─ WebSocket /api/ws 广播给所有 SPA 客户端

frontend/src/utils/ws.js
  └─ useWebSocket() → store 按 data.type 分发
```

**关键文件**: `app_api.py:109-131` (event bus), `app_api.py:234-253` (file watcher), `frontend/src/utils/ws.js`

### 实时行情降级链路

```
quant_app/services/realtime_service.py
  │ 缓存(30s TTL) → 命中直接返回
  ▼
腾讯财经 API（主源，3s 超时）
  │ 失败
  ▼
东方财富 push2 API（备选，3s 超时）
  │ 失败
  ▼
阿里云行情 API（兜底，AppCode 认证）
  │ 全部失败
  ▼
返回缓存过期数据（不抛异常）
```

**规则**: 所有实时行情必须走 `realtime_service.py`，禁止直接调外部行情 API。

### Config 链

```
.env（被 gitignore，参考 .env.example）
  │ python-dotenv
  ▼
quant_app/utils/config.py → Config 单例（命名空间分组）
  ├── config.mysql.*、config.tushare.token
  ├── config.notification.feishu_webhook / smtp / sms
  └── config.data_dir

模块级向后兼容别名（同文件底部）:
  ├── MYSQL_HOST = config.mysql.host
  └── db_connection 上下文管理器（pymysql）
```

**新代码优先用 `from quant_app.utils.config import config`。**

扫描策略参数: `config/scanner_config.yaml`（资金分配/评分阈值/技术因子权重/风控/仓位），由 `quant_app/services/scanner_strategy.py` 读取。

### 双数据库访问模式

| 方式 | 模块 | 适用场景 |
|---|---|---|
| pymysql + `db_connection()` | `quant_app/utils/config.py` | 旧代码、adhoc SQL |
| SQLAlchemy + `with_session()` | `quant_app/data/database.py` | **新代码推荐**、ORM 查询 |

SQLAlchemy 连接池 (pool_size=20)，pymysql 单连接。`pd.read_sql` 含 IN 子句时用 `params=tuple(ts_codes)`，不能用 list。

### 模型注册/加载模式

`quant_app/utils/model_loader.py` 统一管理所有 ML 模型：

```
_MODEL_REGISTRY = {"v11.0": data/ml_stock_model_v11_0.pkl, ...}
  │
  ▼
@lru_cache(maxsize=4)
load_model(version) → joblib.load(path)
  │
  ▼
ml_predict.py → _load_best_model() → 降级链: v11.0 → v8.1 → v11.2(thin)
```

**规则**: 禁止直接 `joblib.load` 文件路径。新模型先注册到 `_MODEL_REGISTRY`。

### 交易执行器模式（策略模式）

```
quant_app/trading/executor.py
  └─ AbstractTradeExecutor → create_executor(mode) 工厂
      ├─ mode="live" → RemoteTraderExecutor（HTTP → QMT :1430）
      ├─ mode="sim"  → SimExecutor（写入 MySQL sim_signals）
      └─ mode="easytrader" → EasytraderExecutor（本地直连, 备用）
```

统一接口: `buy()`, `sell()`, `partial_sell()`, `get_positions()`, `get_balance()`, `get_orders()`, `cancel()`

### 通知服务

`quant_app/services/notification_service.py` — 4 通道独立函数：
- `send_feishu()` / `send_wecom()` / `send_email()` / `send_sms()`
- 通道开关由 `.env` 中对应 webhook/token 是否为空控制。

### 风控过滤管线

```
选股候选列表
  │
  ▼
quant_app/risk/filters.py → apply_risk_filters()
  ├── hot_money.py    游资净流入/流出评分
  ├── sector.py       行业分散度控制
  ├── position_manager.py  仓位上限/单票限制
  └── ../trading/risk/pre_trade_check.py  盘前检查
```

### 回测引擎

| 引擎 | 文件 | 适用场景 |
|---|---|---|
| `StrategyBacktest` | `strategy_engine.py` | 策略评估/参数优化（含T+1/滑点/手续费/止损止盈） |
| `BacktestEngine` | `engine.py` | 模型对比/信号质量验证（轻量，无滑点/手续费） |

## 核心数据流

### ML 预测管线

```
MySQL: daily_price + 因子表（80天历史）
  │
  ▼
ml_predict.py
  ├── build_features_v11_inference() → 131维特征
  ├── _load_model(version) → model_loader.py 注册表
  ├── predict_batch(ts_codes) → 批量预测
  └── predict_single(code) → 输出: {ml_score, ml_prob, rank_pct}
```

**特征**: `quant_app/features/v11_features.py` — 131特征（价格/成交量/动量/资金流/技术/板RPS）

**模型降级链**: V11.2(板RPS) → V11.0 mac_retrain → V11.2 thin

### 交易执行管线

```
live_trading_scheduler.py（crontab * 9-15 * * 1-5，每分钟）
  │ → 持仓监控(止损/移动止盈/超时) → 触发则市价卖出
  │ → V11择时入场
  │ → 板RPS实时扫描 → 实时因子评分 → ML排序 → 买入
  │
  └── RemoteTraderExecutor HTTP POST → iquant_http_service.py :1430
      └── qmt_strategy_v5.py → passorder()
```

**数据源**: QMT 快照(110只) / QMT position/balance/orders/trades / 腾讯行情(降级)

### 生产管线

**盘后扫描 (17:30)**: 板RPS周线(836板块) → 过滤噪音 → Top5板块 → 成分股 → 排除ST/688/8xx → ML排序 → 候选股入库

**盘中监控 (每分钟)**: 三条线并行：
1. 持仓监控: 止损[min(ATR动态, -7%), -5%硬兜底] / 移动止盈[峰值>5%回撤2×ATR] / 超时[≥5天且<3%]
2. V11择时入场: 盘后候选股 → 量价条件筛选 → 满足则买入
3. 板RPS实时扫描: 实时重算 → QMT快照过滤 → 实时因子评分 → ML排序 → 按综合分遍历买入

**资金分配**: 总资产 × 50% 共享预算，V11和实时扫描先到先得

### 市场状态风控

| 状态 | 触发条件 | 止损 | max_pos | 仓位上限 |
|---|---|---|---|---|
| 趋势上涨 | 指数向上 | -7% | 4 | 50% |
| 震荡(常态) | 横盘 | -7% | 3 | 50% |
| 趋势下跌 | 指数向下 | -7% | 2 | 50% |
| 恐慌 | 跌>2.5%+涨跌比<0.3 | -2% | 1 | 5% |
| 过热 | 持续放量上涨 | -7% | 2 | 50% |

**补充**: -5%硬兜底 / 恐慌清仓[跌>3.5%] / 逆市[跌1~2%]仓位减半

### TopDown 三层预测管线

```
quant_app/models/topdown_pipeline.py

Layer1: market_direction_v1/v2/v3 → market_regime + prob_bull + 仓位乘数
Layer2: sector_rotation_v1 + sector_heat_v1 → sector_heat_score + rank_pct
Layer3: wave_catcher_v1 (LightGBM) → 个股 1~3天主升浪概率

候选池 = Top300成交额 ∩ Top5热点板块
综合评分 = 0.3×market + 0.3×sector + 0.4×wave
```

## ML 模型参考

| 模型 | 文件 | 指标 | 状态 |
|---|---|---|---|
| **V11.2(板RPS)** | `data/ml_stock_model_v11_0.pkl` (159MB) | IC=0.139, 18子模型, 131特征 | **生产 (2026-06-13)** |
| V11.0 Mac重训练 | `data/ml_stock_model_v11_0_mac_retrain.pkl` (98MB) | WF IC=0.043 | 备用 |
| V11.2 原始 | `data/ml_stock_model_v11_2.pkl` (19KB) | — | 备用(thin) |

训练脚本: `ml_train_v11_2.py`(Win全量) / `ml_train_v11_0.py`(Mac小样本) / `scripts/train_topdown.py`

## API 端点一览

| 路由模块 | prefix | 主要端点 |
|---|---|---|
| `trading` | `/api/trading` | orders/cancel/balance/positions/status/connect |
| `auth` | `/api/auth` | register/login/me/change-password |
| `scanning` | scanning | scan/scan_pool/ml_top15/market_state/ai_sim |
| `backtest` | `/api/backtest` | ml/scanner |
| `recommend` | — | ML 推荐 |
| `signals` | — | QMT 委托信号 |
| (app_api.py) | — | `/api/scanner/signals`, `/api/scanner/buy`, `/api/ws` |

完整路由注册链见 `app_api.py` 的 `app.include_router()`。

## 数据库

- **MySQL 5.7+** on 192.168.10.30:3306, database `quant_db`
- 数据源: tushare pro 定期同步
- 核心表: daily_price, stock_info, trade_cal, fina_indicator, moneyflow, margin, board, board_member, sim_signals, nav_history, qmt_trades
- ORM 模型: `quant_app/data/models/` (daily_price/stock_info/fina_indicator/moneyflow/margin/board/market_index/sector_moneyflow)
- 持仓追踪: `quant_app/data/track/` (sim_positions/sim_signals 读写)
- 连接方式: pymysql(db_connection) 旧代码 / SQLAlchemy(with_session) 新代码推荐

## 关键约定

### 编码规范
- Ruff: line-length=120, target py312, select E/F/I/W/UP, ignore E501/E741
- `.env` 被 gitignore，参照 `.env.example`
- `quant_app` 内部用相对或绝对导入均可；根脚本用 `sys.path.insert(0, ...)` 后从 `quant_app` 导入

### 配置文件
- `.env`: TUSHARE_TOKEN / MYSQL_* / 通知webhook / TRADE_MODE / ENABLE_REAL_TRADING / 风控参数
- `config/scanner_config.yaml`: 实时扫描策略（资金分配/评分阈值/技术因子权重/止损止盈/仓位）
- `quant_app/utils/config.py`: Config 单例，新代码优先用命名空间方式

### 已知问题
1. **模型 RankIC 低**: A 股 5 日收益难预测，0.02~0.05 属正常
2. **全量训练 Mac 内存不足**: 必须 Windows 训练机（32GB）
3. **QMT 因子不可用**: `get_factor_data()` 在 iQuant 策略中无法调用
4. **QMT 股票池仅 110 只**: 实时扫描候选池受限
5. **国信 iQuant 免费行情无 Level2 盘口**: 虽有 10 档，仍缺逐笔成交/委托队列

## crontab 时间线

安装: `crontab /Users/mozengfu/workspace/quant-system/crontab`

### 盘中 (周一至周五)
| 时间 | 任务 |
|---|---|
| `* 9-15 * * 1-5` | `live_trading_scheduler.py monitor`（主力） |
| `*/5 9-15 * * 1-5` | `feishu_alerts.py alert` + `position_monitor.py` |
| `0 9 * * 1-5` | `feishu_alerts.py morning` + `auto_refresh_data.sh` |
| `*/30 9-14 * * 1-5` | `auto_refresh_data.sh` |
| `5 15 * * 1-5` | `feishu_alerts.py daily` |

### 盘后 (按依赖顺序)
| 时间 | 任务 |
|---|---|
| `0 17` | `update_daily_price_cron.py`（必须先于所有后续） |
| `5 17` | `backfill_moneyflow.py` + `sync_daily_basic.py` |
| `10 17` | `backfill_block_trade.py` |
| `15 17` | `backfill_margin.py` |
| `30 17` | `sim_trading.py scan` |
| `45 17` | `run_three_strategies.py` |
| `55 17` | `scan_bottom_awakening.py` |

### 日内做T & 月度
| 时间 | 任务 |
|---|---|
| `*/1 9-11,13-14` | `intraday_t_monitor.py --mode dryrun --once` |
| `10 17` | `intraday_t_analysis.py --days 7 --tune --send` |
| `0 3 1 * *` | `cron_retrain.py`（月度重训练） |

## 环境

| 主机 | IP | 角色 |
|---|---|---|
| Mac | 192.168.10.30 | 开发/DB(MySQL:3306/quant_db)/Web服务(:5001) |
| QMT(Windows) | 192.168.10.25 | 国信 iQuant 实盘交易终端(:1430) |
| 训练机(Windows) | 192.168.10.39 | 32GB RAM + 16GB VRAM，全量 ML 训练 |
| 公网 | 8.148.158.153 | nginx → lh.mozengfu.com.cn → 反向代理到 Mac :5001 |
| SPA | https://lh.mozengfu.com.cn/app/ | 前端生产地址 |

## 参考文档

| 文档 | 内容 |
|---|---|
| `AGENTS.md` | **AI Agent 操作手册**（管线详解、DB诊断、crash恢复、常用SQL）|
| `MEMORY.md` | 历史决策 + 版本演进 + 近期修复日志 |
| `TRADING_DEPLOY.md` | 实盘部署指南（Windows QMT + Mac） |
| `ML_MODEL_README.md` | ML 模型技术说明 |
| `config/scanner_config.yaml` | 实时扫描策略参数 |
