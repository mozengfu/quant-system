#!/usr/bin/env python3
"""
ML选股模型推理 V6 - 回归模型（按预测收益率排序，无需顺/逆市切换）
"""

import os, logging, warnings, threading
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from quant_app.utils.model_loader import load_model

from quant_app.utils.config import get_db_config

import numpy as np, pandas as pd, pymysql

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore', category=FutureWarning)

DB_CONFIG = get_db_config()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_DAYS = 80  # V6特征构建所需历史天数
_v6_bundle = None  # V6 回归模型
_v6_2_bundle = None  # V6.2 集成模型
_v6_3_bundle = None  # V6.3 集成模型
_v6_4_bundle = None  # V6.4 集成模型（实验性）
_v6_5_bundle = None  # V6.5 精选特征集成模型
_model_lock = threading.Lock()  # 模型加载并发锁

def _load_model(version="v6"):
    """加载指定版本的模型（线程安全）"""
    with _model_lock:
        if version == "v6.5":
            global _v6_5_bundle
            if _v6_5_bundle is None:
                _v6_5_bundle = load_model("v6.5")
                if _v6_5_bundle is None:
                    return None
                ic = _v6_5_bundle.get('final_rank_ic', 'N/A')
                n_models = _v6_5_bundle.get('ensemble_n_models', 1)
                logger.info(f"V6.5 精选集成模型已加载 ({n_models}个子模型, rank_ic={ic})")
            return _v6_5_bundle
        if version == "v6.4":
            global _v6_4_bundle
            if _v6_4_bundle is None:
                _v6_4_bundle = load_model("v6.4")
                if _v6_4_bundle is None:
                    return None
                ic = _v6_4_bundle.get('final_rank_ic', 'N/A')
                n_models = _v6_4_bundle.get('ensemble_n_models', 1)
                logger.info(f"V6.4 集成模型已加载 ({n_models}个子模型, rank_ic={ic})")
            return _v6_4_bundle
        if version == "v6.3":
            global _v6_3_bundle
            if _v6_3_bundle is None:
                _v6_3_bundle = load_model("v6.3")
                if _v6_3_bundle is None:
                    return None
                ic = _v6_3_bundle.get('final_rank_ic', 'N/A')
                n_models = _v6_3_bundle.get('ensemble_n_models', 1)
                logger.info(f"V6.3 集成模型已加载 ({n_models}个子模型, rank_ic={ic})")
            return _v6_3_bundle
        if version == "v6.2":
            global _v6_2_bundle
            if _v6_2_bundle is None:
                _v6_2_bundle = load_model("v6.2")
                if _v6_2_bundle is None:
                    return None
                ic = _v6_2_bundle.get('final_rank_ic', 'N/A')
                n_models = _v6_2_bundle.get('ensemble_n_models', 1)
                logger.info(f"V6.2 集成模型已加载 ({n_models}个子模型, rank_ic={ic})")
            return _v6_2_bundle
        else:
            global _v6_bundle
            if _v6_bundle is None:
                _v6_bundle = load_model("v6")
                if _v6_bundle is None:
                    return None
                ver = _v6_bundle.get('version', 'unknown')
                ic = _v6_bundle.get('final_rank_ic', 'N/A')
                logger.info(f"V6回归模型已加载 (version={ver}, rank_ic={ic})")
            return _v6_bundle

def _load_v6_model():
    """向后兼容：加载V6模型"""
    return _load_model("v6")

def _load_best_model():
    """加载最佳可用模型：优先 V6.5 > V6.4 > V6.3 > V6.2 > V6"""
    bundle = _load_model("v6.5")
    if bundle:
        return bundle, "v6.5"
    bundle = _load_model("v6.4")
    if bundle:
        return bundle, "v6.4"
    bundle = _load_model("v6.3")
    if bundle:
        return bundle, "v6.3"
    bundle = _load_model("v6.2")
    if bundle:
        return bundle, "v6.2"
    bundle = _load_model("v6")
    if bundle:
        return bundle, "v6"
    return None, None




def _fetch_realtime_market_data():
    """
    从腾讯财经获取实时指数数据 + 新浪获取涨跌家数。
    无需 API Key，纯 Python urllib 实现。
    """
    try:
        import urllib.request, json, re, time

        def _retry_urlopen(req, max_retries=3, timeout=5):
            """带重试的 urlopen"""
            for attempt in range(max_retries):
                try:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise e

        # 1. 指数实时数据 (腾讯财经 - 比新浪更稳定)
        mkt_chg = 0.0
        try:
            url = 'http://qt.gtimg.cn/q=sh000001'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'http://finance.qq.com'})
            with _retry_urlopen(req) as resp:
                content = resp.read().decode('gbk', errors='replace')
            
            # 腾讯格式: v_sh000001="1~上证指数~000001~4083.47~4086.34~4076.14~...~涨跌幅%"
            match = re.search(r'v_sh000001="(.+?)";', content)
            if match:
                parts = match.group(1).split('~')
                if len(parts) >= 33:
                    # parts[32] = 涨跌幅%
                    mkt_chg = float(parts[32])
                    logger.info(f"腾讯指数数据: {mkt_chg}%")
        except Exception as e:
            logger.warning(f"腾讯指数数据获取失败: {e}")
            # 回退到新浪
            try:
                url_idx = 'http://hq.sinajs.cn/list=sh000001'
                req = urllib.request.Request(url_idx, headers={'Referer': 'https://finance.sina.com.cn'})
                with _retry_urlopen(req) as resp:
                    content = resp.read().decode('gbk', errors='replace')
                match = re.search(r'var hq_str_sh000001="(.+?)";', content)
                if match:
                    parts = match.group(1).split(',')
                    if len(parts) >= 4:
                        yesterday_close = float(parts[2])
                        current_price = float(parts[3])
                        mkt_chg = round((current_price - yesterday_close) / yesterday_close * 100, 2)
            except Exception as e2:
                logger.warning(f"新浪指数数据也失败: {e2}")

        # 2. 涨跌家数 (新浪财经)
        total_up = 0
        total_down = 0
        total_cnt = 0
        breadth_ratio = 50.0  # 默认中性值
        
        try:
            # 沪市 + 深市
            for node in ['sh_a', 'sz_a']:
                for page in range(1, 16):  # 最多 15 页 * 80 = 1200 只（足够覆盖两市）
                    url = f'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=0&node={node}&symbol=&_s_r_a=auto'
                    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'http://finance.sina.com.cn'})
                    with _retry_urlopen(req) as resp:
                        content = resp.read().decode('gbk', errors='replace')
                        data = json.loads(content)
                    if not data:
                        break
                    for stock in data:
                        change = float(stock.get('changepercent', 0))
                        total_cnt += 1
                        if change > 0:
                            total_up += 1
                        elif change < 0:
                            total_down += 1
            
            if total_cnt > 0:
                breadth_ratio = round((total_up / total_cnt * 100), 1)
                logger.info(f"新浪涨跌比: {breadth_ratio}% (涨{total_up}/跌{total_down}/共{total_cnt})")
        except Exception as e:
            logger.warning(f"新浪涨跌家数获取失败: {e}")

        return {
            'mkt_chg': mkt_chg,
            'breadth_ratio': breadth_ratio,
            'up_cnt': total_up,
            'total_cnt': total_cnt,
            'success': True
        }
    except Exception as e:
        logger.warning(f"实时数据获取失败: {e}")
        return {'success': False}

_realtime_cache = {'data': None, 'updated_at': 0}

def _get_cached_realtime_data():
    """获取缓存的实时数据，超过 30 秒重新获取"""
    import time
    now = time.time()
    if _realtime_cache['data'] is None or (now - _realtime_cache['updated_at']) > 60:
        data = _fetch_realtime_market_data()
        _realtime_cache['data'] = data
        _realtime_cache['updated_at'] = now
    return _realtime_cache['data']

def _is_trading_time():
    """判断当前是否为交易相关时间 (周一至周五 8:30-16:00)"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    hour, minute = now.hour, now.minute
    time_val = hour * 60 + minute
    # 8:30 - 16:00 (覆盖盘前、盘中、午间、盘后)
    return 510 <= time_val <= 960


def get_market_info(conn=None):
    """获取大盘数据 — 委托统一 realtime_service"""
    from quant_app.services.realtime_service import get_market_overview
    return get_market_overview(conn)



def _build_features_for_stocks_v6(conn, ts_codes, as_of_date=None):
    """
    V6 基础版特征 — 80天历史 + shift(1) + 截面因子
    与 ml_train_v6.py 的 build_features() 计算逻辑完全一致。
    V6.2 在此基础扩展了量价因子: amount_ma5_ratio/pos_10d/20d/turnover_ratio 等。

    as_of_date: 指定回测日期（可选），不传则用 MAX(trade_date)
    """
    if not ts_codes:
        return pd.DataFrame()

    placeholders = ','.join(['%s'] * len(ts_codes))

    if as_of_date is None:
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = cur.fetchone()[0]
        cur.close()
        if not latest:
            return pd.DataFrame()
    else:
        latest = as_of_date

    # 行情数据（80天历史）
    df = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio, vol,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
        ORDER BY ts_code, trade_date
    """, conn, params=(*ts_codes, latest))
    for c in ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'turnover_rate',
              'volume_ratio', 'vol', 'ma5', 'ma10', 'ma20', 'rps_20', 'low_52w', 'high_52w']:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # 资金流
    moneyflow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
    """, conn, params=(*ts_codes, latest))
    for c in ['main_net', 'net_mf_amount', 'buy_sm_amount', 'sell_sm_amount',
              'buy_lg_amount', 'sell_lg_amount']:
        if c in moneyflow.columns:
            moneyflow[c] = pd.to_numeric(moneyflow[c], errors='coerce')

    # 指数
    idx = pd.read_sql(f"""
        SELECT trade_date, change_pct, close_price FROM market_index_daily
        WHERE index_code='000001.SH' AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
        ORDER BY trade_date
    """, conn, params=(latest,))
    idx['trade_date'] = pd.to_datetime(idx['trade_date'])

    # 基本面（当日数据）
    fundamentals = pd.read_sql(f"""
        SELECT ts_code, pe_ttm, pb, total_mv
        FROM daily_basic WHERE ts_code IN ({placeholders})
        AND trade_date = %s
    """, conn, params=(*ts_codes, latest))
    for c in ['pe_ttm', 'pb', 'total_mv']:
        if c in fundamentals.columns:
            fundamentals[c] = pd.to_numeric(fundamentals[c], errors='coerce')

    # 指数特征
    idx = idx.sort_values('trade_date').copy()
    idx['idx_ma5'] = idx['close_price'].rolling(5).mean()
    idx['idx_ma20'] = idx['close_price'].rolling(20).mean()
    idx['idx_ma60'] = idx['close_price'].rolling(60).mean()
    idx['idx_ma5_ratio'] = idx['close_price'] / idx['idx_ma5'].replace(0, np.nan) - 1
    idx['idx_ma20_ratio'] = idx['close_price'] / idx['idx_ma20'].replace(0, np.nan) - 1
    idx['idx_ma60_ratio'] = idx['close_price'] / idx['idx_ma60'].replace(0, np.nan) - 1
    idx['idx_pct_5d'] = idx['close_price'] / idx['close_price'].shift(5) - 1
    idx['idx_pct_20d'] = idx['close_price'] / idx['close_price'].shift(20) - 1
    idx['idx_vol_10d'] = idx['change_pct'].rolling(10).std()
    idx['idx_vol_20d'] = idx['change_pct'].rolling(20).std()
    idx['idx_trend'] = 0
    idx.loc[idx['idx_ma5_ratio'] > 0, 'idx_trend'] = 1
    idx.loc[idx['idx_ma5_ratio'] < -0.02, 'idx_trend'] = -1
    idx['ma_spread'] = idx['idx_ma5_ratio'] - idx['idx_ma20_ratio']
    idx_feat = idx[['trade_date', 'idx_ma5_ratio', 'idx_ma20_ratio', 'idx_ma60_ratio',
                    'idx_pct_5d', 'idx_pct_20d', 'idx_vol_10d', 'idx_vol_20d',
                    'idx_trend', 'ma_spread']]

    # 合并
    # 统一日期类型，避免 object vs datetime64 合并错误
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    df = df.merge(moneyflow, on=['ts_code', 'trade_date'], how='left')
    for c in ['main_net', 'net_mf_amount']:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].fillna(0.0)

    df = df.merge(idx_feat, on='trade_date', how='left')

    # 按股计算 V6 特征（shift(1) 模式）
    results = []
    for ts_code, group in df.groupby('ts_code'):
        if ts_code[:2] in ('68', '83', '87', '43') or ts_code[:1] in ('8', '4', '9'):
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue

        g = group.copy()

        # 波动 (shift(1))
        g['vol_5d'] = g['pct_chg'].shift(1).rolling(5).std()
        g['vol_10d'] = g['pct_chg'].shift(1).rolling(10).std()
        g['vol_20d'] = g['pct_chg'].shift(1).rolling(20).std()

        # 均线比率
        g['ma5_ma10_ratio'] = g['ma5'] / g['ma10'].replace(0, np.nan)
        g['ma10_ma20_ratio'] = g['ma10'] / g['ma20'].replace(0, np.nan)
        g['price_ma5_ratio'] = g['close'] / g['ma5'].replace(0, np.nan)
        g['price_ma20_ratio'] = g['close'] / g['ma20'].replace(0, np.nan)

        # 动量 (shift(1))
        g['chg_3d'] = g['close'].shift(1) / g['close'].shift(4) - 1
        g['chg_5d'] = g['close'].shift(1) / g['close'].shift(6) - 1
        g['chg_10d'] = g['close'].shift(1) / g['close'].shift(11) - 1
        g['chg_20d'] = g['close'].shift(1) / g['close'].shift(21) - 1

        # 量趋势 (shift(1))
        g['vr_ma5'] = g['volume_ratio'].shift(1).rolling(5).mean()
        g['vr_ma10'] = g['volume_ratio'].shift(1).rolling(10).mean()
        g['vol_trend'] = g['vr_ma5'] / g['vr_ma10'].replace(0, np.nan)

        g['pos_52w'] = (g['close'] - g['low_52w']) / (g['high_52w'] - g['low_52w']).replace(0, np.nan)
        g['rps_change'] = g['rps_20'].diff(5)
        g['rps_change'] = g['rps_change'].shift(1)
        g['up_ratio_5d'] = (g['pct_chg'] > 0).shift(1).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).shift(1).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].shift(1).rolling(10).corr(g['pct_chg'].shift(1))
        g['ma_pattern'] = 1
        g.loc[(g['ma5'] > g['ma10']) & (g['ma10'] > g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5'] < g['ma10']) & (g['ma10'] < g['ma20']), 'ma_pattern'] = 0

        # MACD (shift(1))
        ema12 = g['close'].ewm(span=12, adjust=False).mean()
        ema26 = g['close'].ewm(span=26, adjust=False).mean()
        g['macd_diff'] = (ema12 - ema26) / g['close']
        g['macd_signal_line'] = g['macd_diff'].ewm(span=9, adjust=False).mean()
        g['macd_hist'] = g['macd_diff'] - g['macd_signal_line']
        g[['macd_diff', 'macd_signal_line', 'macd_hist']] = g[['macd_diff', 'macd_signal_line', 'macd_hist']].shift(1)

        # RSI (shift(1))
        delta = g['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g['rsi_14'] = 100 - (100 / (1 + rs))
        g['rsi_14'] = g['rsi_14'].shift(1)

        # ADX 趋向指标 (14) — 趋势强度
        tr = pd.concat([
            (g['high'] - g['low']).abs(),
            (g['high'] - g['close'].shift(1)).abs(),
            (g['low'] - g['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        up_move = g['high'] - g['high'].shift(1)
        down_move = g['low'].shift(1) - g['low']
        plus_dm = ((up_move > down_move) & (up_move > 0)).astype(int) * up_move
        minus_dm = ((down_move > up_move) & (down_move > 0)).astype(int) * down_move
        tr_14 = tr.rolling(14).mean()
        plus_di_14 = 100 * plus_dm.rolling(14).mean() / tr_14.replace(0, np.nan)
        minus_di_14 = 100 * minus_dm.rolling(14).mean() / tr_14.replace(0, np.nan)
        dx = ((plus_di_14 - minus_di_14).abs() / (plus_di_14 + minus_di_14).replace(0, np.nan)) * 100
        g['adx_14'] = dx.rolling(14).mean()
        g['adx_14'] = g['adx_14'].shift(1)

        # Alpha 因子 (shift(1))
        g['vol_price_corr_10d'] = g['vol'].shift(1).rolling(10).corr(g['pct_chg'].shift(1))
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g[['gap_ratio', 'gap_retention']] = g[['gap_ratio', 'gap_retention']].shift(1)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].shift(1).rolling(5).mean()
        g['amihud'] = g['amihud'].shift(1)

        g['vol_price_divergence'] = g['chg_5d'] * g['vol_trend']
        g['vol_price_div_10d'] = g['chg_10d'] * g['vol_pct_corr']
        g['ma_spread_stock'] = g['price_ma5_ratio'] - g['price_ma20_ratio']

        # 资金流 (shift(1))
        g['amount_est'] = g['vol'] * g['close'] * 100
        g['main_net_ratio'] = g['main_net'] / g['amount_est'].replace(0, np.nan)
        g['main_net_ma5'] = g['main_net_ratio'].shift(1).rolling(5).mean()
        g['main_net_ma10'] = g['main_net_ratio'].shift(1).rolling(10).mean()
        g['main_trend'] = g['main_net_ma5'] / g['main_net_ma10'].replace(0, np.nan)
        g['main_pos'] = (g['main_net'] > 0).astype(int)
        g['main_streak'] = g['main_pos'].shift(1).rolling(5).sum()
        retail_net = g.get('buy_sm_amount', pd.Series(0)).fillna(0) - g.get('sell_sm_amount', pd.Series(0)).fillna(0)
        g['main_vs_retail'] = (g['main_net'] - retail_net) / g['amount_est'].replace(0, np.nan)
        g['lg_ratio'] = (g['buy_lg_amount'].fillna(0) + g['sell_lg_amount'].fillna(0)) / \
                       (g['buy_sm_amount'].fillna(0) + g['sell_sm_amount'].fillna(0) + 1)
        g['main_cum5'] = g['main_net_ratio'].shift(1).rolling(5).sum()
        g['main_cum10'] = g['main_net_ratio'].shift(1).rolling(10).sum()
        g['main_accel_3d'] = g['main_net_ratio'].diff(3)
        g['smart_div_count'] = ((g['pct_chg'] < 0) & (g['main_net'] > 0)).shift(1).rolling(5).sum()
        g['main_inflow_ratio'] = g['buy_lg_amount'].fillna(0) / (g['main_net'].abs() + 1)
        g['main_flow_accel'] = g['main_net_ratio'].diff(1) / (g['main_net_ratio'].abs().rolling(5).mean() + 1e-9)

        # 融资融券 (shift(1))
        try:
            margin_df = pd.read_sql("""
                SELECT trade_date, rzye, rqye, rzmre FROM margin_daily
                WHERE ts_code = %s ORDER BY trade_date
            """, conn, params=(ts_code,))
            if not margin_df.empty:
                margin_df['trade_date'] = pd.to_datetime(margin_df['trade_date'])
                for c in ['rzye', 'rqye', 'rzmre']:
                    margin_df[c] = pd.to_numeric(margin_df[c], errors='coerce').fillna(0)
                g = g.merge(margin_df, on='trade_date', how='left')
                g['margin_total'] = g['rzye'].fillna(0) + g['rqye'].fillna(0)
                g['rzye_chg'] = g['rzye'].fillna(0).diff()
                g['rzmre_ratio'] = g['rzmre'].fillna(0) / g['amount_est'].replace(0, np.nan)
                g[['rzye_chg', 'rzmre_ratio']] = g[['rzye_chg', 'rzmre_ratio']].shift(1)
            else:
                g['rzye_chg'] = 0
                g['rzmre_ratio'] = 0
        except Exception:
            g['rzye_chg'] = 0
            g['rzmre_ratio'] = 0

        # 取最新行
        g_latest = g[g['trade_date'] == pd.Timestamp(latest)]
        if not g_latest.empty:
            results.append(g_latest)

    if not results:
        return pd.DataFrame()

    result = pd.concat(results, ignore_index=True)

    # 添加截面因子
    if not fundamentals.empty:
        result = result.merge(fundamentals, on='ts_code', how='left')
    for col in ['pe_ttm', 'pb', 'total_mv']:
        if col not in result.columns:
            result[col] = np.nan
    result['ln_mv'] = np.log(result['total_mv'].replace(0, np.nan))

    # 当日横截面 Z-Score
    for col in ['ln_mv', 'pe_ttm', 'pb']:
        mean = result[col].mean()
        std = result[col].std()
        result[f'{col}_z'] = (result[col] - mean) / (std + 1e-9)

    # V6 Alpha 情绪特征 — 与训练一致：shift(1) + 5日滚动累计
    try:
        # 获取最近6个交易日（T, T-1, ..., T-5）
        recent_dates = pd.read_sql("""
            SELECT DISTINCT trade_date FROM daily_price
            ORDER BY trade_date DESC LIMIT 6
        """, conn)
        if len(recent_dates) >= 2:
            recent_dates['trade_date'] = pd.to_datetime(recent_dates['trade_date'])
            date_list = recent_dates['trade_date'].tolist()
            t_minus_1 = date_list[1]          # 昨天，用于 max_boost
            rolling_dates = date_list[1:6]    # T-1 ~ T-5，用于 pos_5d / neg_5d

            # 读近30天 alpha 信号（用 pandas 做日期转换再过滤，避免 MySQL 日期格式不一致）
            alpha_df = pd.read_sql("""
                SELECT ts_code, signal_date, MAX(score_boost) as max_boost
                FROM alpha_signals
                WHERE signal_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                GROUP BY ts_code, signal_date
            """, conn)
            if not alpha_df.empty:
                alpha_df['signal_date'] = pd.to_datetime(alpha_df['signal_date'])

                # max_boost = T-1 的信号值（shift(1) 后当天看到的最近信号）
                t1_mask = alpha_df['signal_date'] == t_minus_1
                max_map = alpha_df[t1_mask].groupby('ts_code')['max_boost'].max().reset_index()

                # alpha_pos_5d / alpha_neg_5d = T-5~T-1 区间累计
                rolling_mask = alpha_df['signal_date'].isin(rolling_dates)
                rolling_df = alpha_df[rolling_mask]
                alpha_agg = rolling_df.groupby('ts_code').agg(
                    alpha_pos_5d=('max_boost', lambda x: x.clip(lower=0).sum()),
                    alpha_neg_5d=('max_boost', lambda x: x.clip(upper=0).abs().sum()),
                ).reset_index()

                result = result.merge(max_map, on='ts_code', how='left')
                result = result.merge(alpha_agg, on='ts_code', how='left')
                result['max_boost'] = result['max_boost'].fillna(0.0)
                result['alpha_pos_5d'] = result['alpha_pos_5d'].fillna(0.0)
                result['alpha_neg_5d'] = result['alpha_neg_5d'].fillna(0.0)
            else:
                result['max_boost'] = 0.0
                result['alpha_pos_5d'] = 0.0
                result['alpha_neg_5d'] = 0.0
        else:
            result['max_boost'] = 0.0
            result['alpha_pos_5d'] = 0.0
            result['alpha_neg_5d'] = 0.0
    except Exception as e:
        logger.warning(f"Alpha 信号读取失败: {e}，使用默认值")
        result['max_boost'] = 0.0
        result['alpha_pos_5d'] = 0.0
        result['alpha_neg_5d'] = 0.0

    # === 行业动量特征 ===
    try:
        stock_info_df = pd.read_sql("SELECT ts_code, industry, is_st, list_date FROM stock_info", conn)
        result = result.merge(stock_info_df[['ts_code', 'industry', 'is_st', 'list_date']], on='ts_code', how='left')
        result['industry'] = result['industry'].fillna('OTHER')
        result['is_st'] = result['is_st'].fillna(0).astype(int)
        # 行业平均 pct_chg（T 日）
        result['ind_pct_avg'] = result.groupby('industry')['pct_chg'].transform('mean')
        # 行业动量（无需 shift，因为 pct_chg 在 V6 构建中已 shift(1)）
        result['ind_mom_5d'] = result.groupby('industry')['ind_pct_avg'].transform(
            lambda x: x.rolling(5, min_periods=1).mean()
        )
        result['ind_mom_20d'] = result.groupby('industry')['ind_pct_avg'].transform(
            lambda x: x.rolling(20, min_periods=1).mean()
        )
        # 上市天数
        if result['list_date'].notna().any():
            latest_dt = pd.Timestamp(latest)
            list_dates = pd.to_datetime(result['list_date'], errors='coerce')
            result['list_age_days'] = (latest_dt - list_dates).dt.days.fillna(365 * 20)
            result['ln_list_age'] = np.log(result['list_age_days'].clip(30))
        else:
            result['ln_list_age'] = np.log(365 * 10)
    except Exception as e:
        logger.warning(f"行业/结构特征构建失败: {e}")
        result['ind_mom_5d'] = 0.0
        result['ind_mom_20d'] = 0.0
        result['is_st'] = 0
        result['ln_list_age'] = np.log(365 * 10)

    # 当日横截面排名特征（与训练一致）
    rank_features = [
        'pct_chg', 'turnover_rate', 'volume_ratio', 'rps_20',
        'lg_ratio', 'main_net_ratio', 'pos_52w',
        'chg_5d', 'chg_10d', 'main_cum5',
    ]
    for col in rank_features:
        if col in result.columns:
            result[f'{col}_rank'] = result[col].rank(pct=True)
            result[f'{col}_rank'] = result[f'{col}_rank'].fillna(0.5)

    return result


def _build_features_for_stocks_v6_2(conn, ts_codes, as_of_date=None):
    """
    V6.2 特征 — 在 V6 基础上新增量价因子:
    amount_ma5_ratio, pos_10d/20d, amplitude_10d, turnover_ratio,
    mom_divergence, ret_skew/kurt_20d, ret_max/min_5d, ln_circ_mv。
    去掉了指数特征。与 ml_train_v6_2.py 的 build_features() 计算逻辑完全一致。
    """
    if not ts_codes:
        return pd.DataFrame()

    placeholders = ','.join(['%s'] * len(ts_codes))

    if as_of_date is None:
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = cur.fetchone()[0]
        cur.close()
        if not latest:
            return pd.DataFrame()
    else:
        latest = as_of_date

    # 行情数据
    df = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio, vol, amount,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
        ORDER BY ts_code, trade_date
    """, conn, params=(*ts_codes, latest))
    for c in ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'turnover_rate',
              'volume_ratio', 'vol', 'amount', 'ma5', 'ma10', 'ma20', 'rps_20', 'low_52w', 'high_52w']:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # 资金流
    moneyflow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
    """, conn, params=(*ts_codes, latest))
    for c in ['main_net', 'net_mf_amount', 'buy_sm_amount', 'sell_sm_amount',
              'buy_lg_amount', 'sell_lg_amount']:
        if c in moneyflow.columns:
            moneyflow[c] = pd.to_numeric(moneyflow[c], errors='coerce')

    # 基本面（当日数据）
    fundamentals = pd.read_sql(f"""
        SELECT ts_code, pe_ttm, pb, total_mv, circ_mv
        FROM daily_basic WHERE ts_code IN ({placeholders})
        AND trade_date = %s
    """, conn, params=(*ts_codes, latest))
    for c in ['pe_ttm', 'pb', 'total_mv', 'circ_mv']:
        if c in fundamentals.columns:
            fundamentals[c] = pd.to_numeric(fundamentals[c], errors='coerce')

    # 合并
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    df = df.merge(moneyflow, on=['ts_code', 'trade_date'], how='left')
    for c in ['main_net', 'net_mf_amount']:
        if c not in df.columns:
            df[c] = 0.0
        df[c] = df[c].fillna(0.0)

    results = []
    for ts_code, group in df.groupby('ts_code'):
        if ts_code[:2] in ('68', '83', '87', '43') or ts_code[:1] in ('8', '4', '9'):
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue

        g = group.copy()

        # === V6 基础特征 (全部 shift(1)) ===
        g['vol_5d'] = g['pct_chg'].shift(1).rolling(5).std()
        g['vol_10d'] = g['pct_chg'].shift(1).rolling(10).std()
        g['vol_20d'] = g['pct_chg'].shift(1).rolling(20).std()
        g['ma5_ma10_ratio'] = g['ma5'] / g['ma10'].replace(0, np.nan)
        g['ma10_ma20_ratio'] = g['ma10'] / g['ma20'].replace(0, np.nan)
        g['price_ma5_ratio'] = g['close'] / g['ma5'].replace(0, np.nan)
        g['price_ma20_ratio'] = g['close'] / g['ma20'].replace(0, np.nan)
        g['chg_3d'] = g['close'].shift(1) / g['close'].shift(4) - 1
        g['chg_5d'] = g['close'].shift(1) / g['close'].shift(6) - 1
        g['chg_10d'] = g['close'].shift(1) / g['close'].shift(11) - 1
        g['chg_20d'] = g['close'].shift(1) / g['close'].shift(21) - 1
        g['vr_ma5'] = g['volume_ratio'].shift(1).rolling(5).mean()
        g['vr_ma10'] = g['volume_ratio'].shift(1).rolling(10).mean()
        g['vol_trend'] = g['vr_ma5'] / g['vr_ma10'].replace(0, np.nan)
        g['pos_52w'] = (g['close'] - g['low_52w']) / (g['high_52w'] - g['low_52w']).replace(0, np.nan)
        g['rps_change'] = g['rps_20'].diff(5)
        g['rps_change'] = g['rps_change'].shift(1)
        g['up_ratio_5d'] = (g['pct_chg'] > 0).shift(1).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).shift(1).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].shift(1).rolling(10).corr(g['pct_chg'].shift(1))
        g['ma_pattern'] = 1
        g.loc[(g['ma5'] > g['ma10']) & (g['ma10'] > g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5'] < g['ma10']) & (g['ma10'] < g['ma20']), 'ma_pattern'] = 0

        # MACD (shift(1))
        ema12 = g['close'].ewm(span=12, adjust=False).mean()
        ema26 = g['close'].ewm(span=26, adjust=False).mean()
        g['macd_diff'] = (ema12 - ema26) / g['close']
        g['macd_signal_line'] = g['macd_diff'].ewm(span=9, adjust=False).mean()
        g['macd_hist'] = g['macd_diff'] - g['macd_signal_line']
        g[['macd_diff', 'macd_signal_line', 'macd_hist']] = g[['macd_diff', 'macd_signal_line', 'macd_hist']].shift(1)

        # RSI (shift(1))
        delta = g['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g['rsi_14'] = 100 - (100 / (1 + rs))
        g['rsi_14'] = g['rsi_14'].shift(1)

        # ADX (shift(1))
        tr = pd.concat([
            (g['high'] - g['low']).abs(),
            (g['high'] - g['close'].shift(1)).abs(),
            (g['low'] - g['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        up_move = g['high'] - g['high'].shift(1)
        down_move = g['low'].shift(1) - g['low']
        plus_dm = ((up_move > down_move) & (up_move > 0)).astype(int) * up_move
        minus_dm = ((down_move > up_move) & (down_move > 0)).astype(int) * down_move
        tr_14 = tr.rolling(14).mean()
        plus_di_14 = 100 * plus_dm.rolling(14).mean() / tr_14.replace(0, np.nan)
        minus_di_14 = 100 * minus_dm.rolling(14).mean() / tr_14.replace(0, np.nan)
        dx = ((plus_di_14 - minus_di_14).abs() / (plus_di_14 + minus_di_14).replace(0, np.nan)) * 100
        g['adx_14'] = dx.rolling(14).mean()
        g['adx_14'] = g['adx_14'].shift(1)

        # Alpha 因子 (shift(1))
        g['vol_price_corr_10d'] = g['vol'].shift(1).rolling(10).corr(g['pct_chg'].shift(1))
        g['gap_ratio'] = (g['open'] - g['pre_close']) / (g['pre_close'] + 1e-9)
        g['gap_retention'] = (g['close'] - g['open']) / (g['open'] - g['pre_close']).replace(0, np.nan)
        g[['gap_ratio', 'gap_retention']] = g[['gap_ratio', 'gap_retention']].shift(1)
        g['amihud'] = np.abs(g['pct_chg']) / (g['turnover_rate'] + 1e-9)
        g['amihud_ma5'] = g['amihud'].shift(1).rolling(5).mean()
        g['amihud'] = g['amihud'].shift(1)
        g['vol_price_divergence'] = g['chg_5d'] * g['vol_trend']
        g['vol_price_div_10d'] = g['chg_10d'] * g['vol_pct_corr']
        g['ma_spread_stock'] = g['price_ma5_ratio'] - g['price_ma20_ratio']

        # 资金流 (shift(1))
        g['amount_est'] = g['vol'] * g['close'] * 100
        g['main_net_ratio'] = g['main_net'] / g['amount_est'].replace(0, np.nan)
        g['main_net_ma5'] = g['main_net_ratio'].shift(1).rolling(5).mean()
        g['main_net_ma10'] = g['main_net_ratio'].shift(1).rolling(10).mean()
        g['main_trend'] = g['main_net_ma5'] / g['main_net_ma10'].replace(0, np.nan)
        g['main_pos'] = (g['main_net'] > 0).astype(int)
        g['main_streak'] = g['main_pos'].shift(1).rolling(5).sum()
        retail_net = g.get('buy_sm_amount', pd.Series(0)).fillna(0) - g.get('sell_sm_amount', pd.Series(0)).fillna(0)
        g['main_vs_retail'] = (g['main_net'] - retail_net) / g['amount_est'].replace(0, np.nan)
        g['lg_ratio'] = (g['buy_lg_amount'].fillna(0) + g['sell_lg_amount'].fillna(0)) / \
                       (g['buy_sm_amount'].fillna(0) + g['sell_sm_amount'].fillna(0) + 1)
        g['main_cum5'] = g['main_net_ratio'].shift(1).rolling(5).sum()
        g['main_cum10'] = g['main_net_ratio'].shift(1).rolling(10).sum()
        g['main_accel_3d'] = g['main_net_ratio'].diff(3)
        g['smart_div_count'] = ((g['pct_chg'] < 0) & (g['main_net'] > 0)).shift(1).rolling(5).sum()
        g['main_inflow_ratio'] = g['buy_lg_amount'].fillna(0) / (g['main_net'].abs() + 1)
        g['main_flow_accel'] = g['main_net_ratio'].diff(1) / (g['main_net_ratio'].abs().rolling(5).mean() + 1e-9)

        # 融资融券 (shift(1))
        try:
            margin_df = pd.read_sql("""
                SELECT trade_date, rzye, rqye, rzmre FROM margin_daily
                WHERE ts_code = %s ORDER BY trade_date
            """, conn, params=(ts_code,))
            if not margin_df.empty:
                margin_df['trade_date'] = pd.to_datetime(margin_df['trade_date'])
                for c in ['rzye', 'rqye', 'rzmre']:
                    margin_df[c] = pd.to_numeric(margin_df[c], errors='coerce').fillna(0)
                g = g.merge(margin_df, on='trade_date', how='left')
                g['margin_total'] = g['rzye'].fillna(0) + g['rqye'].fillna(0)
                g['rzye_chg'] = g['rzye'].fillna(0).diff()
                g['rzmre_ratio'] = g['rzmre'].fillna(0) / g['amount_est'].replace(0, np.nan)
                g[['rzye_chg', 'rzmre_ratio']] = g[['rzye_chg', 'rzmre_ratio']].shift(1)
            else:
                g['rzye_chg'] = 0
                g['rzmre_ratio'] = 0
        except Exception:
            g['rzye_chg'] = 0
            g['rzmre_ratio'] = 0

        # === V6.2 新增特征 (shift(1)) ===
        # 量比异常
        g['amount_ma5'] = g['amount'].shift(1).rolling(5).mean()
        g['amount_ma5_ratio'] = g['amount'] / g['amount_ma5'].replace(0, np.nan)
        # 短期价格位置
        g['low_10d'] = g['close'].shift(1).rolling(10).min()
        g['high_10d'] = g['close'].shift(1).rolling(10).max()
        g['low_20d'] = g['close'].shift(1).rolling(20).min()
        g['high_20d'] = g['close'].shift(1).rolling(20).max()
        g['pos_10d'] = (g['close'] - g['low_10d']) / (g['high_10d'] - g['low_10d']).replace(0, np.nan)
        g['pos_20d'] = (g['close'] - g['low_20d']) / (g['high_20d'] - g['low_20d']).replace(0, np.nan)
        # 振幅
        g['amplitude_10d'] = (g['high_10d'] - g['low_10d']) / g['close'].shift(1).rolling(10).mean().replace(0, np.nan)
        # 换手率比
        g['turnover_rate_ma20'] = g['turnover_rate'].shift(1).rolling(20).mean()
        g['turnover_ratio'] = g['turnover_rate'] / g['turnover_rate_ma20'].replace(0, np.nan)
        # 动量分歧
        g['mom_divergence'] = g['chg_5d'] - g['chg_20d']
        # 收益率偏度与峰度
        g['ret_skew_20d'] = g['pct_chg'].shift(1).rolling(20).skew()
        g['ret_kurt_20d'] = g['pct_chg'].shift(1).rolling(20).kurt()
        # max/min ret
        g['ret_max_5d'] = g['pct_chg'].shift(1).rolling(5).max()
        g['ret_min_5d'] = g['pct_chg'].shift(1).rolling(5).min()
        # 流通市值
        g['ln_circ_mv'] = np.nan

        # 取最新行
        g_latest = g[g['trade_date'] == pd.Timestamp(latest)]
        if not g_latest.empty:
            results.append(g_latest)

    if not results:
        return pd.DataFrame()

    result = pd.concat(results, ignore_index=True)

    # 截面因子
    if not fundamentals.empty:
        result = result.merge(fundamentals, on='ts_code', how='left')
    for col in ['pe_ttm', 'pb', 'total_mv', 'circ_mv']:
        if col not in result.columns:
            result[col] = np.nan
    result['ln_mv'] = np.log(result['total_mv'].replace(0, np.nan))
    result['ln_circ_mv'] = np.log(result['circ_mv'].replace(0, np.nan))

    # Z-Score
    for col in ['ln_mv', 'ln_circ_mv', 'pe_ttm', 'pb']:
        mean = result[col].mean()
        std = result[col].std()
        result[f'{col}_z'] = (result[col] - mean) / (std + 1e-9)

    # Alpha 情绪特征
    try:
        recent_dates = pd.read_sql("""
            SELECT DISTINCT trade_date FROM daily_price
            ORDER BY trade_date DESC LIMIT 6
        """, conn)
        if len(recent_dates) >= 2:
            recent_dates['trade_date'] = pd.to_datetime(recent_dates['trade_date'])
            date_list = recent_dates['trade_date'].tolist()
            t_minus_1 = date_list[1]
            rolling_dates = date_list[1:6]

            alpha_df = pd.read_sql("""
                SELECT ts_code, signal_date, MAX(score_boost) as max_boost
                FROM alpha_signals
                WHERE signal_date >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                GROUP BY ts_code, signal_date
            """, conn)
            if not alpha_df.empty:
                alpha_df['signal_date'] = pd.to_datetime(alpha_df['signal_date'])
                t1_mask = alpha_df['signal_date'] == t_minus_1
                max_map = alpha_df[t1_mask].groupby('ts_code')['max_boost'].max().reset_index()
                rolling_mask = alpha_df['signal_date'].isin(rolling_dates)
                rolling_df = alpha_df[rolling_mask]
                alpha_agg = rolling_df.groupby('ts_code').agg(
                    alpha_pos_5d=('max_boost', lambda x: x.clip(lower=0).sum()),
                    alpha_neg_5d=('max_boost', lambda x: x.clip(upper=0).abs().sum()),
                ).reset_index()
                result = result.merge(max_map, on='ts_code', how='left')
                result = result.merge(alpha_agg, on='ts_code', how='left')
                result['max_boost'] = result['max_boost'].fillna(0.0)
                result['alpha_pos_5d'] = result['alpha_pos_5d'].fillna(0.0)
                result['alpha_neg_5d'] = result['alpha_neg_5d'].fillna(0.0)
            else:
                result['max_boost'] = 0.0
                result['alpha_pos_5d'] = 0.0
                result['alpha_neg_5d'] = 0.0
        else:
            result['max_boost'] = 0.0
            result['alpha_pos_5d'] = 0.0
            result['alpha_neg_5d'] = 0.0
    except Exception:
        result['max_boost'] = 0.0
        result['alpha_pos_5d'] = 0.0
        result['alpha_neg_5d'] = 0.0

    # 行业动量
    try:
        stock_info_df = pd.read_sql("SELECT ts_code, industry, is_st, list_date FROM stock_info", conn)
        result = result.merge(stock_info_df[['ts_code', 'industry', 'is_st', 'list_date']], on='ts_code', how='left')
        result['industry'] = result['industry'].fillna('OTHER')
        result['is_st'] = result['is_st'].fillna(0).astype(int)
        result['ind_pct_avg'] = result.groupby('industry')['pct_chg'].transform('mean')
        result['ind_mom_5d'] = result.groupby('industry')['ind_pct_avg'].transform(
            lambda x: x.rolling(5, min_periods=1).mean()
        )
        result['ind_mom_20d'] = result.groupby('industry')['ind_pct_avg'].transform(
            lambda x: x.rolling(20, min_periods=1).mean()
        )
        if result['list_date'].notna().any():
            latest_dt = pd.Timestamp(latest)
            list_dates = pd.to_datetime(result['list_date'], errors='coerce')
            result['list_age_days'] = (latest_dt - list_dates).dt.days.fillna(365 * 20)
            result['ln_list_age'] = np.log(result['list_age_days'].clip(30))
        else:
            result['ln_list_age'] = np.log(365 * 10)
    except Exception:
        result['ind_mom_5d'] = 0.0
        result['ind_mom_20d'] = 0.0
        result['is_st'] = 0
        result['ln_list_age'] = np.log(365 * 10)

    # 横截面排名特征
    rank_features = [
        'pct_chg', 'turnover_rate', 'volume_ratio', 'rps_20',
        'lg_ratio', 'main_net_ratio', 'pos_52w',
        'chg_5d', 'chg_10d', 'main_cum5',
        'amount_ma5_ratio', 'pos_10d', 'turnover_ratio', 'mom_divergence',
    ]
    for col in rank_features:
        if col in result.columns:
            result[f'{col}_rank'] = result[col].rank(pct=True)
            result[f'{col}_rank'] = result[f'{col}_rank'].fillna(0.5)

    return result


def _ensemble_predict(feat_df, bundle):
    """集成模型预测：所有子模型取均值"""
    feature_cols = bundle['feature_cols']
    medians = bundle.get('global_medians', {})
    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = medians.get(col, 0.0)
        elif feat_df[col].isna().any():
            feat_df[col] = feat_df[col].fillna(medians.get(col, 0.0))
    X = feat_df[feature_cols].values.astype(np.float32)

    models = bundle['models']
    preds = np.zeros((len(X), len(models)))
    for i, model in enumerate(models):
        preds[:, i] = model.predict(X)
    return np.mean(preds, axis=1)


def _build_features_for_stocks_v6_3(conn, ts_codes, as_of_date=None):
    """V6.3 特征 — 复用 V6.2 结果，新增涨停板/真实行业动量/概念热度/业绩因子/龙虎榜+股东人数"""
    # 先调用 V6.2 基础特征构建（包含 dragon/holder 特征）
    result = _build_features_for_stocks_v6_2(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result

    # === V6.3 新增特征 ===
    placeholders = ','.join(['%s'] * len(ts_codes))

    # 1. 涨停板特征 (zt_pool: seal_amount, open_count, last_board)
    try:
        zt_df = pd.read_sql(f"""
            SELECT ts_code, trade_date,
                   COALESCE(seal_amount, 0) as seal_amount,
                   COALESCE(open_count, 0) as open_count,
                   COALESCE(last_board, 0) as last_board
            FROM zt_pool WHERE ts_code IN ({placeholders})
            AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
        """, conn, params=(*ts_codes, as_of_date or datetime.now()))
        if not zt_df.empty:
            zt_df['trade_date'] = pd.to_datetime(zt_df['trade_date'])
            for tc in result['ts_code'].unique():
                mask = result['ts_code'] == tc
                zt_stock = zt_df[zt_df['ts_code'] == tc]
                if not zt_stock.empty:
                    result.loc[mask, 'zt_count_30d'] = len(zt_stock)
                    result.loc[mask, 'zt_max_board_30d'] = zt_stock['last_board'].max()
                    result.loc[mask, 'zt_seal_amount_30d'] = zt_stock['seal_amount'].sum()
                    result.loc[mask, 'zt_open_count_30d'] = (zt_stock['open_count'] > 0).sum()
    except Exception as e:
        logger.warning(f"V6.3 涨停板特征失败: {e}")

    for col in ['zt_count_30d', 'zt_max_board_30d', 'zt_seal_amount_30d', 'zt_open_count_30d']:
        if col not in result.columns:
            result[col] = 0

    # 2. 真实行业动量 (board_industry_cons + board_industry_hist via board_code)
    try:
        stock_ind = pd.read_sql(f"""
            SELECT ts_code, board_code FROM board_industry_cons
            WHERE ts_code IN ({placeholders}) AND is_latest=1
        """, conn, params=ts_codes)
        if not stock_ind.empty:
            bcodes = stock_ind['board_code'].unique().tolist()
            bc_placeholders = ','.join(['%s'] * len(bcodes))
            ind_hist = pd.read_sql(f"""
                SELECT board_code, trade_date, pct_change
                FROM board_industry_hist
                WHERE board_code IN ({bc_placeholders})
                AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
                ORDER BY board_code, trade_date
            """, conn, params=(*bcodes, as_of_date or datetime.now()))
            if not ind_hist.empty:
                ind_hist['trade_date'] = pd.to_datetime(ind_hist['trade_date'])
                ind_hist = ind_hist.sort_values(['board_code', 'trade_date'])
                # 预计算每个板块的动量
                ind_mom = {}
                for bc, grp in ind_hist.groupby('board_code'):
                    g = grp.sort_values('trade_date')['pct_change'].astype(float)
                    ind_mom[bc] = {
                        'pct_5d': g.tail(5).sum() if len(g) >= 5 else g.sum(),
                        'pct_10d': g.tail(10).sum() if len(g) >= 10 else g.sum(),
                        'pct_20d': g.tail(20).sum() if len(g) >= 20 else g.sum(),
                    }
                for _, row in stock_ind.iterrows():
                    tc = row['ts_code']
                    bc = row['board_code']
                    if bc in ind_mom:
                        result.loc[result['ts_code'] == tc, 'ind_board_pct_5d'] = ind_mom[bc]['pct_5d']
                        result.loc[result['ts_code'] == tc, 'ind_board_pct_10d'] = ind_mom[bc]['pct_10d']
                        result.loc[result['ts_code'] == tc, 'ind_board_pct_20d'] = ind_mom[bc]['pct_20d']
    except Exception as e:
        logger.warning(f"V6.3 行业动量特征失败: {e}")

    for col in ['ind_board_pct_5d', 'ind_board_pct_10d', 'ind_board_pct_20d']:
        if col not in result.columns:
            result[col] = 0.0

    # 3. 概念数量和热度 (board_concept_cons via board_code)
    try:
        concept_count = pd.read_sql(f"""
            SELECT ts_code, COUNT(DISTINCT board_code) as concept_count
            FROM board_concept_cons WHERE ts_code IN ({placeholders}) AND is_latest=1
            GROUP BY ts_code
        """, conn, params=ts_codes)
        if not concept_count.empty:
            result = result.merge(concept_count, on='ts_code', how='left')
            result['concept_count'] = result['concept_count'].fillna(0)
    except Exception as e:
        logger.warning(f"V6.3 概念数量获取失败: {e}")
        result['concept_count'] = 0

    # 概念板块动量 — board_concept_hist 数据可能为 0 条
    has_concept_hist = False
    for col in ['concept_mom_avg', 'concept_mom_5d', 'concept_mom_10d']:
        if col not in result.columns:
            result[col] = 0.0
        else:
            has_concept_hist = True
    if not has_concept_hist:
        logger.warning("V6.3 board_concept_hist 数据为 0 条，concept_mom_* 特征恒为 0")

    # 4. 业绩因子 (earnings_report)
    try:
        earnings = pd.read_sql(f"""
            SELECT ts_code, report_date,
                   CAST(eps AS DECIMAL(12,4)) as eps,
                   CAST(revenue_yoy AS DECIMAL(12,4)) as revenue_yoy,
                   CAST(net_profit_yoy AS DECIMAL(12,4)) as net_profit_yoy,
                   CAST(roe AS DECIMAL(12,4)) as roe,
                   CAST(gross_margin AS DECIMAL(12,4)) as gross_margin,
                   CAST(net_margin AS DECIMAL(12,4)) as net_margin
            FROM earnings_report WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, report_date DESC
        """, conn, params=ts_codes)
        if not earnings.empty:
            earnings = earnings.drop_duplicates(subset='ts_code', keep='first')
            for col in ['eps', 'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin', 'net_margin']:
                earnings[col] = pd.to_numeric(earnings[col], errors='coerce')
            result = result.merge(earnings[['ts_code', 'revenue_yoy', 'net_profit_yoy',
                                            'roe', 'gross_margin', 'net_margin']], on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V6.3 业绩因子失败: {e}")

    for col in ['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin', 'net_margin']:
        if col not in result.columns:
            result[col] = np.nan

    # 5. 龙虎榜特征 (dragon_tiger / dragon_tiger_inst) — V6.2 训练时有但推理缺失
    try:
        dt_df = pd.read_sql(f"""
            SELECT ts_code, trade_date, net_buy
            FROM dragon_tiger WHERE ts_code IN ({placeholders})
            AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
        """, conn, params=(*ts_codes, as_of_date or datetime.now()))
        dti_df = pd.read_sql(f"""
            SELECT ts_code, trade_date, net_buy
            FROM dragon_tiger_inst WHERE ts_code IN ({placeholders})
            AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
            AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
        """, conn, params=(*ts_codes, as_of_date or datetime.now()))
        for tc in result['ts_code'].unique():
            mask = result['ts_code'] == tc
            dt_stock = dt_df[dt_df['ts_code'] == tc] if not dt_df.empty else pd.DataFrame()
            dti_stock = dti_df[dti_df['ts_code'] == tc] if not dti_df.empty else pd.DataFrame()
            result.loc[mask, 'dragon_count_30d'] = len(dt_stock)
            result.loc[mask, 'dragon_net_buy_30d'] = float(dt_stock['net_buy'].sum()) if not dt_stock.empty else 0
            result.loc[mask, 'dragon_has_institution_30d'] = int(len(dti_stock) > 0)
            result.loc[mask, 'dti_count_30d'] = len(dti_stock)
            result.loc[mask, 'dti_net_buy_30d'] = float(dti_stock['net_buy'].sum()) if not dti_stock.empty else 0
    except Exception as e:
        logger.warning(f"V6.3 龙虎榜特征获取失败: {e}")

    for col in ['dragon_count_30d', 'dragon_net_buy_30d', 'dragon_has_institution_30d', 'dti_count_30d', 'dti_net_buy_30d']:
        if col not in result.columns:
            result[col] = 0

    # 6. 股东人数变化特征 — V6.2 训练时有但推理缺失
    try:
        hc_df = pd.read_sql(f"""
            SELECT ts_code, end_date, holder_num_change, holder_change_pct
            FROM holder_change WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, end_date DESC
        """, conn, params=ts_codes)
        if not hc_df.empty:
            hc_df['holder_num_change'] = pd.to_numeric(hc_df['holder_num_change'], errors='coerce')
            hc_df['holder_change_pct'] = pd.to_numeric(hc_df['holder_change_pct'], errors='coerce')
            for tc in result['ts_code'].unique():
                mask = result['ts_code'] == tc
                hc_stock = hc_df[hc_df['ts_code'] == tc].sort_values('end_date', ascending=False)
                if not hc_stock.empty:
                    result.loc[mask, 'holder_num_change'] = hc_stock['holder_num_change'].iloc[0]
                    result.loc[mask, 'holder_change_pct'] = hc_stock['holder_change_pct'].iloc[0]
                    declines = sum(1 for v in hc_stock['holder_num_change'].head(4) if v < 0)
                    result.loc[mask, 'holder_consecutive_decline'] = declines
    except Exception as e:
        logger.warning(f"V6.3 股东人数特征获取失败: {e}")

    for col in ['holder_num_change', 'holder_change_pct', 'holder_consecutive_decline']:
        if col not in result.columns:
            result[col] = 0

    # 横截面排名
    rank_features_v6_3 = [
        'zt_count_30d', 'zt_max_board_30d',
        'ind_board_pct_5d', 'ind_board_pct_10d',
        'concept_count', 'concept_mom_5d',
        'net_profit_yoy', 'revenue_yoy',
        'dragon_net_buy_30d', 'dragon_count_30d',
    ]
    for col in rank_features_v6_3:
        if col in result.columns:
            result[f'{col}_rank'] = result[col].rank(pct=True).fillna(0.5)

    return result


def predict_batch(ts_codes, db_conn=None, as_of_date=None):
    """批量预测 — 自动选择最佳可用模型"""
    bundle, version = _load_best_model()
    if bundle is None:
        return {c: {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0, 'model_type': 'no_model'} for c in ts_codes}
    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True
    try:
        if version in ("v6.5", "v6.4"):
            feat_df = _build_features_for_stocks_v6_3(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v6.3":
            feat_df = _build_features_for_stocks_v6_3(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v6.2":
            feat_df = _build_features_for_stocks_v6_2(db_conn, ts_codes, as_of_date=as_of_date)
        else:
            feat_df = _build_features_for_stocks_v6(db_conn, ts_codes, as_of_date=as_of_date)
        if feat_df.empty:
            return {c: {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0, 'model_type': 'no_data'} for c in ts_codes}

        if version in ("v6.5", "v6.4", "v6.2", "v6.3") and 'models' in bundle:
            pred_returns = _ensemble_predict(feat_df, bundle)
        else:
            feature_cols = bundle['feature_cols']
            medians = bundle.get('global_medians', {})
            for col in feature_cols:
                if col not in feat_df.columns:
                    feat_df[col] = medians.get(col, 0.0)
                elif feat_df[col].isna().any():
                    feat_df[col] = feat_df[col].fillna(medians.get(col, 0.0))
            X = feat_df[feature_cols].values.astype(np.float32)
            pred_returns = bundle['model'].predict(X)

        # LambdaRank 输出排序分数（无绝对标度），做 z-score 标准化再显示
        # 单只股票时跳过 z-score（N=1 时 z_score 恒为 0），直接使用原始分
        if len(pred_returns) <= 1:
            pred_z = pred_returns
        else:
            pred_mean = np.mean(pred_returns)
            pred_std = np.std(pred_returns) + 1e-9
            pred_z = (pred_returns - pred_mean) / pred_std

        results = {}
        for i, (_, row) in enumerate(feat_df.iterrows()):
            code = row['ts_code']
            z_score = float(pred_z[i])
            # is_likely_up: z-score > 0 意味着跑赢批次中位数
            is_up = z_score > 0
            # 概率映射：sigmoid(z_score * 1.5)，z=0→0.5, z=1→0.82, z=-1→0.18
            prob = round(float(1 / (1 + np.exp(-z_score * 1.5))), 3)
            # predicted_return 显示为 z-score（无量纲的排序强度）
            display_ret = round(z_score, 2)
            results[code] = {
                'probability': prob,
                'predicted_return': display_ret,
                'is_likely_up': is_up,
                'model_type': version,
                'model_task': 'lambdarank',
            }
        return results
    except Exception as e:
        logger.error(f"{version}预测失败: {e}")
        return {c: {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0, 'model_type': 'error'} for c in ts_codes}
    finally:
        if should_close:
            db_conn.close()










def ml_enhanced_score(stocks_list, db_conn=None):
    """ML增强评分 — 自动使用最佳可用模型(V6.2 > V6)"""
    bundle, version = _load_best_model()
    if bundle is None:
        for s in stocks_list:
            s['ml概率'] = 0.5; s['ml看涨'] = False
            s['增强评分'] = s.get('综合评分', 0); s['市场状态'] = '模型未加载'
        return stocks_list

    rank_ic = bundle.get('final_rank_ic', 0)
    model_label = f'V6.5集成(IC={rank_ic:.3f})' if version == 'v6.5' else (
        f'V6.4集成(IC={rank_ic:.3f})' if version == 'v6.4' else (
        f'V6.3集成(IC={rank_ic:.3f})' if version == 'v6.3' else (
        f'V6.2集成(IC={rank_ic:.3f})' if version == 'v6.2' else f'V6(IC={rank_ic:.3f})')))

    codes, code_map = [], {}
    for s in stocks_list:
        raw = s.get('代码', ''); ts_code = s.get('_ts_code', '')
        if not ts_code or '.' not in ts_code:
            if len(raw) >= 8: ts_code = f"{raw[2:]}.{'SH' if raw[:2].upper()=='SH' else 'SZ'}"
            elif '.' in raw: ts_code = raw
            elif len(raw) == 6: ts_code = f"{raw}.{'SH' if raw.startswith('6') else 'SZ'}"
            else: continue
        if '.' not in ts_code or len(ts_code.split('.')[0]) != 6:
            if len(raw) >= 8: ts_code = f"{raw[2:]}.{'SH' if raw[:2].upper()=='SH' else 'SZ'}"
            elif len(raw) == 6: ts_code = f"{raw}.{'SH' if raw.startswith('6') else 'SZ'}"
            else: continue
        codes.append(ts_code); code_map[ts_code] = s
    if not codes: return stocks_list
    predictions = predict_batch(codes, db_conn=db_conn)
    from sector_rotation import get_fund_flow_continuity, get_sector_bonus, get_hot_sectors, _build_industry_map
    hot_sectors = get_hot_sectors(top_n=8, db_conn=db_conn)
    industry_map = _build_industry_map(list(code_map.keys()), db_conn)  # 批量加载，避免N次SQL
    for ts_code, s in code_map.items():
        pred = predictions.get(ts_code, {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0})
        prob = float(pred['probability']); is_up = bool(pred.get('is_likely_up', prob >= 0.5))
        pred_ret = float(pred.get('predicted_return', 0))
        base_score = s.get('综合评分', 0)
        sector_bonus, sector_name, _ = get_sector_bonus(ts_code, hot_sectors, db_conn=db_conn, industry_map=industry_map)
        flow = get_fund_flow_continuity(ts_code, db_conn=db_conn)
        # ML 重排：predicted_return 为主排序依据，板块加分和资金流做微调
        enhanced = pred_ret * 10 + sector_bonus + flow['score']
        s['ml概率'] = round(prob, 3); s['ml看涨'] = is_up; s['增强评分'] = round(enhanced, 1)
        s['预测收益'] = round(pred_ret, 2); s['热点板块'] = sector_name
        s['资金趋势'] = flow['trend']; s['资金连续'] = flow['continuous_inflow']
        s['市场状态'] = f'{model_label}'
    return stocks_list



