# Phase 2: 因子选股 (周线/月线持仓)

## 目标
- 用横截面排序代替"1-2 日方向预测"
- 训练数据从 438 个主升浪样本 → ~280 万 (date × stock) 样本
- 用 LightGBM Ranker (lambdarank) 预测"行业内的相对排名"
- 持仓 5-20 个交易日 (周/月级)

## 因子库设计 (~40 因子)

### 收益类 (Momentum)
1. ret_1d / ret_3d / ret_5d / ret_10d / ret_20d
2. ret_5d_industry_relative (个股 - 行业)
3. ret_20d_industry_relative
4. high_52w_pct (距 52 周新高)

### 波动 / 量价
5. vol_5d / vol_10d / vol_20d (年化波动率)
6. turnover_5d / turnover_20d
7. amount_ratio_5d_20d
8. up_down_ratio_5d (近 5 日涨跌天数)

### 技术
9. macd_hist / macd_signal
10. rsi_14
11. ma5_ma20_diff (均线偏离)
12. boll_pos (布林带位置)
13. volume_price_corr_10d (量价相关性)

### 质量 / 价值 (从 fina_indicator 表)
14. pe_ttm (市盈率)
15. pb (市净率)
16. roe (净资产收益率)
17. revenue_growth_qoq
18. profit_growth_yoy

### 资金流 (已有表)
19. main_net_inflow_5d
20. main_net_inflow_20d
21. north_net_inflow_5d (需建 stock_hsgt 表)
22. margin_balance_change_5d (融资融券)

### 行业 / 板块 (Stage 2 输出)
23. sector_mom_5d / sector_mom_20d
24. sector_pct_chg_rank (行业内涨幅排名)
25. sector_north_flow_5d

### 龙虎榜
26. lhb_net_5d / lhb_net_20d
27. lhb_appear_count_5d

### 涨停 / 形态
28. limit_up_count_20d
29. breakout_20d (横盘突破)
30. consolidation_days (近 10 日振幅均值)

## 目标变量
- **target_rank**: 未来 5 日收益在行业内的百分位排名 (0-1)
- 训练: 每日横截面, 同一行业内所有股票排序

## 模型架构
- **LightGBM Ranker** with `objective=lambdarank`
- query group = trade_date
- early stopping by NDCG@5 within industry
- Cross-validation: time-series 5-fold

## 回测设计
- 持仓: 每周调仓 (5 个交易日)
- 选股: 每个行业内 Top 5
- 仓位: 等权 5%/只
- 止损: -7% 硬止损
- 评估: 周/月/年 收益, Sharpe, 最大回撤

## 时间表
- **Phase 2A (本周)**: 数据准备 - 因子计算, 写入 Parquet
- **Phase 2B (下周)**: 模型训练 + 交叉验证
- **Phase 2C (下下周)**: 周线回测 + 集成到生产

## 预期目标
- **Sharpe 0.8-1.5** (周线动量因子的合理水平)
- **年化 15-30%** (扣除成本后)
- **最大回撤 < 15%**
- **不依赖 1-2 日方向预测**
