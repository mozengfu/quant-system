"""特征构建模块。

各版本 ML 模型的特征构建统一入口。

用法:
    from quant_app.features import build_features_for

    # V11.0 特征（当前主模型）
    feat = build_features_for(conn, ts_codes, as_of_date, version="v11.0")

    # V8.0 fallback
    feat = build_features_for(conn, ts_codes, as_of_date, version="v8.0")
"""
from quant_app.features.v11_features import (
    V11_FEATURE_COLS,
    align_features_to_model,
    build_features_v11_inference,
)

__all__ = [
    "build_features_for",
    "build_features_v11_inference",
    "align_features_to_model",
    "V11_FEATURE_COLS",
]


def build_features_for(conn, ts_codes, as_of_date=None, version="v11.0"):
    """统一的特征构建入口。

    Args:
        conn: pymysql 连接
        ts_codes: 股票代码列表
        as_of_date: 基准日期（默认当前日期）
        version: 模型版本 ("v11.0", "v8.0", "v8.1", ...)

    Returns:
        DataFrame (index=ts_code, columns=特征名)
    """
    version = version.lower().replace("-", ".")

    if version in ("v11.0",):
        feat = build_features_v11_inference(conn, ts_codes, as_of_date)
        if feat is not None and not feat.empty:
            return align_features_to_model(feat, version)
        return feat

    # 其他版本：回退到 ml_predict.py 中的构建函数
    # 这些版本尚未迁移到独立模块，保持原有 import 链
    from ml_predict import (
        _build_features_for_stocks_v6_3,
        _build_features_for_stocks_v8_0,
        _build_features_for_stocks_v8_1,
        _build_features_for_stocks_v8_6,
        _build_features_for_stocks_v9_0,
        _build_features_for_stocks_v10_0,
    )

    build_fn_map = {
        "v8.0": _build_features_for_stocks_v8_0,
        "v8.1": _build_features_for_stocks_v8_1,
        "v8.6": _build_features_for_stocks_v8_6,
        "v9.0": _build_features_for_stocks_v9_0,
        "v10.0": _build_features_for_stocks_v10_0,
        "v6.3": _build_features_for_stocks_v6_3,
    }

    fn = build_fn_map.get(version)
    if fn is None:
        raise ValueError(f"未知模型版本: {version}，可用: {list(build_fn_map)}")
    return fn(conn, ts_codes, as_of_date=as_of_date)
