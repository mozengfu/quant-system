# 技术栈

**分析日期：** 2026-05-05

## 编程语言

- **Python** 3.12+ — 主应用语言（所有后端逻辑、脚本、ML）
- **JavaScript** (ES6) — 浏览器端前端逻辑（`templates/*.html`，内联 `<script>`）
- **HTML** (Jinja2 模板) — 服务端渲染页面（`templates/`）
- **CSS** — 自定义样式表（`static/`）
- **Bash** — 部署、同步和 crontab 自动化的 Shell 脚本（`scripts/auto_scan.sh`、`sync-to-server.sh`、`setup.sh`）
- **SQL** — 嵌入 Python 的 pymysql 字符串查询（无 ORM）

## 运行环境

- **macOS Framework Build** — `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3`（开发环境）
- **Ubuntu/Debian** — 系统 Python 3（阿里云 ECS 生产环境）
- **无容器化** — 直接用 `python3 app.py` 运行；无 Dockerfile

## 框架

- **FastAPI** 0.115.0 — Web 框架，同时提供 API 端点和 HTML 页面
- **Uvicorn** 0.30.0[standard] — ASGI 服务器，绑定 `0.0.0.0:5001`
- **Jinja2** 3.1.4 — 服务端 HTML 模板（登录页、仪表盘、策略页面）
- **Starlette**（FastAPI 依赖）— 异常处理、中间件、静态文件挂载

## 关键依赖

### ML 与数据科学

| 包 | 版本 | 用途 |
|------|---------|---------|
| `lightgbm` | latest | ML 模型训练 + 推理（梯度提升树） |
| `pandas` | 2.2.2 | DataFrame 处理、特征工程、SQL I/O |
| `numpy` | latest | 数值计算（通过 pandas 间接使用） |
| `scipy` | latest | Spearman 秩相关（模型评估） |
| `joblib` | latest | 模型序列化（.pkl 文件存取） |

### 数据库

| 包 | 版本 | 用途 |
|------|---------|---------|
| `pymysql` | 1.1.1 | MySQL 客户端，原生 SQL 执行（无 ORM） |
| `DBUtils` | >=3.1.0 | MySQL 连接池 |

### 外部数据 API

| 包 | 版本 | 用途 |
|------|---------|---------|
| `tushare` | 1.4.7 | A 股行情数据（日线、基本面、财务指标、资金流向、指数数据） |
| `python-dotenv` | 1.0.1 | `.env` 文件加载 |

### 安全与认证

| 包 | 版本 | 用途 |
|------|---------|---------|
| `cryptography` | 42.0.8 | 密码学操作 |
| `bcrypt` | >=4.0.1 | 密码哈希（用户认证） |
| `python-multipart` | 0.0.9 | 表单数据解析（登录表单） |
| `secrets` | 标准库 | 会话令牌生成 |
| `hashlib` | 标准库 | SHA-256（遗留密码格式兼容） |

### HTTP / 网络

| 包 | 用途 |
|------|---------|
| `urllib.request` | 标准库 | 内部 HTTP 调用（行情数据、webhook、短信 API）—— 未使用 `requests` 包 |
| `ssl` | 标准库 | 自定义 SSL 上下文（飞书 webhook、阿里云短信） |

## 配置方式

- **环境变量** 在 `quant-system/.env`（由 `python-dotenv` 通过 `quant_app/utils/config.py` 加载）
- **JSON 数据文件** 在 `quant-system/data/` —— 用户、会话、持仓、信号、回测结果、股票池、特征配置、模型监控历史
- **Crontab** 在 `scripts/quant_crontab` —— 所有定时任务

## 平台要求

- **开发环境**：macOS + Python 3.12+（Framework 构建），本地运行 MySQL，数据库 `quant_db`
- **生产环境**：阿里云 ECS（Ubuntu），Python 3.x，MySQL，基于 SSH 密钥的手动同步
- **MySQL** 8.x —— 开发用本地 socket（`/tmp/mysql.sock`），生产用 TCP
- **无容器化**、无 CI/CD 流水线、无 Docker —— 部署靠手动 `scp` + `pkill` + `nohup` 重启
- **无 ORM** —— 所有数据库访问通过原生 pymysql 游标 + 手写 SQL
- **无测试框架** —— `tests/` 目录存在但为空（代码库中未使用 pytest 或 unittest）
