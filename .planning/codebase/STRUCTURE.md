# 代码结构

**分析日期：** 2026-05-05

## 顶层目录布局

```
quant-system/
├── app.py                  # 入口点 —— 启动 uvicorn 监听 0.0.0.0:5001
├── app_api.py              # FastAPI 应用创建、中间件、路由注册
├── app_core.py             # 再导出外观（从 quant_app/ 导入再导出）
├── app_server.py           # 已废弃 —— 原始单体核心（3489 行，未使用）
├── app_thin.py             # 轻量变体（23KB）
│
├── quant_app/              # 重构后的模块化包（约 8700 行）
│
├── scripts/                # Cron 任务、回测、数据导入（约 50 个文件）
├── templates/              # Jinja2 HTML 模板（10 个文件）
├── static/                 # 静态资源（CSS、图标、图片）
├── data/                   # JSON 状态文件、ML 模型 .pkl、特征配置
├── logs/                   # 运行时日志（应用、导入、监控）
│
├── archive/                # 废弃脚本（旧 ML 版本、策略备份）
├── tests/                  # 空目录（未使用任何测试框架）
├── docs/                   # 文档（较小，1-2 个文件）
│
├── ml_predict.py           # ML 推理模块（LightGBM 预测）
├── ml_train_v6*.py         # ML 训练脚本（v6/v6.2/v6.3/v6.4/v6.5）
├── market_state.py         # 市场状态检测
├── sector_rotation.py      # 板块轮动分析
├── alpha_filter.py         # Alpha 信号过滤
├── alpha_signal_integration.py  # Alpha 信号集成
├── ai_sim_trading.py       # AI 驱动模拟交易
├── backtest_*.py           # 顶层回测脚本
│
├── .env                    # 环境变量（未提交到 git）
├── requirements.txt        # Python 包依赖
├── CLAUDE.md               # Claude Code 项目说明
│
├── .planning/              # 架构规划文档
│   └── codebase/
│
├── data/                   # 详见下方章节
├── logs/                   # 详见下方章节
└── archive/                # 废弃/备份脚本
```

## 目录用途

### `quant_app/` —— 重构包（8700 行）

模块化设计，替代原始单体。按关注点拆分子包。

```
quant_app/
├── __init__.py              # 包标记
├── main.py                  # 外部消费者的再导出
│
├── utils/
│   ├── __init__.py
│   ├── config.py            # 中心配置（环境变量、路径、数据库配置）
│   ├── auth.py              # 密码哈希（bcrypt）、会话令牌生成
│   ├── persistence.py       # JSON 文件 I/O（线程安全、原子写入）
│   ├── indicators.py        # 全序列技术指标（EMA、MACD、KDJ、BOLL、ATR）
│   └── model_loader.py      # ML 模型加载（LRU 缓存）
│
├── services/
│   ├── __init__.py
│   ├── realtime_service.py  # 实时行情降级链路（缓存 -> 腾讯 -> 东财 -> 阿里云）
│   ├── market_service.py    # 行情数据（Tushare 包装、交易日、RPS 计算、历史）
│   ├── strategy_service.py  # 股票扫描、评分、分析（C3.0 V3、V4 组合）
│   ├── backtest_service.py  # 历史回测（单只股票）
│   ├── technical_service.py # 薄包装：委托给 indicators.py，返回最后一个值
│   └── notification_service.py  # 多渠道告警（短信、邮件、飞书）
│
├── routes/
│   ├── __init__.py
│   ├── pages.py             # 页面路由（GET /login、/register、/admin、/market 等）
│   ├── auth.py              # 认证路由（登录、登出、注册、重置密码）
│   ├── admin.py             # 管理路由（访问日志、用户管理）
│   ├── strategy.py          # 策略路由（扫描、分析、买入/卖出信号）
│   ├── market.py            # 行情路由（盘前、指数、板块轮动）
│   └── dashboard.py         # 仪表盘路由（持仓、回测、跟踪）
│
├── models/                  # 未来数据模型占位
│   └── __init__.py
│
└── data/                    # 未来数据文件占位
    └── track/
```

### `scripts/` —— 自动化脚本（约 50 个文件）

所有脚本遵循基本模式：设置 `sys.path`，从 `quant_app/` 或 `app_core` 导入依赖，执行类 `main()` 函数。设计为从 crontab 运行，而非被导入。

关键脚本分类：

**通知与告警：**
- `feishu_alerts.py` —— 盘前（9:00）、盘中告警（每 5 分钟）、收盘报告（15:05）
- `morning_briefing.py` —— 6:30 每日简报生成

**交易与监控：**
- `sim_trading.py` —— 模拟交易引擎（MySQL 存储、ML 驱动）
- `position_monitor.py` —— 盘中持仓监控（止损/止盈检查）
- `add_position.py` / `sell_position.py` —— 持仓管理操作

**数据导入与同步：**
- `update_daily_price_cron.py` —— 每日 Tushare -> MySQL 增量导入
- `backfill_tushare.py` —— 历史数据批量回填（断点续传）
- `sync_fina_indicator.py` —— 财务指标同步
- `sync_mainforce_data.py` —— 主力资金流数据同步
- `sync_akshare.py` —— AKShare 数据同步（备选数据源）

**回测：**
- `backtest_combo_v4.py` 到 `backtest_combo_v6_params.py` —— 策略回测演进
- `backtest_v41_vs_ml.py`、`backtest_v5_ml_real.py` 等 —— ML 集成回测
- `backtest_v65_*.py` —— V6.5 系列回测（对比、优化、组合）
- `backtest_fine_tune.py` / `backtest_param_scan.py` —— 参数优化

**分析：**
- `mainforce_scoring.py` —— 主力资金流评分（V4 组合策略使用）
- `analyze_v4_factors_detail.py` —— 详细因子分析
- `check_ml_style.py` / `ml_deep_analysis.py` —— ML 模型分析

**工具：**
- `calc_technical.py` —— 技术指标计算
- `alicloud_api.py` —— 阿里云行情数据 API 包装
- `eastmoney_api.py` —— 东方财富数据 API 包装
- `auto_refresh_data.sh` / `auto_scan.sh` —— Shell 脚本包装
- `quant_crontab` —— Crontab 配置文件（非脚本）

### `templates/` —— Jinja2 HTML（10 个文件）

服务端渲染的 HTML 页面。每个文件是完整的 HTML 文档（非 Jinja2 片段或块 —— 模板包含完整的 `<html><head><body>` 结构）。内联 JavaScript 通过 `fetch()` 调用 FastAPI 端点处理所有客户端交互。

关键模板：
- `login.html` / `register.html` —— 认证页面
- `index.html`（124KB）—— 主仪表盘（最大、最复杂）
- `admin.html` —— 管理面板
- `landing.html` —— 落地页
- `market_analysis.html` —— 行情分析页面
- `ml_top15.html` —— ML 前 15 名展示
- `strategy_v41.html` —— V4.1 策略页面
- `log_analytics.html` —— 日志分析
- `market_analysis.html` —— 行情数据视图

### `data/` —— JSON 与模型存储

三类文件并存：

**运行时状态（JSON，应用读写）：**
- `users.json`、`pending_users.json` —— 用户账户
- `sessions.json` —— 活跃认证会话
- `positions.json` —— 持仓跟踪
- `signals.json` —— 告警/信号状态
- `access_log.json` —— 请求审计日志（也写入 MySQL）
- `reset_tokens.json` —— 密码重置令牌
- `track/recommendations.json` —— 推荐跟踪历史

**缓存结果（JSON，定期刷新）：**
- `stock_pool.json`、`stock_pool_bottom.json`、`stock_pool_strong.json` —— 选股缓存
- `premarket_analysis.json` —— 盘前分析缓存
- `recommend_cache.json` —— 推荐缓存
- `concept_trend.json` —— 概念趋势数据
- `sector_trend.json` —— 板块轮动数据

**回测结果（JSON，一次性写入）：**
- `backtest_combo_v*.json` —— 策略回测结果
- `backtest_v65_*.json` —— V6.5 系列结果
- `backtest_comparison.json`、`backtest_combo_comparison.json` —— 跨版本对比

**ML 产物：**
- `ml_stock_model_v*.pkl` —— LightGBM 模型包（joblib 格式，每个 1-15 MB）
- `ml_stock_model_ridge.pkl` —— 岭回归模型
- `ml_bear_model.pkl` —— 熊市模型
- `feature_config_v*.json` —— 各模型版本的特征元数据
- `ml_preds_v6_3.parquet` / `ml_preds_v6_3_latest.parquet` —— 预测输出（Parquet 格式）
- `model_monitor_history.json` —— 模型性能跟踪

**用户数据：**
- `holdings_mozengfu.json`、`trades_mozengfu.json` —— 用户持仓数据
- `admins.json` —— 管理员列表

### `logs/` —— 应用日志

- `app.log` / `app.error.log` —— 主 Web 应用输出
- `sync.log` —— 服务器同步操作（最大，约 199KB）
- `server.log` —— 服务器端日志
- `feishu_alerts.log`、`morning_briefing.log` —— 通知日志
- `position_monitor.log` —— 盘中监控输出
- `ai_sim.log` —— AI 模拟交易日志
- `sim_trading.log` —— 模拟交易引擎日志（在 scripts/data/ 下）
- `cron_daily.log` —— 每日 cron 输出（在 scripts/data/ 下）

### `archive/` —— 废弃脚本（17 个文件）

包含旧 ML 训练版本（v1-v5）、废弃的回测实验和策略备份。不被任何活跃代码导入。保留作参考。

## 关键文件位置

| 用途 | 文件路径 |
|---------|-----------|
| 入口点 | `app.py` |
| FastAPI 应用 | `app_api.py` |
| 外观/再导出 | `app_core.py` |
| 配置 | `quant_app/utils/config.py` |
| 认证 | `quant_app/utils/auth.py` |
| 持久化 | `quant_app/utils/persistence.py` |
| 技术指标 | `quant_app/utils/indicators.py` |
| 模型加载 | `quant_app/utils/model_loader.py` |
| 实时行情 | `quant_app/services/realtime_service.py` |
| 行情数据 | `quant_app/services/market_service.py` |
| 策略/扫描 | `quant_app/services/strategy_service.py` |
| 回测 | `quant_app/services/backtest_service.py` |
| 通知 | `quant_app/services/notification_service.py` |
| 页面路由 | `quant_app/routes/pages.py` |
| 认证路由 | `quant_app/routes/auth.py` |
| 管理路由 | `quant_app/routes/admin.py` |
| 策略路由 | `quant_app/routes/strategy.py` |
| 行情路由 | `quant_app/routes/market.py` |
| 仪表盘路由 | `quant_app/routes/dashboard.py` |
| 市场状态 | `market_state.py` |
| ML 推理 | `ml_predict.py` |
| ML 训练 | `ml_train_v6*.py`（v6/v6.2/v6.3/v6.4/v6.5） |
| Crontab 配置 | `scripts/quant_crontab` |
| 环境变量 | `.env`（未提交） |

## 命名规范

- **文件**：所有 Python 文件使用 `snake_case.py`。脚本名描述性强：`update_daily_price_cron.py`、`backtest_combo_v4.py`。
- **类/函数**：函数和方法使用 `snake_case`（`get_stock_realtime`、`strategy_scan`）。此代码库中类很少见（只有 FastAPI 路由处理器通过 APIRouter 隐式使用）。
- **变量**：Python 使用 `snake_case`，内联 JavaScript 中直接映射到显示字段的 API 响应使用中文变量名（`名称`、`代码`、`现价`）。
- **路由路径**：`/api/resource/action` 模式（如 `/api/analysis/sz/000001`、`/api/combo_scan`）。
- **JSON 键**：中英文混合（`"代码"`、`"名称"`、`"ts_code"`、`"close"`、`"ml概率"`）。
- **模型版本**：`v6`、`v6.2`、`v6.3`、`v6.4`、`v6.5` —— 通过训练脚本演进递增。每个版本对应 `feature_config_v*.json` 和 `ml_stock_model_v*.pkl`。
- **数据库表**：`snake_case`（`daily_price`、`stock_info`、`moneyflow_daily`）。

## 代码风格观察

- **无类型提示**：代码库中任何模块均未使用 Python 类型注解。
- **无 dataclasses 或 Pydantic 模型**：数据以纯字典和列表传递。无请求/响应模式。
- **全局可变状态**：缓存（`_quote_cache`、`_state_cache`、`_last_scan_results`）是带线程锁的模块级字典。
- **导入风格**：大多数模块使用 `from x import y` 而非 `import x`。导入链较深：`routes -> app_core -> quant_app.services.* -> quant_app.utils.*`。
- **无 ORM、无查询构建器**：所有 SQL 查询是手写字符串，传入 pymysql 游标。

## 新增代码位置指南

### 新 API 端点

1. 在合适的 `quant_app/routes/*.py` 中添加路由处理器（strategy、market、dashboard、admin 或 auth）。
2. 如果路由需要业务逻辑，在合适的 `quant_app/services/*.py` 中添加函数。
3. 如果函数需要通过外观层访问（被其他路由模块通过 `app_core` 导入时需要），在 `app_core.py` 中添加导入。
4. 如果提供页面，在 `templates/` 中添加 HTML 模板。

### 新 ML 模型版本

1. 基于最新现有版本创建新训练脚本（如 `ml_train_v6_6.py`）。
2. 训练输出到 `data/ml_stock_model_v6_6.pkl` 和 `data/feature_config_v6_6.json`。
3. 在 `quant_app/utils/model_loader.py` 中注册模型路径。
4. 更新 `ml_predict.py` 支持新版本（在 `_load_model()` 中添加加载代码）。

### 新定时任务

1. 在 `scripts/` 中创建脚本，遵循现有模式（sys.path 设置、日志、数据库配置导入）。
2. 将 crontab 条目添加到 `scripts/quant_crontab`。
3. 脚本应自包含，从 `quant_app/` 导入共享服务。

### 新工具函数

- 技术指标：添加到 `quant_app/utils/indicators.py`（全序列版本）。
- 数据库或文件操作：添加到 `quant_app/utils/persistence.py` 或相关服务。
- 配置：将环境变量 + 常量添加到 `quant_app/utils/config.py`。

## 需要注意的架构约束

1. **生产环境无热重载**：`app.py` 以 `reload=False` 启动 uvicorn。变更需要重启进程。
2. **无数据库迁移**：Schema 变更通过手动执行 SQL。无 Alembic 或迁移工具。
3. **全栈无异步**：FastAPI 路由是 `async def`，但所有业务逻辑是同步的。并发请求由 uvicorn 的线程池处理，而非 asyncio。
4. **单用户假设**：认证系统支持多用户，但 UI 和策略假设单一主要用户（mozengfu）。
5. **内存受限的 ML 模型**：所有模型版本在预测时加载到内存。最新的 v6.5 模型约 54 MB。
6. **所有 JSON 文件 I/O 是单线程的**：`persistence.py` 中的 `_write_lock` RLock 序列化所有写入，对当前请求量没问题，但在高负载下会成为瓶颈。
