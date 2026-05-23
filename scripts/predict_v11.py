"""
V11.0 推理辅助：构建特征 + 集成预测
用于 backtest_pure_ml_clean.py 加载 V11.0 模型时使用
"""
import numpy as np, pandas as pd, pymysql, logging, os, sys, json
from datetime import datetime, timedelta
import joblib

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.config import get_db_config
from quant_app.utils.model_loader import load_model

# 缓存
_v11_bundle = None
_v11_feature_cols = None
_v11_medians = None


def load_v11_model(model_path):
    global _v11_bundle, _v11_feature_cols, _v11_medians
    _v11_bundle = joblib.load(model_path)
    _v11_feature_cols = _v11_bundle['feature_cols']
    _v11_medians = _v11_bundle.get('global_medians', {})
    logger.info(f"V11.0 模型已加载: {_v11_bundle.get('n_models', '?')}个子模型, "
                f"{len(_v11_feature_cols)}特征")
    return _v11_bundle


def build_features_v11_inference(conn, ts_codes, as_of_date):
    """
    为指定股票列表构建 V11.0 全套特征（生产推理版，非训练版）
    基于 _build_features_for_stocks_v8_0 + V11.0 新增特征
    """
    from ml_predict import _build_features_for_stocks_v8_0

    # Step 1: 构建 V8.0 基础特征
    feat = _build_features_for_stocks_v8_0(conn, ts_codes, as_of_date=as_of_date)
    if feat is None or feat.empty:
        return feat

    trade_date_str = str(as_of_date)[:10] if as_of_date else datetime.now().strftime('%Y-%m-%d')
    placeholders = ','.join(['%s'] * len(ts_codes))

    # Step 2: fina_indicator (最近一期)
    try:
        cur = conn.connection.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date <= %s", (trade_date_str,))
        max_date = cur.fetchone()[0]
        cur.close()
        if max_date:
            fina = pd.read_sql(f"""
                SELECT f.ts_code, f.roe, f.yoy_sales, f.grossprofit_margin, f.netprofit_margin, f.eps
                FROM fina_indicator f
                INNER JOIN (
                    SELECT ts_code, MAX(end_date) as max_ed
                    FROM fina_indicator WHERE end_date <= %s GROUP BY ts_code
                ) lat ON f.ts_code = lat.ts_code AND f.end_date = lat.max_ed
            """, conn, params=(max_date,))
            if not fina.empty:
                for c in ['roe', 'yoy_sales', 'grossprofit_margin', 'netprofit_margin', 'eps']:
                    fina[c] = pd.to_numeric(fina[c], errors='coerce').fillna(0)
                feat = feat.merge(fina, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V11.0 fina_indicator 特征失败: {e}")

    for c in ['fina_roe', 'fina_yoy_sales', 'fina_gross_margin', 'fina_net_margin', 'fina_eps']:
        if c not in feat.columns:
            feat[c] = 0.0

    # Step 3: sector_moneyflow (通过 industry 映射)
    try:
        si = pd.read_sql(f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({placeholders})",
                         conn, params=ts_codes)
        smf = pd.read_sql(f"""
            SELECT trade_date, sector_name, net_amount, buy_elg_amount, sell_elg_amount, pct_change
            FROM sector_moneyflow
            WHERE trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
        """, conn, params=(trade_date_str, trade_date_str))
        if not si.empty and not smf.empty:
            smf['trade_date'] = pd.to_datetime(smf['trade_date'])
            smf['elg_net'] = smf['buy_elg_amount'] - smf['sell_elg_amount']
            latest_smf = smf.sort_values('trade_date').groupby('sector_name').last().reset_index()
            si_smf = si.merge(latest_smf, left_on='industry', right_on='sector_name', how='left')
            feat = feat.merge(si_smf[['ts_code', 'net_amount', 'elg_net', 'pct_change']].rename(
                columns={'net_amount': 'sector_netflow_1d', 'elg_net': 'sector_elg_net',
                         'pct_change': 'sector_pct_1d'}),
                on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V11.0 sector_moneyflow 特征失败: {e}")

    for c in ['sector_netflow_1d', 'sector_elg_net', 'sector_pct_1d']:
        if c not in feat.columns:
            feat[c] = 0.0

    # Step 4: north_moneyflow
    try:
        nmf = pd.read_sql(f"""
            SELECT trade_date, north_money FROM north_moneyflow
            WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1
        """, conn, params=(trade_date_str,))
        nmf_val = float(nmf['north_money'].iloc[0]) / 1e8 if not nmf.empty else 0
        feat['north_flow_1d'] = nmf_val
    except Exception:
        feat['north_flow_1d'] = 0.0

    # Step 5: ml_predictions 历史预测
    try:
        mlp = pd.read_sql(f"""
            SELECT ts_code, trade_date, _ml_pred FROM ml_predictions
            WHERE trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)
            ORDER BY ts_code, trade_date DESC
        """, conn, params=(trade_date_str, trade_date_str))
        if not mlp.empty:
            mlp = mlp.drop_duplicates(subset='ts_code', keep='first')
            feat = feat.merge(mlp[['ts_code', '_ml_pred']].rename(columns={'_ml_pred': 'ml_pred_prev'}),
                              on='ts_code', how='left')
    except Exception:
        pass
    if 'ml_pred_prev' not in feat.columns:
        feat['ml_pred_prev'] = 0.5

    # Step 6: 新增价格衍生特征
    if 'ret_autocorr_5d' not in feat.columns:
        try:
            hist = pd.read_sql(f"""
                SELECT ts_code, trade_date, pct_chg, vol, turnover_rate, high, low
                FROM daily_price WHERE ts_code IN ({placeholders})
                AND trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                ORDER BY ts_code, trade_date
            """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
            if not hist.empty:
                hist['trade_date'] = pd.to_datetime(hist['trade_date'])
                for tc in ts_codes:
                    h = hist[hist['ts_code'] == tc].sort_values('trade_date')
                    if len(h) < 10:
                        continue
                    mask = feat['ts_code'] == tc
                    # ret_autocorr_5d
                    pct = h['pct_chg'].values
                    autocorr = np.corrcoef(pct[-10:-1], pct[-9:])[0, 1] if len(pct) >= 10 else 0
                    feat.loc[mask, 'ret_autocorr_5d'] = np.nan_to_num(autocorr, 0)
                    # volume_shock
                    vol = h['vol'].values
                    vol_ma20 = np.mean(vol[-21:-1]) if len(vol) >= 21 else np.mean(vol)
                    feat.loc[mask, 'volume_shock'] = (vol[-1] / vol_ma20 - 1) if vol_ma20 > 0 else 0
                    # high_low_spread
                    feat.loc[mask, 'high_low_spread'] = (h['high'].iloc[-1] - h['low'].iloc[-1]) / \
                                                         ((h['high'].iloc[-1] + h['low'].iloc[-1]) / 2 + 1e-9)
        except Exception as e:
            logger.warning(f"V11.0 新衍生特征失败: {e}")

    for c in ['ret_autocorr_5d', 'volume_shock', 'high_low_spread', 'vol_of_vol',
              'max_drawdown_10d', 'rel_strength_idx_5d', 'turnover_zscore_20d',
              'consecutive_wins', 'consecutive_losses', 'ml_pred_chg_5d',
              'sector_netflow_5d', 'sector_pct_5d', 'sector_flow_divergence',
              'money_flow_div_5d', 'money_flow_div_10d', 'flow_accel_5d']:
        if c not in feat.columns:
            feat[c] = 0.0

    # 用中位数填充缺失
    fcols = _v11_feature_cols
    if fcols is None:
        # 延迟初始化：从已加载的模型获取
        from quant_app.utils.model_loader import get_model_path
        model_path = get_model_path("v11.0")
        if model_path and model_path.exists():
            load_v11_model(str(model_path))
            fcols = _v11_feature_cols
    if fcols is None:
        logger.warning("V11.0 特征列未初始化，跳过填充")
    else:
        for c in fcols:
            if c not in feat.columns:
                feat[c] = _v11_medians.get(c, 0.0) if _v11_medians else 0.0

    return feat


def predict_v11(conn, ts_codes, as_of_date=None):
    """V11.0 集成预测：加载模型 → 构建特征 → 集成预测"""
    if _v11_bundle is None:
        logger.error("V11.0 模型未加载，请先调用 load_v11_model()")
        return None

    bundle = _v11_bundle
    feat = build_features_v11_inference(conn, ts_codes, as_of_date)
    if feat is None or feat.empty:
        return None

    from ml_predict import _ensemble_predict
    preds = _ensemble_predict(feat, bundle)
    return preds
