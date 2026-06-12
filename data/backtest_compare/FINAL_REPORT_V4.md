# 主升浪三段式预测模型 — 最终报告 V4

**日期**: 2026-06-10
**版本**: V4 (Phase 1+2 完成)

## 1. 全部 5 个策略对比 (6 月 OOS 2025-12-18 ~ 2026-06-09)

| 策略 | 交易 | WR | P/L | 年化 | 最大回撤 |
|---|---|---|---|---|---|
| V11 板块内 (hold=2, sl=-5%) | 30 | 26.7% | **2.37** | **+0.63%** | ? |
| V11 + 严格 PM | 18 | 44.4% | 0.94 | -2.10% | -1.5% |
| V11 + 宽松 PM | 15 | 60.0% | 0.80 | +0.37% | -0.6% |
| V11 + 仅 DD 闸门 | 16 | 25.0% | 1.58 | -10.41% | -5.4% |
| **Factor Ranker 周线 (新)** | 46 | 41.3% | 1.34 | -11.81% | ? |

**最佳**: V11 板块内 (无 PM), +0.63% 年化, P/L 2.37

## 2. Phase 1 仓位管理结论

| 模式 | 年化 | P/L | 评价 |
|---|---|---|---|
| 无 PM | +0.63% | 2.37 | **最佳**, 默认 |
| 严格 PM (vol+trailing) | -2.10% | 0.94 | vol-targeting 牺牲收益 |
| 宽松 PM (低约束) | +0.37% | 0.80 | 略好于严格但仍不如无 PM |
| 仅 DD 闸门 | -10.41% | 1.58 | DD 控制过度, 6 月触发阻断 |

**结论**: 模型信号弱, PM 摩擦拖累收益. 生产默认用 **No PM**, 仅在 --pm 开关启用.

## 3. Phase 2 因子选股

- **数据集**: 846K 样本 × 21 因子 × 5.3K 股票 × 189 采样日 (2.5 年)
- **模型**: LightGBM Ranker (lambdarank), 5 档分类, cross-section 排序
- **OOS 评估** (2025-12-18 ~ 2026-06-09):
  - NDCG@5: **0.615** (vs random 0.45-0.50)
  - IC: **0.029 ± 0.286** (Spearman)
  - IR: **0.73** (年化近似)
- **Top 因子**:
  1. high_52w_pct (2982 重要性) - 距 52 周新高
  2. amplitude_10d (1737) - 10 日振幅
  3. ma5_ma20_diff (1700) - 均线偏离
  4. vol_price_corr_10d (1575) - 量价相关
  5. ret_1d (1410) - 1 日动量

- **周线回测** (5 日持仓, 行业 top 3, 10 并发):
  - 46 笔, 41.3% WR, P/L 1.34
  - -5.78% 总, -11.81% 年化
  - 按月: 2026-04 +7,855, 2026-06 -6,896

**结论**: 模型有正 EV (P/L > 1) 但 WR 41% 不够, 跨期不稳. 2026-06 大亏拖累. 需更多特征 (北向资金, 融资融券) + 更长 OOS 验证.

## 4. 关键发现

1. **5 档分类 vs 1-2 日预测本质是赌博**: Stage 3 的 124 样本训练不出任何东西
2. **Ranker 模型有信号 (NDCG 0.62) 但预测力弱 (IC 0.03)**: A 股横截面动量就是弱
3. **PM 在弱信号策略上拖后腿**: 模型都没 alpha, 任何执行摩擦都是负贡献
4. **2026-06 月是死亡之月**: 几乎所有策略都亏, 可能市场风格切换
5. **V11 板块内 (production-grade 150MB 模型) 仍是最强 baseline**: 6 个月 +0.63% 年化, P/L 2.37

## 5. 交付物

### 代码
- `quant_app/pipeline/v11_sector_predictor.py` - **生产预测器**
- `quant_app/risk/position_manager.py` - 仓位管理 (可选)
- `scripts/v11_sector_scan.py` - **生产策略脚本** (--pm 开关)
- `scripts/build_factor_dataset.py` - 因子计算
- `scripts/train_factor_ranker.py` - Ranker 训练
- `data/models/factor_ranker_v1.pkl` (1.5MB) - LightGBM Ranker 模型

### 数据
- `data/factor_dataset/factor_2024-01-01_2026-06-09.parquet` (846K × 24 维)

### 回测
- `data/backtest_compare/three_way_oos_fast.json` - V11 vs 集成 vs Stage3
- `data/backtest_compare/v11_6month_oos.json` - V11 6 月
- `data/backtest_compare/factor_ranker_weekly.json` - **新** Ranker 周线

## 6. 现实期望 (基于 6 月 OOS)

| 策略 | 现实年化 | 现实 Sharpe | 现实最大回撤 |
|---|---|---|---|
| V11 板块内 | +0.6% | ~0.2 | 未知 |
| Factor Ranker | -12% (波动) | -0.5 | 未知 |
| 当前生产 sim | +50% (幸存者偏差) | 未知 | 30% |

**最终建议**: 短期内用 V11 板块内 (No PM), 接受 +0.6% 年化 + P/L 2.37 的"小而稳" alpha. 长期用 Phase 2 因子选股扩展 (需要 6-12 个月持续开发).

## 7. 下一步

### 短期 (本月)
1. 把 V11 板块内固化到 cron (每日 17:30 跑 v11_sector_scan.py)
2. 监控 sim_account 实际 P&L vs 预期
3. 加 northbound / margin_balance 因子 (需建 stock_hsgt 表)

### 中期 (3-6 月)
1. 因子选股模型用更长时间序列重训 (2020-2026)
2. 加 50+ 因子 (财务, 资金流, 研报)
3. 用更大的模型 (CatBoost / XGBoost ensemble)
4. 周线回测扩大到 1-2 年

### 长期 (6-12 月)
1. 切换到多因子 + 截面 rank 作为主选股
2. 集成多周期 (周线选股 + 日线调仓)
3. 实盘对接 QMT
