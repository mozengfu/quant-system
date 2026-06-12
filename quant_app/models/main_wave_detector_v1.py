"""
Stage 3: 主升浪个股检测器

核心:
  - 标签: main_wave_labels 表 (label=1 是主升浪启动日)
  - 特征: V11.0 117 维 + 5 个新模块 (~50 维新增)
  - 模型: LightGBM 二分类 + class_weight 处理不平衡
  - 集成: 0.4 * ml_v11_rank + 0.4 * main_wave_prob + 0.2 * sector_score
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

import pymysql

from quant_app.features.hsgt_features import build_hsgt_features
from quant_app.features.lhb_features import build_lhb_features
from quant_app.features.pattern_features import build_pattern_features
from quant_app.features.research_features import build_research_features
from quant_app.features.sector_relay_features import build_sector_relay_features
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "main_wave_detector_v1.pkl"


def _load_v11_features_for_codes(conn, ts_codes: list[str], as_of_date: str) -> pd.DataFrame:
    """复用 ml_predict 的 V8.0 特征构建 (V11 与 V8.0 兼容)"""
    try:
        from ml_predict import _build_features_for_stocks_v8_0
        df = _build_features_for_stocks_v8_0(conn, ts_codes, as_of_date=as_of_date)
        if df is not None and not df.empty:
            return df.set_index('ts_code') if 'ts_code' in df.columns else df
    except Exception as e:
        logger.warning(f"V8.0 feature build failed: {e}, falling back to minimal features")
    return pd.DataFrame(index=ts_codes)


def _build_full_features(conn, ts_codes: list[str], as_of_date: str) -> pd.DataFrame:
    """合并 V11 基础特征 + 5 个新特征模块"""
    # 1) V11 基础特征
    df_v11 = _load_v11_features_for_codes(conn, ts_codes, as_of_date)
    # 2) 5 个新模块
    df_lhb = build_lhb_features(ts_codes, as_of_date, conn)
    df_hsgt = build_hsgt_features(ts_codes, as_of_date, conn)
    df_pat = build_pattern_features(ts_codes, as_of_date, conn)
    df_res = build_research_features(ts_codes, as_of_date, conn)
    df_relay = build_sector_relay_features(ts_codes, as_of_date, conn)
    # 3) 合并
    parts = [df_v11.add_prefix('v11_'), df_lhb, df_hsgt, df_pat, df_res, df_relay]
    df = pd.concat(parts, axis=1)
    # 填充缺失
    df = df.fillna(0)
    return df


def train(conn, label_start='2024-01-01', label_end='2025-12-31'):
    """训练主升浪分类器"""
    from lightgbm import LGBMClassifier
    from sklearn.model_selection import TimeSeriesSplit

    # 读标签
    logger.info("Loading labels...")
    df_label = pd.read_sql("""
        SELECT trade_date, ts_code, label, return_3d, return_5d
        FROM main_wave_labels
        WHERE trade_date BETWEEN %s AND %s
    """, conn, params=(label_start, label_end), parse_dates=['trade_date'])
    logger.info(f"  {len(df_label)} samples, pos rate {df_label['label'].mean()*100:.2f}%")

    # 抽样构建特征 (避免太慢)
    sample_n = min(20000, len(df_label))
    df_sample = df_label.sample(n=sample_n, random_state=42).sort_values('trade_date')

    X_list, y_list = [], []
    grouped = df_sample.groupby('trade_date', sort=False)
    for i, (d, g) in enumerate(grouped):
        codes = g['ts_code'].tolist()
        y = g['label'].values
        feats = _build_full_features(conn, codes, str(d.date()))
        if feats is None or feats.empty:
            continue
        feats = feats.reindex(codes).fillna(0)
        X_list.append(feats.values)
        y_list.append(y)
        if (i+1) % 10 == 0:
            logger.info(f"  built {i+1}/{len(grouped)} dates, total {len(X_list)} groups")

    if not X_list:
        logger.error("No features built!")
        return None
    X = np.vstack(X_list)
    y = np.concatenate(y_list)
    logger.info(f"Final: X={X.shape}, pos={y.sum()}, neg={(1-y).sum()}")

    # 时序 CV
    tscv = TimeSeriesSplit(n_splits=5)
    aucs = []
    for fold, (tr, va) in enumerate(tscv.split(X)):
        clf = LGBMClassifier(
            n_estimators=500, max_depth=5, learning_rate=0.04,
            num_leaves=31, min_child_samples=30,
            class_weight='balanced',
            random_state=42, verbose=-1,
        )
        clf.fit(X[tr], y[tr])
        from sklearn.metrics import roc_auc_score
        prob = clf.predict_proba(X[va])[:, 1]
        auc = roc_auc_score(y[va], prob)
        aucs.append(auc)
        logger.info(f"  fold {fold}: AUC={auc:.3f}")

    # 全量重训
    final = LGBMClassifier(
        n_estimators=600, max_depth=5, learning_rate=0.04,
        num_leaves=31, min_child_samples=30,
        class_weight='balanced', random_state=42, verbose=-1,
    )
    final.fit(X, y)
    bundle = {
        'model': final,
        'feature_names': list(_build_full_features(conn, df_label['ts_code'].head(3).tolist(), str(df_label['trade_date'].iloc[0].date())).columns),
        'cv_auc_mean': float(np.mean(aucs)),
        'cv_auc_std': float(np.std(aucs)),
        'trained_at': datetime.now().isoformat(),
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}, AUC: {np.mean(aucs):.3f} ± {np.std(aucs):.3f}")
    return bundle


def predict(conn, ts_codes: list[str], as_of_date: str, sector_scores: dict = None) -> pd.DataFrame:
    """推理: 返回每只股票的主升浪概率"""
    if not MODEL_PATH.exists():
        logger.warning(f"Model not found: {MODEL_PATH}")
        return pd.DataFrame({'ts_code': ts_codes, 'main_wave_prob': [0.5] * len(ts_codes)})
    bundle = joblib.load(MODEL_PATH)
    feats = _build_full_features(conn, ts_codes, as_of_date)
    if feats is None or feats.empty:
        return pd.DataFrame({'ts_code': ts_codes, 'main_wave_prob': [0.5] * len(ts_codes)})
    # 对齐列
    feat_cols = bundle['feature_names']
    for c in feat_cols:
        if c not in feats.columns:
            feats[c] = 0
    X = feats[feat_cols].fillna(0).values
    prob = bundle['model'].predict_proba(X)[:, 1]
    out = pd.DataFrame({'ts_code': feats.index, 'main_wave_prob': prob})
    if sector_scores:
        out['sector_score'] = out['ts_code'].map(lambda c: sector_scores.get(_industry_of(c, conn), 0))
    return out


def _industry_of(ts_code: str, conn) -> str:
    with conn.cursor() as c:
        c.execute("SELECT industry FROM stock_info WHERE ts_code=%s", (ts_code,))
        row = c.fetchone()
        return row[0] if row else 'OTHER'


if __name__ == '__main__':
    import pymysql
    conn = pymysql.connect(**get_db_config())
    train(conn)
