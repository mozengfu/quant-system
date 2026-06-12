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

# 运行 Web 服务
python3 app.py                    # FastAPI 主服务 (端口 5001)

# 市场监控守护进程
bash scripts/start_monitor.sh
python3 scripts/market_monitor.py
cat data/market_state.json

# 交易调度
python3 scripts/live_trading_scheduler.py scan       # 盘后选股 (17:30)
python3 scripts/live_trading_scheduler.py monitor    # 盘中监控+实时扫描选股
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
| `MEMORY.md` | 历史决策记录 + 版本演进日志（排查过往变更时参考） |

### quant_app 子包补充

| 子包 | 说明 |
|---|---|
| `quant_app/models/` | 预测子模型: 主浪检测(`main_wave_detector_v1`)、市场方向(`market_direction_v1~v3`)、板块轮动(`sector_rotation_v1`)、板块热度(`sector_heat_v1`)、主升浪捕手(`wave_catcher_v1`)、三层集成(`topdown_pipeline`) |
| `quant_app/pipeline/` | 预测管线变体: `topdown_predictor`(自上而下)、`v11_enhanced_predictor`(V11+主升浪集成)、`v11_sector_predictor`(V11+板块动量) |
| `quant_app/features/` | 特征构建统一入口 `build_features_for()`，含 v11/pattern/sector_relay/LHB/HSGT/research 等模块 |
| `quant_app/risk/` | 风控过滤: `hot_money.py`(游资过滤)、`position_manager.py`(仓位管理)、`sector.py`(行业分散)、`filters.py`(过滤管线) |
| `quant_app/backtest/` | 回测引擎: `engine.py`(简单信号回测)、`strategy_engine.py`(完整策略回测，含T+1/滑点/手续费/止损止盈) |
| `quant_app/services/` | 业务服务: `realtime_scanner.py`(盘中实时扫描)、`scanner_strategy.py`(策略参数读取)、`strategy_service.py`(V4+ML过滤策略)、`market_service.py`、`technical_service.py`(技术指标)、`qmt_adapter.py`(QMT协议适配) |
| `quant_app/data/models/` | SQLAlchemy ORM 模型: `daily_price`、`stock_info`、`fina_indicator`、`moneyflow`、`margin`、`board`、`market_index`、`sector_moneyflow` |
| `quant_app/data/track/` | 数据追踪（预留） |
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
  ├── _build_features_for_stocks_v8_0() → 117维特征（价格/成交量/动量/资金流/技术）
  ├── _load_model(version) → 从 data/*.pkl 加载（通过 model_loader.py 注册表）
  ├── predict_batch(ts_codes) → 批量预测（全 A 股）
  └── predict_single(code) → 个股预测（依赖缓存批量结果）
  │
  ├── 输出排序 → 推荐列表
  └── 存入 data/ml_preds_*.parquet
```

**特征列来源**: `quant_app/features/v11_features.py` — 117 个特征（V11.0），含价格/成交量/动量/资金流/技术指标。

**模型降级链**: `_load_best_model()` → V11.0 → V8.1 → V11.2（thin）。

### 交易执行管线

```
live_trading_scheduler.py (Mac)
  │ 读 market_state.json → 判定市场状态（风控阻断/仓位控制）
  │ 调用 ML 预测 → 选股+评分
  │
  └── create_executor(mode) 工厂 → 具体执行器
      ├── RemoteExecutor: HTTP POST → iquant_http_service.py → qmt_cmd.json
      │                       → qmt_strategy_v5.py (iQuant内轮询) → passorder()
      ├── SimExecutor: 写入 MySQL sim_signals
      └── EasytraderExecutor: 本地 easytrader 直连（备用）
```

### 市场状态风控

```
market_monitor.py（守护进程，30s 拉上证实时行情）
  → data/market_state.json（写入 JSON）
  → WebSocket 推送 market_update 事件

├── 恐慌(跌>2.5%):           阻断建仓, max_pos=1
├── 阻断(is_bear+跌>2%+涨跌比<0.3): 阻断+飞书告警
├── 逆市(跌1~2%):            阈值 2.5 + 仓位减半, max_pos=2
├── 偏弱(跌0.3~1%):          正常交易
├── 常态(涨/微跌):           正常交易, max_pos=3(range)/4(trend_up)
└── 恐慌清仓(跌>3.5%):       全部卖出+飞书告警
```

### 仓位管控（按策略独立）

- ML V11 和 实时扫描 各占独立仓位上限 = `round(max_pos × 0.5)`
- 持仓自动归类：查询 `sim_positions.strategy` → `sim_signals.signal_type`
- 未知持仓按策略比例分摊，避免双重扣减
- 盘中监控每轮买入前查询今日已用资金(`sim_signals` SUM)，超预算则跳过

### 资金分配（动态50:50）

- `get_scanner_capital()` / `get_v11_capital()` 实时查询 QMT `/balance` 总资产 × 50%
- 盈利时资金池自动放大，亏损时自动收缩
- 30秒缓存，QMT不可达时降级到 `config/scanner_config.yaml` 的 initial_capital

## API 端点一览

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/pipeline/status` | GET | 管线四阶段聚合（市场/ML/交易/绩效） |
| `/api/pnl/summary` | GET | 实盘盈亏汇总 |
| `/api/trading/orders` | GET | QMT 委托列表 |
| `/api/trading/cancel` | POST | 撤单 |
| `/api/trading/cancel-all` | POST | 批量撤单 |
| `/api/trading/market-order` | POST | 市价单 |
| `/api/trading/batch-order` | POST | 批量下单 |
| `/api/trading/trades` | GET | QMT 成交记录 |
| `/api/trading/balance` | GET | 账户余额 |
| `/api/trading/positions` | GET | 持仓列表 |
| `/api/trading/status` | GET | 远程交易服务状态 |
| `/api/trading/connect` | POST | 连接远程 QMT |
| `/api/scanner/signals` | GET | 实时选股扫描信号 |
| `/api/ws` | WS | WebSocket 事件推送 |

## ML 模型

| 模型 | 文件 | RankIC | 状态 |
|---|---|---|---|
| V11.0 (5日) | `data/ml_stock_model_v11_0.pkl` (159MB) | WF 0.024 / 集成 0.133 | **生产** |
| V11.0 11子模型 | `data/ml_stock_model_v11_0_11models.pkl` | — | 实验 |
| V11.0 7子模型(LGBM) | `data/ml_stock_model_v11_0_7lgb.pkl` (23MB) | — | 实验(轻量) |
| V11.0 OOS-v2 | `data/ml_stock_model_v11_0_oos_v2.pkl` | IC=0.0859 | **选股池注入** |
| V11.0 OOS-v3 | `data/ml_stock_model_v11_0_oos_v3.pkl` | — | 实验 |
| V11.2 (thin) | `data/ml_stock_model_v11_2.pkl` (19KB) | -0.044 | 备用 |
| V8.1 | `data/ml_stock_model_v8_1.pkl` (55MB) | WF ~0.05 / 集成 ~0.06 | 归档 |
| V11.3 (10日) | `data/ml_stock_model_v11_0.pkl` (Windows) | WF 0.010 / 集成 0.254 | 实验(过拟合) |

所有模型通过 `quant_app/utils/model_loader.py` 注册表管理。

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
- `.env` 定义所有敏感参数
- `quant_app/utils/config.py` 中 Config 单例读取
- 扫描策略参数在 `config/scanner_config.yaml`

## 已知问题

1. **模型 RankIC 低**: A 股 5 日收益难预测，0.02~0.05 属行业正常水平
2. **全量训练 Mac 内存不足**: 必须用 Windows 训练机（32GB），训练完手动传回 .pkl
3. **QMT 因子不可用**: `get_factor_data()` 在 iQuant 策略中无法调用
4. **Windows pandas 需 2.2.2**: 3.x 版本 merge_asof 严格 dtype 检查不兼容
5. **`board_concept_hist` 数据仅 2026-01 起**: 概念板块动量特征可用，但早期数据缺失，早期训练的特征值降级为 0
6. **`alpha_filter.py` 依赖新浪财经**: 新浪改版或网络不通时 α 信号中断，不影响主线 ML
7. **OOS 模型文件膨胀**: `data/` 下 10+ 个 .pkl 版本共 ~2GB，清理需确认生产模型不受影响

## 环境

- MacOS (192.168.10.30): Python 3.13, pandas 2.2.2, MySQL 5.7+, FastAPI
- Windows 训练 (192.168.10.39): Python 3.12, pandas 2.2.2, 32GB RAM, 16GB VRAM
- QMT (192.168.10.25): 国信 iQuant 策略交易平台, iquant_http_service.py :1430
- MySQL: 192.168.10.30:3306, root/root123, quant_db
- 域名: `lh.mozengfu.com.cn` → nginx (8.148.158.153) → 反向代理到 Mac :5001
- SPA 地址: https://lh.mozengfu.com.cn/app/
