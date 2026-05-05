# 系统架构

**分析日期：** 2026-05-05

## 架构模式概述

这是一个**传统的服务端渲染 Web 应用**，采用**单体重构中**的后端加**独立定时脚本**架构。不是微服务或事件驱动架构。系统遵循经典的三层 Web 模式（展示层 -> 业务逻辑层 -> 数据层），叠加了批处理 ML 流水线和 cron 驱动的自动化层。

无依赖注入、无服务注册中心、无消息队列、无容器化。架构务实简洁：一个 FastAPI 进程处理所有 HTTP 流量，一组独立的 Python 脚本处理离线计算，JSON 文件加 MySQL 提供持久化。

同一个进程内并存两套代码结构：

- **遗留单体**（`app_core.py` ~97 行导入层 + `app_api.py` ~163 行）—— 这些文件是薄的再导出层。真正的体量在 `app_server.py`（3489 行，原始单体核心）；`app_core.py` 作为外观（facade），从 `quant_app/` 导入所有东西再导出，使 `app_api.py` 和各路由模块能访问它们，无须破坏导入链。
- **重构模块化**（`quant_app/` 包，16 个文件约 8700 行）—— 按领域拆分为独立服务模块（行情数据、策略、回测、实时报价、技术指标、通知）。

## 分层架构

```
+------------------------------------------------------------------+
|                        展示层                                       |
|  Jinja2 HTML 模板 (templates/)                                    |
|  静态资源 (static/)                                                |
|  浏览器端 JS（.html 文件中的内联 <script>）                        |
+------------------------------------------------------------------+
        |  FastAPI 路由处理器返回 HTML 或 JSON
        v
+------------------------------------------------------------------+
|                     API / 路由层                                     |
|  app_api.py          -- FastAPI 应用创建、中间件、路由注册          |
|  quant_app/routes/   -- 6 个路由模块（页面、认证、管理、           |
|                          策略、行情、仪表盘）                     |
+------------------------------------------------------------------+
|        路由从 app_core 导入业务函数
        v
+------------------------------------------------------------------+
|                     业务逻辑层                                       |
|  app_core.py         -- 再导出外观（从 quant_app 导入）             |
|  quant_app/services/ -- 6 个服务模块                                |
|    market_service.py     -- 行情数据、RPS、交易日                   |
|    realtime_service.py   -- 实时行情降级链路                       |
|    strategy_service.py   -- 股票评分、扫描、分析                   |
|    backtest_service.py   -- 历史回测                               |
|    technical_service.py  -- 技术指标（薄包装）                     |
|    notification_service.py -- 短信、邮件、飞书告警                  |
|  独立模块：                                                         |
|    market_state.py         -- 市场状态分类                          |
|    sector_rotation.py      -- 板块轮动分析                          |
|    alpha_filter.py         -- Alpha 信号过滤                        |
+------------------------------------------------------------------+
|        服务层读写数据
        v
+------------------------------------------------------------------+
|                       数据层                                         |
|  MySQL (quant_db)   -- 所有结构化数据                               |
|     daily_price, stock_info, moneyflow_daily, fina_indicator,      |
|     sim_account/sim_trades/sim_positions, alpha_signals, ...       |
|  JSON 文件 (data/) -- 非关系型状态                                  |
|     users.json, sessions.json, positions.json, signals.json,       |
|     回测结果、股票池、ML 模型 .pkl 文件                            |
|  ML 模型文件 (data/) -- LightGBM .pkl 包                           |
|     ml_stock_model_v6*.pkl, feature_config_v*.json                 |
+------------------------------------------------------------------+
|                                                                    |
|  CRON / 脚本层（独立进程，同代码库）                                 |
|  scripts/ -- 50+ 个独立 Python 脚本                                |
|     feishu_alerts.py     -- 盘前/盘中/盘后推送                     |
|     sim_trading.py       -- 模拟交易引擎                            |
|     morning_briefing.py  -- 每日简报生成                            |
|     update_daily_price_cron.py -- Tushare -> MySQL 日线导入         |
|     backfill_tushare.py  -- 历史数据回填                            |
|  顶层 ML 脚本：                                                     |
|     ml_predict.py        -- ML 推理（LightGBM 预测）               |
|     ml_train_v6*.py      -- 模型训练（v6 系列）                     |
+------------------------------------------------------------------+
```

## 数据流

### 主请求流（HTTP API）

```
浏览器 / curl
    |
    v
FastAPI（uvicorn 监听 0.0.0.0:5001）
    |
    v
路由处理器（quant_app/routes/*.py 或 app_api.py 内联路由）
    |
    v
业务函数（从 app_core -> quant_app/services/*）
    |
    +---> MySQL（pymysql 原生查询）---> Tushare Pro API
    |                                        |
    |    或者                                 v
    |                                    MySQL（daily_price）
    |
    +---> 实时行情降级链：
            内存缓存（30s TTL）
            -> 腾讯行情（qt.gtimg.cn，3s 超时）
            -> 东方财富（push2.eastmoney.com，3s 超时）
            -> 阿里云市场（alirmcom2，3s 超时）
    |
    +---> JSON 文件读写（data/*.json）
    |
    v
JSON 响应（API）或 Jinja2 渲染 HTML（页面）
```

### ML 流水线

```
Tushare Pro API（历史数据）
    |
    v
MySQL（daily_price, moneyflow_daily, fina_indicator, market_index_daily）
    |
    v
ml_train_v6.py（特征工程 -> LightGBM 训练 -> 评估）
    |
    v
data/ml_stock_model_v6*.pkl + data/feature_config_v*.json
    |
    v
ml_predict.py（加载模型 -> 构建特征 -> 预测 -> 排序）
    |
    +---> 被 strategy_service.py 使用（ML 分数混合）
    +---> 被 scripts/sim_trading.py 使用（交易信号生成）
    +---> 被 scripts/daily_ml_predict.py 使用（批量预测）
```

### 行情降级链路（`quant_app/services/realtime_service.py`）

```
get_stock_quote(code, market)
    |
    v
_cached = _get_cache(key, 30)  -- 命中则直接返回
    |
    | 未命中
    v
_try_tencent(code)  -- qt.gtimg.cn，3s 超时，GBK 解码
    | 失败
    v
_try_eastmoney(code) -- push2.eastmoney.com，3s 超时，JSON
    | 失败
    v
_try_aliyun(code)   -- alirmcom2.market.alicloudapi.com，3s 超时
    | 失败
    v
返回 None（所有源均耗尽）
```

### 选股流水线（V4 组合策略）

```
MySQL daily_price（最新交易日）
    |
    v
SQL 过滤：price > 5, pct_chg > 1%, turnover > 5%, volume_ratio > 1.2
    |
    v
技术面筛选：MA5 > MA10 > MA20（向上排列）
    |
    v
主力评分（scripts/mainforce_scoring.py）：资金流向分析
    |
    v
ML 分数混合（ml_predict.py）：3 日涨幅概率
    |
    v
增强分数 = rule_score * ML_probability -> 排序输出
    |
    v
前 5 候选 -> 飞书推送（feishu_alerts.py morning）
```

## 关键抽象

### app_core.py —— 外观层

`app_core.py` 是一个薄（~97 行）的再导出模块。其存在目的是为遗留路由代码提供单一导入目标。它从 `quant_app/services/` 和 `quant_app/utils/` 导入所有业务函数，暴露为 `from app_core import analyze_stock, strategy_scan, ...`。

新代码应直接从 `quant_app/` 模块导入，不走 `app_core`。外观层仅用于与模块化重构前编写的路由向后兼容。

### quant_app.utils.config —— 中心配置

所有环境变量、文件路径和常量在 `quant_app/utils/config.py` 中一次性加载。每个其他模块从这里导入，而非直接读 `os.environ` 或重复路径常量。`get_db_config()` 提供 MySQL 连接字典，自动选择 Unix socket（本地开发）或 TCP（生产环境）。

### quant_app.utils.persistence —— JSON 文件 I/O

所有 JSON 文件读写集中在 `persistence.py`，采用线程安全的原子写入（临时文件 + `os.replace()`）。防止并发请求处理导致的文件损坏。

### quant_app.utils.model_loader —— ML 模型缓存

模型加载集中在 `model_loader.py`，使用 `@lru_cache` 装饰器确保每个模型版本只从磁盘加载一次。`ml_predict.py` 在其上叠加了线程安全层。

### market_state.py —— 市场状态适配器

`market_state.py` 读取指数趋势 + 宽度 + 波动率 + 成交量数据，将市场分为五种状态，返回参数字典（`stop_loss_pct`、`take_profit_pct`、`max_positions`、`ml_threshold`）。其他模块（strategy_service、sim_trading、feishu_alerts）调用 `get_market_state()` 来调整行为以适应当前市场条件。

## 定时任务架构

所有定时任务作为**独立 Python 进程**运行，由系统 crontab 启动，配置在 `scripts/quant_crontab`。它们与 Web 应用共享同一个代码库和数据库，但独立运行。

```
Crontab 排期（仅交易日）：
 09:00     feishu_alerts.py morning       -- 盘前推送
 09:30-14:30  每 30min  auto_refresh_data.sh -- 数据刷新
 09:30-15:00  每 5min   feishu_alerts.py alert + position_monitor.py
 15:05     feishu_alerts.py daily         -- 收盘报告
 17:00     update_daily_price_cron.py     -- Tushare 日线数据导入
 17:30     sim_trading.py scan            -- 盘后模拟交易扫描
 17:45     run_three_strategies.py        -- V4 组合策略扫描
```

每个脚本是自包含的：自行设置 `sys.path`，直接导入依赖，写入自己的日志文件到 `logs/`。

## 错误处理策略

- **HTTP 层**：`app_api.py` 中的全局异常处理器捕获所有未处理异常，返回 `{"detail": "服务器内部错误，请稍后重试"}`，状态码 500。错误详情记录到日志但从不泄露给客户端。
- **服务层**：函数用 try/except 包装外部调用，配合 `logger.warning()`，失败时返回 `None` 或空结构。数据源故障不会传播到 HTTP 层。
- **JSON 持久化**：原子写入防止文件损坏。线程锁防止并发写入竞争。
- **行情降级**：三层重试，每层 3s 超时，内存缓存减少外部调用频率。
- **网络调用**：`_retry_urlopen()` 实现指数退避重试（3 次，0.5s/1s/1.5s 延迟）。
- **无熔断器、无健康检查、无结构化错误码**。

## 并发模型

- **单进程**：Uvicorn 单 worker 运行（无 `--workers` 标志）。避免 Python GIL 争用，保持内存缓存（行情缓存、模型缓存、会话缓存）简单。
- **线程安全缓存**：行情缓存用 `threading.Lock`，模型加载用 `threading.Lock`，JSON 写入用 `threading.RLock`。`auth.py` 中的会话状态用 `threading.Lock`。
- **业务逻辑中无异步 I/O**：服务中的所有数据库和网络调用都是同步的（`pymysql`、`urllib.request`）。FastAPI 路由处理器是 `async def` 包装器包裹同步调用 —— 在路由层以下没有真正的 async/await。

## 已废弃/死代码

- `app_server.py`（3489 行）—— 原始单体核心，不再被任何东西导入。很可能是当前模块化代码的前身。
- `app.py.fixed`、`app.py.full`（235KB、306KB）—— 备份/副本文件，未在使用。
- `archive/` 目录 —— 17 个已归档脚本（旧 ML 训练版本、回测备份、研究脚本）。保留作参考但未被导入。
- `index.html`（60KB，项目根目录）—— 似乎是独立 HTML 文件，不属于 FastAPI 模板系统。
- `static.backup_20260427_220128/` —— 静态资源备份。
