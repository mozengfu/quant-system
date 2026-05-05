# 外部集成

**分析日期：** 2026-05-05

## API 与外部服务

### Tushare Pro（主要数据源）

- **类型**：通过 Python SDK 的 REST API（`tushare==1.4.7`）
- **用途**：A 股日线数据、交易日历、股票基本面（PE/PB/total_mv）、财务指标（yoy_sales）、资金流（main_net）、指数数据、股票列表（`stock_basic`）、RPS 计算
- **认证方式**：Token 认证（`TUSHARE_TOKEN` 在 `.env` 中）
- **使用点**：
  - `quant_app/services/market_service.py:get_tushare_pro()` —— 单例 `pro_api()` 实例
  - `scripts/update_daily_price_cron.py` —— 每日增量价格导入（17:00 cron）
  - `scripts/backfill_tushare.py` —— 历史数据批量回填，通过 `progress.json` 做断点续传
  - `ml_train_v6.py` —— 从 MySQL 加载 daily_price、moneyflow_daily、market_index_daily、fina_indicator 表（先前从 Tushare 获取）
- **限制**：Tushare Pro API 有频率限制；`backfill_tushare.py` 实现批量 + sleep 逻辑

### 实时行情（三层降级链路）

统一在 `quant_app/services/realtime_service.py` 中。调用链：

```
内存缓存（30s TTL）→ 腾讯行情（qt.gtimg.cn）→ 东方财富（push2.eastmoney.com）→ 阿里云市场（alirmcom2）
```

- **腾讯**（`_try_tencent`）：`http://qt.gtimg.cn/q={market}{code}`，GBK 编码，17 字段报价。首选源。
- **东方财富**（`_try_eastmoney`）：`http://push2.eastmoney.com/api/qt/stock/get`，JSON，100 倍缩放整数字段。备用 #1。
- **阿里云市场**（`_try_aliyun`）：`http://alirmcom2.market.alicloudapi.com/query/com`，APPCODE 认证头。备用 #2。需要 `ALIYUN_APP_CODE` 环境变量。

其他行情数据源：
- **新浪财经**（`_fetch_market_breadth_from_sina`）：`http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData` —— 市场宽度（涨跌家数），用于市场状态判断
- **新浪行情**（`SINA_HQ_URL`）：已在 `quant_app/utils/config.py` 中配置，但未在降级链路中活跃使用
- **新浪混合**（`SINA_MIX_URL`）：已配置但未活跃使用

### 阿里云短信（通知）

- **类型**：直接 HTTP GET 调用 `dysmsapi.aliyuncs.com`（HMAC-SHA1 签名，无 SDK）
- **用途**：系统事件短信告警
- **认证方式**：`ALIYUN_SMS_ACCESS_KEY` + `ALIYUN_SMS_ACCESS_SECRET` 来自 `.env`
- **代码**：`quant_app/services/notification_service.py:send_sms()`
- **配置**：`ALIYUN_SMS_SIGN_NAME`、`ALIYUN_SMS_TEMPLATE_CODE` 在 `.env` 中
- **注意**：模板代码当前显示 `SMS_xxx` 占位符——可能不可用

### 飞书 Webhook（主要告警渠道）

- **类型**：出站 HTTPS POST（JSON）到机器人 webhook URL
- **用途**：晨间简报（9:00）、止损/止盈告警（交易时段每 5 分钟）、收盘报告（15:05）、系统状态更新
- **认证方式**：Webhook URL 嵌入在 `.env`（`FEISHU_WEBHOOK`）—— 无需额外认证
- **代码**：`quant_app/services/notification_service.py:send_feishu()`
- **使用点**：`scripts/feishu_alerts.py`（morning/alert/daily 命令）、`scripts/morning_briefing.py`
- **消息格式**：`{"msg_type": "text", "content": {"text": message}}`

### QQ 邮件 SMTP（通知）

- **类型**：SSL 上的 SMTP（端口 465）
- **用途**：系统事件邮件告警、用户注册通知
- **认证方式**：SMTP 用户名/密码来自 `.env`（`SMTP_USER`、`SMTP_PASS`）
- **代码**：`quant_app/services/notification_service.py:send_email()`
- **配置**：`SMTP_HOST=smtp.qq.com`、`SMTP_PORT=465` 在 `.env` 中

## 数据存储

### MySQL `quant_db`

- **连接方式**：原生 `pymysql` —— 无 ORM、无查询构建器
- **连接池**：`DBUtils` 连接池（`get_db_config()` 在 `quant_app/utils/config.py` 中）
- **传输方式**：macOS 开发用 Unix socket（`/tmp/mysql.sock`），生产用 TCP
- **关键表**（从代码库查询确定）：
  - `daily_price` —— 日 OHLCV 数据、MA/RPS 列，约 4000+ 只股票
  - `stock_info` —— 股票元数据（名称、行业、交易所）
  - `stock_basic` —— Tushare 股票上市数据
  - `market_index_daily` —— 指数 OHLC（000001.SH、399001.SZ 等）
  - `moneyflow_daily` —— 资金流数据（main_net、按订单规模的买卖）
  - `fina_indicator` —— 财务指标（yoy_sales、pe_ttm、pb、total_mv）
  - `sim_account`、`sim_trades`、`sim_positions`、`sim_signals` —— 模拟交易引擎状态
  - `alpha_signals` —— Alpha 因子信号评分
  - `trade_cal` —— Tushare 交易日历
  - `access_log` —— API 访问日志

### JSON 文件存储（`quant-system/data/`）

- **基于文件的持久化**，用于非关系型状态（核心配置不依赖数据库）：
  - `users.json` —— 用户账户（用户名、哈希密码、角色）
  - `sessions.json` —— 活跃认证会话
  - `positions.json` —— 真实持仓跟踪
  - `signals.json` —— 告警/信号状态
  - `access_log.json` —— 请求审计日志
  - `pending_users.json` —— 注册队列
  - `reset_tokens.json` —— 密码重置令牌
  - `ml_stock_model_v*.pkl` —— LightGBM 模型包（二进制，joblib 格式）
  - `feature_config_v*.json` —— ML 特征元数据
  - `backtest_*.json` —— 回测结果快照
  - `stock_pool*.json` —— 缓存的选股结果
  - `recommend_cache.json`、`premarket_analysis.json` —— API 响应缓存
  - `track/recommendations.json` —— 推荐跟踪历史
- **写入安全**：通过 `quant_app/utils/persistence.py` 中的临时文件 + `os.replace()` 实现原子 JSON 写入

## 认证与身份

- **密码哈希**：通过 `bcrypt` 包使用 bcrypt（`quant_app/utils/auth.py`）。遗留 SHA-256（`salt$hash`）在下次登录时透明升级到 bcrypt。
- **会话令牌**：`secrets.token_hex(32)` —— 存储在内存中（`quant_app/routes/auth.py` 中的 `SESSIONS` 字典）并持久化到 `data/sessions.json`
- **认证流程**：基于 Cookie（`token=<session_token>`）。登录表单 POST -> 验证密码 -> 生成令牌 -> 设置 Cookie。登出清除令牌。
- **注册**：两步流程——用户提交 POST 到 `/api/register`，存入 `pending_users.json`；管理员通过 `/api/admin/approve_user` 审批。
- **用户角色**：`admin` / `user` —— 存储在 `data/users.json`，在 `admin.py` 路由中检查
- **无 OAuth、无 SSO、无 LDAP**

## 监控

- **日志**：标准 Python `logging` 模块 —— 文件处理器写入 `quant-system/logs/`：
  - `app.log` —— 主应用输出（uvicorn 的 stdout/stderr）
  - `app.error.log` —— 错误级别日志
  - `position_monitor.log` —— 盘中持仓扫描结果
  - `feishu_alerts.log` —— 飞书通知投递日志
  - `daily_import.log` —— Tushare 日线数据导入
  - `sim_trading.log` —— 模拟交易引擎活动
  - `morning_briefing.log` —— 晨间简报输出
  - `sync.log` —— 服务器同步操作
- **告警**：飞书 webhook 用于人工可读告警（止损、止盈、日总结）；QQ 邮件和阿里云短信作为辅助渠道
- **无外部监控服务**（无 Datadog、Prometheus、Grafana、Sentry）

## CI/CD

- **无**。无 GitHub Actions、GitLab CI、Jenkins 或任何 CI/CD 流水线。
- **部署**：手动两步流程：
  1. `sync-to-server.sh` —— 从开发机 `scp` 到阿里云 ECS（8.148.158.153）
  2. SSH `pkill -f "python3 app.py"` + `nohup python3 app.py &` 重启
- **同步**：`check-sync.sh` 比较文件时间戳决定同步方向
- **备份**：`setup.sh` 和 `deploy_manual.sh` 是用于全新服务器安装的一次性部署脚本

## 环境变量配置

所有密钥和运行时配置通过 `quant-system/.env`：

| 变量 | 用途 | 来源 |
|----------|---------|--------|
| `TUSHARE_TOKEN` | Tushare Pro API 认证 | `quant_app/utils/config.py` |
| `MYSQL_HOST` | MySQL 主机 | `quant_app/utils/config.py` |
| `MYSQL_PORT` | MySQL 端口（3306） | `quant_app/utils/config.py` |
| `MYSQL_USER` | MySQL 用户（root） | `quant_app/utils/config.py` |
| `MYSQL_PASSWORD` | MySQL 密码 | `quant_app/utils/config.py` |
| `MYSQL_DATABASE` | 数据库名（quant_db） | `quant_app/utils/config.py` |
| `MYSQL_SOCKET` | Unix socket 路径 | `quant_app/utils/config.py` |
| `ALIYUN_APP_CODE` | 阿里云市场 API 密钥 | `quant_app/utils/config.py` |
| `SMTP_HOST` | SMTP 服务器（smtp.qq.com） | `quant_app/utils/config.py` |
| `SMTP_PORT` | SMTP 端口（465） | `quant_app/utils/config.py` |
| `SMTP_USER` | SMTP 登录（QQ 邮箱） | `quant_app/utils/config.py` |
| `SMTP_PASS` | SMTP 密码/应用码 | `quant_app/utils/config.py` |
| `FEISHU_WEBHOOK` | 飞书机器人 webhook URL | `quant_app/utils/config.py` |
| `ALIYUN_SMS_ACCESS_KEY` | 阿里云短信 access key | `quant_app/utils/config.py` |
| `ALIYUN_SMS_ACCESS_SECRET` | 阿里云短信 secret | `quant_app/utils/config.py` |
| `ALIYUN_SMS_SIGN_NAME` | 短信签名（默认：智能量化） | `quant_app/utils/config.py` |
| `ALIYUN_SMS_TEMPLATE_CODE` | 短信模板代码（默认：SMS_xxx） | `quant_app/utils/config.py` |
| `CORS_ORIGINS` | CORS 允许的来源（默认：https://lh.mozengfu.com.cn） | `app_api.py` |

## Webhook

- **出站**（仅）：
  - 飞书机器人 webhook（POST JSON 到 `FEISHU_WEBHOOK` URL）—— 所有告警发送至此
  - 阿里云短信（GET 签名请求）—— 辅助告警渠道
  - QQ 邮件 SMTP —— 第三级告警渠道
- **入站**：无。没有外部系统向此应用发送 webhook。
