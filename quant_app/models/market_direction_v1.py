"""
Stage 1: 大盘方向预测
  - 输入: 6 维市场指标 (来自 ml_regime_detector._compute_indicators)
  - 输出: 未来 1-3 日大盘方向 (5 档) + 概率 + 期望收益
  - 模型: LightGBM 多分类 + 回归头
  - 训练: 滚动 5-fold 时序交叉验证
  - 推理: 加载最新模型, 输出 direction/prob/expected_return
"""
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logger = logging.getLogger(__name__)

# 模型文件
MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "market_direction_v1.pkl"

DIRECTIONS = ['strong_down', 'down', 'range', 'up', 'strong_up']


def _load_index_data(conn, as_of_date: str, lookback: int = 60) -> pd.DataFrame:
    """加载上证指数近 N 日数据"""
    sql = """
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT %s
    """
    df = pd.read_sql(sql, conn, params=(as_of_date, lookback), parse_dates=['trade_date'])
    return df.sort_values('trade_date').reset_index(drop=True)


def _compute_features_v1(idx_df: pd.DataFrame) -> dict:
    """6 维大盘指标 + 2 维动量"""
    if len(idx_df) < 30:
        return None
    close = idx_df['close_price']
    chg = idx_df['change_pct'].fillna(0)
    ma5 = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    features = {
        'idx_ma5_dev': (close.iloc[-1] / ma5 - 1) * 100,
        'idx_ma20_dev': (close.iloc[-1] / ma20 - 1) * 100,
        'idx_ma5_ma20_diff': (ma5 / ma20 - 1) * 100,
        'idx_chg_5d_avg': chg.tail(5).mean(),
        'idx_chg_10d_avg': chg.tail(10).mean(),
        'idx_chg_20d_avg': chg.tail(20).mean(),
        'idx_vol_5d': chg.tail(5).std(),
        'idx_vol_20d': chg.tail(20).std(),
    }
    return features


def _label_direction(returns_3d: float) -> int:
    """根据未来 3 日累计收益打标签"""
    if returns_3d < -1.5: return 0  # strong_down
    elif returns_3d < -0.5: return 1  # down
    elif returns_3d < 0.5: return 2  # range
    elif returns_3d < 1.5: return 3  # up
    else: return 4  # strong_up


def train(conn, start_date='2024-01-01', end_date='2026-06-09'):
    """训练大盘方向模型"""
    from lightgbm import LGBMClassifier
    from sklearn.model_selection import TimeSeriesSplit

    logger.info("Loading market index data...")
    df = pd.read_sql("""
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])

    # 构建特征 + 标签
    X, y, dates = [], [], []
    for i in range(30, len(df) - 3):
        sub = df.iloc[:i+1]
        feats = _compute_features_v1(sub)
        if not feats: continue
        X.append(list(feats.values()))
        # 未来 3 日累计收益
        future_ret = (df['close_price'].iloc[i+3] / df['close_price'].iloc[i] - 1) * 100
        y.append(_label_direction(future_ret))
        dates.append(df['trade_date'].iloc[i])

    X = np.array(X)
    y = np.array(y)
    logger.info(f"Training set: {X.shape}, label dist: {np.bincount(y)}")

    # 时序 CV (5-fold)
    tscv = TimeSeriesSplit(n_splits=5)
    accs = []
    models = []
    for fold, (tr, va) in enumerate(tscv.split(X)):
        clf = LGBMClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            num_leaves=15, min_child_samples=20, random_state=42,
            verbose=-1
        )
        clf.fit(X[tr], y[tr])
        pred = clf.predict(X[va])
        acc = (pred == y[va]).mean()
        accs.append(acc)
        models.append(clf)
        logger.info(f"  fold {fold}: acc={acc:.3f}")

    # 用全部数据重训
    final = LGBMClassifier(
        n_estimators=400, max_depth=4, learning_rate=0.05,
        num_leaves=15, min_child_samples=20, random_state=42, verbose=-1
    )
    final.fit(X, y)
    bundle = {
        'model': final,
        'feature_names': list(_compute_features_v1(df.head(30)).keys()),
        'cv_acc_mean': float(np.mean(accs)),
        'cv_acc_std': float(np.std(accs)),
        'trained_at': datetime.now().isoformat(),
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}, CV acc: {np.mean(accs):.3f} ± {np.std(accs):.3f}")
    return bundle


def predict(conn, as_of_date: str) -> dict:
    """推理: 加载模型, 输出方向 + 概率"""
    if not MODEL_PATH.exists():
        logger.warning(f"Model not found: {MODEL_PATH}, returning 'range' as fallback")
        return {'direction': 'range', 'prob': 0.5, 'expected_return': 0.0, 'confidence': 0.0}
    bundle = joblib.load(MODEL_PATH)
    idx_df = _load_index_data(conn, as_of_date)
    feats = _compute_features_v1(idx_df)
    if feats is None:
        return {'direction': 'range', 'prob': 0.5, 'expected_return': 0.0, 'confidence': 0.0}
    X = np.array([list(feats.values())])
    prob = bundle['model'].predict_proba(X)[0]
    direction_idx = int(np.argmax(prob))
    return {
        'direction': DIRECTIONS[direction_idx],
        'prob': round(float(prob[direction_idx]), 3),
        'probs': {d: round(float(p), 3) for d, p in zip(DIRECTIONS, prob)},
        'expected_return': round(float(prob @ np.array([-2.0, -1.0, 0.0, 1.0, 2.0])), 3),
        'confidence': round(float(prob[direction_idx] - np.sort(prob)[-2]), 3),
    }


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config
    conn = pymysql.connect(**get_db_config())
    train(conn)
