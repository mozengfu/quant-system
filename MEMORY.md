
## 最新状态 (2026-06-09) — 仓位管控重构 + 动态资金池 + 重复买入修复

### 按策略独立仓位管控
- `live_trading_scheduler.py`: 新增 `_classify_holds_by_strategy()`，从 sim_positions/sim_signals 自动归类持仓为 ML/Scanner
- `cmd_morning_execute()` / `cmd_scan()` / `cmd_monitor()`: 改为按策略独立检查仓位上限
  - ML 和 Scanner 各占 `round(max_pos * ratio)` 个仓位，互不阻塞
  - 未知持仓按策略比例分摊，不再双重扣减
- `market_state.py`: `max_positions` 按市场状态分级 — trend_up=4 / range=3 / trend_down=2 / panic=1

### 动态资金池（按账户实时总资产 50:50）
- `scanner_strategy.py`: 新增 `_get_account_balance()` 查询 QMT `/balance` 实时总资产(30s缓存)
- `get_scanner_capital()` / `get_v11_capital()`: 改为 `总资产 × 0.5`，盈利自动放大、亏损自动收缩
- `config/scanner_config.yaml` 的 `total` 仅作为 QMT 不可达时的降级兜底

### 盘中监控资金追踪
- `cmd_monitor()`: 每次买入前查询今日 Scanner 已用资金(`sim_signals` SUM)，剩余不足 5000 则跳过
- 买入后立即扣减 `scanner_remaining`，逐笔追踪不超预算

### 市场状态判断修复
- `sim_trading.py:get_market_state_for_sim()` 第4条规则: 涨跌比<0.5 仅在**指数也下跌**时判逆市，指数上涨时仅降 threshold 不判逆市
- 解决大票拉指数但小票普跌时的误判问题

### 重复买入修复
- `cmd_monitor()`: 去重源从仅 QMT 持仓 → QMT 持仓 + `sim_signals` 今日已执行记录
- 买入后立即 `held_codes.add(ts_code)` 防同轮重复

### ZeroDivisionError 修复
- `cmd_monitor()`: `cost_price==0` 时用 `current_price` 兜底，仍为 0 则 `continue` 跳过

## 最新状态 (2026-06-08) — OOS-v2 选股注入 + V5 策略修复 + 系统清理

### OOS-v2 ML 选股注入股票池
- `strategy_service.py:scan_daily_pool()`: 盘后扫描自动调用 `predict_v11_oos.py` → OOS Top10 注入 `stock_pool.json` 头部
- OOS 模型: `data/ml_stock_model_v11_0_oos_v2.pkl`, IC=0.0859, 9子模型, 30特征
- 同时生成 QMT V5 格式 `data/stockpool.json` (`{"ts":..., "codes":[...]}`)
- 保存后自动 `scp` 到 QMT Windows `C:/Users/Public/stockpool.json`

### V5 策略修复 (qmt_strategy_v5.py)
- **stockpool 解析**: 兼容新 dict 格式 (`codes` key) + 旧 list 格式，不再回退沪深300
- **代码过滤**: `set_universe()` 前统一补 `.SH`/`.SZ` 后缀，过滤无效/退市代码
- **余额缓存**: `m_dMarketValue`→总资产减可用资金推算，去掉不存在的属性
- 余额写入条件: 总资产>0就写，不再跳过满仓情况

### 系统清理
- 移除 `com.quant.sync` (反向同步风险) + `com.quant.sshtunnel` (硬编码密码)
- 删除 7 个残留脚本: start_trader_qmt*.py, iquant_bridge_strategy.py, qmt_trader_service.py, qmt_v6_market.py, setup_windows_trader.ps1, trader_health.sh
- Crontab 精简: 12 条，去掉同花顺过期注释，按功能分组

### QMT 端 (192.168.10.25)
- 统一策略: V5，iQuant 策略编辑器内粘贴运行（非外部文件引用）
- 股票池: 60只 (OOS Top10 打头: 300085, 301358, 002969...)
- 行情: 每3秒刷新，写入 `qmt_market.json`
- 余额/持仓: 每30秒写入缓存
- HTTP 桥: iquant_http_service.py :1430, schtasks 开机自启

### 系统状态确认
- Mac LaunchAgents: apiserver(:5001) + monitor (市场监控)
- QMT 连通: ✅ ping ok, 余额 69,945 (可用 61,922), 持仓 8,023
- 定时任务: 12条 (保活/监控/选股/数据/飞书/训练)

---
# MEMORY.md

项目记忆 — 智能量化系统 v2.0

---

## 最新状态 (2026-06-07) — QMT 全链路打通 + 双策略上线

### QMT 端 (192.168.10.25)
- **统一策略**: `qmt_strategy_v5.py`，iQuant 策略编辑器内运行
  - 行情: 沪深300前200只 + 5大指数，每 **3s** → `qmt_market.json` / `qmt_index.json`
  - 交易: 读取 `qmt_cmd.json` → `passorder()` → 回写详细状态(filled/partial/rejected/canceled)
  - 缓存: 余额/持仓/委托/成交每 **30s** 写入 `qmt_*.json`，成交自动写 MySQL `qmt_trades`
  - 保护: 余额全零不覆盖、持仓空不覆盖、异常回写 failed 状态
  - 账号: `170000758981`（实盘）
- **iQuant 策略路径**: `C:\国信iQuant策略交易平台\userdata\users\18978253999\qmt_strategy_v5.py`
- **HTTP 桥**: `iquant_http_service.py` → :1430 Flask
  - 端点: /ping /buy /sell /balance /position /orders /trades /market/snapshot /market/index /cancel_order
  - IPC轮询: `sim_signals(待执行)` → `qmt_cmd.json` → 等待策略回写(30s超时)
  - 止损信号自动附加 `priceType=-1` 市价单
- **SSH**: `ssh -i ~/.ssh/id_ed25519_qmt mozf@192.168.10.25`
- **Python**: `C:\Python312\python.exe`，HTTP服务部署在 `C:\qmt_service\`
- **已清理**: 删除 `start_trader_qmt.py`、`qmt_strategy_v23.py`、`qmt_market_data.py`、`qmt_strategy_v24.py`、`iquant_strategy_v2.py` 及 7 个死计划任务

### Mac 端双策略
- **策略A — ML V11**: Top500成交额 → V11.0(18子模型,126特征) → 趋势过滤 → Top3（盘后预测，次日早盘买入）
- **策略B — 实时扫描**: QMT实时行情 → 技术因子(量能/动量/趋势/流动性/RSI/布林) → 评分≥65出信号（盘中实时扫描，发现买点立即买入）
- **资金分配**: `scanner_config.yaml` → scanner_ratio=0.5 (各50%)
- **执行流程**: `cmd_scan()` 双路盘后选股 → `cmd_morning_execute()` 早盘择时 → `cmd_monitor()` 盘中持仓监控+实时扫描买入

### 2026-06-07 QMT 策略整合
- 删除旧策略 v23/v24/v2/market_data、start_trader_qmt.py、7个死计划任务
- 合并为统一策略 v5：行情(3s) + 缓存(30s) + 交易一体
- 修 5 个 bug: BSFlag买卖方向(23=BUY)、行情采集降级(拆3s/30s双节流)、命令状态回写(异常时回写failed)、DB密码硬编码(改`_get_db_config()`读环境变量)、连接泄漏(try/finally)
- 加 3 层保护: 余额全零不覆盖、持仓空不覆盖、异常回写失败状态
- 实盘账号: 170000758981
- 仅保留 `iquant_http_service.py` + `qmt_runner.py` + test 脚本在 `C:\qmt_service\`

### 2026-06-07 盘中实时扫描上线
- `cmd_monitor` 新增盘中实时扫描选股（策略B）
- 每10分钟: QMT实时行情 → 技术因子评分 → score≥65立即买入
- 去重已持仓、逆市门槛提高到75分、仓位减半
- 执行后同步 sim_positions + 飞书通知
- 完整流程: scan(17:30 ML预测) → morning(9:35 早盘执行) → monitor(9:30-15:00 持仓监控+盘中实时扫描)

### 2026-06-07 代码修复
- pandas 3.x SQL: `params=ts_codes` → `params=tuple(ts_codes)` 修复 IN 子句
- DB密码: 6文件改用 `_get_db_config()` 读环境变量
- 交叉依赖: `quant_app/` 不再引用根 `market_state`/`app_core`
- 前端市价单: `OrderPanel.vue` 市价单选了不走 `/market-order` API → 已修复
- IPC路径: CMD文件统一为 `C:\Users\Public\qmt_cmd.json`
- Lint: 2308→936条


---



## 最新状态 (2026-06-04)

### QMT 交易桥接 (v22)
- **策略**: `scripts/qmt_strategy_v22.py` → QMT 内运行，账号 620000221031
- **Flask**: `start_trader_qmt_http.py` (v5) → Windows VM :1430，优先读 v22 缓存
- **缓存文件** (C:\Users\Public\): qmt_balance.json / qmt_position.json / qmt_order.json，每30秒刷新
- **余额**: 可用 19,998,914.98，总资产 20,000,000
- **部署命令**: sshpass scp 到 mozf@192.168.10.25，wmic process call create 启动

### 市场状态判断 (今日修改)
- `scripts/sim_trading.py` → `get_market_state_for_sim()`:
  - 数据源: 腾讯财经(实时上证) + Tushare(历史指数) + MySQL daily_price(涨跌比)
  - 逆市触发: ①单日跌>1.5% ②跌>0.7%+涨跌比<1.0 ③连续两天阴跌 ④涨跌比<0.5
  - 涨跌比 0.5~0.8: threshold 降到 0.45
  - 缓存: 10分钟 (Tushare+MySQL)
- `scripts/live_trading_scheduler.py`:
  - cmd_scan: 逆市时 slots 减半，min_score 提高到 threshold
  - cmd_morning_execute: 逆市时量比要求 1.5，高开<1%，slots 减半
  - _v11_scan_recommend: 支持 min_score 参数过滤

### 数据流
```
QMT策略(v22) ──→ qmt_balance/position/order.json ──→ Flask v5 :1430 ──→ macOS RemoteTraderExecutor
                                                                              ↓
macOS MySQL(192.168.10.30) ←── sim_positions/sim_signals ←── live_trading_scheduler
Tushare → daily_price(涨跌比) → get_market_state_for_sim()
```

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

**当前主策略：纯ML 选股**（PURE_ML=1 生产主模式）：
1. 成交额 Top300（前一日）— 防数据泄漏
2. V11.0 排序 — 7-LGBM LambdaRank 等权融合，117 维特征
3. ML 百分位过滤 — 横截面中位数 > 0.50（V11.0 输出为 raw margin，不能用绝对值）
4. 风控过滤 — 涨停追高(>9%) / 异常放量(涨>5%+量比>5) *生产中已禁用*
5. 游资评分 — 5 因子综合 ≥40 分排除 *生产中已禁用*
6. 业绩过滤 — 净利润同比 < -30% 排除 *生产中已禁用*
7. 行业分散 — 每行业最多 2 只
8. Top3 输出

**备选：V4+ML 混合**（PURE_ML≠1）：
1. V4 规则初筛 — `_v4_score_single()` 技术面+资金流评分，取 Top30
2. ML 排序 — V11.0 排序（fallback: V10.0 → V8.1 → V8.0）
3. 混合评分 — `blended = V4分×0.8 + ML百分位×100×0.2`，取 Top5

## ML 模型评估记录

### V11.0（当前主模型，2026-05-19 上线）
- **架构**: 7 子模型纯 LightGBM LambdaRank 集成，等权融合
- **算法**: 7 个 LambdaRank（seed=42,49,56,63,70,77,84），无 XGBoost
- **特征**: 117 维日频（量价/资金流/融资融券/龙虎榜/涨停板/行业动量/概念热度/业绩/大宗交易 + fina_indicator/sector_moneyflow/north_moneyflow/ml_predictions）
- **标签**: alpha_5d（行业中性化 vol-adjusted 5d return），训练时按交易日 qcut 为 10 级整数
- **训练数据**: 2024-01 ~ 2026-05，Top3000 成交额，~204 万样本
- **模型文件**: `data/ml_stock_model_v11_0.pkl` (~23MB)
- **纯ML回测** (2025-10 ~ 2026-05, 29 采样, Top5, 5天持有): +764.40% / 胜率 80.0% / 夏普 5.86 / 回撤 -8.55%
- **滚动训练**: `scripts/rolling_train.sh` 每周六 3:00 重训，成功后覆盖 OOS 备份
- **推理入口**: `ml_predict.py`（_ensemble_scores → 4 列输出: ml_score/z_score/probability/rank_pct）
- **特征构建**: 必须用 `scripts/predict_v11.build_features_v11_inference`，不能用 v6.3 等旧函数
- **注意**: LambdaRank 输出为 raw margin（大部分自然为负），过滤须用横截面百分位中位数 > 0.50

### V10.0（后备模型，2026-05-16 上线同日下线）
- **架构**: Regime-Aware 多周期集成 — Tier A 状态专用(6) + Tier B 多周期(3) + Tier C 特征子集(3)，共 12 子模型
- **算法**: LightGBM + XGBoost 混合
- **训练窗口**: 1000 交易日
- **回测**: +38% vs V8.0 +120%，权重优化方向偏向了"过去涨得好的"而非"未来能涨的"
- **状态**: 当天评估不合格下线，保留作 fallback

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
- 2026-05-19: V11.0 纯LGBM LambdaRank 集成上线
  ref: `ml_train_v11_0.py` — 7 子模型等权融合，无 XGBoost
  ref: `_load_best_model()` 优先级: V11.0 > V10.0 > V8.1 > V8.0 > ...
  ref: 纯ML 回测 +764.40%/胜率80%/夏普5.86 确认为生产主模式
  ref: PURE_ML=1 作为预设，管线为 Top300 → V11.0 → 风控/游资/业绩/行业 → Top3

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

- **纯ML (PURE_ML=1)**: 当前主策略（2026-05-19 起定型）。成交额Top300 → V11.0排序 → 百分位过滤(>0.50) → 风控/游资/业绩/行业 → Top3。回测:+764%/胜率80%/夏普5.86
- **V4+ML 混合 (PURE_ML=0)**: 备选。回测纯V4(+54%)优于混合(+2%)，ML在V4受限池中加的是噪声
- **纯V4**: 历史策略，已归档
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
成交额Top300(前日) → V11.0 ML排序 → **ML百分位中位数过滤(>0.50)** → 行业分散 → Top3
*注：风控(涨停/放量)、游资评分、业绩过滤已在2026-05后生产中禁用（回测确认规则过滤会误杀ML高收益标的）*

## 活跃工作

- [DONE] V11.0 投产（2026-05-19） — 全线切换为 V11.0 LambdaRank 集成
  - `_load_best_model()` 优先级：V11.0 > V10.0 > V8.1 > V8.0
  - 纯 ML 选股（PURE_ML=1）成交额 Top300 → V11.0 排序 → 过滤管线 → Top3
  - 回测确认: +764.40% / 胜率 80.0% / 夏普 5.86
  - V11.0 特征构建需走 `scripts/predict_v11.build_features_v11_inference`
- [DONE] V8.1 降级为 fallback（V11.0→V10.0 之后）
- [DONE] V4.1 评分调优 + 参数扫描 + 纯 ML 架构切换
- [WIP] AI 模拟炒股性能追踪 — `ai_sim_trading.py` 记录 TOP5 推荐后续表现

## 2026-05-18 Pure ML 追高风险控制（2026-05后生产中禁用）

**背景**: Pure ML 回测显示存在追高风险（std 10.82%，最大回撤 -15.84%），模型倾向于挑近期涨幅大/处于高位的票

**改动（已回滚）**:
- strategy_service.py 风控过滤逻辑在回测中确认会误杀ML高收益标的（夏普5.61→0.01），已禁用
- 涨停追高(>9%)、异常放量(涨>5%+量比>5)、游资评分、业绩暴雷过滤当前**生产中均不生效**
- 仅保留行业分散约束

**生产状态**: 
- PURE_ML=1 已通过 LaunchAgent 配置
- 端口: 5001
- **风控禁用**（2026-05后）

**回测结果** (V11.0, 2024-11~2026-05, 72采样):
- Pure ML 无风控: +1429%, 58.3%, Sharpe 2.89, 最大回撤 -15.84%
- Pure ML 有风控: +256%, 56.9%, Sharpe 1.99, 最大回撤 -13.82%

**结论**: 风控过滤显著降低收益（-82%），决定生产中禁用

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
3. **过滤系统（已禁用）** — 2026-05 回测中确认规则风控会误杀ML高收益标的（夏普5.61→0.01），生产中已禁用
4. 过滤减少了4次交易(14%)，但剩余交易质量大幅提升

### 生产配置
- 模式: PURE_ML=1 (纯ML模式)
- 选股: 成交额Top300 → ML排序(V11.0) → ML百分位中位数过滤(>0.50) → 行业分散 → Top3
- 风控(涨停追高)、游资评分、业绩过滤: **生产中已禁用**（回测确认会误杀ML高收益标的）
- PID: com.quant.system

---

## 2026-05-31 实盘交易系统部署

### 架构
- **macOS** 负责策略、调度、风控 — crontab 触发 scan/monitor/keepalive
- 通信方式：HTTP REST API，不共用文件系统

### Windows VM 配置
- 用户: `mozf` / `mozengfu` 密码 `782500`
- Flask 服务: `C:\start_trader.py` 通过 `schtasks ONLOGON` 自动启动
- 交易密码: `782500`（已配置自动解锁）
- RDP 会话保持登录即可（关窗口不断开），进程在 session 中运行

### macOS 配置
- `.env`: `TRADE_MODE=live` `ENABLE_REAL_TRADING=false`（dry-run）
- `REMOTE_TRADER_HOST=192.168.10.25` `REMOTE_TRADER_PORT=1430`
- `RemoteTraderExecutor`: 自动连接 + 自动解锁 + 保活

### Crontab（macOS 端）
```
*/5 9-14 * * 1-5 保活(keepalive)
*/10 9-14 * * 1-5 盘中监控(monitor) — 止盈止损自动执行
30 17 * * 1-5   盘后扫描(scan) — ML选股+买卖执行
```

### 流程
1. 17:00 Tushare 数据导入
2. 17:30 `live_trading_scheduler.py scan` — ML预测Top300 → 止盈止损检查 → 新买入 → 记信号
3. 9:30-15:00 `live_trading_scheduler.py monitor` — 每10分钟检查持仓止盈止损

### 风险控制
- `ENABLE_REAL_TRADING=false` 安全开关，需手动改 `true` 才执行实盘
- 持仓检查 3%止损 / 6%+10%+18%分级止盈 / 超5天清仓
- 新买入最多3只，单只≤30%资产，大盘熊市不买入

### 已知事项
- `live_trading_scheduler.py` 支持命令: scan/monitor/status/init/sync/ping/keepalive
- 首次周一交易日需确认 RDP 会话存活

### 2026-05-31 补充：飞书交易通知
- 每笔实盘买入/卖出成交后，自动推送飞书消息
- 通知内容：买卖方向、股票、价格、数量、金额、原因
- 仅当 ENABLE_REAL_TRADING=true 时才会推送（dry-run 模式下不推送）

---

## 最新状态 (2026-06-10) — Dedup 根因修复 + 限流 + 数据对齐

### P1 Dedup 根因 (UnboundLocalError)
- **症状**: 002171 / 600707 09:55 / 10:00 / 10:05 / 10:10 被 scanner 各买两次
- **根因**: cmd_monitor 函数内 `except` 块有 `import pymysql`, Python 解析期把整个函数的 `pymysql` 当作局部变量。dedup 块 (line 670+) 用 `pymysql.connect()` 时触发 `UnboundLocalError: cannot access local variable 'pymysql'`, 被 `except Exception: pass` 吞掉, dedup 永远不生效
- **修复**: 去掉 except 块内 `import pymysql`, 用模块级 import (line 17)
- **防御**: `sim_signals` 加 `active_executed_date` generated column + `uk_sim_signals_executed` UNIQUE INDEX (ts_code + date, NULL 不触发)
- **捕漏**: scanner + morning_execute UPDATE 路径都加 `isinstance(e, pymysql.err.IntegrityError) and e.args[0]==1062` 兜底

### P2 飞书限流
- `quant_app/services/notification_service.py:send_feishu` 加滑窗限流:
  - 同消息 30s 内去重 (msg_hash)
  - 60s 滑窗最多 10 条 (deque)
- 止损批量触发时不会爆量

### P3 crontab 一致性
- `crontab` 文件 monitor 频率从 `*/10` 改为 `*/5` (匹配 `crontab -l` 实际安装)

### 数据清理
- 002171 / 600707 sim_signals + sim_positions 重复行合并 (加权平均成本)
- 600031 sim_positions 对齐 QMT (1200→100 股, 加审计记录)
- sim_signals 重复 (id 319, 321) 删除

### 2026-06-09 重复买入
- 002171 在 09:55:12 (id 318) + 10:00:15 (id 319) 被买两次
- 600707 在 10:05:13 (id 320) + 10:10:13 (id 321) 被买两次
- 修复后 cmd_monitor dedup 应能拦住相同 ts_code 在同日的 scanner 重复触发
