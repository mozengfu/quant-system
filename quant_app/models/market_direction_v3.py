"""
TopDown V1 — Layer 1: 大盘气象 (MarketRegime)

在 V2 基础上增强:
  - 16 维 → 35+ 维
  - 新增市场宽度、板块分散度、资金流特征
  - 保持 LGBM + KNN 加权集成架构 (3分类: bear/range/bull)
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
MODEL_PATH = MODEL_DIR / "market_direction_v3.pkl"

DIRECTIONS = ['bear', 'range', 'bull']


# ── 特征工程 ────────────────────────────────────────────


def _load_index_data(conn, as_of_date: str, lookback: int = 80) -> pd.DataFrame:
    """加载上证指数近 N 日数据"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close_price, change_pct
        FROM market_index_daily
        WHERE index_code='000001.SH' AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT %s
    """, (as_of_date, lookback))
    rows = cur.fetchall()
    if len(rows) < 30:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=['trade_date', 'close', 'pct_chg'])
    return df.sort_values('trade_date').reset_index(drop=True)


def _load_market_breadth(conn, as_of_date: str, lookback: int = 20) -> dict:
    """加载市场宽度特征: 涨跌比、站上均线比例、放量比例"""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            AVG(CASE WHEN pct_chg>0 THEN 1 ELSE 0 END) as up_ratio,
            AVG(CASE WHEN close>ma20 THEN 1 ELSE 0 END) as above_ma20_ratio,
            AVG(CASE WHEN turnover_rate>0 THEN
                CASE WHEN vol > (SELECT AVG(vol)*1.5 FROM daily_price d2
                    WHERE d2.ts_code=daily_price.ts_code AND d2.trade_date<=%s
                    ORDER BY d2.trade_date DESC LIMIT 20)
                THEN 1 ELSE 0 END
            ELSE 0 END) as vol_breakout_ratio
        FROM daily_price
        WHERE trade_date=%s
    """, (as_of_date, as_of_date))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return {'mkt_up_ratio': 0.5, 'mkt_above_ma20': 0.5, 'mkt_vol_breakout': 0.1}
    return {
        'mkt_up_ratio': float(row[0]),
        'mkt_above_ma20': float(row[1]),
        'mkt_vol_breakout': float(row[2]) if row[2] is not None else 0.1,
    }


def _load_sector_dispersion(conn, as_of_date: str) -> dict:
    """加载板块分散度特征: 板块收益标准差/极差"""
    cur = conn.cursor()
    cur.execute("""
        SELECT pct_change FROM sector_moneyflow WHERE trade_date=%s
    """, (as_of_date,))
    rows = cur.fetchall()
    if not rows:
        return {'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5}
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < 5:
        return {'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5}
    return {
        'sector_ret_std': float(np.std(vals)),
        'sector_ret_range': float(np.ptp(vals)),
        'sector_up_ratio': float(np.mean([1 if v > 0 else 0 for v in vals])),
    }


def _load_sector_flow(conn, as_of_date: str) -> dict:
    """加载板块资金流特征"""
    cur = conn.cursor()
    cur.execute("""
        SELECT
            SUM(net_amount) as total_net_flow,
            SUM(CASE WHEN net_amount>0 THEN 1 ELSE 0 END)*1.0/COUNT(*) as inflow_ratio,
            SUM(buy_elg_amount) as total_elg_buy,
            SUM(sell_elg_amount) as total_elg_sell
        FROM sector_moneyflow WHERE trade_date=%s
    """, (as_of_date,))
    row = cur.fetchone()
    if row is None or row[0] is None:
        return {'sector_net_flow': 0, 'sector_inflow_ratio': 0.5,
                'sector_elg_net': 0}
    total_net = float(row[0]) if row[0] is not None else 0
    inflow_ratio = float(row[1]) if row[1] is not None else 0.5
    total_elg_buy = float(row[2]) if row[2] is not None else 0
    total_elg_sell = float(row[3]) if row[3] is not None else 0
    return {
        'sector_net_flow': total_net,
        'sector_inflow_ratio': inflow_ratio,
        'sector_elg_net': total_elg_buy - total_elg_sell,
    }


def _compute_features_v3(idx_df: pd.DataFrame, breadth: dict,
                          dispersion: dict, flow: dict) -> dict:
    """计算 V3 增强特征集 (35+ 维)"""
    if len(idx_df) < 30:
        return None

    close = idx_df['close'].astype(float)
    pct = idx_df['pct_chg'].fillna(0).astype(float)

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else ma20

    feats = {}

    # ── 均线偏离 (4维) ──
    feats['idx_dev_ma5'] = (close.iloc[-1] / ma5 - 1) * 100
    feats['idx_dev_ma10'] = (close.iloc[-1] / ma10 - 1) * 100
    feats['idx_dev_ma20'] = (close.iloc[-1] / ma20 - 1) * 100
    feats['idx_dev_ma60'] = (close.iloc[-1] / ma60 - 1) * 100 if ma60 > 0 else 0

    # ── 多周期收益 (5维) ──
    for n in [1, 3, 5, 10, 20]:
        if len(close) > n:
            feats[f'idx_ret_{n}d'] = (close.iloc[-1] / close.iloc[-1-n] - 1) * 100
        else:
            feats[f'idx_ret_{n}d'] = 0

    # ── 多周期波动 (3维) ──
    for n in [5, 10, 20]:
        feats[f'idx_vol_{n}d'] = float(pct.tail(n).std())

    # ── 均线关系 (3维) ──
    feats['idx_ma5_ma10_diff'] = (ma5 / ma10 - 1) * 100
    feats['idx_ma10_ma20_diff'] = (ma10 / ma20 - 1) * 100
    feats['idx_ma5_ma20_diff'] = (ma5 / ma20 - 1) * 100

    # ── 趋势强度 (4维) ──
    feats['idx_above_ma5_pct'] = (close.tail(5) > ma5).mean() * 100
    feats['idx_above_ma20_pct'] = (close.tail(20) > ma20).mean() * 100
    feats['idx_up_days_5d'] = (pct.tail(5) > 0).sum()
    feats['idx_down_days_5d'] = (pct.tail(5) < 0).sum()

    # ── 极端行情 (4维) ──
    feats['idx_big_up_5d'] = (pct.tail(5) > 1.5).sum()
    feats['idx_big_down_5d'] = (pct.tail(5) < -1.5).sum()
    feats['idx_big_up_10d'] = (pct.tail(10) > 1.5).sum()
    feats['idx_big_down_10d'] = (pct.tail(10) < -1.5).sum()

    # ── 加速度 (2维) ──
    feats['idx_ret_5d_accel'] = feats['idx_ret_5d'] - feats['idx_ret_10d']
    feats['idx_vol_accel'] = feats['idx_vol_5d'] - feats['idx_vol_20d']

    # ── 量价关系 (2维) ──
    feats['idx_return_vol_ratio'] = feats['idx_ret_5d'] / feats['idx_vol_5d'] if feats['idx_vol_5d'] > 0 else 0
    feats['idx_trend_strength'] = feats['idx_ret_20d'] / feats['idx_vol_20d'] if feats['idx_vol_20d'] > 0 else 0

    # ── 连续涨跌 (2维) ──
    consecutive_up = 0
    consecutive_down = 0
    for i in range(len(pct)-1, max(len(pct)-10, -1), -1):
        if pct.iloc[i] > 0:
            consecutive_up += 1
            consecutive_down = 0
        elif pct.iloc[i] < 0:
            consecutive_down += 1
            consecutive_up = 0
        else:
            break
    feats['idx_consecutive_up'] = consecutive_up
    feats['idx_consecutive_down'] = consecutive_down

    # ── 市场宽度 (来自 daily_price 聚合, 3维) ──
    feats.update(breadth)

    # ── 板块分散度 (3维) ──
    feats.update(dispersion)

    # ── 板块资金流 (3维) ──
    feats.update(flow)

    # ── VIX-like 波动率分位 (1维) ──
    hist_vol = pct.rolling(20).std().dropna()
    if len(hist_vol) > 20:
        current_vol = float(hist_vol.iloc[-1])
        feats['idx_vol_percentile'] = float((hist_vol < current_vol).mean() * 100)
    else:
        feats['idx_vol_percentile'] = 50

    return feats


# ── 标签 ─────────────────────────────────────────────────


def _label_3class(returns_3d: float) -> int:
    if returns_3d < -0.5:
        return 0  # bear
    elif returns_3d > 0.5:
        return 2  # bull
    else:
        return 1  # range


# ── 训练 ─────────────────────────────────────────────────


def _preload_aux_data(conn, start_date, end_date):
    """预加载所有辅助特征 — 从 daily_price 聚合，覆盖全时间段"""

    from quant_app.models.sector_features import build_market_breadth_daily, build_sector_daily, get_sector_features_at

    logger.info("Preloading auxiliary data from daily_price...")

    # 1. 市场宽度
    breadth_df = build_market_breadth_daily(conn)
    breadth_df = breadth_df[(breadth_df['trade_date'].astype(str) >= start_date) & (breadth_df['trade_date'].astype(str) <= end_date)]
    breadth_map = {}
    for _, row in breadth_df.iterrows():
        d = str(row['trade_date'])[:10]
        breadth_map[d] = {
            'mkt_up_ratio': float(row['mkt_up_ratio']),
            'mkt_above_ma20': float(row['mkt_above_ma20']),
            'mkt_vol_breakout': 0.1,
        }
    logger.info(f"  Breadth: {len(breadth_map)} days")

    # 2. 板块分散度 & 资金流（从 daily_price 聚合）
    sector_daily = build_sector_daily(conn)
    dispersion_map = {}
    flow_map = {}
    for _, row in sector_daily.iterrows():
        d = str(row['trade_date'])[:10]
        if d not in dispersion_map:
            # 每个日期只计算一次横截面特征
            feats = get_sector_features_at(sector_daily, d)
            # 将所有板块特征统一为一个日期条目
            pass  # 在下面统一处理

    # 对每个日期计算横截面特征
    unique_dates = sorted(sector_daily['trade_date'].unique())
    for d in unique_dates:
        d_str = str(d)[:10]
        if d_str < start_date or d_str > end_date:
            continue
        feats = get_sector_features_at(sector_daily, d_str)
        dispersion_map[d_str] = {
            'sector_ret_std': feats['sector_ret_std'],
            'sector_ret_range': feats['sector_ret_range'],
            'sector_up_ratio': feats['sector_up_ratio'],
        }
        flow_map[d_str] = {
            'sector_net_flow': feats['sector_net_flow'],
            'sector_inflow_ratio': feats['sector_inflow_ratio'],
            'sector_elg_net': 0,
        }
    logger.info(f"  Sector: {len(dispersion_map)} days")

    return breadth_map, dispersion_map, flow_map


def train(conn, start_date='2024-01-01', end_date='2026-06-09'):
    """训练 V3 大盘方向预测器"""
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

    df = df.rename(columns={'close_price': 'close', 'change_pct': 'pct_chg'})
    df['close'] = df['close'].astype(float)
    df['pct_chg'] = df['pct_chg'].astype(float)

    logger.info(f"  {len(df)} index rows loaded")

    # 预加载所有辅助数据
    breadth_map, dispersion_map, flow_map = _preload_aux_data(conn, start_date, end_date)

    # 默认值（日期缺失时降级）
    default_breadth = {'mkt_up_ratio': 0.5, 'mkt_above_ma20': 0.5, 'mkt_vol_breakout': 0.1}
    default_dispersion = {'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5}
    default_flow = {'sector_net_flow': 0, 'sector_inflow_ratio': 0.5, 'sector_elg_net': 0}

    # 构建特征 + 标签
    X, y, dates = [], [], []
    logger.info("Building features...")
    for i in range(30, len(df) - 3):
        sub = df.iloc[:i+1]
        as_of = str(df['trade_date'].iloc[i])[:10]

        breadth = breadth_map.get(as_of, default_breadth)
        dispersion = dispersion_map.get(as_of, default_dispersion)
        flow = flow_map.get(as_of, default_flow)

        feats = _compute_features_v3(sub, breadth, dispersion, flow)
        if feats is None:
            continue

        X.append(list(feats.values()))

        # 未来3日累计收益 → 标签
        future_ret = (df['close'].iloc[i+3] / df['close'].iloc[i] - 1) * 100
        y.append(_label_3class(future_ret))
        dates.append(df['trade_date'].iloc[i])

        if i % 100 == 0:
            logger.info(f"  progress: {i}/{len(df)}")

    X = np.array(X)
    y = np.array(y)
    logger.info(f"Training: X={X.shape}, dist={np.bincount(y)}")

    # 时序 CV
    tscv = TimeSeriesSplit(n_splits=5)
    accs, f1s = [], []
    for fold, (tr, va) in enumerate(tscv.split(X)):
        # LightGBM
        lgb = LGBMClassifier(
            n_estimators=350, max_depth=5, learning_rate=0.04,
            num_leaves=21, min_child_samples=20, random_state=42, verbose=-1
        )
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
        if p2.shape[1] < p1.shape[1]:
            p2 = np.hstack([p2, np.zeros((p2.shape[0], p1.shape[1]-p2.shape[1]))])
        prob = 0.6 * p1 + 0.4 * p2
        pred = prob.argmax(axis=1)

        acc = accuracy_score(y[va], pred)
        f1 = f1_score(y[va], pred, average='macro')
        accs.append(acc)
        f1s.append(f1)
        logger.info(f"  fold {fold}: acc={acc:.3f}  f1={f1:.3f}  dist={np.bincount(pred, minlength=3)}")

    # 全量训练
    logger.info("Training full model...")
    lgb_full = LGBMClassifier(
        n_estimators=500, max_depth=5, learning_rate=0.04,
        num_leaves=21, min_child_samples=20, random_state=42, verbose=-1
    )
    lgb_full.fit(X, y)
    scaler_full = StandardScaler().fit(X)
    knn_full = KNeighborsClassifier(n_neighbors=15)
    knn_full.fit(scaler_full.transform(X), y)

    # 特征名
    breadth_sample = {'mkt_up_ratio': 0.5, 'mkt_above_ma20': 0.5, 'mkt_vol_breakout': 0.1}
    dispersion_sample = {'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5}
    flow_sample = {'sector_net_flow': 0, 'sector_inflow_ratio': 0.5, 'sector_elg_net': 0}
    feats_sample = _compute_features_v3(df.head(40), breadth_sample, dispersion_sample, flow_sample)
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
        'version': 'v3.0',
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}")
    logger.info(f"  CV acc={np.mean(accs):.3f}±{np.std(accs):.3f}, f1={np.mean(f1s):.3f}")
    return bundle


# ── 推理 ─────────────────────────────────────────────────


def predict(conn, as_of_date: str) -> dict:
    """推理: LGBM + KNN 加权投票, 返回 market regime"""
    if not MODEL_PATH.exists():
        logger.warning(f"Model not found: {MODEL_PATH}, returning range")
        return {'direction': 'range', 'prob': 0.5, 'probs': {'bear': 0.33, 'range': 0.34, 'bull': 0.33},
                'expected_return': 0, 'confidence': 0}

    bundle = joblib.load(MODEL_PATH)

    # 加载指数数据
    idx_df = _load_index_data(conn, as_of_date)
    if len(idx_df) < 30:
        return {'direction': 'range', 'prob': 0.5, 'probs': {'bear': 0.33, 'range': 0.34, 'bull': 0.33},
                'expected_return': 0, 'confidence': 0}

    # 加载辅助特征
    try:
        breadth = _load_market_breadth(conn, as_of_date)
        dispersion = _load_sector_dispersion(conn, as_of_date)
        flow = _load_sector_flow(conn, as_of_date)
    except Exception:
        breadth = {'mkt_up_ratio': 0.5, 'mkt_above_ma20': 0.5, 'mkt_vol_breakout': 0.1}
        dispersion = {'sector_ret_std': 0, 'sector_ret_range': 0, 'sector_up_ratio': 0.5}
        flow = {'sector_net_flow': 0, 'sector_inflow_ratio': 0.5, 'sector_elg_net': 0}

    feats = _compute_features_v3(idx_df, breadth, dispersion, flow)
    if feats is None:
        return {'direction': 'range', 'prob': 0.5, 'probs': {'bear': 0.33, 'range': 0.34, 'bull': 0.33},
                'expected_return': 0, 'confidence': 0}

    X = np.array([list(feats.values())])
    X_s = bundle['scaler'].transform(X)

    p1 = bundle['lgbm'].predict_proba(X)[0]
    p2 = bundle['knn'].predict_proba(X_s)[0]
    if len(p2) < len(p1):
        p2 = np.concatenate([p2, [0]*(len(p1)-len(p2))])

    prob = 0.6 * p1 + 0.4 * p2
    direction_idx = int(prob.argmax())

    # expected_return: 用概率加权 (bear=-1, range=0, bull=+1)
    er = float(prob[2] - prob[0])

    return {
        'direction': DIRECTIONS[direction_idx],
        'prob': round(float(prob[direction_idx]), 3),
        'probs': {d: round(float(p), 3) for d, p in zip(DIRECTIONS, prob)},
        'expected_return': round(er, 3),
        'confidence': round(float(prob[direction_idx] - np.sort(prob)[-2]), 3),
        'position_multiplier': _position_multiplier(DIRECTIONS[direction_idx], prob),
    }


def _position_multiplier(direction: str, prob: np.ndarray) -> float:
    """根据大盘方向返回仓位系数"""
    if direction == 'bull':
        return 0.8 + 0.2 * float(prob[2])  # 0.8 ~ 1.0
    elif direction == 'range':
        return 0.5 + 0.2 * float(prob[1])  # 0.5 ~ 0.7
    else:  # bear
        return 0.2 + 0.1 * float(prob[2])  # 0.2 ~ 0.3 (保留少量参与)


# ── 直接运行 ─────────────────────────────────────────────


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    conn = pymysql.connect(**get_db_config())
    bundle = train(conn)
    print("\n=== Test predictions ===")
    for d in ['2026-04-30', '2026-05-15', '2026-06-05', '2026-06-09']:
        r = predict(conn, d)
        print(f"  {d}: {r['direction']} (p={r['prob']:.2f}, er={r['expected_return']:+.2f}, pos_mul={r['position_multiplier']:.2f})")
    conn.close()
