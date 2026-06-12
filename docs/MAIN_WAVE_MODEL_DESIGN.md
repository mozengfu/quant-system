# 主升浪三段式预测模型 — 设计文档

## 1. 目标

在 A 股市场, 通过三层漏斗式过滤, 选出未来 1-2 个交易日内最可能启动主升浪的个股。

## 2. 整体架构

```
┌─────────────────────┐
│  Stage 1: 大盘预测    │  → 5 档方向 + 概率
│  (ml_regime_detector │
│   + 新 direction)    │
└──────────┬──────────┘
           │ 大盘∈{强涨,涨,震荡,弱跌}
           ▼
┌─────────────────────┐
│ Stage 2: 热点板块     │  → Top 5~8 板块
│  (sector_rotation    │
│   + 动量延续)         │
└──────────┬──────────┘
           │ 板块内候选股票 (~200-500 只)
           ▼
┌─────────────────────┐
│ Stage 3: 主升浪个股   │  → Top 5~10 个股
│  V11 集成 + 主升浪    │
│  分类器 + 5 类新特征  │
└─────────────────────┘
```

## 3. 各模块

### 3.1 Stage 1: 大盘方向预测
- 文件: `quant_app/models/market_direction_v1.py`
- 模型: LightGBM 5 档分类
- 特征: 6 维市场指标 + 动量 (8 维)
- 训练: 时序 5-fold CV, 全量重训
- 指标: CV 准确率 ≥ 55%

### 3.2 Stage 2: 热点板块预测
- 文件: `quant_app/models/sector_rotation_v1.py`
- 模型: LightGBM Ranker (训练) / 加权评分 (推理)
- 特征: 8 维板块指标 (连续流入, 资金趋势, 量能趋势, 涨幅序列, 突破)
- 输出: Top 5~8 板块

### 3.3 Stage 3: 主升浪个股
- 文件: `quant_app/models/main_wave_detector_v1.py`
- 模型: LightGBM 二分类 (class_weight=balanced)
- 标签: 满足"放量 + 横盘 + 突破 + 3 日 5%+"样本为 1, 否则 0
- 特征 (~170 维):
  - V11 基础 117 维 (来自 `ml_predict._build_features_for_stocks_v8_0`)
  - 龙虎榜 12 维 (`lhb_features.py`)
  - 北向 8 维 (`hsgt_features.py`)
  - 形态 19 维 (`pattern_features.py`)
  - 研报 6 维 (`research_features.py`)
  - 板块接力 9 维 (`sector_relay_features.py`)
- 评估: CV AUC ≥ 0.7

## 4. 5 个新特征模块

| 模块 | 文件 | 强信号 | 数据源 |
|---|---|---|---|
| 龙虎榜 | `features/lhb_features.py` | 游资席位净买入, 机构席位净买入, 5 日回补 | tushare top_list, top_inst |
| 北向资金 | `features/hsgt_features.py` | 北向 Top10 活跃, 板块北向情绪 | hsgt_top10, moneyflow_hsgt |
| 形态 | `features/pattern_features.py` | 横盘+突破+放量复合 | daily_price 计算 |
| 研报 | `features/research_features.py` | 首次覆盖, 上调评级, 板块热度 | research_report |
| 板块接力 | `features/sector_relay_features.py` | 行业内涨停序, 板块 5 日动量 | limit_list_d, sector_moneyflow |

## 5. 主升浪精确定义 (用于打标签)

触发日 d = 同时满足:
1. 当日涨幅 ∈ [3%, 9.5%]
2. 成交量 >= 5 日均量 × 1.8
3. 前 5~20 日横盘: 振幅均值 ≤ 8%
4. 当日收盘价 >= 20 日新高
5. 未来 3 日累计收益 ≥ 5%
6. 未来 3 日最大回撤 ≥ -8%

文件: `scripts/build_main_wave_labels.py`

## 6. 策略级回测

文件: `quant_app/backtest/strategy_engine.py`

关键设计:
- T+1 成交: 信号日 T 收盘, 实际 T+1 开盘
- 滑点: 买入 +0.15%, 卖出 -0.15%
- 手续费: 万 2.5, 卖出加印花千 1
- 涨跌停成交概率: 一字板买不进/卖不出
- 仓位: 高分 20% / 中分 10% / 低分不进
- 止损止盈: -3.5% 硬止损, +5% 卖 1/3, +10% 再卖 1/3, 移动止损
- 时间止损: 持有 3 日未启动减半

## 7. A/B 对比设计

| 策略 | 说明 |
|---|---|
| A: V11 baseline | 直接用 V11.0 模型 Top 5 排序 (现有) |
| B: 新三段模型 | Stage 1→2→3 漏斗后的 Top 5 |

判断标准 (任一不达标则回滚):
- 年化收益 ≥ 25%
- 最大回撤 ≤ 15%
- 夏普比率 ≥ 1.2
- 盈亏比 ≥ 2.0

## 8. 部署与排期

| Phase | 内容 | 工期 | 状态 |
|---|---|---|---|
| 0-A | Backfill 4 新表 + 主升浪标签 | 1-2 天 | 进行中 |
| 0-B | 策略级回测框架 | 0.5 天 | 完成 |
| 0-C | 5 个新特征模块 | 0.5 天 | 完成 |
| 0-D | 设计文档 | 0.5 天 | 完成 |
| 1-A | Stage 1 模型训练 | 1 天 | 骨架完成 |
| 1-B | Stage 2 模型训练 | 1 天 | 骨架完成 |
| 1-C | Stage 3 模型训练 | 2 天 | 骨架完成 |
| 2-A | 三段串联 + OOS 回测 | 2 天 | 骨架完成 |
| 2-B | A/B 对比 + 报告 | 1 天 | 待跑 |
| 3-A | 模拟盘 2 周 | 2 周 | 待 |
| 3-B | 接入实盘 | 1 天 | 待 |

## 9. 风险与限制

1. 主升浪标签事后诸葛亮: 必须 OOS 严格留出最近 6 个月
2. 1-2 日预测信噪比极低, 模型只能用于"筛候选", 不能当"必胜信号"
3. 概念板块数据 (`board_concept_hist`) 只有 2026-04-30 起, 5 年回测不完整
4. 5000 积分 tushare 不支持个股级北向资金精确数据, 改用 Top10 代理
5. V11 基础模型本身 RankIC 偏低 (0.024), Stage 3 受此约束

---

## 10. 阶段成果报告 (2026-06-10)

### 已交付
- ✅ 6 张新表 + Backfill 完成 (top_list 26322 / top_inst 301141 / limit_list_d 40661 / research 37830 行)
- ✅ 主升浪标签 275588 行, 438 正样本
- ✅ Stage 3 模型: CV AUC **0.83 ± 0.23**
  - Top 特征: `pat_is_breakout_20d` (193 重要性), `pat_range_ma10`, `vol_20d`
- ✅ 三段 Pipeline 端到端跑通
- ✅ 策略级回测框架 + 修复 2 个 bug
- ✅ 简化版 3 日策略: **+34.4% 年化, 盈亏比 2.36**

### 关键发现
1. **模型有真实 alpha**: mw_prob >= 0.3 的子集, T+3 平均 +3.36%
2. **策略引擎有 bug** 导致原始回测 -88%: time_stop 静默关闭 / end_of_period 误用 cfg.end
3. **Topdown 优于 V11 baseline** (虽然两者都需打磨)

### 已知缺陷
- Stage 1 大盘方向: 19% 准确率, 需重写 (3 档 + 案例检索)
- Stage 2 板块轮动: 用 fallback 评分, 无训练模型
- V11 baseline predict_batch 返回 dict 而非 DataFrame, 旧的 compare 脚本有 bug
- 策略引擎的 stop_loss / take_profit 路径在压力测试中触发频率异常

### 下一步
1. 用全特征重训 Stage 1 + Stage 2
2. 修策略引擎所有 bug 后重跑对比
3. 把简化 3 日策略固化到 daily_scan_report
4. 模拟盘 2 周观察
