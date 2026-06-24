
## 最新状态 (2026-06-15) — 板RPS实时扫描修复 + crontab修复 + 风控规则对齐

### 板RPS实时扫描 buys[:avail_slots] 修复
- `scripts/live_trading_scheduler.py:806`: `for sig in buys[:avail_slots]` → `for sig in buys`
  - bug: 取前N只候选(按综合分排序)，第一只买不起就直接空手返回，不继续试更便宜的候选
  - 修复后: 遍历所有合格候选，从高分到低分，买得起的就买
  - 修复效果: 隆扬电子(100股@100.01) 成功买入，之前因德福科技(156元)买不起就空手

### QMT 持仓 buy_date 补查 sim_positions
- 时间风控(超时卖出/强制平仓/移动止盈历史峰值)依赖 buy_date，但 QMT 持仓不返回此字段
- 修复: `live_trading_scheduler.py:964-980` 当 pos.get("buy_date") 为空时从 sim_positions 表补查
- 修复后: 楚江新材 days_held 从始终 0 → 正确显示 2天

### crontab 环境变量修复(影响所有定时任务)
- 根因: macOS cron 在重定向(>>)中不展开 $QUANT_DIR，且环境变量定义在文件末尾(在所有任务之后)
- 修复: crontab 文件所有日志路径改为绝对路径，去掉 $QUANT_DIR 依赖
- 影响: 之前所有定时任务(盘中监控/数据刷新/盘后扫描/飞书推送)全部静默失败了一整天
- crontab 频率从 */10 → \* (每分钟)，盘中实时监控1分钟一次

### 楚江新材减仓
- QMT 实盘减仓 3800→2000 股，仓位 55.3%→29.1%，释放约 25,000 现金
- 减仓后 V11 预算充足，生益科技 166.41 下单(涨停未成交，手机撤单)

### 止盈止损规则确认
- 当前规则为回测最优: 固定止损-7% / ATR动态止损(2×ATR) / -5%硬兜底 → 三者取最紧
- 移动止盈: 峰值>5%后回撤2×ATR(保底3%) → 锁利卖出
- 超时: ≥5天且<3%盈利 → 卖 / >8天强制平仓
- 无固定+8%止盈(已确认不回测最优)

### V11 入场
- 生益科技(600183.SH) 下单 100股@166.41，涨停未成交，手机撤单

### 板RPS实时扫描 2026-06-15 成交记录
- 10:02 隆扬电子(301389.SZ) 100股@100.01 板RPS实时扫描(原第4候选)
- (约10:00-10:10间) 烽火通信(600498.SH) 100股@66+ 板RPS实时扫描

## 最新状态 (2026-06-13) — 板RPS周线选股管线 + V11.2适配ML模型 + 清理旧模型

### 新选股管线：每周板RPS90 → Top5板块 → ML排序
- `quant_app/services/board_rps_scanner.py`: 新增板RPS周线计算模块
  - `get_board_rps(use_weekly=True)`: board_concept_hist日频→ISO周聚合→836板块RPS排序
  - `get_top_board_stocks()`: 取Top5板块成分股(排除ST/688/8xx)，DISTINCT去重
  - `board_scan_recommend()`: 板RPS+ML排序，失败降级到纯ML
  - `compute_weekly_board_rps_history()`: 全历史周线预计算(用于训练特征)
- `scripts/live_trading_scheduler.py`: cmd_scan()改调用`_board_rps_scan_recommend()`
  - 替换原来的`_factor_scan_recommend()`(5因子等权模型)
  - 满仓crash修复：`ml_candidates`未初始化bug

### V11.2(板RPS) 新模型训练
- Windows(192.168.10.39): 修改`ml_train_v11_2.py`→加入`board_rps_max`/`board_rps_mean`/`in_top5_board`特征
- 18子模型/131特征(原97特征+3板RPS特征+其他新增)/集成IC=0.139
- 训练耗时~15分钟，模型167MB→159MB
- 模型注册为`v11.0`，替换原Mac重训练模型
- Mac重训练(V11.0 Top500)保留为备用: `ml_stock_model_v11_0_mac_retrain.pkl`

### 模型清理
- 删除14个旧模型文件(.pkl)，释放约1.2GB:
  - 实验模型: 7lgb/11models/7model/bad/oos_true → 已删除
  - 旧备份: oos_backup/old/retrain_backup/board_rps_repeat → 已删除
  - V8归档: v8_0/v8_1_oos → 已删除
- 保留6个核心模型(~520MB):
  - 生产: ml_stock_model_v11_0.pkl (V11.2+板RPS)
  - 备用: mac_retrain/full/pre_board_rps/oos_v2/v11_2

### 系统时间线总结
- 2026-06-05: 旧V11.0模型在最新数据WF IC转负(-0.0243)
- 2026-06-13: 诊断→重训练(Mac Top500 WF IC=0.043)→Windows全量训练(IC=0.139)→板RPS周线特征加入→新模型部署
- 选股管线: 5因子等权 → 板RPS周线+ML排序

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

## 2026-06-22 收盘后修复

### QMT 持仓数据自动更新修复
**问题1**：`/position` 端点的盈亏(profit)全显示0
**根因**：QMT 的 `m_dFloatProfit` 在收盘后返回0，HTTP服务直接透传
**修复**：`C:\iquant_http_service.py` (及 `C:\qmt_service\`) — `/position` 端点改为自算利润 `market_value - cost_price × volume`
**问题2**：已清仓股(如002171楚江)仍在持仓列表残留
**根因**：QMT策略写 `qmt_position.json` 不清理 volume=0 的持仓
**修复**：`/position` 和 `/balance` 端点过滤 `volume <= 0` 的持仓

### 文件变更
- `C:\qmt_service\iquant_http_service.py` — /position 自算利润 + 过滤空仓
- Windows 计划任务 `iquant_http` 确保系统启动时自动运行

## 2026-06-23 开盘修复

### _get_dynamic_positions() 缺少 return 语句
- **问题**：monitor 报 `TypeError: cannot unpack non-iterable NoneType object`
- **根因**：第82行注释后漏了 `return (ml, sc)`，函数隐式返回 None
- **修复**：在函数末尾 `# ML和Scanner各管各的仓位` 注释后添加 `return (ml, sc)`

## 2026-06-23 QMT HTTP 服务自动恢复

### 问题
HTTP 服务通过 SSH 后台进程运行时，SSH 断开后约10分钟进程退出，导致 QMT 数据接口不可用。Windows 计划任务 `\iquant_http` 无法可靠拉起。

### 解决方案
在 `market_monitor.py` 中添加 `_ensure_qmt_http()` 守护函数：
- 每60秒（非交易时段）/ 30秒（交易时段）检查 192.168.10.25:1430 端口
- 不可达时通过 `subprocess.run` + SSH 远程拉起
- 日志记录拉起结果

### 文件变更
- `scripts/market_monitor.py`：新增 `_ensure_qmt_http()` + 主循环调用
- 非交易时段轮询间隔从 300s 改为 60s

## 2026-06-23 收盘后虚假止盈 & 交易时段保护

### 问题
- 收盘后(15:56~15:59) `cmd_monitor` 仍在运行，对 300903 触发8组虚假"兜底止盈"卖单
- 根因：QMT快照stale后降级到腾讯财经获取过期价格，止盈逻辑误判
- 共产生16条虚假信号 + 1条IPC pending挂单

### 修复
- `_is_market_open()`: 新增强制交易时段检查(9:15-11:30, 13:00-15:00)
- `cmd_monitor()`: 开头增加 `_is_market_open()` 检查，非交易时段立即return
- 删除16条虚假信号 + 清理QMT端IPC pending命令

## 2026-06-24 盘前修复

### _monitor_v11_entry 待执行信号 SQL 过滤 bug

**问题**：scan 写入的 ML 候选股从来不会被 monitor 选中买入（历史以来 6/18/6/19/6/22 全部"超时"/"已过期"）

**根因**：`scripts/live_trading_scheduler.py:762` SQL 用 `DATE(created_at)=CURDATE()` 过滤，但：
- scan 在 T 日 17:30 写入候选，`created_at = T 日 18:xx`
- monitor 在 T+1 日 9:15+ 跑，`CURDATE() = T+1 日`
- `DATE(created_at)=CURDATE()` 永远不匹配 → 0 只候选

**修复**：把 `DATE(created_at)=CURDATE()` 改为 `signal_date=CURDATE()`，让 monitor 处理"信号日=T-1"的待执行候选（即昨日 scan 写入、今日 monitor 买入）。

**验证**：
- AST 解析通过
- 模拟 6/23 monitor：修复前 0 只 → 修复后 5 只（603938/002805/002407/600206/600596）
- 6/24 monitor 跑时将按 ml_prob 降序择时入场

**文件**：`scripts/live_trading_scheduler.py:762`（仅 1 行 SQL + 注释）


## 2026-06-24 盘前修复 (第二轮)

### 1. sim_signals 122条错乱信号清理
**问题**: 9:15-9:30 集合竞价期间, monitor 30秒循环持续触发 603002/300903 止损/止盈, 每轮2条×30轮=120+条, 全部写入 sim_signals "已平仓", 但 QMT 实际持仓未变。
**修复**: 删除 9:15+ 所有"止损/峰值止盈/分批止盈/兜底止盈"信号 (122条)
**文件**: MySQL 直接 DELETE, 非代码修复

### 2. _is_market_open 开盘时间延后 (9:15→9:30)
**问题**: 集合竞价撮合期 (9:15-9:30), 腾讯财经降级数据用撮合价, 容易误触发止损
**修复**: `morning_start = datetime.time(9, 30)`, 注释说明原因
**文件**: `scripts/live_trading_scheduler.py:84-92`

### 3. monitor 持仓监控加日内去重
**问题**: QMT 卖出单未成交时, 下次30秒循环又看到同只持仓, 重复触发止损
**修复**: T+1检查后插入去重逻辑: 查 sim_signals 当日是否有该 ts_code 的任何 sell 类信号 (止损/峰值止盈/分批止盈/兜底止盈/RPS止损/恐慌清仓), 有就跳过
**文件**: `scripts/live_trading_scheduler.py:~1241` (RPS止损之前)

### 4. _monitor_v11_entry 仓位计算修复 (ML自己的槽位)
**问题**: 原 `ml_max - total_held` 一刀切, 把4只 Scanner 持仓算到 ML 头上, 日志显示"仓位已满(4/2)", 永远挡死 ML 入场
**根因**: scanner 持仓 ≠ ML 持仓, 应该用 `_classify_single_hold(p)` 区分策略
**修复**: `avail_slots = max(0, ml_max - ml_held)` 其中 `ml_held` 按 `_classify_single_hold(p) == "ml"` 计数
**验证**: 4只都是 scanner → ml_held=0, ml_max=2 → avail_slots=2 ✓
**文件**: `scripts/live_trading_scheduler.py:786-795`


## 2026-06-24 10:04 盘前全面修复 (共 6 项修复 + 数据清理)

### 背景
今早开盘发现系统存在多项问题：ML 候选从未被买入、止损失效、IPC 指令丢失、仓位计算错误、集合竞价误触发等。

---

### 1. SQL 过滤 bug — monitor 从未买入 scan 候选 (P0)
**问题**: scan 在 T 日 17:30 写入候选 (signal_date=T), monitor 在 T+1 日 9:15+ 跑时用 `DATE(created_at)=CURDATE()` 过滤, 永远不匹配 (created_at=T 日晚上, CURDATE()=T+1 日)
**根因**: `scripts/live_trading_scheduler.py:762` 一行 SQL
**修复**: `DATE(created_at)=CURDATE()` → `signal_date=DATE_SUB(CURDATE(), INTERVAL 1 DAY)`
**验证**: 模拟 6/24 monitor: 修复前 0 只 → 修复后 5 只 (603938/002805/002407/600206/600596)
**文件**: `scripts/live_trading_scheduler.py:762`

### 2. ML 仓位计算 bug — V11 永远被"仓位已满"阻挡
**问题**: 4 只 scanner 持仓被算到 ML 头上, `ml_max - total_held = 0` → 日志显示"仓位已满(4/2)"
**根因**: `_monitor_v11_entry` 用 `total_held` 一刀切, 没按 `_classify_single_hold` 区分 ML/Scanner
**修复**: `avail_slots = max(0, ml_max - ml_held)` 其中 `ml_held` 按 `_classify_single_hold(p) == "ml"` 计数
**验证**: 4 只都是 scanner → ml_held=0, ml_max=2 → avail_slots=2 → 600206 被 V11 成功买入
**文件**: `scripts/live_trading_scheduler.py:786-795`

### 3. _is_market_open 集合竞价过滤
**问题**: 9:15-9:30 集合竞价撮合期, 腾讯财经降级数据用撮合价, 603002/300903 被误触发 272 次止损
**修复**: `morning_start = datetime.time(9, 15)` → `morning_start = datetime.time(9, 30)`
**文件**: `scripts/live_trading_scheduler.py:84-92`

### 4. monitor 日内去重 — 防重复触发止损
**问题**: QMT 卖出单未成交时, 下次 30 秒循环又看到同只持仓, 重复触发止损 (272 次)
**修复**: T+1检查后插入去重逻辑: 查 sim_signals 当日是否有该 ts_code 的任何 sell 类信号
**验证**: 日志显示 "⏸ 宏昌电子 今日已触发卖出(止损), 跳过本轮"
**文件**: `scripts/live_trading_scheduler.py:~1241` (RPS止损之前)

### 5. IPC 写入 qmt_cmd.json — 买卖指令从未到达 QMT (P0)
**问题**: HTTP 桥 `/sell` 和 `/buy` 端点只写 MySQL sim_signals, 不写 qmt_cmd.json → QMT 策略永远收不到指令
**根因**: `C:\qmt_service\iquant_http_service.py` 缺少 IPC 写入逻辑
**修复**: 在 `/sell` 和 `/buy` handler 的 `_q(INSERT INTO sim_signals...)` 之后插入 IPC 写入代码, 写入 `C:\Users\Public\qmt_cmd.json`
**验证**: 手动发送 SELL 603002 指令 → qmt_cmd.json 出现 pending 命令 → QMT 策略处理 → 603002 成功卖出
**文件**: `C:\qmt_service\iquant_http_service.py` (line 198+ / 262+ 两处)

### 6. priceType 非法值修复
**问题**: IPC 代码硬编码 `priceType=-1` (QMT 非法值) → passorder 返回 0 → 所有下单失败
**修复**: `priceType=-1` → 根据市场选择合法值 (5=SH市价 / 11=SZ市价)
**验证**: 600206 有研新材 成功买入 600 股
**文件**: `C:\qmt_service\iquant_http_service.py` (IPC 写入代码内)

### 7. 数据清理
- 删除 9:15-9:42 期间 272 条误触发的 603002/300903 止损/峰值止盈信号
- 删除 32 条重复 600206 买入候选 + 测试 000001 信号
- 清理 qmt_cmd.json 残留旧命令
- 重启 HTTP 桥进程 (wscript 脚本)

---

### 今日系统运行状态 (10:04)
| 项目 | 状态 |
|---|---|
| QMT 连通 | ✅ ping ok |
| iQuant 客户端 | ✅ XtItClient.exe PID 11916 |
| IPC 管道 | ✅ 指令可到达 QMT 策略 |
| passorder | ✅ 成交 (600206买入/603002卖出/300903卖出) |
| V11 择时入场 | ✅ 5 候选 → 1 买入 600206 |
| 板RPS 扫描 | ✅ 305→20 通过, 今日无触发买入条件 |
| 持仓监控 | ✅ 止损/止盈正常触发并成交 |
| 日内去重 | ✅ 防止重复触发 |

### 今日成交
| 股票 | 操作 | 数量 | 价格 | 类型 |
|---|---|---|---|---|
| 600206 有研新材 | 买入 | 600股 | 47.37 | V11 ML候选 |
| 603002 宏昌电子 | 卖出 | 1600股 | ~22.92 | 止损 -8% |
| 300903 科翔股份 | 卖出 | 200股 | ~111 | 峰值止盈 |

### 当前持仓 (3只)
| 股票 | 持股 | 成本 | 备注 |
|---|---|---|---|
| 600206 有研新材 | 600 | 47.37 | T+1 |
| 002515 金字火腿 | 1300 | 9.12 | 峰值21.4%, trailing stop 9.73 |
| 300655 晶瑞电材 | 2700 | 17.22 | 正常 |
| 总资产 | 152,880 | 可用 62,711 | 市值 90,169 |


## 2026-06-24 11:26 全天修复与策略调整汇总

---

### Mac 端修改 (scripts/live_trading_scheduler.py)

#### 1. SQL 过滤 — monitor 从未买入 scan 候选 (P0)
**问题**: scan 在 T 日 17:30 写入(signal_date=T), monitor T+1 日用 `DATE(created_at)=CURDATE()` 过滤, 永远不匹配
**修复**: → `signal_date=DATE_SUB(CURDATE(), INTERVAL 1 DAY)`
**验证**: 模拟 6/24 monitor: 修复前 0 只 → 修复后 5 只 (603938/002805/002407/600206/600596)

#### 2. ML 仓位计算 — V11 被"仓位已满"永远阻挡
**问题**: 4 只 scanner 持仓被算到 ML 头上, `ml_max - total_held = 0`
**修复**: `avail_slots = ml_max - ml_held` 按 `_classify_single_hold(p) == "ml"` 区分
**验证**: 4只都是 scanner → ml_held=0 → avail_slots=2 → 600206 买入成功

#### 3. _is_market_open 避开集合竞价
**问题**: 9:15-9:30 撮合期腾讯财经用撮合价, 603002/300903 被误触发 272 次止损
**修复**: `morning_start = datetime.time(9, 30)`
**验证**: 集合竞价期不再触发止损

#### 4. 日内去重 — 防重复触发止损
**问题**: QMT 卖出未成交时 30s 循环重复触发 (272 次)
**修复**: 同 ts_code 当日任意 sell 类信号存在 → 跳过本轮
**验证**: 日志 "已触发卖出(止损), 跳过本轮"

#### 5. Scanner 诊断日志
**问题**: "无触发买入条件" 不显示具体原因
**修复**: 打印四类拦截统计: 双不过/仅ML拦/仅分拦/已持仓 + top5 详情
**验证**: 日志 "ML拦: 600183 生益科技 综合66 ML=0.275"

#### 6. Scanner 过滤字段修正 (关键)
**问题**: monitor 用 `realtime_score >= 60`, 但 scanner 内部用 `combined_score = ml×50 + realtime×0.5` 排序, 两者不一致导致全拦
**修复**: monitor 改为 `combined_score >= 60`
**验证**: 修复后 scanner 成功买入 3 只 (600460/600552/603002)

#### 7. Scanner 移除 ml_prob 重复过滤
**原因**: 候选池已过 ML 排序, scanner 只需综合分+盘中条件
**修复**: 移除 `ml_prob >= 0.3`
**验证**: Scanner 买入信号正常触发

#### 8. Scanner "今日已卖出" 过滤
**问题**: 603002 止损后当日又被 Scanner 重新买入 (低买高卖倒挂)
**修复**: 查询 sim_signals 当日止损/止盈/超时/T+3 记录, 过滤已卖出股
**验证**: 603002 不再重复买入

#### 9. Scanner T+3 短线平仓规则
**规则**: Scanner 持仓 ≥ 3 天 → 自动平仓
**实现**: `_classify_single_hold(pos) == "scanner" and days_held >= 3`
**说明**: V11/ML 持仓保持原规则 (≥5天+盈利<3%→超时, >8天→强平)

---

### Windows 端修改 (C:\qmt_service\iquant_http_service.py)

#### 10. IPC 写入 qmt_cmd.json (P0)
**问题**: /sell 和 /buy 端点只写 MySQL, 不写 qmt_cmd.json → QMT 策略收不到指令
**修复**: 在每个 `_q("INSERT INTO sim_signals...")` 之后插入 IPC 写入代码
**验证**: 手动发指令 → qmt_cmd.json 出现 pending → 策略处理 → 603002 成功卖出

#### 11. priceType 修复
**问题**: IPC 代码硬编码 `priceType=-1` (QMT 非法值) → passorder 返回 0
**修复**: `-1` → `5(上交所)/11(深交所)` 市价合法值
**验证**: 600206 有研新材成功买入

---

### 数据清理
- MySQL: 删除 272 条 9:15-9:42 误触发止损信号
- MySQL: 删除 32 条重复 600206 买入 + 测试 000001 信号
- QMT: 清理 qmt_cmd.json 残留旧命令
- QMT: 重启 HTTP 桥进程 (wscript 脚本)

---

### 今日成交验证
| 股票 | 操作 | 数量 | 类型 | 状态 |
|---|---|---|---|---|
| 600206 有研新材 | 买入 | 600股 | V11 ML候选 | ✅ QMT确认 |
| 603002 宏昌电子 | 卖出 | 1600股 | 止损 -8% | ✅ QMT确认 |
| 300903 科翔股份 | 卖出 | 200股 | 峰值止盈 | ✅ QMT确认 |
| 600460 士兰微 | 买入 | 400股 | Scanner综合分 | ✅ QMT确认 |
| 600552 凯盛科技 | 买入 | 700股 | Scanner综合分 | ✅ QMT确认 |
| 002515 金字火腿 | 卖1200 | 剩100股 | 手动 | ✅ 系统已记录 |

### 当前持仓 (6只)
| 股票 | 数量 | 策略 | 备注 |
|---|---|---|---|
| 600206 有研新材 | 600 | V11 ML | T+1 |
| 600460 士兰微 | 400 | Scanner | 今日买入 |
| 600552 凯盛科技 | 700 | Scanner | 今日买入 |
| 603002 宏昌电子 | 1000 | Scanner | 止损后重买 |
| 002515 金字火腿 | 100 | Scanner | 手动卖出后剩余 |
| 300655 晶瑞电材 | 2700 | Scanner | 正常持有 |

### 今日修改代码统计
| 文件 | 修改次数 | 说明 |
|---|---|---|
| scripts/live_trading_scheduler.py | 9 处 | SQL/仓位/集合竞价/去重/scanner过滤/T+3/诊断 |
| C:\qmt_service\iquant_http_service.py | 3 处 | IPC写入×2 + priceType修复 |
