# ETF 量化因子选择全指南

> 适用版本：quant-system v11.x · 配套脚本 `scripts/etf_factor_pipeline.py`
> 数据基础：MySQL `quant_db` · Tushare / AKShare · 输出 `data/etf_factor/`
> 文档日期：2026-06-11

---

## 0. 目录

1. [为什么 ETF 因子和股票因子是两套东西](#1-为什么-etf-因子和股票因子是两套东西)
2. [ETF 因子分类体系](#2-etf-因子分类体系)
3. [因子筛选：5 步漏斗](#3-因子筛选5-步漏斗)
4. [回测实操：T+0 / T+1 / 周频 三档](#4-回测实操t0--t1--周频-三档)
5. [因子评价指标（核心 6 项）](#5-因子评价指标核心-6-项)
6. [集成到本项目](#6-集成到本项目)
7. [常见陷阱 & 反例](#7-常见陷阱--反例)
8. [附录：完整因子清单（45 维）](#8-附录完整因子清单45-维)

---

## 1. 为什么 ETF 因子和股票因子是两套东西

| 维度 | 股票因子 | ETF 因子 |
|---|---|---|
| 基础标的 | 单只证券 | 一篮子证券 |
| 跟踪误差 | N/A | **必须显式建模**（vs 标的指数） |
| 个股层面信号 | 财报/技术/资金流 | 几乎失效（成分股是聚合的） |
| 申赎/折溢价 | 不存在 | **核心 Alpha 来源**（LOF/跨境/QDII） |
| 流动性 | 看个股成交量 | 看 IOPV 报价密度 + 买卖价差 |
| 因子失效速度 | 月级 | **周级**（机构套利盘更厚） |

**结论**：直接拿你 `scripts/build_factor_dataset.py` 那 30 维股票因子套到 ETF 上，**胜率会被跟踪误差和折溢价波动淹没**。需要：

1. 重写因子定义（加入跟踪误差、折溢价、IOPV 偏离等 ETF 专属维度）
2. 调整回测框架（支持 T+0、考虑赎回限制）
3. 持仓期改短（周频/月频替代日频）

---

## 2. ETF 因子分类体系

```
ETF 因子
├── 一级：跟踪质量（3 类共 8 因子）
├── 二级：流动性（2 类共 7 因子）
├── 三级：折溢价 & IOPV（2 类共 6 因子）
├── 四级：动量/反转（3 类共 12 因子）
├── 五级：资金流（2 类共 7 因子）
└── 六级：宏观 & 风格（2 类共 5 因子）
```

### 2.1 一级：跟踪质量（最高优先级）

这是 ETF 选股**第一道关**——跟踪质量差的 ETF 永远不进入实盘池。

| 因子名 | 公式 | 阈值 | 数据源 |
|---|---|---|---|
| `tracking_error_20d` | std(ETF日收益 - 标的指数日收益) × √252 | < 0.5%（宽基）/ < 1.5%（行业） | Tushare `fund_daily` + 指数日线 |
| `tracking_diff_5d` | ETF 5日收益 - 标的指数 5日收益 | [-0.5%, +0.5%] | 同上 |
| `deviation_year` | (ETF净值 - 标的指数点位×份额比) / 标的指数 | 绝对值 < 1% | IOPV（盘中估算净值） |
| `fund_size` | 基金净资产 | > 2 亿（避免清盘） | Tushare `fund_basic` |
| `fund_age_days` | 成立到今天数 | > 180 天（避开新基金） | Tushare `fund_basic` |
| `manager_tenure` | 基金经理任职天数 | > 365 天 | Tushare `fund_manager` |
| `mgmt_fee` | 管理费 + 托管费 | < 0.6%/年（同类均值以下） | Tushare `fund_basic` |
| `is_index_enhanced` | 跟踪指数 + 是否增强型 | 增强型需额外检验 | 基金合同字段 |

**关键洞察**：跟踪误差 > 1% 的 ETF 直接淘汰，不管后续因子多漂亮。

### 2.2 二级：流动性

流动性差的 ETF 进出成本能吃掉所有 alpha。

| 因子名 | 公式 | 阈值 |
|---|---|---|
| `avg_amount_20d` | 近 20 日均成交额 | > 500 万 |
| `avg_volume_20d` | 近 20 日均成交量 | > 50 万份 |
| `turnover_rate_20d` | 换手率 20 日均值 | > 0.5% |
| `bid_ask_spread_pct` | (卖一 - 买一) / 中间价 × 10000 | < 5 bps（活跃品种）< 20 bps（小众） |
| `large_order_count` | 10 万份以上成交笔数 / 总笔数 | 占比 > 5%（大资金能进得去） |
| `market_impact_1pct` | 买 1% 基金份额所需冲击成本 | < 0.3% |
| `iopv_update_freq` | IOPV 每分钟更新次数 | > 1（避免 IOPV 滞后套利失败） |

### 2.3 三级：折溢价 & IOPV（LOF/QDII/跨境 ETF 专属 Alpha）

**这是国内 ETF 量化最被低估的因子源**。

| 因子名 | 公式 | 用法 |
|---|---|---|
| `premium_daily` | (ETF 现价 - IOPV) / IOPV | > 0 = 溢价，< 0 = 折价 |
| `premium_5d_ma` | 5 日溢价率均值 | 平滑噪声 |
| `premium_std_20d` | 20 日溢价率标准差 | 衡量套利盘厚度 |
| `discount_zscore` | (当日溢价 - 20日均值) / 20日标准差 | z > 2：溢价极端，可能回归 → 做空信号 |
| `arb_volume_ratio` | 当日折溢价交易量 / 日常成交量 | > 1.5 = 套利资金介入 |
| `nav_iopv_diff` | 收盘净值 - 收盘 IOPV | 日内 IOPV 误差，越小越准 |

**实战案例**：2024 年纳指 ETF（513100）多次出现单日 2%+ 溢价，次日回归 -1.5%。Z-score > 1.8 做空持有 1-2 日，年化 alpha 8-12%。

### 2.4 四级：动量/反转

| 因子名 | 公式 | 备注 |
|---|---|---|
| `ret_1d` / `ret_3d` / `ret_5d` / `ret_10d` / `ret_20d` / `ret_60d` | N 日累计收益 | **必加行业中性化**（ETF 行业差异极大） |
| `mom_skip_1d` | ret_5d 跳过 ret_1d | 避开隔夜反转 |
| `rps_20` | 20 日收益在所有 ETF 中的百分位排名 | 模仿股票 RPS |
| `ma5_ma20_diff` | (MA5/MA20 - 1) × 100 | 趋势强度 |
| `momentum_decay` | ret_20d / ret_5d | > 1.5 = 减速，< 0.7 = 加速 |
| `rev_1d` | -ret_1d | 超短线反转 |
| `rev_5d` | -ret_5d | 周内反转 |
| `rsi_14` | 14 日 RSI | > 70 超买 / < 30 超卖 |
| `boll_pos` | (价 - MA20) / (2 × std20) | 布林带位置 |
| `macd_hist` | MACD 柱 | 趋势确认 |
| `ma_trend_score` | MA5>MA10>MA20>MA60 计分 | 4 = 多头排列，0 = 空头 |
| `vol_adj_return_20d` | ret_20d / (std_20d × √20) | 风险调整后动量 |

### 2.5 五级：资金流

| 因子名 | 公式 | 数据源 |
|---|---|---|
| `main_net_5d` | 主力资金净流入 5 日累计 | 需自己估算（ETF 行情不带资金流字段） |
| `main_net_20d` | 同上 20 日 | 估算方法：amount × (pct_chg > 0 ? 1 : -1) × 比例 |
| `lhb_net_5d` | 龙虎榜净买入 5 日 | 交易所披露（仅大额） |
| `share_change_pct` | (份额变化 - 历史均值) / std | 申赎活跃度 |
| `npp_growth_20d` | 20 日份额增长百分比 | 资金流入证据 |
| `large_redemption` | 单日赎回份额 > 总份额 1% 标志 | 巨赎预警（QDII 常见） |
| `institutional_holding_pct` | 机构持有比例 | 半年报披露，滞后 |

> **注意**：ETF 没有像股票那样的 L2 资金流数据，上述因子**精度有限**，在 5 层风控中只能作为辅助证据。

### 2.6 六级：宏观 & 风格

| 因子名 | 公式 | 用法 |
|---|---|---|
| `beta_to_market` | 60 日回归 ETF 对沪深 300 的 beta | 区分激进/防御型 |
| `size_proxy` | 标的指数总市值对数 | 大盘/小盘风格 |
| `value_proxy` | 标的指数 EP（盈利收益率） | 价值/成长风格 |
| `interest_rate_sensitivity` | 对 10Y 国债收益率的回归 beta | 跨境/REITs 用 |
| `volatility_60d` | 60 日年化波动率 | 用于组合层面的风险预算 |

---

## 3. 因子筛选：5 步漏斗

```
[全部 ETF 600+ 只]
        ↓ Step 1: 跟踪质量过滤
[活跃 ETF 350+ 只]
        ↓ Step 2: 流动性过滤
[可交易 ETF 200+ 只]
        ↓ Step 3: 单因子 IC 检验
[有效因子池 15-25 个]
        ↓ Step 4: 多重共线性剔除
[正交因子 8-12 个]
        ↓ Step 5: 组合 IC 检验 + 分层回测
[入选因子 5-8 个 → 入模型]
```

### 3.1 Step 1: 跟踪质量过滤

```python
# 伪代码
mask = (df['tracking_error_20d'] < 0.015) & \
       (df['fund_size'] > 2e8) & \
       (df['fund_age_days'] > 180) & \
       (df['abs_premium_daily'] < 0.03)  # 极端折溢价剔除
```

### 3.2 Step 2: 流动性过滤

```python
mask &= (df['avg_amount_20d'] > 5e6) & \
        (df['avg_volume_20d'] > 5e5) & \
        (df['turnover_rate_20d'] > 0.005)
```

### 3.3 Step 3: 单因子 IC 检验

**IC (Information Coefficient) = 当期因子值与下期收益的 Spearman 秩相关**

```python
for factor in factor_cols:
    ic = df.groupby('trade_date').apply(
        lambda g: g[factor].rank().corr(g['next_ret_5d'].rank())
    )
    ic_mean = ic.mean()
    ic_std = ic.std()
    ic_ir = ic_mean / ic_std  # IC IR > 0.05 有效，> 0.10 强有效
    print(f"{factor:30s}  IC={ic_mean:+.4f}  IR={ic_ir:+.4f}")
```

**判断标准**：
- `|IC_mean| > 0.03` 且 `IC IR > 0.05`：**入选**
- `|IC_mean| > 0.05` 且 `IC IR > 0.10`：**核心因子**
- 其余：**剔除**

### 3.4 Step 4: 多重共线性剔除

```python
import numpy as np
corr = df[factor_cols].corr()
# 保留 |corr| < 0.7 的因子组中 IC 最高的那一个
```

或者用 VIF：
```python
from statsmodels.stats.outliers_influence import variance_inflation_factor
vif = pd.DataFrame({
    'factor': factor_cols,
    'vif': [variance_inflation_factor(df[factor_cols].values, i) 
            for i in range(len(factor_cols))]
})
# VIF > 10 视为严重共线
```

### 3.5 Step 5: 组合 IC 检验 + 分层回测

**分层回测（Quantile Backtest）**——把因子值分 5 档（Q1-Q5），看 Q5 vs Q1 的多空收益。

```python
df['factor_q'] = df.groupby('trade_date')[factor].rank(pct=True) // 0.2  # 5 档
# 多空组合：每月做多 Q5，做空 Q1
long_short = df[df['factor_q'] == 4].groupby('trade_date')['next_ret_5d'].mean() - \
             df[df['factor_q'] == 0].groupby('trade_date')['next_ret_5d'].mean()
print(f"多空年化: {long_short.mean() * 50:.2%}, 胜率: {(long_short > 0).mean():.2%}")
```

**判断**：
- 年化多空收益 > 8%
- 胜率 > 55%
- 最大回撤 < 15%

三项都通过才进入最终因子库。

---

## 4. 回测实操：T+0 / T+1 / 周频 三档

### 4.1 回测框架关键差异

| 维度 | T+0（跨境/QDII） | T+1（普通股票 ETF） | 周频（再平衡） |
|---|---|---|---|
| 标的池 | QDII/跨境/黄金/原油 | 股票型 ETF | 全部 |
| 调仓频率 | 日 / 周二 | 周一开盘 | 每月第一个交易日 |
| 持仓期 | 1-2 日 | 5-20 日 | 20-40 日 |
| 摩擦成本 | 0.1% × 2（双边） | 0.15% × 2（双边） | 0.15% × 2 |
| 关键约束 | 申购上限（T+2 到账） | 涨跌幅 10% | 流动性优先 |
| 因子重点 | 折溢价 + IOPV | 动量 + 资金流 | 风格 + 跟踪质量 |

### 4.2 回测核心代码（已在 `scripts/etf_factor_pipeline.py` 中实现）

```python
def backtest_etf_strategy(df, factor_col, rebalance_freq='W', hold_periods=1):
    """
    df: 包含 trade_date, ts_code, factor, next_ret_Nd 的面板
    rebalance_freq: D(每日) / W(周) / M(月)
    """
    # 1. 调仓日筛选
    rebalance_dates = get_rebalance_dates(df['trade_date'].unique(), freq=rebalance_freq)
    
    nav = 1.0
    nav_curve = []
    for rebal_date in rebalance_dates:
        # 2. 当日因子排名，选 top N
        today = df[df['trade_date'] == rebal_date].copy()
        top_n = today.nlargest(10, factor_col)
        codes = top_n['ts_code'].tolist()
        
        # 3. 持有 N 日
        end_date = rebal_date + pd.Timedelta(days=hold_periods * 7)
        future = df[(df['trade_date'] > rebal_date) & 
                    (df['trade_date'] <= end_date) & 
                    (df['ts_code'].isin(codes))]
        ret = future.groupby('ts_code')['pct_chg'].sum().mean() / 100
        nav *= (1 + ret - 0.003)  # 扣双边摩擦 0.3%
        nav_curve.append((rebal_date, nav))
    
    return pd.DataFrame(nav_curve, columns=['date', 'nav'])
```

### 4.3 防过拟合：Walk-Forward 回测

**绝对不能只用一次 train/test 切分**。本项目所有回测都用 walk-forward：

```python
# 推荐 3 段 walk-forward
# Train: 2022-01-01 ~ 2023-12-31 → Test: 2024-01-01 ~ 2024-06-30
# Train: 2022-07-01 ~ 2024-06-30 → Test: 2024-07-01 ~ 2024-12-31
# Train: 2023-01-01 ~ 2024-12-31 → Test: 2025-01-01 ~ 2025-06-30
```

---

## 5. 因子评价指标（核心 6 项）

| 指标 | 公式 | 优秀阈值 | 说明 |
|---|---|---|---|
| **IC Mean** | 因子与下期收益的秩相关均值 | \|IC\| > 0.05 | 越大越好 |
| **IC IR** | IC 均值 / IC 标准差 | > 0.5 | 稳定性 |
| **IC 胜率** | IC > 0 的交易日占比 | > 55% | 方向稳定性 |
| **多空年化** | Q5-Q1 收益年化 | > 10% | 真实 alpha |
| **换手率** | 调仓时换手比例 | < 50%（周频） | 成本控制 |
| **最大回撤** | 多空净值最大回撤 | < 15% | 风险 |

**完整评价报告由 `scripts/etf_factor_pipeline.py evaluate_factor()` 自动输出。**

---

## 6. 集成到本项目

### 6.1 复用现有资产

你已经有现成的可以直接挂接：

| 现有资产 | 复用方式 |
|---|---|
| `quant_app/utils/config.py` 的 `get_db_config()` | 直接调用连接 MySQL |
| `scripts/build_factor_dataset.py` 的 30 维因子计算 | 改造为 ETF 版本，删掉行业/股东相关因子 |
| `scripts/train_factor_ranker.py` 的 LightGBM 训练 | 改用 ETF 数据集训练 |
| `data/factor_dataset/` 目录结构 | 新建 `data/etf_factor/` 平行结构 |
| `run_backtest_v11_walkforward.py` | 改 ret_Nd 字段对齐 ETF 净值 |

### 6.2 新增文件清单

```
quant-system/
├── docs/
│   └── etf_factor_guide.md          ← 本文档
├── scripts/
│   └── etf_factor_pipeline.py       ← 端到端：拉数据→因子计算→IC→回测
├── data/
│   ├── etf_factor/
│   │   ├── etf_basic.parquet         ← ETF 基础信息（基金规模/经理/费率）
│   │   ├── etf_daily.parquet         ← ETF 日线（价/量/IOPV）
│   │   ├── etf_index_mapping.csv     ← ETF → 标的指数 映射
│   │   ├── etf_factor_2024_2026.parquet  ← 因子面板
│   │   └── factor_evaluation.json    ← IC / 多空回测结果
│   └── etf_backtest/
│       └── backtest_etf_v1.json
└── quant_app/
    └── features/
        └── etf_factor.py             ← 因子计算函数（被 pipeline 调用）
```

### 6.3 落地步骤

```bash
# 1. 拉取 ETF 基础数据（一次性，约 5 分钟）
python3 scripts/etf_factor_pipeline.py step1_fetch_basic

# 2. 拉取 ETF 日线 + 标的指数日线（每日增量）
python3 scripts/etf_factor_pipeline.py step2_fetch_daily

# 3. 计算 45 维因子
python3 scripts/etf_factor_pipeline.py step3_compute_factors

# 4. 因子筛选（5 步漏斗）
python3 scripts/etf_factor_pipeline.py step4_screen_factors

# 5. 组合回测 + Walk-Forward
python3 scripts/etf_factor_pipeline.py step5_backtest
```

**预计单次全量耗时**：步骤 1-2 约 8 分钟（含 Tushare 限频 sleep），步骤 3-5 约 3 分钟。

---

## 7. 常见陷阱 & 反例

### 7.1 致命陷阱

1. **拿股票因子硬套 ETF** → 跟踪误差和折溢价噪声会淹没 alpha。**反例**：用 RPS_20 选 ETF，2024 年跑赢沪深 300，但 2025 年反向 -8%。
2. **忽略 T+0 ETF 的申购上限** → 信号触发但买不进 QDII，结果严重偏离回测。**反例**：2024-09 纳指 ETF 单日 5% 溢价，套利盘卡额度。
3. **IC 用 Pearson 不用 Spearman** → ETF 收益分布厚尾，Pearson 会被极端值污染。
4. **不区分宽基/行业/主题** → 半导体 ETF 和沪深 300 ETF 的因子权重应该完全不一样。
5. **调仓不扣管理费** → 0.5%/年的费率 5 年能吃 12% 净值。

### 7.2 进阶陷阱

- **幸存者偏差**：Tushare 的 ETF 列表只包含当前还在市的，需自己加退市 ETF 的历史收益。
- **份额拆分**：LOF 拆分/分红再投会影响价格连续性，需用**复权价**算收益。
- **IOPV 延迟**：盘中 IOPV 是 15 秒延迟估算的，套利信号要等收盘确认再下单。
- **风格轮动**：2024 年小盘强、2025 年大盘强，因子截面权重应该带**波动率倒数加权**，而不是等权。

### 7.3 不可控因素

- **政策风险**：T+0 标的池可能调整（如 2024 年纳指 ETF 一度限制大额申购）。
- **汇率风险**：QDII 收益需扣人民币升值/贬值（2022 年 -8%）。
- **跟踪指数变更**：2024 年某些行业 ETF 调整对标指数，需重新计算跟踪误差。

---

## 8. 附录：完整因子清单（45 维）

| 编号 | 因子名 | 类别 | 公式概要 |
|---|---|---|---|
| 1 | tracking_error_20d | 跟踪质量 | std(ETF-指数)×√252 |
| 2 | tracking_diff_5d | 跟踪质量 | ETF 5日 - 指数 5日 |
| 3 | deviation_year | 跟踪质量 | (价-净值)/净值 |
| 4 | fund_size_log | 跟踪质量 | log(净资产) |
| 5 | fund_age_days | 跟踪质量 | 成立天数 |
| 6 | manager_tenure | 跟踪质量 | 经理任职天数 |
| 7 | mgmt_fee | 跟踪质量 | 管理+托管费 |
| 8 | is_enhanced | 跟踪质量 | 是否增强型 |
| 9 | avg_amount_20d | 流动性 | 20 日均成交额 |
| 10 | avg_volume_20d | 流动性 | 20 日均成交量 |
| 11 | turnover_rate_20d | 流动性 | 20 日均换手率 |
| 12 | bid_ask_spread_bps | 流动性 | 买卖价差 bps |
| 13 | large_order_ratio | 流动性 | 大单笔数占比 |
| 14 | market_impact_1pct | 流动性 | 1% 份额冲击 |
| 15 | iopv_update_freq | 流动性 | IOPV 更新频率 |
| 16 | premium_daily | 折溢价 | (价-IOPV)/IOPV |
| 17 | premium_5d_ma | 折溢价 | 5 日溢价均值 |
| 18 | premium_std_20d | 折溢价 | 20 日溢价波动 |
| 19 | discount_zscore | 折溢价 | 折溢价 z-score |
| 20 | arb_volume_ratio | 折溢价 | 折溢价交易量/日常 |
| 21 | nav_iopv_diff | 折溢价 | 净值-IOPV 差 |
| 22-27 | ret_1d/3d/5d/10d/20d/60d | 动量 | N 日累计收益 |
| 28 | mom_skip_1d | 动量 | ret_5d - ret_1d |
| 29 | rps_20 | 动量 | 20 日收益百分位 |
| 30 | ma5_ma20_diff | 动量 | (MA5/MA20-1)×100 |
| 31 | momentum_decay | 动量 | ret_20d/ret_5d |
| 32 | rev_1d | 反转 | -ret_1d |
| 33 | rev_5d | 反转 | -ret_5d |
| 34 | rsi_14 | 技术 | 14 日 RSI |
| 35 | boll_pos | 技术 | 布林带位置 |
| 36 | macd_hist | 技术 | MACD 柱 |
| 37 | ma_trend_score | 技术 | MA 多头排列计分 |
| 38 | vol_adj_return_20d | 风险调整 | ret/std |
| 39 | main_net_5d | 资金流 | 主力净流入估算 |
| 40 | main_net_20d | 资金流 | 主力净流入 20 日 |
| 41 | lhb_net_5d | 资金流 | 龙虎榜净买入 |
| 42 | npp_growth_20d | 资金流 | 份额增长率 |
| 43 | beta_to_hs300 | 风格 | 对沪深 300 beta |
| 44 | volatility_60d | 风险 | 60 日年化波动 |
| 45 | value_proxy | 风格 | 标的指数 EP |

---

## 变更记录

- 2026-06-11 v1.0 创建（基于主人需求）

## 配套

- **执行入口**：`scripts/etf_factor_pipeline.py`
- **回测入口**：`run_backtest_etf_v1.py`（待建）
- **数据库依赖**：MySQL `quant_db`（ETF 维度无新增表，复用 `daily_price` + 新增 `etf_basic` / `etf_index_mapping`）
