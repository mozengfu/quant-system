# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 角色定位

称呼"莫富"，沟通对象"主任"。结论先行，风格简洁务实。
对量化策略参数调整、风控相关操作必须征得主任同意。

## 技术栈

Python 3.12, FastAPI 0.115, MySQL(pymysql裸SQL), LightGBM/XGBoost, pandas 2.2, Tushare Pro, AKShare

**完整依赖**（pyproject.toml）：fastapi, uvicorn[standard], jinja2, python-multipart, pandas, numpy, pymysql, sqlalchemy, cryptography, tushare, python-dotenv, bcrypt, DBUtils, joblib, lightgbm, xgboost

## 常用命令

```bash
# 项目安装
pip3 install -e .                   # 开发模式安装（依赖在 pyproject.toml）

# 代码检查
ruff check quant_app/ scripts/      # lint 检查
ruff check --fix quant_app/         # 自动修复 import 排序等问题
ruff format .                       # 代码格式化

# 测试
pytest tests/ -m "not slow"           # 快速测试（跳过模型加载）
pytest tests/ -m "slow"               # 全量测试（含模型加载+预测冒烟）
pytest tests/                          # 运行全部测试

# ORM 查询（新代码推荐）
python3 -c "from quant_app.data import with_session, DailyPrice; from sqlalchemy import text; from quant_app.utils.config import MySQLConfig; print(MySQLConfig.url())"

# 启动
python3 app.py                    # Web服务 (端口5001)
bash start.sh                     # 带日志启动

# ML模型
python3 ml_train_v11_0.py         # V11.0 训练
python3 ml_train_v11_0.py --max_date 2024-10-31 --output data/oos.pkl  # 样本外训练
# 回测
python3 scripts/backtest_run.py --start 2024-11-01 --end 2026-05-08  # 统一回测引擎
python3 scripts/backtest_pure_ml_clean.py --model data/oos.pkl         # OOS回测（旧）

# 旧回测脚本（保留作历史参考，建议新功能使用统一引擎）：
# scripts/backtest_current_pipeline.py — 完整管线回测
# scripts/backtest_v4_ml_v65_vs_v80.py — 多模型对比
# scripts/backtest_pure_ml.py — 纯ML回测
# scripts/backtest_param_scan.py — 参数扫描
# scripts/backtest_v8_6_tune.py — V8.6 专属参数扫描
python3 ml_predict.py             # 日常预测
python3 scripts/predict_v11.py    # V11.0 专用预测/特征构建脚本

# 根目录独立脚本（不经过FastAPI）
python3 ml_daily_top5.py          # 每日Top5预测
python3 ml_regime_detector.py     # 市场状态检测
python3 ai_sim_trading.py         # AI模拟交易
python3 run_three_strategies.py   # V4组合策略扫描
python3 run_backtest_v11.py       # V11回测
python3 run_backtest_v4_pool.py   # V4池回测
python3 run_backtest_v4_pool_filter.py  # V4池过滤回测
python3 run_ml_quality_*.py       # ML质量分析系列
python3 market_state.py             # 市场状态机
python3 alpha_filter.py             # Alpha信号过滤
python3 sector_rotation.py          # 行业轮动

# 生产模式: PURE_ML=1 (纯ML模式，回测证明显著优于V4+ML混合)
# 纯ML管线: 成交额Top300 → V11.0排序(11子模型等权融合) → ML百分位过滤(>0.50) → 风控过滤 → 游资评分 → 业绩过滤 → 行业分散 → Top3
# V11.0 模型结构: 7个LambdaRank(lgb_seed_*) + 1个XGBoost回归(xgb_reg) + 3个特征子集模型(momentum/flow/quality)
# 模型加载优先级(quant_app/utils/model_loader.py): V11.0 > V10.0 > V8.1 > V8.0 > ...
# 当前主模型: V11.0 三层堆叠集成 (11子模型, 117特征)
# 标签: alpha_5d (行业中性化vol-adjusted 5d return)
# 特征: 日频量价/资金流/融资融券/龙虎榜/涨停板/行业动量/概念热度/业绩/大宗交易
#       + fina_indicator/sector_moneyflow/north_moneyflow/ml_predictions
# 推理入口: ml_predict.py (_load_model + _build_features)，特征构建约需80天历史数据
# 重要: V11.0 必须用 scripts/predict_v11.build_features_v11_inference 构建特征，不能用 v6.3 函数

# 策略扫描
python3 run_three_strategies.py   # V4组合策略扫描
python3 scripts/feishu_alerts.py morning/alert/daily  # 飞书推送
python3 scripts/sim_trading.py scan                   # 模拟交易
python3 scripts/position_monitor.py                   # 止盈止损执行

# 数据同步
python3 scripts/update_daily_price_cron.py   # Tushare日线
python3 scripts/sync_akshare.py              # AKShare数据
python3 scripts/sync_mainforce_data.py       # 主力资金
python3 scripts/sync_fina_indicator.py       # 财务指标
python3 scripts/sync_tushare_boards.py       # Tushare板块数据
python3 scripts/backfill_margin.py           # 融资融券回填
python3 scripts/backfill_tushare.py          # Tushare历史回填
python3 scripts/calc_technical.py            # 技术指标批量计算

# 诊断分析
python3 scripts/check_pipeline.py            # 选股管线健康检查
python3 scripts/analyze_drawdown.py          # 回撤归因分析
python3 scripts/ml_deep_analysis.py          # ML模型深度分析
python3 scripts/sector_rotation_filter.py    # 行业轮动过滤
python3 scripts/mainforce_scoring.py         # 主力资金评分

# 滚动训练 (crontab: 每周六 3:00)
bash scripts/rolling_train.sh
```

## 架构

```
app.py          → 入口/启动 (uvicorn.run "app_api:app" port 5001)
app_api.py      → FastAPI 应用实例、CORS、全局异常处理、9个子路由注册、启动时日志迁移
app_core.py     → Facade 协调层，从 quant_app 重新导出，保持旧 import 兼容
quant_app/
  backtest/     → 统一回测引擎
    engine.py       BacktestEngine / BacktestResult / TradeRecord
  features/     → 特征构建模块
    __init__.py     build_features_for() 统一入口
    v11_features.py V11.0 特征构建 + 模型对齐
  data/         → 数据库层（SQLAlchemy ORM）
    database.py     引擎/会话管理/with_session 上下文管理器
    models/         ORM 模型（9 张表：DailyPrice, StockInfo, MoneyflowDaily 等）
  risk/         → 风控模块
    filters.py      风控过滤（涨停追高/52周高位/异常放量/短期过热）
    hot_money.py    游资 5 因子评分（连板/封单萎缩/涨停后跌停/高换手/主力流出）
    sector.py       行业分散约束（每行业最多 2 只）
  services/     → 业务逻辑层
    strategy_service.py    选股/评分/风控/游资评分/业绩过滤/行业分散（最大模块~120KB）
    market_service.py      行情数据(Tushare/实时)/持仓同步/RPS计算
    realtime_service.py    实时行情三层降级(缓存→腾讯→东方财富→阿里云)
    backtest_service.py    单股回测(V4规则)
    technical_service.py   技术指标计算
    notification_service.py 飞书/邮件/短信通知
  routes/       → API 端点（9个）
    pages.py        页面路由(index/admin等HTML渲染)
    auth.py         登录/注册/密码重置/会话管理
    admin.py        管理后台(用户管理/系统日志)
    strategy.py     策略选股接口
    signals.py      技术指标信号接口
    scanning.py     技术扫描(概念趋势/底部突破/均线回踩)
    recommend.py    推荐股票接口
    market.py       行情数据接口
    dashboard.py    仪表盘数据接口
  utils/        → 工具层
    config.py         数据库配置
    auth.py           认证工具
    authz.py          权限控制
    persistence.py    JSON/MySQL 持久化 + 访问日志
    indicators.py     纯Python技术指标(EMA/MACD/KDJ/布林带/ATR)
    model_loader.py   ML模型加载器+缓存+版本注册表
    risk_config.py    风控配置
scripts/        → 独立脚本(cron/回测/数据同步/飞书推送/模拟交易)，独立运行不经过 FastAPI
templates/      → Jinja2 模板 (index.html 含内联 JS, admin.html)
static/         → CSS/图标/PWA manifest
data/           → JSON 运行时状态 + ML 模型 .pkl + parquet 预测缓存
archive/        → 已下线策略的归档(root/scripts/training)
```

## 根目录独立脚本

这些脚本不经过 FastAPI，直接调用 ML 模型或数据库：

| 脚本 | 用途 |
|------|------|
| `ml_predict.py` | 主推理入口，模型加载+特征构建+预测（支持V6~V11所有版本） |
| `ml_train_v11_0.py` | V11.0 训练脚本，支持 --max_date 和 --output 参数 |
| `ml_daily_top5.py` | 每日 Top5 预测 |
| `ml_regime_detector.py` | 市场状态检测（趋势/恐慌/过热识别） |
| `ai_sim_trading.py` | AI模拟交易，记录推荐后续表现 |
| `run_three_strategies.py` | V4组合策略扫描 |
| `run_backtest_v11.py` | V11 回测 |
| `run_backtest_v4_pool.py` | V4 选股池回测 |
| `run_backtest_v4_pool_filter.py` | V4 池过滤回测 |
| `run_ml_quality_*.py` | ML 质量分析系列 |
| `market_state.py` | 市场状态机（趋势/宽度/波动率/成交量 → 5种状态） |
| `alpha_filter.py` | Alpha 信号过滤 |
| `alpha_signal_integration.py` | Alpha 信号集成 |
| `sector_rotation.py` | 行业轮动 |

**各层职责**：
- `app.py` 只做启动和信号处理，不处理业务逻辑
- `app_api.py` 解析请求、调用 app_core，不直接读写数据库
- `app_core.py` 是 Facade，组合多个 service 完成业务用例
- `services/` 单一职责，不直接处理 HTTP 请求
- `utils/` 可被 services/routes 共同依赖，但不含业务决策
- `scripts/` 独立运行，可被 crontab 调用，不经过 FastAPI

## 环境配置 (.env)

```bash
# 数据源
TUSHARE_TOKEN=your_token_here

# 数据库
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=xxx
MYSQL_DATABASE=quant

# 飞书告警
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 邮件通知
SMTP_HOST=smtp.xxx.com
SMTP_USER=xxx
SMTP_PASSWORD=xxx

# 阿里云行情
ALIYUN_ACCESS_KEY=xxx
ALIYUN_SECRET=xxx
```

数据库表结构由 MySQL 初始化（无 schema.sql），修改需同步更新 MEMORY.md 和 `.env` 中的 MYSQL_DATABASE。

## 选股流水线

### Pure ML 模式（生产主模式，PURE_ML=1）

回测对比（2025-10 ~ 2026-05, 29次采样, Top5, 5天持有）：
- 纯V4: +54.47% / 胜率51.7% / 夏普1.40
- V4+ML混合: +2.49% / 胜率51.7% / 夏普0.32（ML在受限池里加的是噪声）
- **纯ML(含过滤): +764.40% / 胜率80.0% / 夏普5.86** ← 当前生产模式

管线：`成交额Top300 → V11.0排序(11子模型) → ML百分位过滤(>0.50中位数) → 风控过滤(涨停/放量) → 游资评分(≥40排除) → 业绩过滤(利润同比<-30%排除) → 行业分散 → Top3`

**注意**：V11.0 使用 LambdaRank 排序模型，输出为 raw margin 值（大部分自然为负），不能用 `ml_score > 0` 绝对值过滤。改用横截面百分位中位数（pct > 0.50）过滤。特征构建必须按模型版本选择：v11.0 → `scripts/predict_v11.build_features_v11_inference`，v10.0 → `_build_features_for_stocks_v10_0`，其他版本有专用函数。`scripts/predict_v11.py` 是 V11.0 专用预测脚本，独立于 `ml_predict.py`。

### V4+ML 混合模式（备选，PURE_ML≠1）

1. **V4初筛** — `_v4_score_single()` 技术面+资金流评分，取 Top30
2. **行业分散** — 单一行业最多2只候选
3. **ML预测** — V11.0模型排序（fallback: V10.0 → V8.1 → V8.0）
4. **混合评分** — `V4*(1-0.20) + ML百分位*100*0.20`，取 Top5

**模型优先级**：新模型通过滚动回测验证后逐步替代旧模型，旧模型保留作 fallback。模型损坏或特征缺失时在 `ml_predict.py` 中自动降级。

## 异常处理

| 场景 | 处理方式 |
|------|----------|
| 行情数据源超时 | 依次尝试 腾讯→东方财富→阿里云，超限返回 None 并记录日志 |
| 选股池为空（V4初筛无结果） | 返回空列表，不抛异常，上层调用方处理 |
| ML预测失败（模型文件损坏/特征缺失） | 自动尝试下一优先级模型；全失败时 ML权重置0，纯V4评分 |
| 数据库连接失败 | 写日志返回错误码，不暴露异常细节给前端 |
| 飞书推送失败 | 降级到邮件/日志，不阻塞主流程 |

## 定时任务 (crontab: scripts/quant_crontab)

| 时间 | 任务 |
|------|------|
| 交易日 09:00 | 盘前飞书推送 |
| 09:00 | 数据刷新（auto_refresh_data.sh） |
| 09:30-14:30每30min | 数据刷新 |
| 09:30-14:55每5min | 飞书告警 + 持仓监控（含自动止盈止损执行） |
| 15:05 | 收盘飞书推送 |
| 17:00 | Tushare数据导入 |
| 17:30 | 模拟交易扫描 |
| 17:45 | V4策略扫描 |
| 17:55 | 底部苏醒策略扫描（scripts/scan_bottom_awakening.py） |
| 周六 3:00 | V11.0滚动训练（scripts/rolling_train.sh → ml_train_v11_0.py → 成功则覆盖OOS备份） |

## 行情降级链路

`缓存(30s)→腾讯(3s)→东方财富(3s)→阿里云(3s)`

## 易错点

- **日志格式**: 使用惰性 `%s` 占位符而非 f-string（`logger.info("msg %s", val)` 不用 `logger.info(f"msg {val}")`）
- **配置入口**: `from quant_app.utils.config import config` 新代码推荐用 `config.mysql.url` 等单例属性
- **JSON 序列化**: 含 numpy 类型的数据用 `from quant_app.utils.json_encoder import safe_json_dumps`
- **SQL注释中的`%`**: cursor.execute用`%`格式化，注释里写`%`会报错
- **行情fallback**: 所有实时数据必须走 `realtime_service.py`
- **import链**: 删模块前 `grep -rn "import.*模块名"` 确认零引用
- **前端JS**: `let`/`const`放IIFE之前声明；登录后恢复hash
- **两个index.html**: 根目录(60KB独立页) vs templates/下(141KB含内联JS)
- **`.env`**：含 TUSHARE_TOKEN, MYSQL_*, FEISHU_WEBHOOK, SMTP_*, ALIYUN_*

## data/ 目录结构

```
data/
  ml_stock_model_v11_0.pkl      # V11.0 主模型 (~65MB)
  ml_stock_model_v11_0_oos.pkl  # V11.0 样本外模型
  ml_stock_model_v8_1.pkl       # V8.1 fallback
  ml_stock_model_v8_1_oos.pkl   # V8.1 样本外
  ml_stock_model_v8_0.pkl       # V8.0 最后兜底
  feature_config_ml_stock_model_v11_0.json  # V11.0 特征配置
  feature_config_ml_stock_model_v11_0_oos.json
  predictions_YYYYMMDD_vXX.X.json  # 每日预测结果
  ml_preds_YYYY-MM-DD.parquet   # parquet 预测缓存
  positions.json                # 当前持仓
  sessions.json                 # 活跃会话
  users.json                    # 用户数据
  signals.json                  # 技术信号
  stock_pool_v4.json            # V4选股池
  recommend_cache.json          # 推荐缓存
  alert_state.json              # 告警状态
  risk_config.json              # 风控配置
  track/recommendations.json    # 推荐追踪记录
  backtest_*.json               # 回测结果缓存
  access_log.json               # 访问日志(启动时自动迁入MySQL)
```

**模型注册表**（`quant_app/utils/model_loader.py`）：
v6, v6.2~v6.7, v8.0~v8.4, v8.6, v9.0, v10.0, v11.0。

**模型加载优先级**：V11.0 → V10.0 → V8.1 → V8.0 → 纯V4。v8.6 在注册表中但被降级链跳过（回测不如 v8.1）。模型损坏或特征缺失时在 `ml_predict.py` 中自动降级。

## 新增功能指引

**添加新 API 端点**：
1. 在 `quant_app/routes/` 下创建 `new_module.py`，定义 `APIRouter`
2. 在 `app_api.py` 中 import 并注册：`app.include_router(new_module.router)`
3. 路由处理函数调用 `app_core.py` / services 中对应的方法，不直接访问数据库

**添加新 service**：
1. 在 `quant_app/services/` 下创建 `new_service.py`
2. 如需数据库操作，推荐使用 `quant_app.data` 中的 SQLAlchemy ORM（`with_session()` 上下文管理器）
3. 也可继续使用 `utils/persistence.py` 的 pymysql 裸SQL（向后兼容）
4. 不引入业务逻辑到 utils 层

**添加独立脚本**：
1. 在 `scripts/` 下创建 `new_script.py`
2. 需要数据库时 import `quant_app.utils.config.get_db_config`
3. 可被 crontab 独立调用，不经过 FastAPI

## 测试

`tests/` 目录当前以 pytest 框架为基础（pyproject.toml 已配置 testpaths）。验证主要靠：
- 回测脚本 (`scripts/backtest_*.py`) 验证策略表现
- `python3 ml_predict.py` 验证模型推理是否正常
- 浏览器手动验证前端功能

requirements.txt 保留作向后兼容，依赖以 pyproject.toml 为准。

## V11.0 模型详情

- **架构**: 三层堆叠集成（11子模型，117特征）
- **算法**: 7个 LambdaRank (lgb_seed_*) + 1个 XGBoost 回归 (xgb_reg) + 3个特征子集模型 (momentum/flow/quality)
- **标签**: alpha_5d（行业中性化 vol-adjusted 5d return）
- **特征**: 日频量价/资金流/融资融券/龙虎榜/涨停板/行业动量/概念热度/业绩/大宗交易 + fina_indicator/sector_moneyflow/north_moneyflow/ml_predictions
- **训练**: 1000 交易日窗口，Expanding window walk-forward
- **推理入口**: `ml_predict.py`（_load_model + _build_features），特征构建约需 80 天历史数据
- **滚动训练**: `scripts/rolling_train.sh` 每周六 3:00 重训，成功后覆盖 OOS 备份

## 部署

- 单进程 uvicorn，本机运行（服务器已下线，不再部署）
- 无容器化/CI/CD
- 生产服务由 macOS LaunchAgent 管理（`com.quant.system`）
- 端口: 5001
- `.env` 包含: TUSHARE_TOKEN, MYSQL_*, FEISHU_WEBHOOK, SMTP_*, ALIYUN_*
- 同步脚本: `sync-to-server.sh`, `sync-from-server.sh`, `deploy_manual.sh`
