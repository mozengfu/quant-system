"""
TopDown V1 — Layer 2: 热点板块 (SectorHeat)

预测未来3日哪些板块会成为热点，输出板块热度排序。
使用 sector_moneyflow + board_industry_cons + daily_price 聚合。

模型: LightGBM LambdaRank (10级标签)
特征: ~25维 (板块动量/资金流/宽度/轮动/相对强度)
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
MODEL_PATH = MODEL_DIR / "sector_heat_v1.pkl"


def _load_board_list(conn) -> pd.DataFrame:
    """加载行业板块列表"""
    return pd.read_sql("SELECT board_code, board_name FROM board_industry", conn)


def _load_board_cons(conn) -> pd.DataFrame:
    """加载板块-成分股映射"""
    return pd.read_sql(
        "SELECT board_code, ts_code FROM board_industry_cons WHERE is_latest=1",
        conn
    )


def _load_sector_data(conn, start_date, end_date) -> pd.DataFrame:
    """加载板块资金流+日收益数据（从 sector_moneyflow）"""
    df = pd.read_sql("""
        SELECT trade_date, sector_name, net_amount, buy_elg_amount,
               sell_elg_amount, pct_change
        FROM sector_moneyflow
        WHERE trade_date BETWEEN %s AND %s
        ORDER BY trade_date
    """, conn, params=(start_date, end_date), parse_dates=['trade_date'])
    for col in ['net_amount', 'buy_elg_amount', 'sell_elg_amount', 'pct_change']:
        df[col] = df[col].astype(float)
    return df


def _build_sector_features(sector_df: pd.DataFrame, sector_name: str,
                           trade_date, lookback: int = 60) -> dict:
    """为单个板块构建特征向量 (~25维)"""
    sec = sector_df[sector_df['sector_name'] == sector_name].sort_values('trade_date')
    if len(sec) < 20:
        return None

    # 定位到当前日期
    sec = sec[sec['trade_date'] <= trade_date]
    if len(sec) < 20:
        return None

    pct = sec['pct_change'].astype(float)
    net = sec['net_amount'].astype(float)
    buy_elg = sec['buy_elg_amount'].astype(float)
    sell_elg = sec['sell_elg_amount'].astype(float)

    feats = {}

    # ── 板块动量 (5维) ──
    for n in [1, 3, 5, 10, 20]:
        if len(pct) > n:
            feats[f'sector_ret_{n}d'] = float(pct.iloc[-n:].mean())
        else:
            feats[f'sector_ret_{n}d'] = 0

    # ── 资金流 (6维) ──
    feats['sector_net_1d'] = float(net.iloc[-1])
    feats['sector_net_3d'] = float(net.tail(3).sum())
    feats['sector_net_5d'] = float(net.tail(5).sum())
    feats['sector_elg_net_1d'] = float(buy_elg.iloc[-1] - sell_elg.iloc[-1])
    feats['sector_elg_net_3d'] = float(buy_elg.tail(3).sum() - sell_elg.tail(3).sum())
    # 资金流加速
    feats['sector_flow_accel'] = float(net.tail(3).mean() - net.tail(10).mean())

    # ── 板块波动 (2维) ──
    feats['sector_vol_5d'] = float(pct.tail(5).std())
    feats['sector_vol_20d'] = float(pct.tail(20).std())

    # ── 持续性 (3维) ──
    feats['sector_up_days_5d'] = int((pct.tail(5) > 0).sum())
    feats['sector_consecutive_up'] = 0
    for i in range(len(pct)-1, max(len(pct)-10, -1), -1):
        if pct.iloc[i] > 0:
            feats['sector_consecutive_up'] += 1
        else:
            break
    feats['sector_net_inflow_days'] = int((net.tail(5) > 0).sum())

    # ── 收益加速度 (2维) ──
    feats['sector_ret_accel'] = feats['sector_ret_5d'] - feats['sector_ret_10d']
    feats['sector_ret_vol_ratio'] = (
        feats['sector_ret_5d'] / feats['sector_vol_5d']
        if feats['sector_vol_5d'] > 0 else 0
    )

    # ── 资金流/收益背离 (1维) ──
    if feats['sector_ret_5d'] != 0:
        feats['sector_flow_yield'] = feats['sector_net_5d'] / abs(feats['sector_ret_5d'])
    else:
        feats['sector_flow_yield'] = 0

    # ── 排名变化 (2维) ──
    feats['sector_mom_rank'] = 0  # 占位，训练时填充
    feats['sector_flow_rank'] = 0

    # ── 相对大盘强度 (2维) ──
    feats['sector_rel_strength'] = 0  # 占位
    feats['sector_trend_score'] = 0

    return feats


def _label_sector_heat(returns_3d: float) -> int:
    """板块标签: 按日横截面 qcut 10级，此处做单值转换"""
    # 实际 qcut 在训练时做跨板块分组
    return max(0, min(9, int((returns_3d + 5) / 1.0)))  # 临时映射，训练时会替换


def _compute_cross_sectional_features(sectors_features: dict) -> dict:
    """计算横截面对比特征（排名、相对强度等）"""
    if len(sectors_features) < 3:
        return sectors_features

    ret_5d_vals = {k: v['sector_ret_5d'] for k, v in sectors_features.items()
                   if v is not None and 'sector_ret_5d' in v}
    ret_20d_vals = {k: v['sector_ret_20d'] for k, v in sectors_features.items()
                    if v is not None and 'sector_ret_20d' in v}
    flow_vals = {k: v['sector_net_5d'] for k, v in sectors_features.items()
                 if v is not None and 'sector_net_5d' in v}

    if len(ret_5d_vals) < 3:
        return sectors_features

    # 排名 (0~1, 1=最强)
    ret_5d_rank = {k: (sorted(ret_5d_vals.values()).index(v)+1)/len(ret_5d_vals)
                   for k, v in ret_5d_vals.items()}
    ret_20d_rank = {k: (sorted(ret_20d_vals.values()).index(v)+1)/len(ret_20d_vals)
                    for k, v in ret_20d_vals.items()}
    flow_rank = {k: (sorted(flow_vals.values()).index(v)+1)/len(flow_vals)
                 for k, v in flow_vals.items()}

    avg_ret_5d = np.mean(list(ret_5d_vals.values()))

    for name in sectors_features:
        if sectors_features[name] is None:
            continue
        feats = sectors_features[name]
        feats['sector_mom_rank'] = ret_5d_rank.get(name, 0.5)
        feats['sector_flow_rank'] = flow_rank.get(name, 0.5)
        # 相对强度 = 板块收益 - 平均收益
        feats['sector_rel_strength'] = feats['sector_ret_5d'] - avg_ret_5d
        # 趋势得分 = 短期排名 + 长期排名 加权
        feats['sector_trend_score'] = 0.6 * ret_5d_rank.get(name, 0.5) + 0.4 * ret_20d_rank.get(name, 0.5)

    return sectors_features


def train(conn, start_date='2024-01-01', end_date='2025-09-30'):
    """训练板块热度 LambdaRank 模型（从 daily_price 聚合，覆盖全时间段）"""
    import lightgbm as lgb

    from quant_app.models.sector_features import build_sector_features_for_heat

    logger.info("Building sector features from daily_price aggregation...")
    feat_df = build_sector_features_for_heat(conn)

    # 过滤时间范围
    feat_df = feat_df[(feat_df['trade_date'] >= start_date) & (feat_df['trade_date'] <= end_date)]
    logger.info(f"  Filtered to {start_date}~{end_date}: {len(feat_df)} rows")

    # 特征列（排除非特征列）
    exclude_cols = ['trade_date', 'board_code', 'board_name', 'future_ret_3d', 'label']
    feature_cols = [c for c in feat_df.columns if c not in exclude_cols]

    # 按日期排序
    feat_df = feat_df.sort_values('trade_date')
    dates_all = [str(d)[:10] for d in feat_df['trade_date']]
    X_all = feat_df[feature_cols].values.astype(float)
    y_all = feat_df['label'].values.astype(int)

    logger.info(f"Training: X={X_all.shape}, y distribution={np.bincount(y_all)}")
    n_classes = len(np.unique(y_all))
    logger.info(f"  {n_classes} classes, {len(set(dates_all))} unique dates")

    # q_group 按日期分组
    date_arr = np.array(dates_all)
    unique_dates = sorted(set(dates_all))
    q_groups = [int((date_arr == d).sum()) for d in unique_dates]

    # ── 时序CV ──
    n_dates = len(unique_dates)
    fold_size = n_dates // 5
    rank_ics = []

    for fold in range(5):
        va_start = fold * fold_size
        va_end = min((fold + 1) * fold_size, n_dates)
        if va_start >= n_dates - 3:
            break

        va_dates = set(unique_dates[va_start:va_end])
        tr_idx = [j for j, d in enumerate(dates_all) if d not in va_dates]
        va_idx = [j for j, d in enumerate(dates_all) if d in va_dates]

        if len(tr_idx) < 500 or len(va_idx) < 100:
            continue

        tr_dates = [dates_all[j] for j in tr_idx]
        tr_q_groups = [int((np.array(tr_dates) == d).sum()) for d in sorted(set(tr_dates))]

        model = lgb.LGBMRanker(
            n_estimators=400, max_depth=6, learning_rate=0.03,
            num_leaves=31, min_child_samples=30, random_state=42, verbose=-1,
            objective='lambdarank', metric='ndcg',
        )
        model.fit(X_all[tr_idx], y_all[tr_idx], group=tr_q_groups)

        preds = model.predict(X_all[va_idx])
        va_labels = y_all[va_idx]
        if len(np.unique(va_labels)) > 1:
            ic = np.corrcoef(preds, va_labels)[0, 1]
            rank_ics.append(ic)
            logger.info(f"  fold {fold}: RankIC={ic:.4f}, n_tr={len(tr_idx)}, n_va={len(va_idx)}")

    # ── 全量训练 ──
    logger.info("Training full model...")
    full_model = lgb.LGBMRanker(
        n_estimators=600, max_depth=6, learning_rate=0.03,
        num_leaves=31, min_child_samples=30, random_state=42, verbose=-1,
        objective='lambdarank', metric='ndcg',
    )
    full_model.fit(X_all, y_all, group=q_groups)

    bundle = {
        'model': full_model,
        'feature_names': feature_cols,
        'cv_rank_ic_mean': float(np.mean(rank_ics)) if rank_ics else 0,
        'cv_rank_ic_std': float(np.std(rank_ics)) if rank_ics else 0,
        'trained_at': datetime.now().isoformat(),
        'n_samples': int(len(y_all)),
        'version': 'v1.1',
    }
    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"Saved → {MODEL_PATH}")
    if rank_ics:
        logger.info(f"  CV RankIC={np.mean(rank_ics):.4f}±{np.std(rank_ics):.4f}")
    return bundle


def predict(conn, as_of_date: str) -> dict:
    """推理: 返回板块热度排序（从 daily_price 聚合特征）"""
    if not MODEL_PATH.exists():
        logger.warning(f"Model not found: {MODEL_PATH}")
        return {'sectors': [], 'top_sectors': []}

    bundle = joblib.load(MODEL_PATH)

    from quant_app.models.sector_features import build_sector_daily

    sector_daily = build_sector_daily(conn)

    # 获取 as_of_date 当天的板块特征
    day_data = sector_daily[sector_daily['trade_date'] == as_of_date]
    if len(day_data) < 5:
        return {'sectors': [], 'top_sectors': []}

    # 构建特征向量（简化版：仅用当日可直接计算的列）
    # 动量特征需要历史序列，这里做近似
    feature_cols_in_model = bundle['feature_names']
    X_pred_rows = []

    for _, row in day_data.iterrows():
        bd_code = row['board_code']
        bd_name = row['board_name']

        # 获取该板块的历史序列
        hist = sector_daily[
            (sector_daily['board_code'] == bd_code) &
            (sector_daily['trade_date'] <= as_of_date)
        ].sort_values('trade_date')

        if len(hist) < 20:
            continue

        rets = hist['sector_ret'].values
        amounts = hist['sector_amount'].values

        feats = {}
        # 动量
        for n in [1, 3, 5, 10, 20]:
            feats[f'sector_ret_{n}d'] = float(np.mean(rets[-n:])) if len(rets) >= n else 0
        # 资金流
        feats['sector_net_1d'] = float(amounts[-1])
        feats['sector_net_3d'] = float(np.sum(amounts[-3:]))
        feats['sector_net_5d'] = float(np.sum(amounts[-5:]))
        feats['sector_elg_net_1d'] = 0
        feats['sector_elg_net_3d'] = 0
        feats['sector_flow_accel'] = float(np.mean(amounts[-3:]) - np.mean(amounts[-10:])) if len(amounts) >= 10 else 0
        # 波动
        feats['sector_vol_5d'] = float(np.std(rets[-5:])) if len(rets) >= 5 else 0
        feats['sector_vol_20d'] = float(np.std(rets[-20:])) if len(rets) >= 20 else 0
        # 持续性
        feats['sector_up_days_5d'] = int(np.sum(rets[-5:] > 0))
        feats['sector_consecutive_up'] = int(_count_consecutive_up(rets))
        feats['sector_net_inflow_days'] = int(np.sum(np.diff(amounts[-6:]) > 0))
        # 加速度
        feats['sector_ret_accel'] = feats['sector_ret_5d'] - feats['sector_ret_10d']
        feats['sector_ret_vol_ratio'] = _safe_div(feats['sector_ret_5d'], feats['sector_vol_5d'])
        feats['sector_flow_yield'] = _safe_div(feats['sector_net_5d'], abs(feats['sector_ret_5d']))
        # 占位
        feats['sector_mom_rank'] = 0
        feats['sector_flow_rank'] = 0
        feats['sector_rel_strength'] = 0
        feats['sector_trend_score'] = 0

        # 对齐模型特征
        aligned = {k: feats.get(k, 0) for k in feature_cols_in_model}
        X_pred_rows.append((bd_name, list(aligned.values())))

    if not X_pred_rows:
        return {'sectors': [], 'top_sectors': []}

    names = [x[0] for x in X_pred_rows]
    X_pred = np.array([x[1] for x in X_pred_rows], dtype=float)
    scores = bundle['model'].predict(X_pred)

    results = []
    for i, sn in enumerate(names):
        results.append({
            'sector_name': sn,
            'heat_score': round(float(scores[i]) * 100, 1),
            'heat_rank_pct': 0,
        })

    results.sort(key=lambda x: x['heat_score'], reverse=True)
    max_score = max(r['heat_score'] for r in results) if results else 1
    min_score = min(r['heat_score'] for r in results) if results else 0
    for i, r in enumerate(results):
        r['heat_rank_pct'] = round((i + 1) / len(results) * 100, 1)
        if max_score > min_score:
            r['heat_score'] = round((r['heat_score'] - min_score) / (max_score - min_score) * 100, 1)

    return {
        'sectors': results,
        'top_sectors': [r['sector_name'] for r in results[:5]],
        'top5_scores': {r['sector_name']: r['heat_score'] for r in results[:5]},
    }


def _count_consecutive_up(arr):
    count = 0
    for v in reversed(arr):
        if v > 0:
            count += 1
        else:
            break
    return count


def _safe_div(a, b):
    return float(a / b) if b != 0 else 0.0


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    conn = pymysql.connect(**get_db_config())
    bundle = train(conn)
    print("\n=== Test predictions ===")
    for d in ['2026-05-15', '2026-06-05', '2026-06-09']:
        r = predict(conn, d)
        print(f"  {d}: top5={r['top_sectors']}")
    conn.close()
