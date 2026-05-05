# 项目状态

## 当前阶段

Phase 2 — 模拟交易全自动化 ✅（2026-05-05 完成）

## 已完成

- [x] 代码库开发运行
- [x] 代码库映射（7 份分析文档）
- [x] GSD 项目初始化（PROJECT.md / config.json / REQUIREMENTS.md / ROADMAP.md / STATE.md）
- [x] Phase 1: 策略打磨与回测体系（run_backtest.py + 参数优化 + 多策略对比）
- [x] Phase 2: 自动止盈/超时卖出（execute_partial_sell + daily_scan 自动执行）
- [x] Phase 2: sim_signals 表修复 + 回撤断路器 + 仓位上限
- [x] Phase 2: 数据源统一（MySQL→JSON 同步）+ 止损阈值对齐 -3%
- [x] Phase 2: v4_scan CLI + check_pipeline.py + crontab 注释

## 待办

- [ ] Phase 3 规划与执行（风控体系完善）

## 当前上下文

- 系统已在生产环境运行（阿里云 ECS）
- V4 组合策略为主策略
- 模拟交易引擎已基本可用
- 无单元测试，维护依赖手动验证
- 存在死代码（app_server.py）和模型版本堆积问题

## 决策记录

| 日期 | 决策 |
|------|------|
| 2026-05-05 | 采用 GSD 管理项目，交互模式 + 快速深度 + 并行执行 |
| 2026-05-05 | v1 聚焦策略/模拟交易/风控/ML/监控，v2 多用户+实盘 |
| 2026-05-05 | 稳定收益标准：6 月盈利、年化>15%、回撤<15%、夏普>1.5 |
