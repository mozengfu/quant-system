"""V11.0 特征构建器。

包装 scripts/predict_v11.py 的 V11 特征构建逻辑，提供独立接口。
底层实现仍依赖 ml_predict.py 中的 _build_features_for_stocks_v8_0 函数，
后续计划将完整的特征构建链迁移到此模块。

用法:
    from quant_app.features.v11_features import build_features_v11_inference, align_features_to_model

    feat = build_features_v11_inference(conn, ts_codes, as_of_date)
    feat = align_features_to_model(feat, "v11.0")  # 对齐到 117 个特征列
"""
import logging

import pandas as pd
import pymysql

logger = logging.getLogger(__name__)

# V11.0 模型 117 个特征列定义
# 来源: quant_app/utils/model_loader.py 加载的 v11.0 模型 bundle['feature_cols']
V11_FEATURE_COLS: list[str] = [
    "pct_chg", "turnover_rate", "volume_ratio",
    "vol_5d", "vol_10d", "vol_20d",
    "ma5_ma10_ratio", "ma10_ma20_ratio", "price_ma5_ratio", "price_ma20_ratio",
    "chg_3d", "chg_5d", "chg_10d", "chg_20d",
    "vol_trend", "pos_52w", "rps_20", "rps_change",
    "up_ratio_5d", "up_ratio_10d", "vol_pct_corr", "ma_pattern",
    "macd_diff", "macd_signal_line", "macd_hist", "rsi_14", "adx_14",
    "main_net_ratio", "main_net_ma5", "main_net_ma10", "main_trend",
    "main_streak", "main_vs_retail", "lg_ratio",
    "main_cum5", "main_cum10", "main_accel_3d",
    "smart_div_count", "main_inflow_ratio", "main_flow_accel",
    "vol_price_corr_10d", "gap_ratio", "gap_retention", "amihud", "amihud_ma5",
    "vol_price_divergence", "vol_price_div_10d", "ma_spread_stock",
    "rzye_chg", "rzmre_ratio",
    "ind_mom_5d", "ind_mom_20d",
    "amount_ma5_ratio", "pos_10d", "pos_20d", "amplitude_10d",
    "turnover_ratio", "mom_divergence", "ret_skew_20d", "ret_kurt_20d",
    "ret_max_5d", "ret_min_5d",
    "dragon_net_buy_30d", "dragon_count_30d", "dti_net_buy_30d", "dti_count_30d",
    "holder_change_pct", "holder_num_change", "holder_consecutive_decline",
    "zt_count_30d", "zt_max_board_30d", "zt_seal_amount_30d",
    "ind_board_pct_5d", "ind_board_pct_10d", "ind_board_pct_20d",
    "concept_count", "concept_mom_avg", "concept_mom_5d", "concept_mom_10d",
    "revenue_yoy", "net_profit_yoy", "roe", "gross_margin",
    "ret_1d_reversal", "volume_div_days_10d", "turnover_std_ratio",
    "bt_count_30d", "bt_premium_avg_30d", "bt_premium_weighted_30d",
    "forecast_type_code", "forecast_is_positive", "forecast_days_since", "forecast_net_profit_max",
    "fina_roe", "fina_yoy_sales", "fina_gross_margin", "fina_net_margin", "fina_eps",
    "sector_netflow_1d", "north_flow_1d", "ml_pred_prev",
    "ret_autocorr_5d", "vol_of_vol", "max_drawdown_10d", "rel_strength_idx_5d",
    "volume_shock", "turnover_zscore_20d", "high_low_spread",
    "consecutive_wins", "consecutive_losses", "money_flow_div_5d",
    "money_flow_div_10d", "flow_accel_5d", "sector_pct_5d",
    "sector_flow_divergence",
]

# 模型中位数缓存（从 bundle 加载）
_medians_cache: dict = {}


def build_features_v11_inference(
    conn: pymysql.Connection,
    ts_codes: list[str],
    as_of_date=None,
) -> pd.DataFrame:
    """为指定股票列表构建 V11.0 的 117 个特征。

    Args:
        conn: pymysql 数据库连接
        ts_codes: 股票代码列表
        as_of_date: 基准日期，不传则为当前日期

    Returns:
        DataFrame，包含 ts_code 列和所有特征列
    """
    from scripts.predict_v11 import build_features_v11_inference as _v11_build

    return _v11_build(conn, ts_codes, as_of_date)


def load_medians_from_model(model_bundle: dict) -> dict:
    """从模型 bundle 加载全局中位数（用于填充缺失特征）。"""
    global _medians_cache
    _medians_cache = model_bundle.get("global_medians", {})
    return _medians_cache


def align_features_to_model(
    features: pd.DataFrame,
    version: str = "v11.0",
    medians: dict | None = None,
) -> pd.DataFrame:
    """将特征 DataFrame 对齐到模型期望的列。

    - 补充缺失列（用 medians 或 0 填充）
    - 移除多余列
    - 保持模型定义的列顺序

    Args:
        features: 特征 DataFrame
        version: 模型版本
        medians: 全局中位数字典（可选，优先使用 medians）

    Returns:
        对齐后的 DataFrame
    """
    from quant_app.utils.model_loader import load_model

    # 获取模型特征列
    if version == "v11.0":
        expected_cols = V11_FEATURE_COLS
    else:
        # 其他版本：从模型 bundle 获取
        bundle = load_model(version)
        if bundle is None:
            raise ValueError(f"无法加载模型 {version}")
        expected_cols = bundle.get("feature_cols", [])

    # 获取中位数
    fill_values = medians if medians else _medians_cache

    # 补充缺失列
    missing = set(expected_cols) - set(features.columns)
    if missing:
        for col in missing:
            features[col] = fill_values.get(col, 0.0)

    # 按模型顺序排列列
    available = [c for c in expected_cols if c in features.columns]
    if "ts_code" in features.columns:
        available = ["ts_code"] + [c for c in available if c != "ts_code"]

    return features[available]


def build_and_align(
    conn: pymysql.Connection,
    ts_codes: list[str],
    as_of_date=None,
    model_bundle: dict | None = None,
) -> pd.DataFrame:
    """一步完成特征构建 + 对齐。

    Args:
        conn: 数据库连接
        ts_codes: 股票代码列表
        as_of_date: 基准日期
        model_bundle: 模型 bundle（可选，用于获取 feature_cols 和 medians）

    Returns:
        对齐后的特征 DataFrame
    """
    features = build_features_v11_inference(conn, ts_codes, as_of_date)
    if features is None or features.empty:
        return features

    medians = model_bundle.get("global_medians", {}) if model_bundle else None
    return align_features_to_model(features, "v11.0", medians=medians)
