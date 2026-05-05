#!/usr/bin/env python3
"""
ML选股模型推理 V3 — 横截面Z-Score标准化版
兼容 ml_train_v3.py 训练的模型
核心：拉全市场数据 → 计算原始特征 → 横截面Z-Score → 预测
"""

import os, logging, warnings
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import numpy as np, pandas as pd, pymysql, joblib

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')

DB_CONFIG = {
    'host': 'localhost', 'unix_socket': '/tmp/mysql.sock',
    'user': 'root', 'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db', 'connect_timeout': 5,
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
V3_MODEL_PATH = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v3.pkl')
GENERAL_MODEL_PATH = os.path.join(BASE_DIR, 'data', 'ml_stock_model.pkl')
BEAR_MODEL_PATH = os.path.join(BASE_DIR, 'data', 'ml_bear_model.pkl')

_v3_bundle = None
_general_bundle = None
_bear_bundle = None
_market_state = None

EXCLUDE_PREFIXES = ('68', '83', '87', '8', '4', '9', '16')

# Raw features used by V3 (before Z-Score)
V3_RAW_FEATURES = [
    'pct_chg','turnover_rate','volume_ratio',
    'vol_5d','vol_10d','vol_20d',
    'ma5_ma10_ratio','ma10_ma20_ratio','price_ma5_ratio','price_ma20_ratio',
    'chg_3d','chg_5d','chg_10d','vol_trend','pos_52w',
    'rps_20','rps_change','up_ratio_5d','up_ratio_10d','vol_pct_corr',
    'ma_pattern',
    'macd_diff','macd_hist','rsi_14',
    'main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
    'main_streak','main_vs_retail','main_cum5','main_cum10',
    'idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d',
    'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
    'amihud', 'amihud_ma5',
    'main_accel_3d', 'smart_div_count'
]


def _load_v3_model():
    global _v3_bundle
    if _v3_bundle is None:
        if not os.path.exists(V3_MODEL_PATH):
            logger.warning(f"V3模型文件不存在: {V3_MODEL_PATH}")
            return None
        try:
            _v3_bundle = joblib.load(V3_MODEL_PATH)
            logger.info(f"V3 ML模型已加载: {_v3_bundle.get('trained_at', 'unknown')}")
        except Exception as e:
            logger.error(f"V3模型加载失败: {e}")
            return None
    return _v3_bundle


def _load_general_model():
    global _general_bundle
    if _general_bundle is None:
        if not os.path.exists(GENERAL_MODEL_PATH):
            return None
        try:
            _general_bundle = joblib.load(GENERAL_MODEL_PATH)
        except Exception as e:
            logger.error(f"通用模型加载失败: {e}")
            return None
    return _general_bundle


def _load_bear_model():
    global _bear_bundle
    if _bear_bundle is None:
        if not os.path.exists(BEAR_MODEL_PATH):
            return None
        try:
            _bear_bundle = joblib.load(BEAR_MODEL_PATH)
        except Exception as e:
            logger.error(f"逆市模型加载失败: {e}")
            return None
    return _bear_bundle


def get_market_state(db_conn=None):
    """获取大盘状态"""
    global _market_state
    if _market_state is not None:
        return _market_state
    
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True
    
    try:
        cur = db_conn.cursor()
        cur.execute("""
            SELECT trade_date, close_price, change_pct 
            FROM market_index_daily WHERE index_code='000001.SH'
            ORDER BY trade_date DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            latest, close, chg = row
            is_bear = float(chg) < -0.5
            state_name = '下跌' if is_bear else ('平稳' if float(chg) < 0.5 else '上涨')
            _market_state = {
                'mkt_chg': float(chg),
                'is_bear': is_bear,
                'date': str(latest),
                'state_name': state_name,
            }
        else:
            _market_state = {'mkt_chg': 0, 'is_bear': False, 'date': '', 'state_name': '未知'}
        cur.close()
        return _market_state
    except Exception as e:
        logger.error(f"获取市场状态失败: {e}")
        return {'mkt_chg': 0, 'is_bear': False, 'date': '', 'state_name': 'error'}
    finally:
        if should_close:
            db_conn.close()


def _build_full_market_features(conn):
    """拉全市场当日数据，计算V3原始特征"""
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest = cur.fetchone()[0]
    cur.close()
    
    if not latest:
        return pd.DataFrame(), None
    
    # 拉全市场60天数据（足够计算rolling指标）
    all_daily = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio, vol,
               ma5, ma10, ma20, rps_20, high_52w, low_52w
        FROM daily_price WHERE trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
        ORDER BY ts_code, trade_date
    """, conn, params=(latest,))
    
    for c in ['open','high','low','close','pre_close','pct_chg','turnover_rate',
              'volume_ratio','vol','ma5','ma10','ma20','rps_20','high_52w','low_52w']:
        all_daily[c] = pd.to_numeric(all_daily[c], errors='coerce')
    
    # 资金流
    all_mf = pd.read_sql("""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
    """, conn, params=(latest,))
    for c in ['main_net','net_mf_amount','buy_sm_amount','sell_sm_amount',
              'buy_lg_amount','sell_lg_amount']:
        if c in all_mf.columns:
            all_mf[c] = pd.to_numeric(all_mf[c], errors='coerce')
    
    # 指数
    idx = pd.read_sql("""
        SELECT trade_date, change_pct, close_price FROM market_index_daily
        WHERE index_code='000001.SH' AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
        ORDER BY trade_date
    """, conn, params=(latest,))
    idx['trade_date'] = pd.to_datetime(idx['trade_date'])
    
    all_daily['trade_date'] = pd.to_datetime(all_daily['trade_date'])
    all_mf['trade_date'] = pd.to_datetime(all_mf['trade_date'])
    
    # 指数特征
    idx = idx.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    idx['idx_pct_5d'] = idx['close_price'] / idx['close_price'].shift(5) - 1
    idx['idx_vol_10d'] = idx['change_pct'].rolling(10).std()
    idx_feat = idx[['trade_date','idx_ma5_ratio','idx_ma20_ratio','idx_pct_5d','idx_vol_10d']]
    
    # 按股计算特征
    all_daily = all_daily.merge(all_mf, on=['ts_code', 'trade_date'], how='left')
    for c in ['main_net','net_mf_amount']:
        if c not in all_daily.columns:
            all_daily[c] = 0.0
    
    results = []
    for ts_code, group in all_daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE_PREFIXES:
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue
        
        g = group.copy()
        
        # 技术指标
        g['vol_5d'] = g['pct_chg'].shift(1).rolling(5).std()
        g['vol_10d'] = g['pct_chg'].shift(1).rolling(10).std()
        g['vol_20d'] = g['pct_chg'].shift(1).rolling(20).std()
        g['ma5_ma10_ratio'] = g['ma5'] / g['ma10'].replace(0, np.nan)
        g['ma10_ma20_ratio'] = g['ma10'] / g['ma20'].replace(0, np.nan)
        g['price_ma5_ratio'] = g['close'] / g['ma5'].replace(0, np.nan)
        g['price_ma20_ratio'] = g['close'] / g['ma20'].replace(0, np.nan)
        g['chg_3d'] = g['close'] / g['close'].shift(3) - 1
        g['chg_5d'] = g['close'] / g['close'].shift(5) - 1
        g['chg_10d'] = g['close'] / g['close'].shift(10) - 1
        g['vr_ma5'] = g['volume_ratio'].rolling(5).mean()
        g['vr_ma10'] = g['volume_ratio'].rolling(10).mean()
        g['vol_trend'] = g['vr_ma5'] / g['vr_ma10'].replace(0, np.nan)
        g['pos_52w'] = (g['close'] - g['low_52w']) / (g['high_52w'] - g['low_52w']).replace(0, np.nan)
        g['rps_change'] = g['rps_20'].diff(5)
        g['up_ratio_5d'] = (g['pct_chg'] > 0).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].rolling(5).corr(g['pct_chg'])
        g['ma_pattern'] = 1
        g.loc[(g['ma5']>g['ma10'])&(g['ma10']>g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5']<g['ma10'])&(g['ma10']<g['ma20']), 'ma_pattern'] = 0
        
        # MACD
        ema12 = g['close'].ewm(span=12, adjust=False).mean()
        ema26 = g['close'].ewm(span=26, adjust=False).mean()
        g['macd_diff'] = (ema12 - ema26) / g['close']
        g['macd_hist'] = g['macd_diff'] - g['macd_diff'].ewm(span=9, adjust=False).mean()
        
        # RSI
        delta = g['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g['rsi_14'] = 100 - (100 / (1 + rs))
        
        # 资金流特征
        if 'main_net' in g.columns:
            g['amount_est'] = g['vol'] * g['close'] * 100
            g['main_net_ratio'] = g['main_net'] / g['amount_est'].replace(0, np.nan)
            g['main_net_ma5'] = g['main_net_ratio'].rolling(5).mean()
            g['main_net_ma10'] = g['main_net_ratio'].rolling(10).mean()
            g['main_trend'] = g['main_net_ma5'] / g['main_net_ma10'].replace(0, np.nan)
            g['main_pos'] = (g['main_net'] > 0).astype(int)
            g['main_streak'] = g['main_pos'].rolling(5).sum()
            retail_net = g.get('buy_sm_amount', 0).fillna(0) - g.get('sell_sm_amount', 0).fillna(0)
            g['main_vs_retail'] = (g['main_net'] - retail_net) / g['amount_est'].replace(0, np.nan)
            g['main_cum5'] = g['main_net_ratio'].rolling(5).sum()
            g['main_cum10'] = g['main_net_ratio'].rolling(10).sum()
            g['main_accel_3d'] = g['main_net_ratio'].diff(3)
            g['smart_div_count'] = ((g['pct_chg'] < 0) & (g['main_net'] > 0)).rolling(5).sum()
        else:
            for c in ['main_net_ratio','main_net_ma5','main_net_ma10','main_trend',
                      'main_streak','main_vs_retail','main_cum5','main_cum10',
                      'main_accel_3d', 'smart_div_count']:
                g[c] = np.nan
        
        # Alpha因子
        g['vol_price_corr_10d'] = g['vol'].rolling(10).corr(g['pct_chg'])
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].rolling(5).mean()
        
        # 合并指数
        g = g.merge(idx_feat, on='trade_date', how='left')
        
        # 取最新一天
        latest_rows = g[g['trade_date'] == pd.Timestamp(latest)]
        if not latest_rows.empty:
            results.append(latest_rows)
    
    if not results:
        return pd.DataFrame(), None
    
    result = pd.concat(results, ignore_index=True)
    return result, latest


def _cross_sectional_zscore(df, feature_cols):
    """对给定特征做横截面Z-Score标准化"""
    df = df.copy()
    for col in feature_cols:
        if col not in df.columns:
            continue
        mean = df[col].mean()
        std = df[col].std()
        df[f'{col}_z'] = (df[col] - mean) / (std + 1e-9)
    return df


def predict_batch_v3(ts_codes, db_conn=None):
    """V3模型批量预测 — 全市场横截面Z-Score"""
    bundle = _load_v3_model()
    if bundle is None:
        return {c: {'probability': 0.5, 'is_likely_up': False, 'model_type': 'v3_unavailable'} 
                for c in ts_codes}
    
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True
    
    try:
        # 拉全市场数据
        full_df, latest_date = _build_full_market_features(db_conn)
        if full_df.empty:
            return {c: {'probability': 0.5, 'is_likely_up': False, 'model_type': 'no_data'} 
                    for c in ts_codes}
        
        # 横截面Z-Score
        raw_feats = bundle.get('raw_features', V3_RAW_FEATURES)
        full_df = _cross_sectional_zscore(full_df, raw_feats)
        
        # 取目标股票
        target_codes = set(ts_codes)
        target_df = full_df[full_df['ts_code'].isin(target_codes)].copy()
        
        if target_df.empty:
            return {c: {'probability': 0.5, 'is_likely_up': False, 'model_type': 'not_found'} 
                    for c in ts_codes}
        
        # 对齐特征列
        feature_cols = bundle['feature_cols']
        medians = bundle.get('global_medians', {})
        
        for col in feature_cols:
            if col not in target_df.columns:
                target_df[col] = medians.get(col, 0.0)
            elif target_df[col].isna().any():
                target_df[col] = target_df[col].fillna(medians.get(col, 0.0))
        
        X = target_df[feature_cols].values.astype(np.float32)
        probs = bundle['model'].predict_proba(X)[:, 1]
        
        state = get_market_state(db_conn)
        threshold = 0.40 if state['is_bear'] else 0.55
        
        results = {}
        for idx, (_, row) in enumerate(target_df.iterrows()):
            code = row['ts_code']
            prob = float(probs[idx])
            results[code] = {
                'probability': round(prob, 3),
                'is_likely_up': prob >= threshold,
                'model_type': 'v3',
                'mkt_chg': round(state['mkt_chg'], 2),
            }
        
        # 补充未找到的股票
        for c in ts_codes:
            if c not in results:
                results[c] = {'probability': 0.5, 'is_likely_up': False, 'model_type': 'not_found'}
        
        return results
    except Exception as e:
        logger.error(f"V3批量预测失败: {e}")
        import traceback
        traceback.print_exc()
        return {c: {'probability': 0.5, 'is_likely_up': False, 'model_type': 'error'} for c in ts_codes}
    finally:
        if should_close:
            db_conn.close()


def ml_enhanced_score_v3(stocks_list, db_conn=None):
    """V3 ML增强评分"""
    bundle = _load_v3_model()
    state = get_market_state(db_conn)
    model_type = 'V3横截面'
    threshold = 0.40 if state['is_bear'] else 0.55
    
    if bundle is None:
        for s in stocks_list:
            s['ml概率'] = 0.5
            s['ml看涨'] = False
            s['增强评分'] = s.get('综合评分', 0)
            s['市场状态'] = '未知'
        return stocks_list
    
    codes = []
    code_map = {}
    for s in stocks_list:
        raw = s.get('代码', '')
        ts_code = s.get('_ts_code', '')
        
        # 标准化代码
        if not ts_code or '.' not in ts_code:
            if len(raw) >= 8:
                ts_code = f"{raw[2:]}.{'SH' if raw[:2]=='SH' else 'SZ'}"
            elif '.' in raw:
                ts_code = raw
            elif len(raw) == 6:
                ts_code = f"{raw}.{'SH' if raw.startswith('6') else 'SZ'}"
            else:
                continue
        
        if '.' not in ts_code or len(ts_code.split('.')[0]) != 6:
            if len(raw) >= 8:
                ts_code = f"{raw[2:]}.{'SH' if raw[:2]=='SH' else 'SZ'}"
            elif len(raw) == 6:
                ts_code = f"{raw}.{'SH' if raw.startswith('6') else 'SZ'}"
            else:
                continue
        
        codes.append(ts_code)
        code_map[ts_code] = s
    
    if not codes:
        return stocks_list
    
    predictions = predict_batch_v3(codes, db_conn=db_conn)
    
    from sector_rotation import get_fund_flow_continuity, get_sector_bonus, get_hot_sectors, _build_industry_map
    hot_sectors = get_hot_sectors(top_n=8, db_conn=db_conn)
    industry_map = _build_industry_map(list(code_map.keys()), db_conn)

    for ts_code, s in code_map.items():
        pred = predictions.get(ts_code, {'probability': 0.5, 'is_likely_up': False})
        prob = pred['probability']
        is_up = pred.get('is_likely_up', prob >= threshold)

        base_score = s.get('综合评分', 0)
        ml_bonus = int(prob * 30) if is_up else int((threshold - prob) * -20)
        sector_bonus, sector_name, _ = get_sector_bonus(ts_code, hot_sectors, db_conn=db_conn, industry_map=industry_map)
        flow = get_fund_flow_continuity(ts_code, db_conn=db_conn)
        
        enhanced = base_score + ml_bonus + sector_bonus + flow['score']
        
        s['ml概率'] = round(prob, 3)
        s['ml看涨'] = is_up
        s['增强评分'] = enhanced
        s['板块加分'] = sector_bonus
        s['热点板块'] = sector_name or ''
        s['资金趋势'] = flow.get('trend', 'unknown')
        s['市场状态'] = model_type
    
    return stocks_list
