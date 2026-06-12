"""
V11.2 推理辅助：使用 V11.0 特征 + V11.2 轻量模型 + 市场状态交互特征
"""
import logging
import os
import sys
from datetime import datetime

import joblib
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from scripts.predict_v11 import build_features_v11_inference

_v11_2_bundle = None

def load_v11_2_model():
    global _v11_2_bundle
    if _v11_2_bundle is None:
        model_path = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_2.pkl')
        _v11_2_bundle = joblib.load(model_path)
        logger.info(f"V11.2 模型已加载: {_v11_2_bundle.get('version','?')}, "
                    f"{len(_v11_2_bundle.get('feature_cols',[]))}特征")
    return _v11_2_bundle

def predict_v11_2(conn, ts_codes, as_of_date):
    """
    V11.2 预测管线：
    1. 加载 V11.0 特征（通过 V11.0 模型的 build_features_v11_inference）
    2. 加载当前市场状态
    3. 构建交互特征
    4. V11.2 模型预测
    """
    bundle = load_v11_2_model()
    feature_cols = bundle['feature_cols']

    # Step 1: V11.0 features
    feat = build_features_v11_inference(conn, ts_codes, as_of_date=as_of_date)
    if feat is None or feat.empty:
        return None, None

    # Step 2: Current market regime
    try:
        trade_date_str = str(as_of_date)[:10] if as_of_date else datetime.now().strftime('%Y-%m-%d')
        cur = conn.cursor()
        cur.execute("""
            SELECT prob_bull, prob_bear, prob_panic, prob_range, prob_overheated,
                   sh_trend, market_breadth, volatility, volume_trend, momentum_breadth, zt_ratio
            FROM market_regime_daily
            WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 1
        """, (trade_date_str,))
        row = cur.fetchone()
        cur.close()
        if row:
            rp = {
                'prob_bull': float(row[0] or 0), 'prob_bear': float(row[1] or 0),
                'prob_panic': float(row[2] or 0), 'prob_range': float(row[3] or 0),
                'prob_overheated': float(row[4] or 0),
                'sh_trend': float(row[5] or 0), 'market_breadth': float(row[6] or 0),
                'volatility': float(row[7] or 0), 'volume_trend': float(row[8] or 0),
                'momentum_breadth': float(row[9] or 0), 'zt_ratio': float(row[10] or 0),
            }
        else:
            rp = {k: 0.0 for k in ['prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                                   'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio']}
    except Exception:
        rp = {k: 0.0 for k in ['prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                               'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio']}

    # Step 3: Add regime features
    for k, v in rp.items():
        feat[k] = v

    feat['chg_5d_x_panic'] = feat.get('chg_5d', 0) * rp.get('prob_panic', 0)
    feat['chg_10d_x_panic'] = feat.get('chg_10d', 0) * rp.get('prob_panic', 0)
    feat['rsi_14_x_panic'] = feat.get('rsi_14', 0) * rp.get('prob_panic', 0)
    feat['volume_ratio_x_panic'] = feat.get('volume_ratio', 0) * rp.get('prob_panic', 0)
    if 'main_net_ratio' in feat.columns:
        feat['main_net_ratio_x_panic'] = feat['main_net_ratio'] * rp.get('prob_panic', 0)
    if 'vol_price_divergence' in feat.columns:
        feat['vol_price_divergence_x_panic'] = feat['vol_price_divergence'] * rp.get('prob_panic', 0)
    feat['volume_ratio_x_overheated'] = feat.get('volume_ratio', 0) * rp.get('prob_overheated', 0)
    feat['chg_5d_x_bull'] = feat.get('chg_5d', 0) * rp.get('prob_bull', 0)
    feat['breadth_x_main_net'] = rp.get('market_breadth', 0) * feat.get('main_net_ratio', 0)
    feat['volatility_x_rsi'] = rp.get('volatility', 0) * feat.get('rsi_14', 0)

    # Fill missing features
    for col in feature_cols:
        if col not in feat.columns:
            feat[col] = bundle['global_medians'].get(col, 0.0)
    feat = feat.fillna(0)

    # Step 4: Predict
    X = feat[feature_cols].values.astype(np.float32)
    preds = bundle['model'].predict(X)

    return feat, preds
