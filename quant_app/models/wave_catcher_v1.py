"""
TopDown V1 — Layer 3: 主升浪捕手 (WaveCatcher)

预测个股在未来1~3天内启动主升浪的概率。

特征:
  - V11 117维基础特征 (通过 ml_predict._build_features_for_stocks_v8_0)
  - Layer1 输出: market_regime(one-hot) + prob_bull + expected_return
  - Layer2 输出: sector_heat_score + sector_heat_rank_pct
  - 新增突破特征: price_breakout, vol_breakout, ma_convergence, bb_squeeze等

模型: LightGBM binary classifier (class_weight='balanced')
标签: main_wave_3d (自构建, 见 labels.py)
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
MODEL_PATH = MODEL_DIR / "wave_catcher_v1.pkl"


# ── 辅助数据加载 ──────────────────────────────────────


def _load_board_mapping(conn) -> dict:
    """ts_code → board_name 映射"""
    cur = conn.cursor()
    cur.execute("""
        SELECT ic.ts_code, bi.board_name
        FROM board_industry_cons ic
        JOIN board_industry bi ON ic.board_code=bi.board_code
        WHERE ic.is_latest=1
    """)
    rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def _load_turnover_top_n(conn, as_of_date: str, n: int = 500) -> list:
    """加载成交额TopN股票列表"""
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_code FROM daily_price
        WHERE trade_date=%s
        ORDER BY amount DESC LIMIT %s
    """, (as_of_date, n))
    return [row[0] for row in cur.fetchall()]


# ── 突破特征构建 ─────────────────────────────────────


def _build_breakout_features(conn, ts_codes: list, as_of_date: str) -> pd.DataFrame:
    """为个股构建突破相关特征（从 daily_price 直接计算）"""
    if not ts_codes:
        return pd.DataFrame()

    cur = conn.cursor()
    placeholders = ','.join(['%s'] * len(ts_codes))

    cur.execute(f"""
        SELECT ts_code, trade_date, close, pre_close, pct_chg, vol, amount,
               high, low, turnover_rate, ma5, ma10, ma20
        FROM daily_price
        WHERE ts_code IN ({placeholders})
          AND trade_date <= %s
        ORDER BY ts_code, trade_date
    """, tuple(ts_codes) + (as_of_date,))

    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()

    cols = ['ts_code', 'trade_date', 'close', 'pre_close', 'pct_chg',
            'vol', 'amount', 'high', 'low', 'turnover_rate', 'ma5', 'ma10', 'ma20']
    df = pd.DataFrame(rows, columns=cols)
    for c in ['close', 'pre_close', 'pct_chg', 'vol', 'amount', 'high', 'low',
              'turnover_rate', 'ma5', 'ma10', 'ma20']:
        df[c] = df[c].astype(float)

    results = []
    for code in ts_codes:
        stock = df[df['ts_code'] == code].sort_values('trade_date')
        if len(stock) < 30:
            continue

        feats = {'ts_code': code}

        # ── 价格突破 ──
        close = stock['close']
        high_20 = stock['high'].tail(20).max()
        feats['price_breakout_20d'] = 1 if close.iloc[-1] >= high_20 * 0.98 else 0
        high_60 = stock['high'].tail(60).max() if len(stock) >= 60 else high_20
        feats['near_60d_high'] = close.iloc[-1] / high_60 if high_60 > 0 else 0

        # ── 量比突破 ──
        vol = stock['vol']
        vol_5_avg = vol.tail(5).mean()
        vol_20_avg = vol.tail(20).mean()
        feats['vol_breakout_ratio'] = vol.iloc[-1] / vol_20_avg if vol_20_avg > 0 else 1
        feats['vol_5d_expand'] = vol_5_avg / vol_20_avg if vol_20_avg > 0 else 1

        # ── 均线收敛天数 ──
        if 'ma5' in stock.columns and 'ma10' in stock.columns and 'ma20' in stock.columns:
            ma5, ma10, ma20 = stock['ma5'].iloc[-1], stock['ma10'].iloc[-1], stock['ma20'].iloc[-1]
            if ma20 > 0:
                feats['ma_spread'] = (ma5 - ma20) / ma20 * 100
                feats['ma_convergence'] = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100
            else:
                feats['ma_spread'] = 0
                feats['ma_convergence'] = 100
            # 均线多头排列
            feats['ma_bullish'] = 1 if (ma5 > ma10 > ma20) else 0
        else:
            feats['ma_spread'] = 0
            feats['ma_convergence'] = 100
            feats['ma_bullish'] = 0

        # ── 布林带收窄 ──
        close_20 = close.tail(20)
        if len(close_20) >= 20:
            bb_mid = close_20.mean()
            bb_std = close_20.std()
            bb_width = (2 * bb_std) / bb_mid * 100 if bb_mid > 0 else 10
            # 历史布林带宽度（前40-20天）
            if len(close) >= 40:
                close_prev = close.iloc[-40:-20]
                bb_width_prev = (2 * close_prev.std()) / close_prev.mean() * 100 if close_prev.mean() > 0 else 10
                feats['bb_squeeze'] = bb_width / bb_width_prev if bb_width_prev > 0 else 1
            else:
                feats['bb_squeeze'] = 1
            feats['bb_position'] = (close.iloc[-1] - bb_mid) / (2 * bb_std) if bb_std > 0 else 0
        else:
            feats['bb_squeeze'] = 1
            feats['bb_position'] = 0

        # ── RSI 信号 ──
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
            loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
            if loss > 0:
                rsi = 100 - 100 / (1 + gain / loss)
            else:
                rsi = 100
            feats['rsi_14'] = float(rsi)
            feats['rsi_golden'] = 1 if 30 < rsi < 60 else 0  # 非超买超卖区
        else:
            feats['rsi_14'] = 50
            feats['rsi_golden'] = 0

        # ── 连续涨跌 ──
        pct = stock['pct_chg']
        consecutive_up = 0
        for i in range(len(pct)-1, max(len(pct)-10, -1), -1):
            if pct.iloc[i] > 0:
                consecutive_up += 1
            else:
                break
        feats['consecutive_up_days'] = consecutive_up

        gap_ups = 0
        for i in range(max(len(pct)-5, 0), len(pct)):
            if i > 0 and stock['low'].iloc[i] > stock['high'].iloc[i-1]:
                gap_ups += 1
        feats['gap_up_count_5d'] = gap_ups

        # ── 价格位置 ──
        feats['price_vs_ma20'] = (close.iloc[-1] / stock['ma20'].iloc[-1] - 1) * 100 if stock['ma20'].iloc[-1] > 0 else 0
        feats['price_vs_ma60'] = 0  # placeholder

        # ── 动量加速 ──
        if len(close) >= 5:
            ret_3d = (close.iloc[-1] / close.iloc[-4] - 1) * 100 if len(close) >= 4 else 0
            ret_5d = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else 0
            feats['momentum_accel'] = ret_3d - ret_5d
        else:
            feats['momentum_accel'] = 0

        results.append(feats)

    return pd.DataFrame(results)


# ── 训练 ─────────────────────────────────────────────────


def train(conn, start_date='2024-01-01', end_date='2025-09-30',
          sample_interval=3, max_stocks_per_day=200, max_days=0):
    """训练主升浪捕手模型

    Args:
        sample_interval: 每N个交易日采样一次
        max_stocks_per_day: 每个交易日最多取TopN成交额股票
        max_days: 最多使用多少个交易日（控制训练时间）
    """
    import lightgbm as lgb
    from sklearn.metrics import precision_score, recall_score, roc_auc_score

    from quant_app.models.labels import build_wave_labels

    logger.info("Building wave labels...")
    labels_df = build_wave_labels(conn, start_date, end_date)
    logger.info(f"  Labels: {len(labels_df)} rows, positive rate={labels_df['label'].mean():.4f}")

    # 加载股票-行业映射
    board_map = _load_board_mapping(conn)
    logger.info(f"  Board mapping: {len(board_map)} stocks")

    # 获取所有交易日，按间隔采样
    trade_dates = sorted(labels_df['trade_date'].unique())
    sampled_dates = trade_dates[::sample_interval]
    if len(sampled_dates) > max_days:
        sampled_dates = sampled_dates[-max_days:]  # 取最近的max_days天
    logger.info(f"  {len(trade_dates)} total days → {len(sampled_dates)} sampled days")

    # 先构建少量基础特征获取列名
    logger.info("Loading V11 base features from ml_predict...")
    try:
        from sqlalchemy import create_engine

        from ml_predict import _build_features_for_stocks_v8_0
        from quant_app.utils.config import MYSQL_DATABASE, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER
        engine = create_engine(
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4",
            pool_size=5, pool_recycle=3600
        )
        sql_conn = engine.connect()
        first_date = str(sampled_dates[0])[:10]
        # 取当天成交额Top股票
        cur = conn.cursor()
        cur.execute("SELECT ts_code FROM daily_price WHERE trade_date=%s ORDER BY amount DESC LIMIT %s",
                    (first_date, max_stocks_per_day))
        sample_codes = [r[0] for r in cur.fetchall()]
        sample_v11 = _build_features_for_stocks_v8_0(sql_conn, sample_codes, as_of_date=first_date)
        v11_feature_cols = [c for c in sample_v11.columns if c not in ('ts_code', 'trade_date')]
        logger.info(f"  V11 base features: {len(v11_feature_cols)} dims")
        sql_conn.close()
    except Exception as e:
        logger.warning(f"  Cannot load V11 features: {e}, using simplified feat set")
        v11_feature_cols = []
        sample_v11 = pd.DataFrame()

    # 构建突破特征样本获取列名
    sample_breakout = _build_breakout_features(conn, sample_codes[:50], first_date)
    breakout_feature_cols = [c for c in sample_breakout.columns if c != 'ts_code']
    logger.info(f"  Breakout features: {len(breakout_feature_cols)} dims")

    # 额外特征列
    extra_cols = [
        'market_bull_prob', 'market_expected_return',
        'market_is_bear', 'market_is_range', 'market_is_bull',
        'sector_heat_score', 'sector_heat_rank_pct',
    ]

    # ── 按交易日构建训练数据 ──
    X_list, y_list = [], []
    n_done = 0

    for td in sampled_dates:
        td_str = str(td)[:10]
        td_label = labels_df[labels_df['trade_date'] == td]

        # 取成交额TopN股票（减少特征构建量）
        cur = conn.cursor()
        cur.execute("SELECT ts_code FROM daily_price WHERE trade_date=%s ORDER BY amount DESC LIMIT %s",
                    (td_str, max_stocks_per_day))
        top_codes = [r[0] for r in cur.fetchall()]
        ts_codes = [c for c in top_codes if c in td_label['ts_code'].values]
        if len(ts_codes) < 30:
            continue

        # V11 基础特征
        try:
            sql_conn = engine.connect()
            v11_df = _build_features_for_stocks_v8_0(sql_conn, ts_codes, as_of_date=td_str)
            sql_conn.close()
        except Exception:
            continue

        if v11_df.empty:
            continue

        # 突破特征
        breakout_df = _build_breakout_features(conn, ts_codes, td_str)

        # 合并基础特征
        feat_df = v11_df.merge(breakout_df, on='ts_code', how='left')

        # 添加板块特征
        feat_df['sector_name'] = feat_df['ts_code'].map(board_map)
        feat_df['sector_heat_score'] = 50.0
        feat_df['sector_heat_rank_pct'] = 50.0

        # 添加市场特征
        feat_df['market_bull_prob'] = 0.33
        feat_df['market_expected_return'] = 0
        feat_df['market_is_bear'] = 0
        feat_df['market_is_range'] = 1
        feat_df['market_is_bull'] = 0

        # 合并标签
        feat_df = feat_df.merge(
            td_label[['ts_code', 'label']], on='ts_code', how='inner'
        )

        if len(feat_df) < 20:
            continue

        # 收集特征列
        feature_cols = (
            [c for c in v11_feature_cols if c in feat_df.columns] +
            [c for c in breakout_feature_cols if c in feat_df.columns] +
            extra_cols
        )
        feature_cols = [c for c in feature_cols if c in feat_df.columns]

        # 确保所有列为数值型
        X_day = feat_df[feature_cols].copy()
        for col in X_day.columns:
            X_day[col] = pd.to_numeric(X_day[col], errors='coerce').fillna(0).astype(float)
        X_list.append(X_day)
        y_list.append(feat_df['label'].copy())

        n_done += 1
        if n_done % 10 == 0:
            logger.info(f"  progress: {n_done}/{len(sampled_dates)} days, "
                        f"total samples={sum(len(y) for y in y_list)}")

    if not X_list:
        logger.error("No training data built!")
        return None

    X_all = pd.concat(X_list, ignore_index=True)
    # Ensure all numeric
    for col in X_all.columns:
        X_all[col] = pd.to_numeric(X_all[col], errors='coerce').fillna(0).astype(float)
    y_all = pd.concat(y_list, ignore_index=True).astype(int)
    logger.info(f"Training: X={X_all.shape}, y_pos_rate={y_all.mean():.4f}")

    # ── 时序CV ──
    aucs, precs, recalls = [], [], []
    for fold in range(5):
        # 简化：按样本索引切分
        n_total = len(X_all)
        va_start = n_total * fold // 5
        va_end = n_total * (fold + 1) // 5 if fold < 4 else n_total

        tr_idx = list(range(0, va_start)) + list(range(va_end, n_total))
        va_idx = list(range(va_start, va_end))

        if len(tr_idx) < 500 or len(va_idx) < 100:
            continue

        model = lgb.LGBMClassifier(
            n_estimators=400, max_depth=6, learning_rate=0.03,
            num_leaves=31, min_child_samples=50,
            class_weight='balanced', random_state=42, verbose=-1,
        )
        model.fit(X_all.iloc[tr_idx], y_all.iloc[tr_idx])

        preds = model.predict_proba(X_all.iloc[va_idx])[:, 1]
        pred_binary = (preds > 0.5).astype(int)

        auc = roc_auc_score(y_all.iloc[va_idx], preds)
        prec = precision_score(y_all.iloc[va_idx], pred_binary, zero_division=0)
        rec = recall_score(y_all.iloc[va_idx], pred_binary, zero_division=0)

        aucs.append(auc)
        precs.append(prec)
        recalls.append(rec)
        logger.info(f"  fold {fold}: AUC={auc:.4f}, Prec={prec:.4f}, Rec={rec:.4f}")

    # ── 全量训练 ──
    logger.info("Training full model...")
    full_model = lgb.LGBMClassifier(
        n_estimators=600, max_depth=6, learning_rate=0.03,
        num_leaves=31, min_child_samples=50,
        class_weight='balanced', random_state=42, verbose=-1,
    )
    full_model.fit(X_all, y_all)

    # 特征重要性
    importances = dict(zip(X_all.columns, full_model.feature_importances_))
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:20]
    logger.info(f"  Top20 features: {top_features}")

    bundle = {
        'model': full_model,
        'feature_cols': list(X_all.columns),
        'v11_feature_cols': v11_feature_cols,
        'breakout_feature_cols': breakout_feature_cols,
        'extra_cols': extra_cols,
        'cv_auc_mean': float(np.mean(aucs)) if aucs else 0,
        'cv_auc_std': float(np.std(aucs)) if aucs else 0,
        'cv_prec_mean': float(np.mean(precs)) if precs else 0,
        'cv_recall_mean': float(np.mean(recalls)) if recalls else 0,
        'trained_at': datetime.now().isoformat(),
        'n_samples': int(len(y_all)),
        'version': 'v1.0',
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}")
    if aucs:
        logger.info(f"  CV AUC={np.mean(aucs):.4f}±{np.std(aucs):.4f}, "
                    f"Prec={np.mean(precs):.4f}, Rec={np.mean(recalls):.4f}")
    return bundle


# ── 推理 ─────────────────────────────────────────────────


def predict(conn, ts_codes: list, as_of_date: str,
            market_features: dict = None, sector_features: dict = None) -> dict:
    """推理: 返回每个股票的主升浪概率

    Args:
        conn: pymysql 连接
        ts_codes: 候选股票列表
        as_of_date: 预测日期
        market_features: Layer1 输出 {'bull_prob': ..., 'expected_return': ..., 'regime': ...}
        sector_features: Layer2 输出 {'board_heat': {...}}  board_name → heat_score
    """
    if not MODEL_PATH.exists():
        logger.warning(f"Model not found: {MODEL_PATH}")
        return {c: {'wave_prob': 0.5, 'is_main_wave': False} for c in ts_codes}

    bundle = joblib.load(MODEL_PATH)

    # V11 基础特征
    try:
        from sqlalchemy import create_engine

        from ml_predict import _build_features_for_stocks_v8_0
        from quant_app.utils.config import MYSQL_DATABASE, MYSQL_HOST, MYSQL_PASSWORD, MYSQL_PORT, MYSQL_USER
        engine = create_engine(
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4",
            pool_size=3, pool_recycle=3600
        )
        sql_conn = engine.connect()
        v11_df = _build_features_for_stocks_v8_0(sql_conn, ts_codes, as_of_date=as_of_date)
        sql_conn.close()
    except Exception as e:
        logger.error(f"Failed to build V11 features: {e}")
        return {c: {'wave_prob': 0.5, 'is_main_wave': False} for c in ts_codes}

    if v11_df.empty:
        return {c: {'wave_prob': 0.5, 'is_main_wave': False} for c in ts_codes}

    # 突破特征
    breakout_df = _build_breakout_features(conn, ts_codes, as_of_date)
    feat_df = v11_df.merge(breakout_df, on='ts_code', how='left')

    # 市场特征
    if market_features:
        regime = market_features.get('direction', 'range')
        feat_df['market_bull_prob'] = market_features.get('probs', {}).get('bull', 0.33)
        feat_df['market_expected_return'] = market_features.get('expected_return', 0)
        feat_df['market_is_bear'] = 1 if regime == 'bear' else 0
        feat_df['market_is_range'] = 1 if regime == 'range' else 0
        feat_df['market_is_bull'] = 1 if regime == 'bull' else 0
    else:
        feat_df['market_bull_prob'] = 0.33
        feat_df['market_expected_return'] = 0
        feat_df['market_is_bear'] = 0
        feat_df['market_is_range'] = 1
        feat_df['market_is_bull'] = 0

    # 板块特征
    board_map = _load_board_mapping(conn)
    feat_df['sector_name'] = feat_df['ts_code'].map(board_map)
    feat_df['sector_heat_score'] = 50.0
    feat_df['sector_heat_rank_pct'] = 50.0

    if sector_features and 'board_heat' in sector_features:
        for s in sector_features.get('sectors', []):
            sn = s.get('sector_name', '')
            score = s.get('heat_score', 50)
            mask = feat_df['sector_name'] == sn
            feat_df.loc[mask, 'sector_heat_score'] = score
            feat_df.loc[mask, 'sector_heat_rank_pct'] = s.get('heat_rank_pct', 50)

    # 对齐特征列并确保数值型
    feature_cols = [c for c in bundle['feature_cols'] if c in feat_df.columns]
    X = feat_df[feature_cols].copy()
    for col in X.columns:
        X[col] = pd.to_numeric(X[col], errors='coerce').fillna(0).astype(float)

    # 预测
    probs = bundle['model'].predict_proba(X)[:, 1]
    ts_codes_out = feat_df['ts_code'].tolist()

    results = {}
    for i, code in enumerate(ts_codes_out):
        p = float(probs[i])
        results[code] = {
            'wave_prob': round(p, 4),
            'is_main_wave': p > 0.5,
            'confidence': round(abs(p - 0.5) * 2, 3),
        }

    # 未覆盖的股票返回默认值
    for code in ts_codes:
        if code not in results:
            results[code] = {'wave_prob': 0.5, 'is_main_wave': False, 'confidence': 0}

    return results


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    conn = pymysql.connect(**get_db_config())
    bundle = train(conn)
    if bundle:
        print("\n=== Test predictions ===")
        codes = _load_turnover_top_n(conn, '2026-06-05', 100)
        r = predict(conn, codes, '2026-06-05')
        top = sorted(r.items(), key=lambda x: x[1]['wave_prob'], reverse=True)[:10]
        for code, info in top:
            print(f"  {code}: wave_prob={info['wave_prob']:.4f}, is_main_wave={info['is_main_wave']}")
    conn.close()
