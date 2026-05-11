# MEMORY.md

项目记忆 — 智能量化系统 v2.0

---

## 架构

- Web 入口: app.py → uvicorn.run("app_api:app", port=5001)
- app_api.py: FastAPI 应用创建，CORS/中间件，注册 9 个子路由
- app_core.py: 外观层，从 quant_app 重新导出，保持旧 import 兼容
- quant_app/: 重构后的模块化包（当前主代码）
  - utils/: config, auth, authz, persistence, indicators, model_loader, risk_config
  - services/: strategy_service(最大模块91KB), market_service, realtime_service(三层降级), backtest_service, technical_service, notification_service
  - routes/: market, scanning, dashboard, recommend, auth, admin, pages, signals, strategy
- scripts/: 84 个独立脚本（cron/回测/数据工具），独立运行不经过 FastAPI
- templates/: Jinja2 模板，index.html 134KB（含内联 JS）
- static/: CSS/图标/PWA manifest
- data/: JSON 运行时状态 + ML 模型 .pkl 文件

## 数据流

```
Tushare Pro API ──→ MySQL (daily_price / market_index_daily / stock_basic)
阿里云/东方财富 ──→ HTTP 实时行情（JSON）
        ↓
quant_app/services/* (业务逻辑 + 策略)
        ↓
JSON 文件 (data/*.json) + MySQL + 飞书/邮件/短信
```

## 行情降级链路

缓存(30s TTL) → 腾讯行情(3s超时) → 东方财富(3s超时) → 阿里云(3s超时)
外部请求统一 3 秒超时。

## 选股流水线

1. 规则过滤 — strategy_service C3.0 V3 评分
2. ML 评分 — LightGBM 预测 3 日涨幅概率（22 特征）
3. Alpha 集成 — 额外因子信号
4. 综合得分 = 规则得分 × ML 概率 → 排序

## 策略状态

- V4 组合: 当前主策略。技术面+主力评分综合，回测+21.76%/夏普1.79
- 底部起步: 已下线(回测-6.31%, 2026-05-02), 要素融入V4
- 强势活跃: 已下线(回测-14.87%), 归档至 archive/

## 市场状态机

market_state.py: 指数趋势+宽度+波动率+成交量 → trend_up/trend_down/range/panic/overheated → 动态调参

## 决策记录

- 2026-04-27: 从 app_server.py 单体迁移至 quant_app/ 模块化结构
  ref: app_api.py, quant_app/
- 2026-05-02: 底部起步策略下线，要素融入V4
  ref: backtest_bottom_*.py → archive/
- 2026-05-02: 强势活跃策略下线，归档至 archive/
  ref: backtest_strong_active.py → archive/

## 约定

- 实时行情统一走 realtime_service.py，不直调外部 API
- SQL 注释不能用 %，与 cursor.execute 格式化冲突
- 删模块前 grep -rn "import.*模块名" 确认零引用
- 写前端前先 curl 看实际 JSON 结构，不猜字段名
- 登录跳转用 sessionStorage 保存和恢复 hash

## 部署

- 单进程 uvicorn，手动 scp 到阿里云 ECS
- 无容器化/CI-CD
- .env 包含: TUSHARE_TOKEN, MYSQL_*, ALIYUN_APP_CODE, FEISHU_WEBHOOK, SMTP_*, ALIYUN_SMS_*

## 活跃工作

- [WIP] ML 模型迭代 — ml_train_v6_5.py → v6_6.py → v6_7.py → v8_0.py（持续演进中）
- [WIP] V4 策略 ML 融合调优
