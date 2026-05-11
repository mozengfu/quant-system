# ML选股模型 — 实现说明

## 概览

用LightGBM机器学习模型替换/增强原有的规则打分系统。

### 文件说明

| 文件 | 用途 |
|------|------|
| `ml_train.py` | 训练脚本 — 从MySQL提取特征，训练LightGBM模型 |
| `ml_predict.py` | 推理模块 — 被`app_core.py`调用，实时预测 |
| `data/ml_stock_model.pkl` | 训练好的模型文件 |
| `data/ml_feature_config.json` | 特征配置和模型元信息 |

### 模型信息

- **算法**: LightGBM (梯度提升树)
- **特征**: 22个技术指标（均线比率、MACD、RPS、波动率、量趋势等）
- **标签**: 3天后涨幅≥2% = 正样本
- **训练数据**: 137万条样本，4415只股票，2025-01 ~ 2026-04
- **性能**: AUC 0.578，准确率61.5%

### 集成方式

`app_core.py` 的 `strategy_scan()` 函数中：
1. 先用规则筛选（C3.0 V3）
2. 再用ML模型对候选股票预测上涨概率
3. 规则分 + ML分 = 增强评分
4. 按增强评分排序输出

### 特征列表

| 特征 | 含义 |
|------|------|
| pct_chg | 当日涨跌幅 |
| turnover_rate | 换手率 |
| volume_ratio | 量比 |
| vol_5d / vol_10d | 5/10日波动率 |
| ma5_ma10_ratio | MA5/MA10比率 |
| ma10_ma20_ratio | MA10/MA20比率 |
| price_ma5_ratio | 股价/MA5比率 |
| price_ma20_ratio | 股价/MA20比率 |
| chg_3d / chg_5d / chg_10d | 3/5/10日涨幅动量 |
| vol_trend | 量比趋势 |
| pos_52w | 52周位置 |
| rps_20 | RPS相对强度 |
| rps_change | RPS变化 |
| up_ratio_5d / up_ratio_10d | 近N天涨跌比例 |
| vol_pct_corr | 量价相关性 |
| ma_pattern | 均线形态 |
| macd_diff / macd_signal / macd_hist | MACD指标 |

### 重新训练

数据更新后重新训练：
```bash
cd /Users/mozengfu/workspace/quant-system
python3 ml_train.py
```

建议每两周重新训练一次，或每月一次。

### 下一步优化方向

1. **增加板块资金流向特征** — 将sector_moneyflow数据纳入特征
2. **增加大盘状态特征** — 市场趋势/震荡识别
3. **调整阈值** — 0.55阈值可通过回测优化
4. **模型集成** — 用多个模型投票（LightGBM + XGBoost + 随机森林）
5. **动态标签** — 根据市场状态调整LABEL_THRESHOLD
