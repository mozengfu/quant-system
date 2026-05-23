"""ML 模型加载器 - 统一管理模型加载和缓存"""
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent
MODELS_DIR = BASE_DIR / "data"

# 注册所有已知模型路径
_MODEL_REGISTRY = {
    "v6": MODELS_DIR / "ml_stock_model_v6.pkl",
    "v6.2": MODELS_DIR / "ml_stock_model_v6_2.pkl",
    "v6.3": MODELS_DIR / "ml_stock_model_v6_3.pkl",
    "v6.4": MODELS_DIR / "ml_stock_model_v6_4.pkl",
    "v6.5": MODELS_DIR / "ml_stock_model_v6_5.pkl",
    "v6.6": MODELS_DIR / "ml_stock_model_v6_6.pkl",
    "v6.7": MODELS_DIR / "ml_stock_model_v6_7.pkl",
    "v8.0": MODELS_DIR / "ml_stock_model_v8_0.pkl",
    "v8.1": MODELS_DIR / "ml_stock_model_v8_1.pkl",
    "v8.2": MODELS_DIR / "ml_stock_model_v8_2.pkl",
    "v8.3": MODELS_DIR / "ml_stock_model_v8_3.pkl",
    "v8.4": MODELS_DIR / "ml_stock_model_v8_4.pkl",
    "v8.6": MODELS_DIR / "ml_stock_model_v8_6.pkl",
    "v9.0": MODELS_DIR / "ml_stock_model_v9_0.pkl",
    "v10.0": MODELS_DIR / "ml_stock_model_v10_0.pkl",
    "v11.0": MODELS_DIR / "ml_stock_model_v11_0.pkl",
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
        logger.info(f"模型已加载: {path.name} (version={bundle.get('version', 'N/A')})")
        return bundle
    except Exception as e:
        logger.error(f"模型加载失败 ({path.name}): {e}")
        return None
