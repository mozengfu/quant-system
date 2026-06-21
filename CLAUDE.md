# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 量化交易系统

智能量化选股分析 + 实盘交易系统。v2.0，模块化重构后。

## 开发命令

```bash
# 安装依赖
pip install -e .
pip install -r requirements.txt          # 后端
cd frontend && npm install && cd ..      # 前端

# 前端开发
cd frontend && npm run dev        # Vite 开发服务器 (:5173, 自动代理 /api → :5001)
cd frontend && npm run build      # 构建到 dist/ → FastAPI /app/ 路由 serve

# Lint (ruff)
ruff check .
ruff check --fix .
ruff format --check .
ruff format .

# 测试
pytest
pytest -v
pytest -m "not slow"              # 跳过慢测试（不加载模型/连DB）
pytest tests/test_inference.py -v
pytest tests/test_inference.py::TestModelAvailability -v
pytest -k "v11" -v                # 按关键字筛选
bash verify.sh                    # 自检脚本（环境检查+连接测试）

# 运行 Web 服务
python3 app.py                    # FastAPI 主服务 (端口 5001)

# 市场监控守护进程
bash scripts/start_monitor.sh
python3 scripts/market_monitor.py
cat data/market_state.json

# 交易调度（当前管线: 每周板RPS → Top5板块 → ML排序 → 买入候选）
python3 scripts/live_trading_scheduler.py scan       # 盘后选股 (17:30)
python3 scripts/live_trading_scheduler.py monitor    # 盘中监控+实时扫描选股 (9:35择时执行)
python3 scripts/live_trading_scheduler.py morning    # 早盘择时买入 (9:35)
python3 scripts/live_trading_scheduler.py status     # 持仓状态
python3 scripts/live_trading_scheduler.py init       # 初始化表结构
python3 scripts/live_trading_scheduler.py sync       # 同步持仓到 JSON
python3 scripts/live_trading_scheduler.py ping       # QMT 远程连接健康检查
python3 scripts/live_trading_scheduler.py keepalive  # QMT 保活

# 模拟交易
python3 scripts/sim_trading.py scan       # 每日ML扫描（盘后执行）
python3 scripts/sim_trading.py v4_scan    # V4级联策略候选扫描
python3 scripts/sim_trading.py status     # 账户状态/持仓/交易记录
python3 scripts/sim_trading.py init       # 建表初始化

# 回测
python3 run_backtest_v11.py                           # 全量回测（注意：含有数据泄露，见脚本注释）
python3 run_backtest_v11_walkforward.py                 # Walk-Forward 无泄漏回测（推荐）
python3 run_backtest_v11_walkforward.py --n_folds 3     # 少 fold 数（更快速，约 2/3 训练时间）
python3 run_backtest_v11_walkforward.py --max_date 2025-12-31  # 指定数据截止日期
python3 run_backtest_v4_pool.py
python3 scripts/backtest_all_strategies.py
python3 scripts/backtest_pure_ml.py              # 纯ML+风控回测
python3 scripts/backtest_current_pipeline.py      # 当前管线回测
python3 scripts/backtest_topdown.py               # TopDown回测
python3 scripts/backtest_scanner.py               # 实时扫描回测
python3 scripts/backtest_param_scan.py            # 参数扫描优化

# ML 训练（无数据泄露版）
# 训练脚本已修复两个数据泄露源：
#   1. NaN填充: walk-forward 中每折独立计算中位数，不用全量数据
#   2. 标签3-sigma截断: walk-forward 中每折独立截断，不用全量数据
# ⚠️ 月训练提醒：每月1号执行重训练，防止IC衰减
#   一键重训（需Windows开机）：python3 scripts/retrain_monthly.py
#   或Mac快速重训（Top500）：python3 scripts/retrain_v11_fast.py
# Windows 训练机 (192.168.10.39): 全量训练
ssh quant@192.168.10.39
cd C:\Users\quant\quant-system
# 选择训练脚本：
python ml_train_v11_2.py           # V11.2 retrain (thin, ~900MB mem)
python ml_train_v11_3.py           # V11.3 实验 (10日标签)
# Mac 本地: 仅小样本训练或增量
bash scripts/rolling_train.sh      # 滚动训练（ML+TopDown）
python3 ml_train_v11_0.py          # V11.0 全量（仅小样本可行）
python3 scripts/train_topdown.py   # TopDown 三层训练
python3 scripts/train_factor_ranker.py

# ML 质量分析
python3 run_ml_quality_analysis.py    # 全面分析
python3 run_ml_quality_fast.py        # 快速分析
python3 run_ml_quality_v2.py          # V2 版分析

# Alpha 信号
python3 alpha_filter.py              # 从新浪财经多频道提取 Alpha 信号
python3 alpha_signal_integration.py  # 将 Alpha 信号注入 ML 预测

# AI 模拟炒股跟踪
python3 ai_sim_trading.py daily      # 每日记录 TOP5 推荐
python3 ai_sim_trading.py track      # 追踪后续收益表现
python3 ai_sim_trading.py stats      # 统计胜率/平均收益

# 其他分析
python3 ml_daily_top5.py             # 每日 Top5 预测输出
python3 ml_regime_detector.py        # 市场状态分类检测
python3 sector_rotation.py           # 板块轮动分析
python3 run_factor5_backtest.py      # 5因子回测
python3 run_factor7_backtest.py      # 7因子回测
python3 run_three_strategies.py      # 三策略综合回测

# 日常预测 & 数据同步
python3 scripts/daily_ml_predict.py
python3 scripts/predict_v11.py
python3 scripts/predict_v11_2.py
python3 scripts/predict_v11_oos.py
python3 scripts/precompute_v11.py
python3 scripts/sync_tushare_boards.py
python3 scripts/update_daily_price_cron.py
python3 scripts/calc_technical.py
bash scripts/auto_refresh_data.sh
```

## 架构概览

### 双系统并线

代码经历了重构，目前并存两套结构：

**入口 (app.py):** `app.py → app_api.py` 挂载所有路由 + `quant_app.routes.*` 新路由。`app_core.py` 现已精简为仅加载 dotenv + 设置 sys.path 的 stub（37行），仅 `app_api.py` 导入它。

**模块化 (quant_app/):** 按职责拆分的包结构，新代码优先写入此处。根目录的 `ml_predict.py`、`market_monitor.py`、`live_trading_scheduler.py` 等独立脚本仍在大量使用，**不要擅自删除这些模块**。

`app.py` 同时启动两种体系：在 `app_api.py` 中创建 FastAPI app，注册所有子路由（auth/dashboard/market/strategy/etc.），再挂载前端 SPA 和 WebSocket 端点。

**路由注册链**: 每个 `quant_app/routes/*.py` 导出独立的 `APIRouter(prefix="/api/xxx")`，`app_api.py` 通过 `app.include_router(xxx_router)` 统一注册。新增 API 端点时在对应 route 模块中添加路由函数，FastAPI 自动挂载到 `/api/xxx/...` 下。

### 前端架构

```
frontend/src/
├── main.js              Vue 3 入口，注册 Element Plus + Pinia + Router
├── App.vue              根组件
├── router/index.js      hash 路由（10 页面），createWebHashHistory
├── stores/              Pinia 状态管理
│   ├── market.js        市场状态（WebSocket 实时更新）
│   ├── portfolio.js     持仓/收益数据
│   └── trading.js       交易面板状态
├── api/                 axios 请求封装（按模块拆分）
│   ├── index.js         通用请求 + 拦截器
│   ├── trading.js       交易 API
│   ├── market.js        行情 API
│   ├── ml.js            ML 推荐 API
│   └── data.js          数据查询 API
├── utils/ws.js          useWebSocket() composable（自动重连 5s）
├── views/               10 个页面组件（Pipeline/Trading/Recommend/Signals/Backtest/Pnl/Analysis/Market/Login/Admin）
└── components/          可复用组件
```

**技术栈**: Vue 3 (Composition API) + Pinia + Element Plus + Vue Router (hash mode) + Vite + axios

**WebSocket 集成**: `ws.js` 提供 `useWebSocket(url, handlers)` composable，5s 自动重连。handlers 按 `data.type` 分发到对应 store 更新。

**Vite 开发代理**: `vite.config.js` 配置 `/api` → `http://localhost:5001` 代理，`base: '/app/'` 匹配生产 nginx 路径。

**生产部署**: `npm run build` → `frontend/dist/`，`app_api.py` 通过 `app.mount("/app", StaticFiles(...))` 直接 serve，无需 nginx 分路径代理。

**前端与后端的数据流**:
```
用户操作 → Pinia action → axios API 请求 → FastAPI /api/* → MySQL/QMT
WebSocket /api/ws ← PipelineEventBus ← market_monitor.py 文件轮询
                                        ← 其他后端事件
    ↓
ws.js onMessage → 按 type 分发 → market/portfolio/trading store → 组件响应式更新
```

### 部署架构

```
Mac (192.168.10.30, 本地开发/DB/Web)
├── app.py + app_api.py       FastAPI 主服务 (:5001, uvicorn)
├── market_monitor.py          守护进程，30s拉上证实时行情 → data/market_state.json
├── market_state.py            市场状态判定逻辑
├── ml_predict.py              ML 预测主引擎（V6~V11 多版本，加载 data/*.pkl）
├── live_trading_scheduler.py  实盘交易调度器，选股→评分→下单
│   └── HTTP POST 192.168.10.25:1430 (QMT HTTP 桥)
├── scripts/                   可执行脚本（训练、同步、监控、诊断）
├── data/                      运行时数据（.pkl/.json/.parquet/.csv）
├── quant_app/                 模块化 Python 包
├── frontend/                  Vue 3 + Vite SPA
└── templates + static         旧版 Jinja2 前端

QMT机器 (192.168.10.25, 国信iQuant交易终端, Windows)
├── iquant_http_service.py     Flask HTTP API (:1430)
├── qmt_strategy_v5.py         iQuant 主力策略（gbk 编码，行情+交易一体）
├── qmt_strategy_v19~v23.py    iQuant 策略迭代版（尝试不同选股池/风控逻辑）
│   ├── 股票池: stockpool.json (OOS-v2 Top10 + 技术面扫描), 60只
│   ├── 行情推流: qmt_market.json / qmt_index.json
│   ├── 交易: passorder() + 轮询确认成交
│   └── 成交自动写入 MySQL qmt_trades 表
└── 账户: 170000758981（实盘）

Windows训练机 (192.168.10.39, 32GB RAM + 16GB VRAM)
├── ml_train_v11_2.py         全量训练脚本（~13分钟）
└── 训练完将 .pkl 传回 Mac data/
```

### 项目目录

| 目录/根文件 | 说明 |
|---|---|
| `app.py` / `app_api.py` | FastAPI 主入口 + app 创建/路由注册/中间件/WebSocket |
| `app_core.py` | 精简 stub（37行），仅加载 dotenv + sys.path，供 `app_api.py` 导入 |
| `ml_predict.py` | ML 推理主引擎（V6~V11），特征构建+批量预测 |
| `market_state.py` | 市场状态判定（恐慌/阻断/逆市/偏弱/常态） |
| `quant_app/` | 模块化包: `routes/`, `services/`, `trading/`, `backtest/`, `features/`, `data/`, `risk/`, `utils/`, `models/`, `pipeline/` |
| `scripts/` | 独立可执行脚本（交易调度、QMT、ML预测、回测、数据同步、诊断） |
| `alpha_filter.py` | 新闻情绪 Alpha 信号提取（新浪财经多频道 → alpha_signals 表） |
| `alpha_signal_integration.py` | Alpha 信号注入 ML 预测（增加 boost 因子） |
| `ai_sim_trading.py` | AI TOP5 推荐跟踪系统（记录→追踪→统计胜率） |
| `ml_train_v11_*.py` | Mac 本地训练脚本（仅小样本，全量需 Windows 训练机） |
| `run_backtest_*.py` | 回测启动脚本（v11/v4_pool/v4_pool_filter/alpha 等） |
| `run_ml_quality_*.py` | ML 模型质量分析（全面/快速/v2） |
| `frontend/` | Vue 3 + Vite SPA（10页面，hash 路由） |
| `config/` | 扫描策略 YAML 配置 |
| `data/` | 运行时数据（模型 .pkl、状态 .json、预测 .parquet、股票池 .csv） |
| `tests/` | 测试（`test_inference.py` ML 推理管线测试） |
| `archive/` | 历史脚本归档（仅参考，不删除） |
| `MEMORY.md` | 历史决策记录 + 版本演进日志 — **排查不明行为或回顾架构变更时优先查阅此文件** |
| `ml_regime_detector.py` | 市场状态分类检测器（独立运行） |

### quant_app 子包补充

| 子包 | 说明 |
|---|---|
| `quant_app/models/` | 预测子模型: 主浪检测(`main_wave_detector_v1`)、市场方向(`market_direction_v1~v3`)、板块轮动(`sector_rotation_v1`)、板块热度(`sector_heat_v1`)、主升浪捕手(`wave_catcher_v1`)、三层集成(`topdown_pipeline`) |
| `quant_app/pipeline/` | 预测管线变体: `topdown_predictor`(自上而下)、`v11_enhanced_predictor`(V11+主升浪集成)、`v11_sector_predictor`(V11+板块动量) |
| `quant_app/features/` | 特征构建统一入口 `build_features_for()`，含 v11/pattern/sector_relay/LHB/HSGT/research 等模块 |
| `quant_app/risk/` | 风控过滤: `hot_money.py`(游资过滤)、`position_manager.py`(仓位管理)、`sector.py`(行业分散)、`filters.py`(过滤管线) |
| `quant_app/backtest/` | 回测引擎: `engine.py`(简单信号回测)、`strategy_engine.py`(完整策略回测，含T+1/滑点/手续费/止损止盈) |
| `quant_app/services/` | 业务服务: `board_rps_scanner.py`(每周板RPS筛选+ML排序)、`realtime_scanner.py`(盘中实时扫描)、`scanner_strategy.py`(策略参数读取)、`scanner_backtest.py`(扫描策略回测)、`factor_scorer.py`(5因子等权打分)、`strategy_service.py`(V4+ML过滤策略)、`market_service.py`、`technical_service.py`(技术指标)、`qmt_adapter.py`(QMT协议适配)、`backtest_service.py`(回测数据/指标)、`notification_service.py`(飞书/企微/邮件/短信)、`market_state.py`(服务层市场状态封装) |
| `quant_app/data/database.py` | SQLAlchemy 引擎创建 + `with_session()` 上下文管理器 |
| `quant_app/data/models/` | SQLAlchemy ORM 模型: `daily_price`、`stock_info`、`fina_indicator`、`moneyflow`、`margin`、`board`、`market_index`、`sector_moneyflow` |
| `quant_app/data/track/` | 持仓追踪 `sim_positions`/`sim_signals` 表读写 |
| `quant_app/trading/config.py` | 交易配置单例(TRADE_MODE/券商/安全控制) |
| `quant_app/trading/orders.py` | 数据模型: `Order`/`Position`/`Balance` dataclass |
| `quant_app/trading/trade_recorder.py` | QMT 成交记录写入 `qmt_trades` 表 |
| `quant_app/trading/modes/` | 执行器具体实现: `remote_executor.py` (生产)、`sim_executor.py` (模拟) |
| `quant_app/trading/risk/` | 风控: `pre_trade_check.py` |
| `quant_app/services/market_state.py` | 服务层市场状态封装，与根目录 `market_state.py` 独立脚本配合使用 |

## 跨模块架构模式

以下模式需要阅读多个文件才能理解，在此归纳。

### WebSocket 事件推送链

```
market_monitor.py（30s 轮询拉行情）
  └─ 写 data/market_state.json

app_api.py（startup 时）
  └─ asyncio 定时 (5s) 轮询 market_state.json mtime 变化
      └─ PipelineEventBus.publish({"type": "market_update", ...})
          └─ WebSocket /api/ws 广播给所有连接的 SPA 客户端

前端 frontend/src/utils/ws.js
  └─ useWebSocket() 自动重连
      └─ store 按 data.type 分发: market_update → market store
```

**关键文件**: `app_api.py:109-131` (event bus), `app_api.py:234-253` (file watcher), `frontend/src/utils/ws.js` (client)

### 实时行情降级链路

```
quant_app/services/realtime_service.py
  │ 缓存(30s TTL) → 命中直接返回
  ▼
腾讯财经 API（主源，3s 超时，支持涨停价/跌停价）
  │ 失败
  ▼
东方财富 push2 API（备选，3s 超时）
  │ 失败
  ▼
阿里云行情 API（兜底，AppCode 认证，3s 超时）
  │ 全部失败
  ▼
返回缓存过期数据（不抛异常，尽可能返回数据）
```

**规则**: 所有实时行情必须走 `realtime_service.py`，禁止在业务逻辑中直接调用外部行情 API。

### Config 链

```
.env（敏感参数，被 gitignore）
  │ python-dotenv 载入
  ▼
quant_app/utils/config.py → Config 单例（命名空间分组）
  ├── config.mysql.url、config.mysql.get_connection_params()
  ├── config.tushare.token
  ├── config.notification.feishu_webhook / smtp / sms
  └── config.data_dir（= ROOT / "data"）

模块级向后兼容别名（同文件底部）:
  ├── MYSQL_HOST = config.mysql.host  （旧代码 from config import MYSQL_HOST）
  ├── FEISHU_WEBHOOK = …
  └── db_connection 上下文管理器（pymysql）
```

**新代码优先用 `from quant_app.utils.config import config` 的命名空间方式。**

`config/scanner_config.yaml` 管理实时扫描策略参数：资金分配(scanner_ratio)、评分阈值(min_score)、技术因子权重(momentum/trend/volume_breakout/RSI/布林)、风控(stop_loss/take_profit/trailing)、仓位(max_positions)。由 `quant_app/services/scanner_strategy.py` 读取，QMT 不可达时降级到其中的 `capital.total`。

### 双数据库访问模式

| 方式 | 模块 | 适用场景 |
|---|---|---|
| pymysql + `db_connection()` | `quant_app/utils/config.py` | 旧代码兼容、adhoc SQL |
| SQLAlchemy + `with_session()` | `quant_app/data/database.py` | 新代码推荐、ORM 查询 |

两者共存，连接同一个 MySQL (192.168.10.30:3306/quant_db)。SQLAlchemy 引擎连接池化 (pool_size=20)，pymysql 走单连接上下文管理器。SQLAlchemy 2.0+ 注意：`pd.read_sql` 含 IN 子句时 `params=tuple(ts_codes)`，不能用 list。

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
ml_predict.py → _load_best_model() → 降级链: V11.0 → V8.1 → V11.2(thin)
```

**规则**: 禁止直接 `joblib.load` 文件路径。新模型先注册到 `_MODEL_REGISTRY`，再通过 `load_model(version)` 加载。

### 交易执行器模式（策略模式）

```
quant_app/trading/executor.py
  └─ AbstractTradeExecutor 抽象基类
      └─ create_executor(mode) 工厂
          ├─ mode="live" → RemoteTraderExecutor（HTTP → QMT :1430, 生产主力）
          ├─ mode="sim"  → SimExecutor（写入 MySQL sim_signals）
          └─ mode="easytrader" → EasytraderExecutor（本地直连, 备用）
```

执行器统一接口: `buy()`, `sell()`, `partial_sell()`, `get_positions()`, `get_balance()`, `get_orders()`, `cancel()`

### 通知服务多通道

`quant_app/services/notification_service.py` 提供 4 种通知通道，全为独立函数：

- `send_feishu(message)` — 飞书群机器人 webhook
- `send_wecom(message)` — 企业微信机器人 webhook
- `send_email(to, subject, content)` — SMTP SSL
- `send_sms(phone, template_code, params)` — 阿里云短信 API

通道开关由 `.env` 中相应 webhook/token 是否为空控制。

### 生产管线（盘后扫描 + 盘中双管线）

#### 盘后扫描 (cmd_scan, 17:30)
```
板RPS周线(836板块) → 过滤噪音板块 → 周累积收益排序 → Top5板块
  → board_concept_cons 取成分股 → 排除ST/688/8xx → 成分股池
  → ML(V11.2)排序 → 候选股入库 → 次日 V11择时监控
```
**关键文件**: `quant_app/services/board_rps_scanner.py`, `scripts/live_trading_scheduler.py:374`

#### 盘中监控 (cmd_monitor, 每分钟 9:15-15:00)
三条线并行：

```
① 持仓监控
  遍历 QMT 实盘持仓:
    止损: min( max(ATR动态, 固定-7%), -5%硬兜底 )
    移动止盈: 峰值>5% 回撤2×ATR(保底3%) → 市价卖
    超时: ≥5天且盈利<3% → 卖
    强制: >8天 → 卖

② V11择时入场
  盘后候选股 → 监控量价条件(回踩开盘价/量比/涨跌幅)
  → 满足则买入(资金上限: 总资产×50%÷3)

③ 板RPS实时扫描
  实时重算板RPS→Top5板块→成分股
  → 过滤仅 QMT 快照 (110只) 有的股票
  → 实时因子评分(量能/动量/趋势/流动/RSI/布林/盘口/日内/资金/指数)
  → ML排序 → 综合分 = ML概率×50 + 实时分×0.5
  → 实时分≥60 且 ML概率>0 → 按综合分遍历 → 预算够就买
```

**资金分配**: 总资产 × 50% = V11预算(70%) + 实时扫描预算(30%)

#### 风控参数（按市场状态）

| 状态 | 触发条件 | 止损 | 止盈 | max_pos | 仓位上限 |
|---|---|---|---|---|---|
| 趋势上涨 | 指数趋势向上 | -7% | 移动止盈 | 4 | 50% |
| 震荡(常态) | 横盘 | -7% | 移动止盈 | 3 | 50% |
| 趋势下跌 | 指数向下 | -7% | 移动止盈 | 2 | 50% |
| 恐慌 | 跌>2.5%+涨跌比<0.3 | -2% | 3% | 1 | 5% |
| 过热 | 持续放量上涨 | -7% | 移动止盈 | 2 | 50% |

**补充规则**:
- 硬性兜底: 所有持仓成本×0.95 (-5%) 为最后防线
- 恐慌清仓: 跌>3.5% 全仓市价清仓+飞书告警
- 逆市: 跌1~2% 时仓位减半, 实时扫描min_score提高到75

### 回测引擎架构

`quant_app/backtest/` 提供两套回测引擎，不同场景选不同引擎：

```
StrategyBacktest (strategy_engine.py)  ← 完整仿真，用于策略评估
  ├── T+1 真实成交（次日开盘价）
  ├── 涨跌停成交概率处理
  ├── 滑点: 买入+0.15%, 卖出-0.15%
  ├── 手续费: 买入万2.5, 卖出万2.5+印花千1
  ├── 仓位按信号强度分仓（高/中/低）
  ├── 多档移动止盈 + 时间止损
  └── 输出: 资金曲线 + 交易记录 + 年化/夏普/回撤/胜率/盈亏比

BacktestEngine (engine.py)  ← 轻量信号回测，用于模型对比
  ├── 直接使用 signal_fn(trade_date) → list[str]
  ├── 以开盘价买入，N日后开盘价卖出
  ├── 不含滑点/手续费/仓位管理
  └── 输出: 总收益、胜率、交易记录
```

**选型规则**: 策略评估/参数优化用 StrategyBacktest；模型对比/信号质量验证用 BacktestEngine。

**启动入口**: `run_backtest_*.py` 和 `scripts/backtest_*.py` 选择相应引擎并配参数。

### TopDown 三层预测管线

V11 之外的另一条预测路径，按「大盘→板块→个股」自上而下选股：

```
quant_app/models/topdown_pipeline.py — 三层集成入口

Layer1: market_direction_v1/v2/v3
  └─ 判断市场状态（牛市/熊市/震荡/反弹）
  └─ 输出: market_regime(one-hot) + prob_bull + 仓位乘数

Layer2: sector_rotation_v1 + sector_heat_v1
  └─ 板块热度排名，识别资金流入方向
  └─ 输出: sector_heat_score + rank_pct

Layer3: wave_catcher_v1（主升浪捕手）
  └─ LightGBM binary classifier
  └─ 预测个股 1~3 天内启动主升浪的概率
  └─ 特征: V11 117维 + Layer1输出 + Layer2输出 + 突破特征

候选池 = Top300成交额 ∩ Top5热点板块成分股
综合评分 = 0.3×market + 0.3×sector + 0.4×wave
```

依赖路径: `quant_app/models/labels.py` (构建主升浪标签) → `scripts/build_main_wave_labels*.py` → 训练 `wave_catcher_v1`

### 风控过滤管线

预交易风控链，选股结果提交到执行器之前拦截风险：

```
选股候选列表
  │
  ▼
risk/filters.py → apply_risk_filters()
  ├── risk/hot_money.py
  │   └─ 游资净流入/流出评分，过滤纯游资炒作票
  │
  ├── risk/sector.py
  │   └─ apply_sector_diversification()
  │       └─ 行业分散度控制，同行业不超过 N 只
  │
  ├── risk/position_manager.py
  │   └─ 仓位上限检查，总仓位/单票仓位限制
  │
  └── trading/risk/pre_trade_check.py
      └─ 盘前检查: 涨跌停可买性、当日已买量/金额限制
```

`live_trading_scheduler.py` 和 `sim_trading.py` 在执行买入前都会过此过滤链。

## 核心数据流

### ML 预测管线

```
MySQL: daily_price + 因子表（80天历史）
  │
  ▼
ml_predict.py
  ├── build_features_v11_inference() → 131维特征（价格/成交量/动量/资金流/技术/板RPS）
  ├── _load_model(version) → 通过 model_loader.py 注册表
  ├── predict_batch(ts_codes) → 批量预测
  └── predict_single(code) → 个股预测（依赖缓存批量结果）
      │
      └── 输出: {ml_score, ml_prob, rank_pct}
```

**特征**: `quant_app/features/v11_features.py` — 131特征（原117维+板RPS指标等）

**模型**: V11.2(板RPS) 18子模型 LGB LambdaRank 集成, IC=0.139

**模型降级链**: V11.2 → V11.0 (mac_retrain) → V11.2原始

### 交易执行管线

```
live_trading_scheduler.py (Mac, crontab * 9-15 * * 1-5)
  │ 每分钟:
  │   → 持仓监控(止损/移动止盈/超时) → 触发则市价卖出
  │   → V11择时入场
  │   → 板RPS实时扫描 → 实时因子评分 → ML排序 → 买入
  │
  └── RemoteTraderExecutor (create_executor("live"))
      └── HTTP POST → iquant_http_service.py :1430 (QMT桥)
          └── qmt_strategy_v5.py (iQuant策略) → passorder()
```

**数据源**:
- QMT 快照 `/market/snapshot` (110只) → 实时扫描候选池
- QMT `/position`, `/balance` → 持仓/资金
- QMT `/orders`, `/trades` → 委托/成交
- 腾讯行情接口(降级) → 持仓价格监控

### 市场状态风控

```
market_monitor.py（守护进程，30s 拉上证 → data/market_state.json）

├── 恐慌(跌>2.5%+涨跌比<0.3):  阻断建仓, max_pos=1, 止损-2%
├── 恐慌清仓(跌>3.5%):         全部市价卖出+飞书告警
├── 逆市(跌1~2%):              threshold=2.5, 仓位减半
├── 常态/趋势上涨:              max_pos=3~4
└── 硬性兜底:                  所有持仓成本×0.95必触发
```

**WebSocket**: `/api/ws` — market_state 变更时推送至前端

### 仓位与资金管控

- V11(70%) + 实时扫描(30%) 各占独立仓位，互不阻塞
- `总资产 × 50%` 为总投资资金，按比例分给 V11 和 扫描
- 每轮买入前查询今日已用资金，超预算跳过
- 持仓自动归类: `sim_positions.strategy` 含 qmt_sync/ML/扫描等标签
- 单票最高 30%（硬性上限，减仓执行触达）

## API 端点一览

完整路由注册在 `app_api.py` 的 `app.include_router()` 链中，按 APIRouter 模块分组：

| 路由模块 | prefix/tags | 主要端点 |
|---|---|---|
| `trading` | `/api/trading` | `/orders`, `/cancel`, `/cancel-all`, `/market-order`, `/batch-order`, `/trades`, `/balance`, `/positions`, `/status`, `/connect` |
| `auth` | `/api/auth` | `/register`, `/login`, `/forgot-password`, `/reset-password`, `/change-password`, `/me` |
| `scanning` | scanning | `/api/scan`, `/api/scan_pool`, `/api/scan/strong`, `/api/scan/aimodel`, `/api/scan/top5`, `/api/scan/v5`, `/api/scan/ml`, `/api/scan/rule`, `/api/scan/bottom-awakening`, `/api/combo_scan`, `/api/market_state`, `/api/pool`, `/api/ml_top15`, `/api/ai_sim/performance`, `/api/ai_sim/run` |
| `backtest` | `/api/backtest` | `/ml`, `/scanner` |
| `admin` | `admin` | `/access_log`, `/log_stats`, `/log_import`, `/users`, `/pending`, `/approve`, `/reject` |
| `strategy` | strategy | `/api/analysis/{market}/{code}`, `/api/sentiment`, `/api/blocks`, `/api/strategy/compare` |
| `pipeline` | — | `/api/pipeline/status` |
| `pnl` | — | `/api/pnl/summary` |
| `market` | — | 行情/指数相关 |
| `dashboard` | — | 仪表盘聚合 |
| `recommend` | — | ML 推荐 |
| `signals` | — | QMT 委托信号 |
| `pages` | — | Jinja2 页面路由 |
| (app_api.py 直挂) | — | `/api/scanner/signals`, `/api/scanner/buy`, `/api/ws` |

**WebSocket**: `/api/ws` — PipelineEventBus 广播，type 分发给 market/portfolio/trading store。5s 心跳保活。**无自动重连逻辑在前端（ws.js 实现了 5s 重连）**。

## ML 模型

| 模型 | 文件 | 指标 | 状态 |
|---|---|---|---|
| **V11.2(板RPS)** | `data/ml_stock_model_v11_0.pkl` (159MB) | 集成 IC=0.139, 18子模型, 131特征 | **生产 (2026-06-13)** |
| V11.0 Mac重训练 | `data/ml_stock_model_v11_0_mac_retrain.pkl` (98MB) | WF IC=0.043 | 备用 |
| V11.2 原始 | `data/ml_stock_model_v11_2.pkl` (19KB) | — | 备用(thin) |

所有模型通过 `quant_app/utils/model_loader.py` 注册表管理。生产模型降级链: v11.0 → v11.2。

## 数据库

- **MySQL 5.7+** on 192.168.10.30:3306, database `quant_db`
- 数据源：tushare pro 定期同步
- 核心表：`daily_price`, `stock_info`, `trade_cal`, `fina_indicator`, `moneyflow`, `margin`, `board`, `board_member`, `sim_signals`, `nav_history`, `qmt_trades`
- 连接方式见上方「双数据库访问模式」

## 关键约定

### 模块导入
- `quant_app` 内部使用相对或绝对导入均可
- 根目录脚本使用 `sys.path.insert(0, ...)` 后从 `quant_app` 导入

### 编码规范
- Ruff 配置在 `pyproject.toml` `[tool.ruff]` 下: line-length=120, target py312, select E/F/I/W/UP
- 忽略 E501（行长度）和 E741（单字母变量名如 df）
- `.env` 被 gitignore 排除，参照 `.env.example`

### 配置文件
- `.env` 定义所有敏感参数（参照 `.env.example`），分组：
  - 数据源: `TUSHARE_TOKEN`（tushare pro token）
  - MySQL: `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`
  - 通知: `FEISHU_WEBHOOK` / `WECOM_WEBHOOK` / SMTP / 阿里云 SMS
  - 交易: `TRADE_MODE` (sim/live) / `ENABLE_REAL_TRADING`（安全开关）/ `REMOTE_TRADER_HOST:PORT`
  - 风控: `MAX_DAILY_LOSS_PCT` / `MAX_SINGLE_ORDER_AMOUNT` / `MAX_POSITION_PCT`
- `quant_app/utils/config.py` 中 Config 单例按命名空间读取
- 扫描策略参数在 `config/scanner_config.yaml`

## MEMORY.md 查阅指引

`MEMORY.md` 记录了本项目的关键决策、版本演进历史（v1→v2）、重要 Bug 修复。以下情况**必须先查阅 MEMORY.md**：

- 排查历史行为变更原因（"这个功能以前是可以用的"）
- 理解版本号对应关系（V11.0/V11.2/V11.3 等）
- 回顾模块重构过程（`app.py`→`app_api.py`→`quant_app/`）
- 了解已修复的已知问题（数据泄露、配置兼容性等）

**不要**在已知问题列表中有对应 MEMORY 条目时重复造轮子。

## 其他参考文档

| 文档 | 内容 |
|---|---|
| `MEMORY.md` | 历史决策记录 + 版本演进日志 — **排查不明行为时优先查阅** |
| `CODE_REVIEW_REPORT.md` | 代码审查报告（2026-05-30） |
| `REFACTOR_PLAN.md` | 模块化重构计划（v1→v2） |
| `TRADING_DEPLOY.md` | 实盘部署指南（Windows QMT + Mac 端配置） |
| `ML_MODEL_README.md` | ML 模型技术说明 |
| `config/scanner_config.yaml` | 实时扫描策略参数配置 |

## 已知问题

1. **模型 RankIC 低**: A 股 5 日收益难预测，0.02~0.05 属行业正常水平
2. **全量训练 Mac 内存不足**: 必须用 Windows 训练机（32GB），手动传回 .pkl
3. **QMT 因子不可用**: `get_factor_data()` 在 iQuant 策略中无法调用
4. **QMT 股票池仅 110 只**: 板RPS实时扫描候选池受限于 QMT 快照范围，不在池中的票无法评分买入
5. **QMT 持仓无 buy_date**: 时间风控依赖 sim_positions 表补查，需保持 sim_positions 同步
6. **国信iQuant免费行情无Level2盘口**: `ask1/bid1` 恒为0，`factor_orderbook` 等盘口因子无数据。不影响核心交易，需付费订阅Level 2才可解决

## 定时任务（crontab）

项目根 `crontab` 文件定义了 macOS 的自动执行计划，周一至周五运行：

| 时间 | 任务 | 说明 |
|---|---|---|
| `0 9 * * 1-5` | `feishu_alerts.py morning` | 飞书开盘预警 |
| `0 9 * * 1-5` | `auto_refresh_data.sh` | 开盘前数据刷新 |
| `*/30 9-14 * * 1-5` | `auto_refresh_data.sh` | 盘中数据刷新（每 30 分钟） |
| `35 9 * * 1-5` | `live_trading_scheduler.py morning` | 早盘择时买入 |
| `36 9 * * 1-5` | `daily_health_check.py` | 每日开盘健康检查 |
| `* 9-15 * * 1-5` | `live_trading_scheduler.py monitor` | 实时扫描+持仓监控（每分钟） |
| `*/5 9-15 * * 1-5` | `position_monitor.py` | 风控扫描（备用） |
| `*/30 9-14 * * 1-5` | `auto_refresh_data.sh` | 盘中数据刷新（每 30 分钟） |
| `35 9 * * 1-5` | `live_trading_scheduler.py morning` | 早盘择时买入 |
| `5 15 * * 1-5` | `feishu_alerts.py daily` | 飞书盘后汇总 |
| `0 17 * * 1-5` | `update_daily_price_cron.py` | 盘后数据导入 |
| `15 17 * * 1-5` | `scan_daily_pool()` | 股票池生成（含 OOS Top10 注入） |
| `30 17 * * 1-5` | `live_trading_scheduler.py scan` | 盘后选股 |
| `45 17 * * 1-5` | `run_three_strategies.py` | V4 辅助策略 |
| `55 17 * * 1-5` | `scan_bottom_awakening.py` | 底部觉醒扫描 |
| `0 3 * * 6` | `rolling_train.sh` | 周末模型重训 |

## 环境

- MacOS (192.168.10.30): Python 3.13, pandas 2.2.2, MySQL 5.7+, FastAPI
- Windows 训练 (192.168.10.39): Python 3.12, pandas 2.2.2, 32GB RAM, 16GB VRAM
- QMT (192.168.10.25): 国信 iQuant 策略交易平台, iquant_http_service.py :1430
- MySQL: 192.168.10.30:3306, root/root123, quant_db
- 域名: `lh.mozengfu.com.cn` → nginx (8.148.158.153) → 反向代理到 Mac :5001
- SPA 地址: https://lh.mozengfu.com.cn/app/
