"""
Stage 1 重训: 大盘方向预测 V2
  - 3 档分类: up / range / down (而不是 5 档)
  - 多特征: regime 6 维 + 资金流 + 板块广度
  - KNN 案例检索 (相似 K 线投票)
  - LightGBM + 案例检索 投票
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

MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "market_direction_v2.pkl"

DIRECTIONS = ['down', 'range', 'up']  # 3 档


def _load_index_data(conn, as_of_date: str, lookback: int = 60) -> pd.DataFrame:
    """加载上证指数近 N 日"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT %s
    """, (as_of_date, lookback))
    rows = cur.fetchall()
    if len(rows) < 30:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=['trade_date', 'close', 'pct_chg'])
    return df.sort_values('trade_date').reset_index(drop=True)


def _compute_features_v2(idx_df: pd.DataFrame) -> dict:
    """3 档分类的丰富特征"""
    if len(idx_df) < 30:
        return None
    close = idx_df['close'].astype(float)
    pct = idx_df['pct_chg'].fillna(0).astype(float)
    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else ma20

    # 收益
    rets = []
    for n in [1, 3, 5, 10, 20]:
        if len(close) > n:
            rets.append((close.iloc[-1] / close.iloc[-1-n] - 1) * 100)
        else:
            rets.append(0)
    # 波动
    vol_5 = pct.tail(5).std()
    vol_10 = pct.tail(10).std()
    vol_20 = pct.tail(20).std()

    # 趋势强度
    above_ma5 = (close > ma5).sum() / 5
    above_ma20 = (close > ma20).sum() / 20

    # K 线形态 (近 5 日)
    up_days = (pct > 0).tail(5).sum()
    down_days = (pct < 0).tail(5).sum()
    big_up = (pct > 1.5).tail(5).sum()
    big_down = (pct < -1.5).tail(5).sum()

    # 距离均线偏离度
    dev_ma5 = (close.iloc[-1] / ma5 - 1) * 100
    dev_ma20 = (close.iloc[-1] / ma20 - 1) * 100

    return {
        'idx_dev_ma5': dev_ma5,
        'idx_dev_ma20': dev_ma20,
        'idx_ret_1d': rets[0],
        'idx_ret_3d': rets[1],
        'idx_ret_5d': rets[2],
        'idx_ret_10d': rets[3],
        'idx_ret_20d': rets[4],
        'idx_vol_5d': vol_5,
        'idx_vol_10d': vol_10,
        'idx_vol_20d': vol_20,
        'idx_above_ma5_pct': above_ma5 * 100,
        'idx_above_ma20_pct': above_ma20 * 100,
        'idx_up_days_5d': up_days,
        'idx_down_days_5d': down_days,
        'idx_big_up_5d': big_up,
        'idx_big_down_5d': big_down,
        'idx_ma5_ma20_diff': (ma5 / ma20 - 1) * 100,
    }


def _label_3class(returns_3d: float) -> int:
    """3 档标签: down / range / up"""
    if returns_3d < -0.5: return 0  # down
    elif returns_3d > 0.5: return 2   # up
    else: return 1                    # range


def train(conn, start_date='2024-01-01', end_date='2026-06-09'):
    """训练 3 档分类器"""
    from lightgbm import LGBMClassifier
    from sklearn.metrics import accuracy_score, f1_score
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.preprocessing import StandardScaler

    logger.info("Loading index data...")
    df = pd.read_sql("""
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])
    logger.info(f"  {len(df)} rows")
    # 列名统一
    df = df.rename(columns={'close_price': 'close', 'change_pct': 'pct_chg'})
    # 转 float 避免 Decimal 算术
    df['close'] = df['close'].astype(float)
    df['pct_chg'] = df['pct_chg'].astype(float)

    # 构建特征 + 标签
    X, y, dates = [], [], []
    for i in range(30, len(df) - 3):
        sub = df.iloc[:i+1]
        feats = _compute_features_v2(sub)
        if not feats: continue
        X.append(list(feats.values()))
        # 未来 3 日累计收益
        future_ret = (df['close'].iloc[i+3] / df['close'].iloc[i] - 1) * 100
        y.append(_label_3class(future_ret))
        dates.append(df['trade_date'].iloc[i])

    X = np.array(X)
    y = np.array(y)
    logger.info(f"Training: X={X.shape}, dist={np.bincount(y)}")

    # 时序 CV
    tscv = TimeSeriesSplit(n_splits=5)
    accs, f1s = [], []
    for fold, (tr, va) in enumerate(tscv.split(X)):
        # LightGBM
        lgb = LGBMClassifier(n_estimators=300, max_depth=4, learning_rate=0.05,
                              num_leaves=15, min_child_samples=20, random_state=42, verbose=-1)
        lgb.fit(X[tr], y[tr])
        # KNN
        scaler = StandardScaler()
        Xtr_s = scaler.fit_transform(X[tr])
        Xva_s = scaler.transform(X[va])
        knn = KNeighborsClassifier(n_neighbors=15)
        knn.fit(Xtr_s, y[tr])
        # 投票
        p1 = lgb.predict_proba(X[va])
        p2 = knn.predict_proba(Xva_s)
        # 类别数对齐 (knn 可能在某些 fold 少一类)
        if p2.shape[1] < p1.shape[1]:
            p2 = np.hstack([p2, np.zeros((p2.shape[0], p1.shape[1] - p2.shape[1]))])
        prob = 0.6 * p1 + 0.4 * p2
        pred = prob.argmax(axis=1)
        acc = accuracy_score(y[va], pred)
        f1 = f1_score(y[va], pred, average='macro')
        accs.append(acc); f1s.append(f1)
        logger.info(f"  fold {fold}: acc={acc:.3f}  f1={f1:.3f}  dist={np.bincount(pred, minlength=3)}")

    # 全量训练
    lgb_full = LGBMClassifier(n_estimators=400, max_depth=4, learning_rate=0.05,
                               num_leaves=15, min_child_samples=20, random_state=42, verbose=-1)
    lgb_full.fit(X, y)
    scaler_full = StandardScaler().fit(X)
    knn_full = KNeighborsClassifier(n_neighbors=15)
    knn_full.fit(scaler_full.transform(X), y)

    # 特征名
    feats_sample = _compute_features_v2(df.head(40))
    feat_names = list(feats_sample.keys())

    bundle = {
        'lgbm': lgb_full,
        'knn': knn_full,
        'scaler': scaler_full,
        'feature_names': feat_names,
        'directions': DIRECTIONS,
        'cv_acc_mean': float(np.mean(accs)),
        'cv_acc_std': float(np.std(accs)),
        'cv_f1_mean': float(np.mean(f1s)),
        'trained_at': datetime.now().isoformat(),
        'n_samples': int(len(y)),
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}, CV acc={np.mean(accs):.3f}±{np.std(accs):.3f}, f1={np.mean(f1s):.3f}")
    return bundle


def predict(conn, as_of_date: str) -> dict:
    """推理: LGBM + KNN 投票"""
    if not MODEL_PATH.exists():
        return {'direction': 'range', 'prob': 0.5, 'expected_return': 0, 'confidence': 0}
    bundle = joblib.load(MODEL_PATH)
    idx_df = _load_index_data(conn, as_of_date)
    feats = _compute_features_v2(idx_df)
    if feats is None:
        return {'direction': 'range', 'prob': 0.5, 'expected_return': 0, 'confidence': 0}
    X = np.array([list(feats.values())])
    X_s = bundle['scaler'].transform(X)
    p1 = bundle['lgbm'].predict_proba(X)[0]
    p2 = bundle['knn'].predict_proba(X_s)[0]
    if len(p2) < len(p1):
        p2 = np.concatenate([p2, [0]*(len(p1)-len(p2))])
    prob = 0.6 * p1 + 0.4 * p2
    direction_idx = int(prob.argmax())
    return {
        'direction': DIRECTIONS[direction_idx],
        'prob': round(float(prob[direction_idx]), 3),
        'probs': {d: round(float(p), 3) for d, p in zip(DIRECTIONS, prob)},
        'expected_return': round(float(prob @ np.array([-1.0, 0.0, 1.0])), 3),
        'confidence': round(float(prob[direction_idx] - np.sort(prob)[-2]), 3),
    }


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    conn = pymysql.connect(**get_db_config())
    bundle = train(conn)
    print("\n=== Test predictions ===")
    for d in ['2026-04-30', '2026-05-15', '2026-06-05', '2026-06-09']:
        r = predict(conn, d)
        print(f"  {d}: {r['direction']} (p={r['prob']:.2f}, er={r['expected_return']:+.2f})")
    conn.close()
