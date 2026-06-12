"""
V11.0 推理辅助：构建特征 + 集成预测
用于 backtest_pure_ml_clean.py 加载 V11.0 模型时使用
"""
import logging
import os
import sys
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


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
        # 兼容 pymysql 和 SQLAlchemy 两种连接类型
        max_date_df = pd.read_sql(
            "SELECT MAX(trade_date) AS max_date FROM daily_price WHERE trade_date <= %s",
            conn, params=(trade_date_str,)
        )
        max_date_val = max_date_df['max_date'].iloc[0] if not max_date_df.empty else None
        if max_date_val:
            fina = pd.read_sql("""
                SELECT f.ts_code, f.roe, f.yoy_sales, f.grossprofit_margin, f.netprofit_margin, f.eps
                FROM fina_indicator f
                INNER JOIN (
                    SELECT ts_code, MAX(end_date) as max_ed
                    FROM fina_indicator WHERE end_date <= %s GROUP BY ts_code
                ) lat ON f.ts_code = lat.ts_code AND f.end_date = lat.max_ed
            """, conn, params=(max_date_val,))
            if not fina.empty:
                for c in ['roe', 'yoy_sales', 'grossprofit_margin', 'netprofit_margin', 'eps']:
                    fina[c] = pd.to_numeric(fina[c], errors='coerce').fillna(0)
                # 重命名列添加 fina_ 前缀，避免与 v8.0 基础特征中的同名列冲突
                fina = fina.rename(columns={
                    'roe': 'fina_roe', 'yoy_sales': 'fina_yoy_sales',
                    'grossprofit_margin': 'fina_gross_margin',
                    'netprofit_margin': 'fina_net_margin', 'eps': 'fina_eps',
                })
                feat = feat.merge(fina, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V11.0 fina_indicator 特征失败: {e}")

    for c in ['fina_roe', 'fina_yoy_sales', 'fina_gross_margin', 'fina_net_margin', 'fina_eps']:
        if c not in feat.columns:
            feat[c] = 0.0

    # Step 3: sector_moneyflow (通过 industry 映射)
    try:
        si = pd.read_sql(f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({placeholders})",
                         conn, params=tuple(ts_codes))
        smf = pd.read_sql("""
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
        nmf = pd.read_sql("""
            SELECT trade_date, north_money FROM north_moneyflow
            WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1
        """, conn, params=(trade_date_str,))
        nmf_val = float(nmf['north_money'].iloc[0]) / 1e8 if not nmf.empty else 0
        feat['north_flow_1d'] = nmf_val
    except Exception:
        feat['north_flow_1d'] = 0.0

    # Step 5: ml_predictions 历史预测
    try:
        mlp = pd.read_sql("""
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
                SELECT ts_code, trade_date, pct_chg, vol, turnover_rate, high, low, close
                FROM daily_price WHERE ts_code IN ({placeholders})
                AND trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 365 DAY)
                ORDER BY ts_code, trade_date
            """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
            if not hist.empty:
                hist['trade_date'] = pd.to_datetime(hist['trade_date'])
                feat = feat.copy()  # 避免 SettingWithCopyWarning
                for tc in ts_codes:
                    h = hist[hist['ts_code'] == tc].sort_values('trade_date')
                    if len(h) < 10:
                        continue
                    mask = feat['ts_code'] == tc
                    # ret_autocorr_5d
                    pct = np.asarray(h['pct_chg'].values, dtype=np.float64)
                    autocorr = float(np.corrcoef(pct[-10:-1], pct[-9:])[0, 1]) if len(pct) >= 10 else 0.0
                    feat.loc[mask, 'ret_autocorr_5d'] = autocorr
                    # volume_shock
                    vol = np.asarray(h['vol'].values, dtype=np.float64)
                    vol_ma20 = float(np.mean(vol[-21:-1])) if len(vol) >= 21 else float(np.nanmean(vol))
                    feat.loc[mask, 'volume_shock'] = float(vol[-1] / vol_ma20 - 1) if vol_ma20 > 1e-9 else 0.0
                    # high_low_spread
                    feat.loc[mask, 'high_low_spread'] = (float(h['high'].iloc[-1]) - float(h['low'].iloc[-1])) / ((float(h['high'].iloc[-1]) + float(h['low'].iloc[-1])) / 2.0 + 1e-9)
            # V11.1: price_range_pos_10d
                    if len(h) >= 10:
                        hh = float(np.max(h['high'].values[-11:-1]))
                        ll = float(np.min(h['low'].values[-11:-1]))
                        rr = hh - ll
                        cur_close = float(h['close'].values[-1])
                        feat.loc[mask, 'price_range_pos_10d'] = (cur_close - ll) / rr if rr > 1e-9 else 0.5
                    # V11.1: volume_contraction
                    if len(h) >= 21:
                        vol_arr = np.asarray(h['vol'].values[-21:-1], dtype=np.float64)
                        vol_ma20 = float(np.mean(vol_arr))
                        feat.loc[mask, 'volume_contraction'] = -(float(h['vol'].values[-1]) / vol_ma20 - 1) if vol_ma20 > 1e-9 else 0.0
                    # V11.1: price_ma50_ratio
                    if len(h) >= 50:
                        close_arr = np.asarray(h['close'].values[-51:-1], dtype=np.float64)
                        ma50 = float(np.mean(close_arr))
                        feat.loc[mask, 'price_ma50_ratio'] = float(h['close'].values[-1]) / ma50 - 1 if ma50 > 1e-9 else 0.0
                    # V11.1: oversold_boost
                    # rsi_14 和 volume_ratio 来自 V8.0 base features, 已存在 feat 中
                    if 'rsi_14' in feat.columns and 'volume_ratio' in feat.columns:
                        idx = feat.index[mask][0] if mask.any() else None
                        if idx is not None:
                            rsi_val = float(feat.loc[idx, 'rsi_14']) if pd.notna(feat.loc[idx, 'rsi_14']) else 50
                            rsi_low = 1.0 if rsi_val < 35 else 0.0
                            pp = float(feat.loc[idx, 'price_range_pos_10d']) if pd.notna(feat.loc[idx, 'price_range_pos_10d']) else 0.5
                            price_low = 1.0 if pp < 0.25 else 0.0
                            vr_val = float(feat.loc[idx, 'volume_ratio']) if pd.notna(feat.loc[idx, 'volume_ratio']) else 1.0
                            vol_shrink = 1.0 if vr_val < 0.7 else 0.0
                            feat.loc[mask, 'oversold_boost'] = (rsi_low + price_low + vol_shrink) / 3.0
                    # V11.1: ma_dispersion
                    if len(h) >= 20:
                        close_vals = np.asarray(h['close'].values[-21:-1], dtype=np.float64)
                        if len(close_vals) >= 20:
                            ma5 = np.mean(close_vals[-5:])
                            ma10 = np.mean(close_vals[-10:])
                            ma20_val = np.mean(close_vals)
                            ma_mean = (ma5 + ma10 + ma20_val) / 3.0
                            ma_std = np.std([ma5, ma10, ma20_val])
                            feat.loc[mask, 'ma_dispersion'] = ma_std / ma_mean if ma_mean > 1e-9 else 0.0
                    # V11.1: ret_vol_ratio_5d
                    if len(h) >= 5:
                        pct5 = np.asarray(h['pct_chg'].values[-6:-1], dtype=np.float64)
                        feat.loc[mask, 'ret_vol_ratio_5d'] = float(np.sum(pct5) / (np.std(pct5) + 1e-9))

                    # V11.2: std_20d
                    if len(h) >= 20:
                        pct_arr = np.asarray(h['pct_chg'].values[-21:-1], dtype=np.float64)
                        feat.loc[mask, 'std_20d'] = float(np.std(pct_arr))
                    # V11.2: cmra (12月收益极差)
                    if len(h) >= 63:
                        close_vals = np.asarray(h['close'].values[-253:-1], dtype=np.float64)
                        n = len(close_vals)
                        monthly_rets = []
                        for i in range(20, n, 21):
                            if i < n:
                                monthly_rets.append(close_vals[i] / close_vals[i-20] - 1)
                        if len(monthly_rets) >= 3:
                            zmax = float(np.max(monthly_rets[-12:]))
                            zmin = float(np.min(monthly_rets[-12:]))
                            feat.loc[mask, 'cmra'] = float(np.log((1+zmax)/(1+zmin+1e-9)))
        except Exception as e:
            logger.warning(f"V11.1 新衍生特征失败: {e}")

    # ====== V11.1 补充全局市场状态特征 ======
    if 'mkt_breadth' not in feat.columns or feat['mkt_breadth'].sum() == 0:
        try:
            from datetime import datetime as _dt
            _td = str(as_of_date)[:10] if as_of_date else _dt.now().strftime('%Y-%m-%d')
            _mkt = pd.read_sql(f"""
                SELECT trade_date, pct_chg FROM daily_price
                WHERE trade_date >= DATE_SUB('{_td}', INTERVAL 10 DAY) AND trade_date < '{_td}'
                  AND LEFT(ts_code,1) NOT IN ('8','4','9')
            """, conn)
            if not _mkt.empty:
                _mkt['is_zt'] = (_mkt['pct_chg'] > 9.5).astype(int)
                _mkt['is_dt'] = (_mkt['pct_chg'] < -9.5).astype(int)
                _daily = _mkt.groupby('trade_date').agg(
                    zt_cnt=('is_zt','sum'), dt_cnt=('is_dt','sum'),
                    ret_std=('pct_chg','std'), above_zero=('pct_chg', lambda x: (x>0).mean())
                ).reset_index()
                _daily['spread'] = _daily['zt_cnt'] - _daily['dt_cnt']
                _latest = _daily.iloc[-1]
                mkt_breadth_val = float(_daily['above_zero'].tail(5).mean())
                mkt_vol_val = float(_daily['ret_std'].tail(5).mean())
                mkt_spread_val = float(_daily['spread'].tail(5).mean())
                feat['mkt_zt_dt_spread'] = mkt_spread_val
                feat['mkt_volatility'] = mkt_vol_val
                feat['mkt_breadth'] = mkt_breadth_val
        except Exception:
            pass

    for c in ['price_range_pos_10d', 'oversold_boost', 'ma_dispersion',
              'volume_contraction', 'price_ma50_ratio', 'ret_vol_ratio_5d',
              'mkt_zt_dt_spread', 'mkt_volatility', 'mkt_breadth',
              'ret_autocorr_5d', 'volume_shock', 'high_low_spread', 'vol_of_vol',
              'max_drawdown_10d', 'rel_strength_idx_5d', 'turnover_zscore_20d',
              'consecutive_wins', 'consecutive_losses', 'ml_pred_chg_5d',
              'sector_netflow_5d', 'sector_pct_5d', 'sector_flow_divergence',
              'money_flow_div_5d', 'money_flow_div_10d', 'flow_accel_5d',
              'std_20d', 'cmra']:
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

    # 补全缺失的股票（特征构建失败时用中位数填充）
    missing_codes = [c for c in ts_codes if c not in feat['ts_code'].values]
    if missing_codes:
        logger.warning(f"特征构建缺失 {len(missing_codes)} 只股票，用中位数填充")
        for mc in missing_codes:
            row_data = {'ts_code': mc}
            for c in fcols:
                row_data[c] = _v11_medians.get(c, 0.0) if _v11_medians else 0.0
            feat = pd.concat([feat, pd.DataFrame([row_data])], ignore_index=True)

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
