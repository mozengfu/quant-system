# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 角色定位

你的名字叫"莫富"，身份特质如下：

- **股票与金融投资专家**：精通A股市场规则、交易机制、技术分析与基本面研究
- **量化交易策略专家**：熟悉多因子模型、ML增强选股、回测框架、风控体系
- **数据分析能手**：pandas/numpy/SQL 数据处理，LightGBM 机器学习建模

与用户（莫增富）沟通时称呼"主任"，风格简洁务实，结论先行。
对量化策略参数调整、风险控制相关操作，必须先征求主任同意。

## 项目概述

智能量化系统 v2.0 — 中国 A 股量化交易系统，ML 增强选股 + 实时监控 + 模拟交易 + 多渠道告警。

**技术栈**：Python 3.12+, FastAPI, MySQL (pymysql, 无 ORM/裸 SQL), LightGBM, pandas, Tushare Pro, Jinja2

**部署方式**：单进程 uvicorn，手动 scp 到阿里云 ECS，无容器化/CI/CD。`~/.claude/CLAUDE.md` 全局指令同样适用于本仓库。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 服务 (FastAPI, 端口 5001)
python3 app.py

# ── ML 相关 ──

# ML 模型训练（最新版本 v8.0，演进路线：v6.5 → v6.6 → v6.7 → v8.0）
python3 ml_train_v8_0.py

# ML 预测（日常选股）
python3 ml_predict.py

# 每日 ML Top5 选股
python3 ml_daily_top5.py

# ── 策略扫描 ──

# V4 组合策略扫描
python3 run_three_strategies.py

# 底部苏醒策略扫描（盘后）
python3 scripts/scan_bottom_awakening.py

# Alpha 过滤
python3 alpha_filter.py

# 板块轮动分析
python3 sector_rotation.py

# ── 数据同步 ──

# Tushare 数据导入
python3 scripts/update_daily_price_cron.py
python3 scripts/backfill_tushare.py
python3 scripts/backfill_margin.py          # 融资融券数据回填
python3 scripts/sync_akshare.py             # AKShare 拓展数据源
python3 scripts/sync_mainforce_data.py      # 主力资金数据同步
python3 scripts/sync_fina_indicator.py      # 财务指标数据同步

# ── 模拟交易 & 持仓 ──

# 模拟交易扫描
python3 scripts/sim_trading.py scan

# AI 模拟交易
python3 ai_sim_trading.py

# 盘中自动止盈止损执行（每5分钟运行）
python3 scripts/position_monitor.py

# ── 通知 & 报告 ──

# 飞书推送（盘前/盘中/收盘）
python3 scripts/feishu_alerts.py morning
python3 scripts/feishu_alerts.py alert  # 盘中 5min 监控
python3 scripts/feishu_alerts.py daily

# 每日早报 (6:30)
python3 scripts/morning_briefing.py

# ── 回测 & 分析 ──

# Alpha 信号
python3 alpha_signal_integration.py
python3 backtest_alpha.py

# V4 + ML 对比回测（V6.5 vs V8.0，最优参数 pct=0.10 bw=0.10）
ML_BACKTEST_PCT=0.10 ML_BACKTEST_BW=0.10 python3 scripts/backtest_v4_ml_v65_vs_v80.py

# ── 调试工具 ──

python3 market_state.py                   # 市场状态检测
python3 debug_prediction.py               # ML 预测输出
python3 check_features.py                 # ML 特征分布
python3 check_label_dist.py               # 标签分布
python3 scripts/mainforce_scoring.py      # 主力资金评分
python3 scripts/check_pipeline.py         # 全流水线验证

# ── 部署 ──

bash sync-to-server.sh   # 本地 → 阿里云 ECS
bash sync-from-server.sh # 阿里云 ECS → 本地
```

## 定时任务 (crontab)

来源：`scripts/quant_crontab`

| 时间 | 任务 |
|------|------|
| 交易日 09:00 | 盘前飞书推送（候选股） |
| 09:30-14:30 每30min | `auto_refresh_data.sh` 数据刷新 |
| 09:30-14:55 每5min | 飞书告警（仅通知，不交易） + 持仓监控（`position_monitor.py` 自动止盈止损执行） |
| 15:05 | 收盘报告飞书推送 |
| 17:00 | Tushare 日线数据导入 |
| 17:30 | 模拟交易扫描 |
| 17:45 | V4 策略扫描 |
| 17:55 | 底部苏醒策略扫描 |

注意：`feishu_alerts.py alert` 仅发送飞书通知，不执行交易；`position_monitor.py` 负责实际自动止盈止损卖出。两者独立运行，互不依赖。

## 架构概述

### 双代码结构并存（迁移中）

**Import 链**：`app.py` → `app_api.py` → `app_core.py`(facade) → `quant_app/services/*` & `quant_app/utils/*`

```
app.py                     # 入口：uvicorn.run("app_api:app", port=5001)
app_api.py                 # FastAPI 应用创建，CORS/中间件，注册 9 个子路由
app_core.py                # ★ 外观(facade)：从 quant_app 重新导出，保持旧 import 兼容
quant_app/                 # ★ 重构后的模块化包（当前主代码）
├── main.py                # 包入口，导出 config/auth/notification 符号
├── models/                # 预留（ORM/data models）
├── data/                  # 预留
├── utils/
│   ├── config.py          # 中央配置（env 变量、路径、DB 连接）
│   ├── auth.py            # bcrypt 密码哈希 + 会话令牌
│   ├── authz.py           # 授权检查
│   ├── persistence.py     # JSON 文件 I/O（线程安全原子写入）
│   ├── indicators.py      # 技术指标（EMA/MACD/KDJ/BOLL/ATR）
│   ├── model_loader.py    # ML 模型加载（LRU 缓存）
│   └── risk_config.py     # 风控参数配置
├── services/
│   ├── strategy_service.py    # ★ 最大模块(~106KB)：股票评分/扫描/分析
│   ├── market_service.py      # 行情数据、RPS、交易日判断
│   ├── realtime_service.py    # ★ 三层行情降级链路
│   ├── backtest_service.py    # 历史回测引擎
│   ├── technical_service.py   # 技术指标薄封装
│   └── notification_service.py# 飞书/邮件/短信通知
└── routes/
    ├── market.py              # 行情路由（盘前/指数/板块）~41KB
    ├── scanning.py            # 扫描路由（~47KB，第二大）
    ├── dashboard.py           # 仪表盘路由
    ├── recommend.py           # 推荐路由
    ├── auth.py                # 登录注册路由
    ├── admin.py               # 管理后台路由
    ├── pages.py               # 页面渲染路由
    ├── signals.py             # 信号路由
    └── strategy.py            # 策略路由（~3.4KB，最小）
scripts/                   # 独立运行的脚本（cron/回测/数据工具）
templates/                 # Jinja2 模板（index.html ~134KB，含内联 JS）
static/                    # CSS/图标/PWA manifest（manifest.json + sw.js）
data/                      # JSON 运行时状态 + ML 模型 .pkl 文件（自动生成，除 .env 外无需手工编辑）
├── *.json                 # positions/signals/users/sessions 等运行时状态
├── ml_stock_model_v*.pkl  # LightGBM 模型文件
├── rps_data/              # RPS 计算缓存
├── track/                 # 推荐记录
└── ml_features/           # ML 特征缓存
logs/                      # 应用日志
```

### 依赖清单 (requirements.txt)

```
fastapi==0.115.0  uvicorn[standard]==0.30.0  jinja2==3.1.4  python-multipart==0.0.9
pandas==2.2.2  pymysql==1.1.1  cryptography==42.0.8
tushare==1.4.7  python-dotenv==1.0.1  bcrypt>=4.0.1  DBUtils>=3.1.0
```

无 `pyproject.toml`，无 Docker 文件，无 pytest/lint 配置。

### 已废弃代码（不要改、不要用）

| 文件 | 说明 |
|------|------|
| `app_server.py` (141KB) | 原始单体核心，带独立 SHA256 认证。已由 `quant_app/` 替代 |
| `app_thin.py` | 指向其他项目的轻量变体，与本项目无关 |
| `archive/` | 废弃脚本归档 + 旧版 ML 模型（ml_stock_model.pkl, v3/v4 系列等），不应被引用 |

### 数据流

```
Tushare Pro API ──→ MySQL (daily_price / market_index_daily / stock_basic)
  └── scripts/update_daily_price_cron.py, backfill_tushare.py, sync_akshare.py 按 cron 填充

阿里云/东方财富 ──→ HTTP 实时行情（JSON）
  └── realtime_service.py 三层降级：缓存(30s TTL) → 腾讯(3s) → 东方财富(3s) → 阿里云(3s)

        ↓
quant_app/services/* (业务逻辑 + 策略)
        ↓
JSON 文件 (data/*.json) + MySQL + 飞书/邮件/短信
```

### ML 流水线

- **模型**：LightGBM LambdaRank，~105 特征
- **训练**：`ml_train_v8_0.py` → 模型保存至 `data/ml_stock_model_v8_0.pkl`
- **预测**：`ml_predict.py` 读取模型，输出每日预测排序
- **日常选股**：`ml_daily_top5.py` 基于 ML 预测输出 Top5
- **V8.0 回测**：+64.62% / 夏普 1.96 / 回撤 27.57% (2025-10 ~ 2026-05)

### 架构约束

- **无 ORM**：全项目使用 pymysql 裸 SQL，无 SQLAlchemy/SQLModel
- **无类型注解**：Python 函数无 type hints
- **路由层以下无异步**：仅 FastAPI 层使用 async/await 语义，services/utils 均为同步代码
- **实时数据统一入口**：所有实时行情必须走 `realtime_service.py`，禁止直接调用外部 API

### 行情降级链路

所有实时行情统一走 `quant_app/services/realtime_service.py`，调用链：

```
缓存(30s TTL) → 腾讯行情(3s超时) → 东方财富(3s超时) → 阿里云(3s超时)
```

外部请求统一 3 秒超时确保快速降级。前端 JS 调用 API 前，先 `curl http://localhost:5001/api/xxx` 确认返回 JSON 结构。

### 选股流水线

当前主策略 **V4+ML 混合选股**（`generate_v4_ml_candidates`）：

1. **V4 规则初筛** — `_v4_score_single()` 技术面+资金流评分，取 Top60
2. **ML 预测** — LightGBM LambdaRank 模型（V8.0 > V6.7 > V6.6 > V6.5）预测排序分数
3. **百分位软过滤** — 候选池内 ML 分数转横截面百分位，低于阈值淘汰（默认 ≥0.10，历史调优最优参数 pct=0.10, bw=0.10）
4. **混合评分排序** — `blended = V4分×(1-w) + ML百分位×100×w`（默认 w=0.10），取 Top5

### 策略状态

| 策略 | 状态 | 说明 |
|------|------|------|
| **V4+ML 混合** | **当前主策略** | V4.1 初筛(30只) → ML百分位过滤(≥0.10) → 混合评分(ML权重0.10)取Top5，v8.0 回测 +64.62% / 夏普 1.96 / 回撤 27.57% (2025-10 ~ 2026-05) |
| 底部起步 | 已下线 | 回测 -6.31%，2026-05-02 关闭。要素融入 V4 |
| 强势活跃 | 已下线 | 回测 -14.87%，文件归档至 archive/ |

### 市场状态机

`market_state.py` 读取指数趋势 + 宽度 + 波动率 + 成交量 → 分类为 `trend_up / trend_down / range / panic / overheated` → 动态调整止损/止盈/最大持仓/ML阈值。

## 项目易错点

### 前端 JS
- **TDZ 暂时性死区**：`let`/`const` 必须在 IIFE 之前声明。验证语法用 `node -e "new Function(jsCode)"`。
- **登录跳转丢 hash**：登录后恢复 `#positions` 等 hash，用 `sessionStorage` 保存和恢复。
- **API 字段映射**：写前端前先 `curl` 看实际 JSON 结构，不要猜字段名/类型/单位。

### 后端 Python
- **SQL 注释中的 `%`**：`cursor.execute(sql, params)` 内部用 `%` 格式化，注释里写 `%` 会报错。
- **import 链**：删模块前 `grep -rn "import.*模块名"` 确认零引用。重导出时保持原名兼容。
- **行情 fallback**：所有实时数据必须走 `realtime_service.py`，不要直接调用外部 API。

## 注意事项

- `.env` 文件必须含 TUSHARE_TOKEN、MYSQL_*、ALIYUN_APP_CODE、FEISHU_WEBHOOK、SMTP_*、ALIYUN_SMS_*
- 两个 CLAUDE.md 同时生效：本文件（项目级）和 `~/.claude/CLAUDE.md`（全局工作规范）
- `index.html`（根目录，~60KB）为独立页面，与 `templates/index.html`（~134KB，内联 JS）不同，注意区分
