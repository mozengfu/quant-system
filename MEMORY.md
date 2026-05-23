# MEMORY.md

项目记忆 — 智能量化系统 v2.0

---

## 架构

- Web 入口: app.py → uvicorn.run("app_api:app", port=5001)
- app_api.py: FastAPI 应用创建，CORS/中间件，注册 9 个子路由
- app_core.py: 外观层，从 quant_app 重新导出，保持旧 import 兼容
- quant_app/: 重构后的模块化包（当前主代码）
  - utils/: config, auth, authz, persistence, indicators, model_loader, risk_config
  - services/: strategy_service(最大模块~106KB), market_service, realtime_service(三层降级), backtest_service, technical_service, notification_service
  - routes/: market(~41KB), scanning(~49KB 第二大), dashboard, recommend, auth, admin, pages, signals, strategy
- scripts/: ~39 个独立脚本（cron/回测/数据工具），独立运行不经过 FastAPI
- templates/: Jinja2 模板，index.html ~32KB（含内联 JS）
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

**当前主策略：V4+ML 混合选股**（`generate_v4_ml_candidates`）：
1. V4 规则初筛 — `_v4_score_single()` 技术面+资金流评分，取 Top60
2. ML 预测 — V10.0 Regime-Aware（优先）或 V8.0 LambdaRank（后备）预测排序
3. 百分位软过滤 — ML 分数转横截面百分位，低于阈值淘汰（默认 ≥0.10）
4. 混合评分排序 — `blended = V4分×0.9 + ML百分位×100×0.1`，取 Top5

## ML 模型评估记录

### V10.0（当前主模型，2026-05-16 上线）
- **架构**: Regime-Aware 多周期集成 — Tier A 状态专用(6) + Tier B 多周期(3) + Tier C 特征子集(3)，共 12 子模型
- **算法**: LightGBM + XGBoost 混合
- **训练窗口**: 1000 交易日（V8.0 为 600）
- **验证**: Expanding window walk-forward 8 折
- **模型文件**: `data/ml_stock_model_v10_0.pkl` (~93MB)

### V8.0（后备模型）
- **特征数**: 125 (~105 base + rank features)
- **Rank IC**: 0.0664
- **集成模型数**: 5
- **回测结果** (2025-10 ~ 2026-05, pct=0.10, bw=0.10): +40.11% / 夏普 1.82 / 回撤 22.11% / 胜率 54.9% / 盈亏比 1.43
- **注**: 历史文档写 +64.62%/夏普1.96 是不同回测区间的数据

### V8.6（已废弃，2026-05-16 评估）
- **特征数**: 62 (V6 核心精简: 52 base + 10 rank)
- **Rank IC**: 0.064
- **最优参数** (pct=0.15, bw=0.20): +31.67% / 夏普 1.13 / 回撤 38.02%
- **同参数对比** (pct=0.10, bw=0.10): +15.33% vs V8.0 的 +40.11%
- **结论**: 精简特征导致信息量损失，全面不如 V8.0，不具生产价值

### V9.0（存在模型文件，未评估）
- V6.5 超参回归 + 波动率截断 + 去 rank 特征集成模型
- 加载优先级排在 V10.0/V8.0/V8.6 之后

## 决策记录

- 2026-04-27: 从 app_server.py 单体迁移至 quant_app/ 模块化结构
  ref: app_api.py, quant_app/
- 2026-05-02: 底部起步策略下线，要素融入V4
  ref: backtest_bottom_*.py → archive/
- 2026-05-02: 强势活跃策略下线，归档至 archive/
  ref: backtest_strong_active.py → archive/
- 2026-05-09: V4+ML 混合策略上线，替换原有 TOP5 选股管道
  ref: 回测 +64.62%/夏普1.96/回撤27.57% (V8.0)
- 2026-05-16: V10.0 Regime-Aware 多周期集成模型上线并下线
  ref: `ml_train_v10_0.py` — 12 子模型 3 层架构，LightGBM+XGBoost
  ref: 上线后 IC 加权融合 IC=0.24 vs V8.0 IC=0.17（代理标签 RPS_20）
  ref: 但实际回测 V10.0 +38% vs V8.0 +120%，V10.0 权重优化方向偏向了"过去涨得好的"而非"未来能涨的"
  ref: 2026-05-16 当天下线，_load_best_model() 回退 V8.0 > V10.0
- 2026-05-16: V8.6 模型评估完成，确认为降级
  ref: `ml_predict.py:163` — V8.0 > V8.6 > V9.0
  ref: 修复 `_build_features_for_stocks_v8_6` 丢失 ts_code 列的 bug (ml_predict.py:1669)
  ref: 注册 v8.1~v8.6 模型路径到 model_loader.py

## 约定

- 实时行情统一走 realtime_service.py，不直调外部 API
- SQL 注释不能用 %，与 cursor.execute 格式化冲突
- 删模块前 grep -rn "import.*模块名" 确认零引用
- 写前端前先 curl 看实际 JSON 结构，不猜字段名
- 登录跳转用 sessionStorage 保存和恢复 hash

## 部署

- 单进程 uvicorn，本机运行（服务器已下线，不再部署）
- 无容器化/CI/CD
- .env 包含: TUSHARE_TOKEN, MYSQL_*, ALIYUN_APP_CODE, FEISHU_WEBHOOK, SMTP_*, ALIYUN_SMS_*

## 策略状态

- **V4+ML 混合**: 当前主策略。V4.1 初筛(30只) → ML百分位过滤(≥0.10) → 混合评分(ML权重0.10)取Top5
- 底部起步: 已下线(回测-6.31%, 2026-05-02), 要素融入V4
- 强势活跃: 已下线(回测-14.87%), 归档至 archive/

## 市场状态机

market_state.py: 指数趋势+宽度+波动率+成交量 → trend_up/trend_down/range/panic/overheated → 动态调参

## 2026-05-22: 三重过滤管线重构

**改动**:
- `quant_app/services/strategy_service.py`:
  - 新增 ML正分过滤（ml_score > 0）：纯ML模式下，排除模型看空的候选，仅有正分股票进入排序
  - 新增 5日主力累计过滤后**回撤**：回测显示夏普2.44→0.01，误杀好票，改为仅记录不拦截
  - 去掉主力过滤降级逻辑：宁可不出结果也不推不合格的

**回测验证** (V11.0, 2024-11~2026-05, 71采样):
- 无过滤: 夏普0.52, 胜率50.7%
- **ML正分过滤: 夏普2.44, 胜率64.3%, 累积+54.63%** ✅
- 正分+主力过滤: 夏普0.01, 胜率37.5% ❌ 已回撤

**生产管线(当前)**:
成交额Top300(前日) → V11.0 ML排序 → **ML正分过滤** → 风控过滤(涨停/放量/RPS) → 游资评分 → 业绩过滤 → 行业分散 → Top3

## 活跃工作

- [DONE] V8.1 正式投产（2026-05-16） — 全线切换为 V8.1 模型
  - `_load_best_model()` 优先级：V8.1 > V8.0 > V10.0 > ...
  - 纯 ML 选股（PURE_ML=1 默认）成交额 Top300 → V8.1 直接排序选 Top3/Top15
  - 建仓推荐改为 Top3，前端字段"预测收益"→"排序强度"
  - ML Top15 页面、策略选股路由全部同步至 V8.1
  - 前端添加禁止缓存 meta，SW 版本号升至 v2
- [DONE] V8.1 模型训练：11 市场状态特征（volume_trend/sh_trend 进 Top5 重要性）
  RankIC 0.0688，5 模型 LambdaRank 集成
- [DONE] V4.1 评分调优 + 参数扫描 + 纯 ML 架构切换
- [WIP] AI 模拟炒股性能追踪 — `ai_sim_trading.py` 记录 TOP5 推荐后续表现

## 2026-05-18 Pure ML 追高风险控制

**背景**: Pure ML 回测显示存在追高风险（std 10.82%，最大回撤 -15.84%），模型倾向于挑近期涨幅大/处于高位的票

**改动**:
- `quant_app/services/strategy_service.py:2277` V4候选补充 `high_52w` / `low_52w`
- `quant_app/services/strategy_service.py:2294` Pure ML 候选补充 52周高低位
- `quant_app/services/strategy_service.py:2383-2426` 风控过滤逻辑，按市场状态分级:
  - 涨停追高(>9%): **始终生效**
  - 异常放量(涨>5%+量比>5): **始终生效**
  - 52周高位(>85%): **仅弱市(trend_down/panic/overheated)**
  - RPS过热(>95+涨4%): **仅弱市(trend_down/panic/overheated)**
  - Fallback bug 修复: 全被过滤时降级宽松模式
- `market_state.py` 市场状态判断: 上证趋势35%+创业板15%+市场广度25%+波动率15%+成交量10%

**生产状态**: 
- PURE_ML=1 已通过 LaunchAgent (`com.quant.apiserver.plist`) 配置
- 当前市场: 震荡(10.1分)，不触发严格风控
- 端口: 5001

**回测结果** (V11.0, 2024-11~2026-05, 72采样):
- Pure ML 无风控: +1429%, 58.3%, Sharpe 2.89, 最大回撤 -15.84%
- Pure ML 有风控: +256%, 56.9%, Sharpe 1.99, 最大回撤 -13.82%
- V4+ML Pool=500: +24%, 49.2%, Sharpe 0.57, 最大回撤 -16.62%

**待观察**: 2026-05-18 当周运行 Pure ML 模式，观察风控效果
- `scripts/sim_trading.py:1263` 买入前涨停检查：当日涨幅>=9.5% 跳过（买不进）
- `scripts/sim_trading.py:1263` 买入前涨停检查：当日涨幅>=9.5% 跳过
- 生产服务由 `com.quant.system` LaunchAgent 管理，plist 已加入 PURE_ML=1
- `strategy_service.py:2433` 游资收割票排除：满足≥2条排除（暴涨暴跌/高换手/连板/小市值）

## 2026-05-19: 纯V4 vs V4+ML vs 纯ML 三组回测对比

**结论：纯ML(含风控过滤) 为最优策略**

### 回测参数
- 区间: 2025-10-01 ~ 2026-05-15
- 采样: 每5天, 共29次
- 持有: 5天, Top5
- 模型: V11.0 (117特征), 成交额Top500池
- 特征构建: v6.3（V11.0缺失特征由global_medians填充）

### 三组对比结果

| 策略 | 累积收益 | 胜率 | 盈亏比 | 夏普 | 回撤 |
|------|:-------:|:---:|:-----:|:---:|:---:|
| 纯V4 | +54.47% | 51.7% | 1.99 | 1.40 | -8.36% |
| V4+ML | +2.49% | 51.7% | 1.05 | 0.32 | -8.69% |
| 纯ML(无过滤) | +305.38% | 65.5% | 3.54 | 4.38 | -7.54% |
| **纯ML(含过滤)** | **+764.40%** | **80.0%** | **3.70** | **5.86** | **-8.55%** |

### 关键发现
1. **V4+ML 混合策略无效** — ML在V4初筛的受限池(30只)里加的是噪声而非信号，累积仅+2.49%，不及纯V4的+54% (t-test p=0.48, 无显著差异)
2. **纯ML显著优于V4+ML** (p=0.0198)
3. **过滤系统有效提升纯ML质量** — 风控(涨停追高/异常放量) + 游资出货评分(≥40分排除) + 业绩暴雷(利润同比<-30%) 将胜率从65.5%提升到80.0%，累积收益从305%提升到764%
4. 过滤减少了4次交易(14%)，但剩余交易质量大幅提升

### 生产配置
- 模式: PURE_ML=1 (纯ML模式)
- 选股: 成交额Top300 → ML排序(V11.0) → 风控过滤 → 游资评分 → 业绩过滤 → 行业分散 → Top5
- 过滤阈值: 涨停追高>9%, 游资≥40分排除, 业绩<-30%排除
- PID: 54852 (com.quant.system)
