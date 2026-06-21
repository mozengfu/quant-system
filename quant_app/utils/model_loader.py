"""ML 模型加载器 - 统一管理模型加载和缓存"""

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent
MODELS_DIR = BASE_DIR / "data"

# 注册所有已知模型路径（仅保留文件存在的有效版本）
_MODEL_REGISTRY = {
    "v11.0": MODELS_DIR / "ml_stock_model_v11_0.pkl",
    "v11.2": MODELS_DIR / "ml_stock_model_v11_2.pkl",
    "v11.0-oos": MODELS_DIR / "ml_stock_model_v11_0_oos_v2.pkl",
    # TopDown V1 三层模型
    "market-v3": MODELS_DIR / "models" / "market_direction_v3.pkl",
    "sector-heat-v1": MODELS_DIR / "models" / "sector_heat_v1.pkl",
    "wave-catcher-v1": MODELS_DIR / "models" / "wave_catcher_v1.pkl",
}


def get_model_path(version="v6"):
    """获取模型文件路径"""
    return _MODEL_REGISTRY.get(version)


def register_model(version, path):
    """注册额外的模型路径（覆盖或新增）"""
    _MODEL_REGISTRY[version] = Path(path)


@lru_cache(maxsize=4)
def load_model(version="v6"):
    """带缓存的模型加载"""
    path = get_model_path(version)
    if path is None:
        logger.warning(f"未知模型版本: {version}")
        return None
    if not path.exists():
        logger.info(f"模型文件不存在: {path}")
        return None
    try:
        import joblib

        bundle = joblib.load(path)
        # 兼容 v11.x 的 bundle 结构（feature_cols 替代 feature_names）
        if bundle is not None and "feature_cols" in bundle and "feature_names" not in bundle:
            bundle["feature_names"] = bundle["feature_cols"]
        if bundle is not None and "rank_ic" not in bundle and "final_rank_ic" in bundle:
            bundle["rank_ic"] = bundle["final_rank_ic"]
        logger.info(f"模型已加载: {path.name} (version={bundle.get('version', 'N/A')})")
        return bundle
    except Exception as e:
        logger.error(f"模型加载失败 ({path.name}): {e}")
        return None
