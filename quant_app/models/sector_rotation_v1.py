"""
Stage 2: 热点板块预测
  - 改造自 sector_rotation.get_hot_sectors, 加:
    1. 动量延续性特征 (历史 3/5/10 日涨幅/资金流 序列)
    2. 突破特征 (板块自身的技术突破)
    3. LightGBM Ranker 训练目标: 未来 3 日板块涨幅排名
"""
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent.parent / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "sector_rotation_v1.pkl"


def _load_sector_history(conn, lookback: int = 60) -> pd.DataFrame:
    """加载所有板块近 N 日资金流 + 涨幅"""
    sql = """
        SELECT trade_date, sector_name, net_amount, pct_change, buy_elg_amount + sell_elg_amount AS total_trade
        FROM sector_moneyflow
        WHERE trade_date >= (SELECT MAX(trade_date) FROM sector_moneyflow) - INTERVAL %s DAY
        ORDER BY sector_name, trade_date DESC
    """
    return pd.read_sql(sql, conn, params=(lookback,), parse_dates=['trade_date'])


def _build_sector_features(sector_data: pd.DataFrame) -> dict:
    """单板块特征 (per sector, per day)"""
    if len(sector_data) < 10:
        return None
    nets = sector_data['net_amount'].values
    pcts = sector_data['pct_change'].values
    trades = sector_data['total_trade'].values

    # 连续净流入天数
    continuous = 0
    for n in nets:
        if n > 0: continuous += 1
        else: break
    # 资金趋势
    if len(nets) >= 6:
        recent_avg = np.mean(nets[:3])
        older_avg = np.mean(nets[3:6])
        trend_score = (recent_avg / (older_avg + 1e-6)) - 1
    else:
        trend_score = 0
    # 量能趋势
    if len(trades) >= 6:
        vol_recent = np.mean(trades[:3])
        vol_older = np.mean(trades[3:6])
        vol_ratio = vol_recent / (vol_older + 1e-6)
    else:
        vol_ratio = 1.0
    # 涨幅趋势
    pct_3d = np.sum(pcts[:3]) if len(pcts) >= 3 else 0
    pct_5d = np.sum(pcts[:5]) if len(pcts) >= 5 else 0
    pct_10d = np.sum(pcts[:10]) if len(pcts) >= 10 else 0
    # 突破: 板块涨幅 3 日累计 > 5%
    pct_breakout = int(pct_3d >= 5.0)

    return {
        'sector_continuous': continuous,
        'sector_net_total_5d': float(np.sum(nets[:5])),
        'sector_trend': float(trend_score),
        'sector_vol_ratio': float(vol_ratio),
        'sector_pct_3d': float(pct_3d),
        'sector_pct_5d': float(pct_5d),
        'sector_pct_10d': float(pct_10d),
        'sector_pct_breakout': pct_breakout,
    }


def predict(conn, as_of_date: str, top_n: int = 8) -> list[dict]:
    """推理: 输出 TopN 热点板块"""
    end_date = pd.Timestamp(as_of_date)
    start_date = end_date - timedelta(days=60)
    df = _load_sector_history(conn)
    df = df[(df['trade_date'] >= start_date) & (df['trade_date'] <= end_date)]

    if df.empty:
        return []

    # 按板块取截止 as_of_date 的数据
    sector_features = []
    for name, g in df.groupby('sector_name', sort=False):
        g = g.sort_values('trade_date', ascending=False)
        # 只取 as_of_date 及之前
        g = g[g['trade_date'] <= end_date]
        if g.empty: continue
        feats = _build_sector_features(g)
        if feats is None: continue
        feats['sector_name'] = name
        sector_features.append(feats)

    df_feat = pd.DataFrame(sector_features)
    if df_feat.empty:
        return []

    # 加载模型 (如果有)
    if MODEL_PATH.exists():
        bundle = joblib.load(MODEL_PATH)
        feat_cols = bundle['feature_cols']
        X = df_feat[feat_cols].values
        scores = bundle['model'].predict(X)
        df_feat['score'] = scores
    else:
        # Fallback: 沿用旧 get_hot_sectors 评分
        score = (
            df_feat['sector_continuous'] * 5
            + np.log10(df_feat['sector_net_total_5d'].clip(lower=0) + 1) * 5
            + df_feat['sector_pct_3d'].clip(lower=0) * 5
            + (df_feat['sector_vol_ratio'] - 1).clip(lower=0) * 10
        )
        df_feat['score'] = score

    df_feat = df_feat.sort_values('score', ascending=False).head(top_n)
    return df_feat[['sector_name', 'score', 'sector_continuous', 'sector_pct_3d', 'sector_pct_breakout']].to_dict('records')


if __name__ == '__main__':
    import pymysql
    conn = pymysql.connect(**get_db_config())
    print(predict(conn, '2026-06-03'))
