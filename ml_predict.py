#!/usr/bin/env python3
"""
ML选股模型推理 V6 - 回归模型（按预测收益率排序，无需顺/逆市切换）
"""

import json
import logging
import os
import threading
import warnings
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

from quant_app.utils.config import (
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_SOCKET,
    MYSQL_USER,
    get_db_config,
)
from quant_app.utils.model_loader import load_model

logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore', category=UserWarning, module='pandas')

DB_CONFIG = get_db_config()


def _get_engine():
    """创建 SQLAlchemy engine"""
    if MYSQL_SOCKET:
        return create_engine(
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@/{MYSQL_DATABASE}"
            f"?unix_socket={MYSQL_SOCKET}&charset=utf8mb4",
            pool_pre_ping=True,
        )
    return create_engine(
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}"
        f"/{MYSQL_DATABASE}?charset=utf8mb4",
        pool_pre_ping=True,
    )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_DAYS = 80  # V6特征构建所需历史天数
# 模型加载委托给 quant_app.utils.model_loader.load_model()（已用 @lru_cache 缓存），
# 此处仅做首次加载日志。无需重复维护版本清单。

# 最近一次批量扫描的 ML 结果缓存（股票代码 → ML 预测）
# 个股分析时直接使用，不重新计算（单只股票 rank 特征全是 1.0 导致模型输出失真）
_last_scan_results = {}

def _load_model(version="v6"):
    """加载指定版本的模型。委托给 quant_app.utils.model_loader.load_model()，
    后者已用 @lru_cache(maxsize=4) 线程安全缓存。此函数仅输出版本特征日志。"""
    bundle = load_model(version)
    if bundle is None:
        return None
    n_models = bundle.get('ensemble_n_models') or bundle.get('n_models', 1)
    n_features = bundle.get('n_features', 0)
    ic = bundle.get('final_rank_ic', 'N/A')
    logger.info(
        "ML模型加载: version=%s models=%d features=%d rank_ic=%s",
        version, n_models, n_features, ic,
    )
    return bundle


def _load_v6_model():
    """向后兼容：加载V6模型"""
    return _load_model("v6")

def _load_best_model():
    """加载最佳可用模型。降级链: V11.0 → V11.2(thin)"""
    bundle = _load_model("v11.0")
    if bundle:
        return bundle, "v11.0"
    bundle = _load_model("v11.2")
    if bundle:
        return bundle, "v11.2"
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
        import json
        import re
        import time
        import urllib.request

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
        # 兼容 pymysql 和 SQLAlchemy connection
        if hasattr(conn, 'cursor'):
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            latest = cur.fetchone()[0]
            cur.close()
        else:
            with conn.execute(text("SELECT MAX(trade_date) FROM daily_price")) as result:
                latest = result.scalar()
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
        AND trade_date < %s
        ORDER BY ts_code, trade_date
    """, conn, params=(*ts_codes, latest, latest))
    for c in ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'turnover_rate',
              'volume_ratio', 'vol', 'ma5', 'ma10', 'ma20', 'rps_20', 'low_52w', 'high_52w']:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # 资金流
    moneyflow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
        AND trade_date < %s
    """, conn, params=(*ts_codes, latest, latest))
    for c in ['main_net', 'net_mf_amount', 'buy_sm_amount', 'sell_sm_amount',
              'buy_lg_amount', 'sell_lg_amount']:
        if c in moneyflow.columns:
            moneyflow[c] = pd.to_numeric(moneyflow[c], errors='coerce')

    # 指数
    idx = pd.read_sql(f"""
        SELECT trade_date, change_pct, close_price FROM market_index_daily
        WHERE index_code='000001.SH' AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
          AND trade_date < %s
        ORDER BY trade_date
    """, conn, params=(latest, latest))
    idx['trade_date'] = pd.to_datetime(idx['trade_date'])

    # 基本面（当日数据）
    fundamentals = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, total_mv
        FROM daily_basic WHERE ts_code IN ({placeholders})
        AND trade_date < %s
        AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
    """, conn, params=(*ts_codes, latest, latest))
    if not fundamentals.empty:
        fundamentals = fundamentals.sort_values('trade_date').groupby('ts_code').last().reset_index()
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

    # 合并行业信息（用于行业动量特征，需在循环前完成）
    stock_info_df = pd.read_sql("SELECT ts_code, industry, is_st, list_date FROM stock_info", conn)
    df = df.merge(stock_info_df[['ts_code', 'industry', 'is_st', 'list_date']], on='ts_code', how='left')
    df['industry'] = df['industry'].fillna('OTHER')
    df['is_st'] = df['is_st'].fillna(0).astype(int)
    # 每日行业平均 pct_chg（原始值，后续在循环内 shift(1) 避免未来数据）
    df['ind_pct_avg_raw'] = df.groupby(['trade_date', 'industry'])['pct_chg'].transform('mean')

    # 按股计算 V6 特征（shift(1) 模式）
    results = []
    for ts_code, group in df.groupby('ts_code'):
        if ts_code[:2] in ('68', '83', '87', '43') or ts_code[:1] in ('8', '4', '9'):
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue

        g = group.copy()

        # 行业动量：shift(1) 使 T 日看到 T-1 行业均值（与训练端一致）
        g['ind_pct_avg'] = g['ind_pct_avg_raw'].shift(1)
        g['ind_mom_5d'] = g['ind_pct_avg'].rolling(5, min_periods=1).mean()
        g['ind_mom_20d'] = g['ind_pct_avg'].rolling(20, min_periods=1).mean()
        # 上市天数
        latest_dt = pd.Timestamp(latest)
        list_dates = pd.to_datetime(g['list_date'], errors='coerce')
        g['list_age_days'] = (latest_dt - list_dates).dt.days.fillna(365 * 20)
        g['ln_list_age'] = np.log(g['list_age_days'].clip(30))

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
                WHERE ts_code = %s AND trade_date < %s ORDER BY trade_date
            """, conn, params=(ts_code, latest))
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
        g_latest = g.iloc[[-1]]
        if not g_latest.empty:
            results.append(g_latest)

    if not results:
        return pd.DataFrame()

    result = pd.concat(results, ignore_index=True)

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

    # === 行业动量特征（已移至循环内计算，此处不再重复） ===

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
        # 兼容 pymysql 和 SQLAlchemy connection
        if hasattr(conn, 'cursor'):
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            latest = cur.fetchone()[0]
            cur.close()
        else:
            with conn.execute(text("SELECT MAX(trade_date) FROM daily_price")) as result:
                latest = result.scalar()
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
        AND trade_date < %s
        ORDER BY ts_code, trade_date
    """, conn, params=(*ts_codes, latest, latest))
    for c in ['open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'turnover_rate',
              'volume_ratio', 'vol', 'amount', 'ma5', 'ma10', 'ma20', 'rps_20', 'low_52w', 'high_52w']:
        df[c] = pd.to_numeric(df[c], errors='coerce')

    # 资金流
    moneyflow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL {HISTORY_DAYS} DAY)
        AND trade_date < %s
    """, conn, params=(*ts_codes, latest, latest))
    for c in ['main_net', 'net_mf_amount', 'buy_sm_amount', 'sell_sm_amount',
              'buy_lg_amount', 'sell_lg_amount']:
        if c in moneyflow.columns:
            moneyflow[c] = pd.to_numeric(moneyflow[c], errors='coerce')

    # 基本面（当日数据）
    fundamentals = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, total_mv, circ_mv
        FROM daily_basic WHERE ts_code IN ({placeholders})
        AND trade_date < %s
        AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
    """, conn, params=(*ts_codes, latest, latest))
    if not fundamentals.empty:
        fundamentals = fundamentals.sort_values('trade_date').groupby('ts_code').last().reset_index()
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

    # 合并行业信息（用于行业动量特征，需在循环前完成）
    stock_info_df = pd.read_sql("SELECT ts_code, industry, is_st, list_date FROM stock_info", conn)
    df = df.merge(stock_info_df[['ts_code', 'industry', 'is_st', 'list_date']], on='ts_code', how='left')
    df['industry'] = df['industry'].fillna('OTHER')
    df['is_st'] = df['is_st'].fillna(0).astype(int)
    df['ind_pct_avg_raw'] = df.groupby(['trade_date', 'industry'])['pct_chg'].transform('mean')

    # === 加载龙虎榜/股东数据（用于 V6.2 龙虎榜/股东特征，需在循环前完成） ===
    # 龙虎榜
    dt_all = pd.read_sql(f"""
        SELECT ts_code, trade_date, net_buy
        FROM dragon_tiger WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
        AND trade_date < %s
    """, conn, params=(*ts_codes, latest, latest))
    if not dt_all.empty:
        dt_all['trade_date'] = pd.to_datetime(dt_all['trade_date'])
        dt_all['net_buy'] = pd.to_numeric(dt_all['net_buy'], errors='coerce').fillna(0)
    dt_dict = {tc: g.sort_values('trade_date') for tc, g in dt_all.groupby('ts_code') if not g.empty}
    # 龙虎榜机构
    dti_all = pd.read_sql(f"""
        SELECT ts_code, trade_date, net_buy
        FROM dragon_tiger_inst WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
        AND trade_date < %s
        AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
    """, conn, params=(*ts_codes, latest, latest))
    if not dti_all.empty:
        dti_all['trade_date'] = pd.to_datetime(dti_all['trade_date'])
        dti_all['net_buy'] = pd.to_numeric(dti_all['net_buy'], errors='coerce').fillna(0)
    dti_dict = {tc: g.sort_values('trade_date') for tc, g in dti_all.groupby('ts_code') if not g.empty}
    # 股东人数变化
    hc_all = pd.read_sql(f"""
        SELECT ts_code, end_date, holder_num_change, holder_change_pct
        FROM holder_change WHERE ts_code IN ({placeholders})
        AND end_date >= DATE_SUB(%s, INTERVAL 180 DAY)
        AND end_date < %s
        ORDER BY ts_code, end_date
    """, conn, params=(*ts_codes, latest, latest))
    if not hc_all.empty:
        hc_all['end_date'] = pd.to_datetime(hc_all['end_date'])
        for c in ['holder_num_change', 'holder_change_pct']:
            hc_all[c] = pd.to_numeric(hc_all[c], errors='coerce').fillna(0)
    hc_dict = {tc: g.sort_values('end_date') for tc, g in hc_all.groupby('ts_code') if not g.empty}

    results = []
    for ts_code, group in df.groupby('ts_code'):
        if ts_code[:2] in ('68', '83', '87', '43') or ts_code[:1] in ('8', '4', '9'):
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 30:
            continue

        g = group.copy()

        # 行业动量：shift(1) 使 T 日看到 T-1 行业均值（与训练端一致）
        g['ind_pct_avg'] = g['ind_pct_avg_raw'].shift(1)
        g['ind_mom_5d'] = g['ind_pct_avg'].rolling(5, min_periods=1).mean()
        g['ind_mom_20d'] = g['ind_pct_avg'].rolling(20, min_periods=1).mean()
        # 上市天数
        latest_dt = pd.Timestamp(latest)
        list_dates = pd.to_datetime(g['list_date'], errors='coerce')
        g['list_age_days'] = (latest_dt - list_dates).dt.days.fillna(365 * 20)
        g['ln_list_age'] = np.log(g['list_age_days'].clip(30))

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
                WHERE ts_code = %s AND trade_date < %s ORDER BY trade_date
            """, conn, params=(ts_code, latest))
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

        # --- 龙虎榜特征（shift(1)+rolling(30) 时间保护，与训练端一致） ---
        if ts_code in dt_dict:
            dt = dt_dict[ts_code]
            dt_daily = dt.groupby('trade_date', as_index=False)['net_buy'].agg(['sum', 'count'])
            dt_daily.columns = ['trade_date', 'dragon_net_buy_30d', 'dragon_count_30d']
            g = g.merge(dt_daily, on='trade_date', how='left')
            g[['dragon_net_buy_30d', 'dragon_count_30d']] = g[['dragon_net_buy_30d', 'dragon_count_30d']].fillna(0)
            g['dragon_net_buy_30d'] = g['dragon_net_buy_30d'].shift(1).rolling(30, min_periods=1).sum()
            g['dragon_count_30d'] = g['dragon_count_30d'].shift(1).rolling(30, min_periods=1).sum()
        else:
            g['dragon_net_buy_30d'] = 0.0
            g['dragon_count_30d'] = 0.0

        # --- 龙虎榜机构特征（shift(1)+rolling(30) 时间保护） ---
        if ts_code in dti_dict:
            dti = dti_dict[ts_code]
            dti_daily = dti.groupby('trade_date', as_index=False)['net_buy'].agg(['sum', 'count'])
            dti_daily.columns = ['trade_date', 'dti_net_buy_30d', 'dti_count_30d']
            g = g.merge(dti_daily, on='trade_date', how='left')
            g[['dti_net_buy_30d', 'dti_count_30d']] = g[['dti_net_buy_30d', 'dti_count_30d']].fillna(0)
            g['dti_net_buy_30d'] = g['dti_net_buy_30d'].shift(1).rolling(30, min_periods=1).sum()
            g['dti_count_30d'] = g['dti_count_30d'].shift(1).rolling(30, min_periods=1).sum()
        else:
            g['dti_net_buy_30d'] = 0.0
            g['dti_count_30d'] = 0.0

        # --- 股东集中度特征（merge_asof + shift(1) 时间保护） ---
        if ts_code in hc_dict:
            hc = hc_dict[ts_code][['end_date', 'holder_change_pct', 'holder_num_change']].copy()
            hc = hc.rename(columns={'end_date': 'trade_date'})
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            g_sorted['trade_date'] = g_sorted['trade_date'].values.astype('datetime64[us]')
            hc_sorted = hc.sort_values('trade_date')
            hc_sorted['trade_date'] = hc_sorted['trade_date'].values.astype('datetime64[us]')
            merged = pd.merge_asof(g_sorted, hc_sorted, on='trade_date', direction='backward')
            merged = merged.set_index('trade_date')
            g = g.merge(merged[['holder_change_pct', 'holder_num_change']],
                       left_on='trade_date', right_index=True, how='left')
            g['holder_change_pct'] = g['holder_change_pct'].fillna(0)
            g['holder_num_change'] = g['holder_num_change'].fillna(0)
            neg_mask = g['holder_num_change'] < 0
            decline_count = 0
            decline_series = []
            for v in neg_mask.values:
                decline_count = decline_count + 1 if v else 0
                decline_series.append(decline_count)
            g['holder_consecutive_decline'] = decline_series
            g['holder_consecutive_decline'] = g['holder_consecutive_decline'].shift(1).fillna(0).astype(int)
        else:
            g['holder_change_pct'] = 0.0
            g['holder_num_change'] = 0
            g['holder_consecutive_decline'] = 0

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
        g_latest = g.iloc[[-1]]
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

    # 行业动量（已移至循环内计算，此处不再重复）

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
    """集成模型预测：支持 dict/list 两种模型结构，支持加权融合"""
    import lightgbm as lgb
    import xgboost as xgb

    feature_cols = bundle['feature_cols']
    medians = bundle.get('global_medians', {})
    for col in feature_cols:
        if col not in feat_df.columns:
            feat_df[col] = medians.get(col, 0.0)
        elif feat_df[col].isna().any():
            feat_df[col] = feat_df[col].fillna(medians.get(col, 0.0))
    X = feat_df[feature_cols].values.astype(np.float32)

    models = bundle['models']
    weights = bundle.get('ensemble_weights', None)

    if isinstance(models, dict):
        # V10.0+ 风格：dict of models, 支持加权
        preds = np.zeros(len(X))
        weight_sum = 0.0
        for name, m_list in models.items():
            w = 1.0
            if weights is not None:
                w = weights.get(name, 0.0)
                if w <= 0:
                    continue

            if isinstance(m_list, dict):
                cols = m_list.get('feature_cols', feature_cols)
                sub_X = feat_df[cols].values.astype(np.float32)
                m = m_list['model']
                if hasattr(m, '_Booster') or 'xgboost' in type(m).__module__:
                    pred = m.predict(xgb.DMatrix(sub_X))
                else:
                    pred = m.predict(sub_X)
            elif isinstance(m_list, list):
                sub_preds = []
                for m in m_list:
                    if hasattr(m, '_Booster') or 'xgboost' in type(m).__module__:
                        sub_preds.append(m.predict(xgb.DMatrix(X)))
                    else:
                        sub_preds.append(m.predict(X))
                pred = np.mean(sub_preds, axis=0)
            elif isinstance(m_list, (lgb.Booster, xgb.Booster)):
                # V11.0 风格：raw Booster 对象（LambdaRank / XGBoost 回归）
                if hasattr(m_list, '_Booster') or 'xgboost' in type(m_list).__module__:
                    pred = m_list.predict(xgb.DMatrix(X))
                else:
                    pred = m_list.predict(X)
            else:
                logger.debug(f"跳过未知模型 {name}: {type(m_list).__name__}")
                continue

            preds += w * pred
            weight_sum += w

        if weight_sum > 0:
            preds /= weight_sum
        return preds
    else:
        # V8.0- 风格：list of boosters, 支持加权
        w = None
        if weights is not None:
            w = np.array([weights.get(f'model_{i}', 1.0) for i in range(len(models))])
        if w is not None and w.sum() > 0:
            preds = np.zeros(len(X))
            for i, model in enumerate(models):
                if w[i] > 0:
                    preds += w[i] * model.predict(X)
            return preds / w.sum()
        else:
            preds = np.zeros((len(X), len(models)))
            for i, model in enumerate(models):
                preds[:, i] = model.predict(X)
            return np.mean(preds, axis=1)


def _ensemble_scores(feat_df, bundle):
    """集成模型预测 + 后处理，返回 DataFrame（lh 方案）。

    输出列: ml_score, z_score, probability, rank_pct
    - ml_score: 子模型预测均值（LambdaRank 原始排序分数）
    - z_score: 横截面 z-score 标准化
    - probability: sigmoid(z_score * 1.5) 映射到 [0,1]
    - rank_pct: 横截面百分位排名（值越小排名越高）
    """
    raw = _ensemble_predict(feat_df, bundle)
    raw = np.asarray(raw, dtype=np.float64)

    if len(raw) > 1:
        z_scores = (raw - raw.mean()) / (raw.std() + 1e-9)
    else:
        z_scores = np.zeros_like(raw)

    probabilities = 1 / (1 + np.exp(-z_scores * 1.5))
    rank_pcts = _scores_to_percentile(-raw)

    return pd.DataFrame({
        "ml_score": raw,
        "z_score": z_scores,
        "probability": probabilities,
        "rank_pct": rank_pcts,
    }, index=feat_df.index)


def _safe_last(series, default=0.0):
    """安全获取 Series 最后一个非空值，空则返回 default"""
    vals = series.dropna()
    return vals.iloc[-1] if len(vals) > 0 else default


def _scores_to_percentile(scores):
    """
    将分数数组转换为横截面百分位排名 [0, 1]。
    纯 numpy 实现，支持 NaN 处理。
    相同分数赋予相同百分位（平均排名法）。
    """
    scores = np.asarray(scores, dtype=np.float64)
    n = len(scores)
    if n == 0:
        return np.array([], dtype=np.float64)
    if n == 1:
        return np.array([0.5], dtype=np.float64)

    # 处理 NaN：NaN 排在最后
    nan_mask = np.isnan(scores)
    valid = scores[~nan_mask]
    if len(valid) == 0:
        return np.full(n, 0.5, dtype=np.float64)

    # 使用 scipy.stats.rankdata 的平均排名法
    # 或者手动实现：argsort 两次得到排名
    order = valid.argsort()
    ranks = np.empty_like(order)
    ranks[order] = np.arange(len(valid))
    # 将排名转为 [0, 1] 百分位
    percentile = ranks.astype(np.float64) / (len(valid) - 1) if len(valid) > 1 else np.array([0.5])

    result = np.full(n, 0.5, dtype=np.float64)
    result[~nan_mask] = percentile
    return result


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
        zt_as_of = as_of_date or datetime.now()
        zt_df = pd.read_sql(f"""
            SELECT ts_code, trade_date,
                   COALESCE(seal_amount, 0) as seal_amount,
                   COALESCE(open_count, 0) as open_count,
                   COALESCE(last_board, 0) as last_board
            FROM zt_pool WHERE ts_code IN ({placeholders})
            AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
            AND trade_date < %s
        """, conn, params=(*ts_codes, zt_as_of, zt_as_of))
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
        """, conn, params=tuple(ts_codes))
        if not stock_ind.empty:
            bcodes = stock_ind['board_code'].unique().tolist()
            bc_placeholders = ','.join(['%s'] * len(bcodes))
            ind_hist = pd.read_sql(f"""
                SELECT board_code, trade_date, pct_change
                FROM board_industry_hist
                WHERE board_code IN ({bc_placeholders})
                AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
                AND trade_date < %s
                ORDER BY board_code, trade_date
            """, conn, params=(*bcodes, as_of_date or datetime.now(), as_of_date or datetime.now()))
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
        """, conn, params=tuple(ts_codes))
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
        """, conn, params=tuple(ts_codes))
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

    # 5/6. 龙虎榜/股东人数特征：已在 V6.2 中正确计算（带 shift(1)+rolling(30) 时间保护），此处不再重复

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


# ========== V6.6 特征构建（新增 5 组因子） ==========

def _build_features_for_stocks_v6_6(conn, ts_codes, as_of_date=None):
    """
    V6.6 特征 — 在 V6.3 基础上新增 5 组因子：
      1. 大宗交易溢价 (block_trade)
      2. 板块资金流向 (sector_moneyflow)
      3. 概念板块历史动量 (board_concept_hist)
      4. 北向资金个股持仓 (hsgt_hold_stock)
      5. 业绩预告超预期 (stock_forecast)
    """
    result = _build_features_for_stocks_v6_3(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result

    placeholders = ','.join(['%s'] * len(ts_codes))
    trade_date_str = str(as_of_date)[:10] if as_of_date else datetime.now().strftime('%Y-%m-%d')

    # ---- 1. 大宗交易溢价特征 ----
    try:
        bt_df = pd.read_sql(f"""
            SELECT ts_code, trade_date,
                   COALESCE(premium_rate, 0) as premium_rate,
                   COALESCE(deal_amount, 0) as deal_amount,
                   COALESCE(deal_volume, 0) as deal_volume,
                   buyer, seller
            FROM block_trade
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 35 DAY)
        """, conn, params=(*ts_codes, trade_date_str, trade_date_str))

        if not bt_df.empty:
            bt_df['trade_date'] = pd.to_datetime(bt_df['trade_date'])
            inst_keywords = ['机构', '证券', '资管', '基金']
            bt_df['is_inst_buyer'] = bt_df['buyer'].apply(
                lambda x: 1 if any(kw in str(x) for kw in inst_keywords) else 0
            )
            for tc in result['ts_code'].unique():
                mask = result['ts_code'] == tc
                bt_stock = bt_df[bt_df['ts_code'] == tc]
                if bt_stock.empty:
                    continue
                total_amt = bt_stock['deal_amount'].sum()
                if total_amt > 0:
                    w_sum = (bt_stock['premium_rate'] * bt_stock['deal_amount']).sum()
                    result.loc[mask, 'bt_premium_weighted_30d'] = w_sum / total_amt
                result.loc[mask, 'bt_amount_30d'] = np.log(total_amt + 1)
                inst_cnt = bt_stock['is_inst_buyer'].sum()
                result.loc[mask, 'bt_inst_buyer_ratio_30d'] = inst_cnt / max(len(bt_stock), 1)
                result.loc[mask, 'bt_count_30d'] = len(bt_stock)
                result.loc[mask, 'bt_premium_avg_30d'] = bt_stock['premium_rate'].mean()
    except Exception as e:
        logger.warning(f"V6.6 大宗交易特征失败: {e}")

    for col in ['bt_premium_weighted_30d', 'bt_amount_30d', 'bt_inst_buyer_ratio_30d',
                'bt_count_30d', 'bt_premium_avg_30d']:
        if col not in result.columns:
            result[col] = 0.0

    # ---- 2. 板块资金流向特征 ----
    try:
        stock_ind = pd.read_sql(f"""
            SELECT ts_code, industry FROM stock_info
            WHERE ts_code IN ({placeholders})
        """, conn, params=tuple(ts_codes))

        if not stock_ind.empty:
            sector_mf = pd.read_sql("""
                SELECT trade_date, sector_name, net_amount, buy_elg_amount,
                       sell_elg_amount, pct_change
                FROM sector_moneyflow
                WHERE trade_date < %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
            """, conn, params=(trade_date_str, trade_date_str))

            if not sector_mf.empty:
                sector_mf['trade_date'] = pd.to_datetime(sector_mf['trade_date'])
                sector_mf['elg_net'] = sector_mf['buy_elg_amount'] - sector_mf['sell_elg_amount']

                for _, srow in stock_ind.iterrows():
                    tc = srow['ts_code']
                    ind = srow['industry']
                    if not ind:
                        continue
                    smf = sector_mf[sector_mf['sector_name'] == ind].sort_values('trade_date')
                    if smf.empty:
                        continue
                    result.loc[result['ts_code'] == tc, 'sector_elg_net_today'] = smf['elg_net'].iloc[-1]
                    result.loc[result['ts_code'] == tc, 'sector_pct_today'] = smf['pct_change'].iloc[-1]
                    if len(smf) >= 5:
                        result.loc[result['ts_code'] == tc, 'sector_elg_net_cum5'] = smf['elg_net'].tail(5).sum()
                        result.loc[result['ts_code'] == tc, 'sector_pct_5d'] = smf['pct_change'].tail(5).sum()
    except Exception as e:
        logger.warning(f"V6.6 板块资金流向特征失败: {e}")

    for col in ['sector_elg_net_today', 'sector_pct_today', 'sector_elg_net_cum5', 'sector_pct_5d']:
        if col not in result.columns:
            result[col] = 0.0

    # ---- 3. 概念板块历史动量 ----
    try:
        stock_concepts = pd.read_sql(f"""
            SELECT ts_code, board_code FROM board_concept_cons
            WHERE ts_code IN ({placeholders}) AND is_latest=1
        """, conn, params=tuple(ts_codes))

        if not stock_concepts.empty:
            bcodes = stock_concepts['board_code'].unique().tolist()
            bc_ph = ','.join(['%s'] * len(bcodes))
            concept_hist = pd.read_sql(f"""
                SELECT board_code, trade_date, pct_change
                FROM board_concept_hist
                WHERE board_code IN ({bc_ph})
                  AND trade_date < %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
                ORDER BY board_code, trade_date
            """, conn, params=(*bcodes, trade_date_str, trade_date_str))

            if not concept_hist.empty:
                concept_hist['trade_date'] = pd.to_datetime(concept_hist['trade_date'])
                concept_hist = concept_hist.sort_values(['board_code', 'trade_date'])
                concept_hist['pct_5d'] = concept_hist.groupby('board_code')['pct_change'].transform(
                    lambda x: x.rolling(5).sum()
                )
                latest_date = concept_hist['trade_date'].max()
                c5d = concept_hist[concept_hist['trade_date'] == latest_date][['board_code', 'pct_5d']].drop_duplicates('board_code')
                c5d['concept_mom_rank'] = c5d['pct_5d'].rank(pct=True)

                for _, crow in stock_concepts.iterrows():
                    tc = crow['ts_code']
                    bc = crow['board_code']
                    rv = c5d.loc[c5d['board_code'] == bc, 'concept_mom_rank'].values
                    if len(rv) > 0:
                        existing = result.loc[result['ts_code'] == tc, 'concept_mom_rank']
                        if existing.empty or pd.isna(existing.iloc[0]):
                            result.loc[result['ts_code'] == tc, 'concept_mom_rank'] = rv[0]
    except Exception as e:
        logger.warning(f"V6.6 概念板块动量特征失败: {e}")

    if 'concept_mom_rank' not in result.columns:
        result['concept_mom_rank'] = 0.5

    # ---- 4. 北向资金个股持仓特征 ----
    try:
        hsgt_df = pd.read_sql(f"""
            SELECT ts_code, trade_date, hold_ratio, hold_shares, hold_mv, `rank`
            FROM hsgt_hold_stock
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND ts_code != '000000.NORTH'
            ORDER BY ts_code, trade_date
        """, conn, params=(*ts_codes, trade_date_str))

        if not hsgt_df.empty:
            hsgt_df['trade_date'] = pd.to_datetime(hsgt_df['trade_date'])
            for tc in result['ts_code'].unique():
                hsgt_stock = hsgt_df[hsgt_df['ts_code'] == tc].sort_values('trade_date')
                if hsgt_stock.empty:
                    continue
                hrow = hsgt_stock.iloc[-1]
                mask = result['ts_code'] == tc
                result.loc[mask, 'hsgt_has_hold'] = 1
                result.loc[mask, 'hsgt_hold_ratio'] = hrow['hold_ratio']
                if len(hsgt_stock) >= 5:
                    ratio_5d_ago = hsgt_stock['hold_ratio'].iloc[-5]
                    result.loc[mask, 'hsgt_ratio_chg_5d'] = hrow['hold_ratio'] - ratio_5d_ago
    except Exception as e:
        logger.warning(f"V6.6 北向资金特征失败: {e}")

    for col in ['hsgt_has_hold', 'hsgt_hold_ratio', 'hsgt_ratio_chg_5d']:
        if col not in result.columns:
            result[col] = 0.0

    # ---- 5. 业绩预告超预期特征 ----
    try:
        forecast_df = pd.read_sql(f"""
            SELECT ts_code, end_date, report_date, forecast_type,
                   net_profit_min, net_profit_max
            FROM stock_forecast
            WHERE ts_code IN ({placeholders})
            ORDER BY ts_code, report_date DESC
        """, conn, params=tuple(ts_codes))

        if not forecast_df.empty:
            forecast_latest = forecast_df.drop_duplicates(subset='ts_code', keep='first')
            for _, frow in forecast_latest.iterrows():
                tc = frow['ts_code']
                mask = result['ts_code'] == tc
                ftype = str(frow['forecast_type'])
                type_map = {'预增': 2, '扭亏': 2, '略增': 1, '续盈': 1,
                            '略减': -1, '预减': -2, '首亏': -2, '续亏': -3}
                result.loc[mask, 'forecast_type_code'] = type_map.get(ftype, 0)
                result.loc[mask, 'forecast_is_positive'] = 1 if ftype in ('预增', '扭亏', '略增', '续盈') else 0
                result.loc[mask, 'forecast_net_profit_max'] = frow['net_profit_max']
                try:
                    rd = str(frow['report_date'])[:10]
                    ad = trade_date_str
                    days = (datetime.strptime(ad, '%Y-%m-%d') - datetime.strptime(rd, '%Y-%m-%d')).days
                    result.loc[mask, 'forecast_days_since'] = days
                except Exception:
                    result.loc[mask, 'forecast_days_since'] = 30
    except Exception as e:
        logger.warning(f"V6.6 业绩预告特征失败: {e}")

    for col in ['forecast_type_code', 'forecast_is_positive', 'forecast_net_profit_max', 'forecast_days_since']:
        if col not in result.columns:
            result[col] = 0.0

    # ---- V6.6 横截面排名特征 ----
    rank_features_v6_6 = [
        'bt_premium_weighted_30d', 'bt_amount_30d', 'bt_count_30d',
        'sector_elg_net_today', 'sector_elg_net_cum5',
        'concept_mom_rank',
        'hsgt_hold_ratio', 'hsgt_ratio_chg_5d',
        'forecast_type_code', 'forecast_is_positive',
    ]
    for col in rank_features_v6_6:
        if col in result.columns:
            result[f'{col}_rank'] = result[col].rank(pct=True).fillna(0.5)

    return result


# ========== V8.0 特征构建（改进标签 + 新特征） ==========

def _build_features_for_stocks_v8_0(conn, ts_codes, as_of_date=None):
    """
    V8.0 特征 — V6.5 全部特征 + 3 个新特征 + 大宗交易/业绩预告
      1. ret_1d_reversal: 昨日涨跌幅反号（A股短期反转效应）
      2. volume_div_days_10d: 过去10天缩量上涨天数
      3. turnover_std_ratio: 20日换手率变异系数
      4. bt_premium_rank: 大宗交易溢价率排名（泄漏修复版）
      5. forecast_surprise_rank: 业绩预告类型排名（泄漏修复版）
    """
    result = _build_features_for_stocks_v6_3(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result

    trade_date_str = str(as_of_date)[:10] if as_of_date else datetime.now().strftime('%Y-%m-%d')
    placeholders = ','.join(['%s'] * len(ts_codes))

    # ---- 1. ret_1d_reversal: 短期反转 ----
    try:
        prior_day = pd.read_sql(f"""
            SELECT ts_code, pct_chg
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
            ORDER BY ts_code, trade_date DESC
        """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
        if not prior_day.empty:
            prior_day = prior_day.drop_duplicates(subset='ts_code', keep='first')
            prior_day['ret_1d_reversal'] = -prior_day['pct_chg'] / 100.0
            result = result.merge(prior_day[['ts_code', 'ret_1d_reversal']], on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V8.0 短期反转特征失败: {e}")

    # ---- 2. volume_div_days_10d: 缩量上涨天数 ----
    try:
        vol_div = pd.read_sql(f"""
            SELECT ts_code,
                   SUM(CASE WHEN pct_chg > 0 AND volume_ratio < 1.0 THEN 1 ELSE 0 END) as volume_div_days_10d
            FROM (
                SELECT ts_code, pct_chg, volume_ratio
                FROM daily_price
                WHERE ts_code IN ({placeholders})
                  AND trade_date < %s
                  AND trade_date >= DATE_SUB(%s, INTERVAL 15 DAY)
                ORDER BY ts_code, trade_date DESC
                LIMIT 10
            ) sub
            GROUP BY ts_code
        """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
        if not vol_div.empty:
            result = result.merge(vol_div, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V8.0 缩量上涨特征失败: {e}")

    # ---- 3. turnover_std_ratio: 换手率变异系数 ----
    try:
        turnover_stats = pd.read_sql(f"""
            SELECT ts_code,
                   STD(turnover_rate) / NULLIF(AVG(turnover_rate), 0) as turnover_std_ratio
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
            GROUP BY ts_code
        """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
        if not turnover_stats.empty:
            result = result.merge(turnover_stats, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V8.0 换手率变异系数特征失败: {e}")

    # ---- 4. 大宗交易溢价率排名（泄漏修复版） ----
    try:
        bt_df = pd.read_sql(f"""
            SELECT ts_code,
                   COALESCE(AVG(premium_rate), 0) as bt_premium_avg_30d,
                   COUNT(*) as bt_count_30d,
                   COALESCE(SUM(premium_rate * deal_amount) / NULLIF(SUM(deal_amount), 0), 0) as bt_premium_weighted_30d
            FROM block_trade
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 35 DAY)
            GROUP BY ts_code
        """, conn, params=(*ts_codes, trade_date_str, trade_date_str))
        if not bt_df.empty:
            result = result.merge(bt_df, on='ts_code', how='left')
    except Exception as e:
        logger.warning(f"V8.0 大宗交易特征失败: {e}")

    # ---- 5. 业绩预告特征（泄漏修复版） ----
    try:
        forecast_df = pd.read_sql(f"""
            SELECT ts_code, forecast_type,
                   COALESCE(net_profit_max, 0) as forecast_net_profit_max,
                   report_date
            FROM stock_forecast
            WHERE ts_code IN ({placeholders})
              AND (report_date IS NULL OR report_date <= %s)
            ORDER BY ts_code, report_date DESC
        """, conn, params=(*ts_codes, trade_date_str))
        if not forecast_df.empty:
            forecast_latest = forecast_df.drop_duplicates(subset='ts_code', keep='first')
            type_map = {'预增': 2, '扭亏': 2, '略增': 1, '续盈': 1,
                        '略减': -1, '预减': -2, '首亏': -2, '续亏': -3}
            forecast_latest['forecast_type_code'] = forecast_latest['forecast_type'].map(type_map).fillna(0)
            forecast_latest['forecast_is_positive'] = forecast_latest['forecast_type'].isin(
                ('预增', '扭亏', '略增', '续盈')
            ).astype(int)
            trade_date_dt = pd.Timestamp(trade_date_str)
            forecast_latest['forecast_days_since'] = forecast_latest['report_date'].apply(
                lambda x: max((trade_date_dt - pd.Timestamp(x)).days, 0) if pd.notna(x) else 30
            ).clip(0, 365).fillna(30).astype(int)
            result = result.merge(
                forecast_latest[['ts_code', 'forecast_type_code', 'forecast_is_positive',
                                 'forecast_net_profit_max', 'forecast_days_since']],
                on='ts_code', how='left'
            )
    except Exception as e:
        logger.warning(f"V8.0 业绩预告特征失败: {e}")

    # ---- 填充默认值 ----
    for col in ['ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
                'bt_premium_avg_30d', 'bt_count_30d', 'bt_premium_weighted_30d',
                'forecast_type_code', 'forecast_is_positive', 'forecast_net_profit_max',
                'forecast_days_since']:
        if col not in result.columns:
            result[col] = 0.0

    # ---- V8.0 横截面排名特征 ----
    rank_features_v8_0 = [
        'ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
        'bt_premium_avg_30d', 'bt_count_30d', 'bt_premium_weighted_30d',
        'forecast_type_code', 'forecast_is_positive', 'forecast_net_profit_max',
        'forecast_days_since',
    ]
    for col in rank_features_v8_0:
        if col in result.columns:
            result[f'{col}_rank'] = result[col].rank(pct=True).fillna(0.5)

    return result


def _build_features_for_stocks_v10_0(conn, ts_codes, as_of_date=None):
    """
    V10.0 特征构建 — V8.0 全部特征 + 26 个时序衍生特征

    V10.0 新增特征分为三类：
    A. 可从 V8.0 输出直接计算（无需历史数据）: 7 个
    B. 需每日行情历史 SQL 计算（窗口/滚动）: 16 个
    C. 需 Alpha 信号表: 3 个
    """
    import time
    t0 = time.time()
    logger = logging.getLogger(__name__)

    result = _build_features_for_stocks_v8_0(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result

    trade_date_str = str(as_of_date)[:10] if as_of_date else datetime.now().strftime('%Y-%m-%d')

    # ===== A. 从 V8.0 输出直接计算 =====
    # money_flow_div_5d/10d
    if 'main_cum5' in result.columns and 'chg_5d' in result.columns:
        result['money_flow_div_5d'] = result['main_cum5'] - result['chg_5d']
        result['money_flow_div_10d'] = (result.get('main_cum10', result['main_cum5'])
                                        - result.get('chg_10d', result['chg_5d']))

    # ma_fan_out
    if all(c in result.columns for c in ['ma5', 'ma10', 'ma20', 'close']):
        ma_std = np.std([result['ma5'].values, result['ma10'].values, result['ma20'].values], axis=0)
        result['ma_fan_out'] = ma_std / result['close'].replace(0, np.nan)
        result['ma_fan_out'] = result['ma_fan_out'].fillna(0)

    # limit_proximity: 从 pos_52w 反推
    if 'pos_52w' in result.columns:
        result['limit_proximity'] = (result['pos_52w'] - 1).clip(-0.1, 0)

    # range_ratio_20d, close_to_high_ratio, price_channel_pos
    if all(c in result.columns for c in ['close', 'high_20d', 'low_20d']):
        h20 = result['high_20d'].replace(0, np.nan)
        l20 = result['low_20d']
        result['range_ratio_20d'] = (h20 - l20) / result['close'].replace(0, np.nan)
        denom = (h20 - l20).replace(0, np.nan)
        result['close_to_high_ratio'] = (result['close'] - l20) / denom
        result['price_channel_pos'] = result['close_to_high_ratio']
        result[['range_ratio_20d', 'close_to_high_ratio', 'price_channel_pos']] = \
            result[['range_ratio_20d', 'close_to_high_ratio', 'price_channel_pos']].fillna(0.5)

    # ===== B. 需历史数据：提取过去交易日数据计算时序特征 =====
    placeholders = ','.join(['%s'] * len(ts_codes))
    lookback = trade_date_str

    try:
        hist = pd.read_sql(f"""
            SELECT ts_code, trade_date, `open`, `close`, `high`, `low`, `pre_close`,
                   pct_chg, vol, amount, turnover_rate, volume_ratio
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 80 DAY)
            ORDER BY ts_code, trade_date
        """, conn, params=(*ts_codes, lookback, lookback))

        mf = pd.read_sql(f"""
            SELECT ts_code, trade_date, main_net
            FROM moneyflow_daily
            WHERE ts_code IN ({placeholders})
              AND trade_date < %s
              AND trade_date >= DATE_SUB(%s, INTERVAL 80 DAY)
            ORDER BY ts_code, trade_date
        """, conn, params=(*ts_codes, lookback, lookback))

        try:
            alpha = pd.read_sql("""
                SELECT ts_code, signal_date, max_boost
                FROM alpha_signals
                WHERE signal_date <= %s
                  AND signal_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                ORDER BY ts_code, signal_date
            """, conn, params=(lookback, lookback))
        except Exception:
            alpha = pd.DataFrame()

        # 合并资金流
        if not mf.empty:
            hist = hist.merge(mf, on=['ts_code', 'trade_date'], how='left')

        # 按股票分组计算时序特征
        feature_dfs = []
        for ts_code, grp in hist.groupby('ts_code'):
            grp = grp.sort_values('trade_date').reset_index(drop=True)

            grp['chg_5d'] = grp['pct_chg'].rolling(5).sum()
            grp['chg_10d'] = grp['pct_chg'].rolling(10).sum()
            grp['vol_5d'] = grp['vol'].rolling(5).mean()
            grp['vol_20d'] = grp['vol'].rolling(20).mean()

            # 资金流派生
            if 'main_net' in grp.columns and grp['main_net'].notna().sum() > 5:
                grp['amount_est'] = grp['vol'] * grp['close'] * 100
                grp['main_net_ratio'] = grp['main_net'] / grp['amount_est'].replace(0, np.nan)
                grp['main_net_ma5'] = grp['main_net_ratio'].rolling(5).mean()
                grp['main_net_ma10'] = grp['main_net_ratio'].rolling(10).mean()
                grp['main_cum5'] = grp['main_net_ratio'].rolling(5).sum()
                grp['main_cum10'] = grp['main_net_ratio'].rolling(10).sum()
            else:
                for c in ['main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_cum5', 'main_cum10']:
                    grp[c] = 0

            grp['gap_ratio'] = (grp['open'] - grp['pre_close']) / grp['pre_close'].replace(0, np.nan)
            grp['high_52w'] = grp['close'].rolling(250).max()
            grp['low_60d'] = grp['close'].rolling(60).min()
            grp['high_60d'] = grp['close'].rolling(60).max()

            delta = grp['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            grp['rsi_14'] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

            # MACD 近似
            ema12 = grp['close'].ewm(span=12, adjust=False).mean()
            ema26 = grp['close'].ewm(span=26, adjust=False).mean()
            macd_diff = ema12 - ema26
            macd_signal = macd_diff.ewm(span=9, adjust=False).mean()
            grp['macd_hist'] = (macd_diff - macd_signal) / grp['close']

            # ---- 计算 V10.0 特征 ----
            pct = grp['pct_chg']
            out = {'ts_code': ts_code}

            out['mom_accel_5d'] = _safe_last(grp['chg_5d'] - grp['chg_5d'].shift(5), 0)
            out['mom_accel_10d'] = _safe_last(grp['chg_10d'] - grp['chg_10d'].shift(10), 0)
            out['vol_regime_change'] = _safe_last(grp['vol_20d'] / grp['vol_20d'].shift(20).replace(0, np.nan) - 1, 0)
            out['vol_clustering_10d'] = _safe_last((grp['vol_5d'] > grp['vol_5d'].rolling(20).mean()).rolling(10).mean(), 0)
            out['vol_mean_revert'] = _safe_last(
                (grp['vol_20d'] - grp['vol_20d'].rolling(60).mean()) / grp['vol_20d'].rolling(60).std().replace(0, np.nan), 0)
            out['turnover_accel'] = _safe_last(
                (grp['turnover_rate'] / grp['turnover_rate'].rolling(20).mean().replace(0, np.nan)).diff(5), 0)
            out['flow_accel_5d'] = _safe_last(grp['main_net_ma5'].diff(5), 0)
            pos_rets = pct.clip(lower=0)
            neg_rets = pct.clip(upper=0)
            out['ret_asymmetry'] = _safe_last(
                pos_rets.rolling(20).mean() / (neg_rets.rolling(20).mean().abs() + 1e-9), 1.0)
            out['ret_autocorr_5d'] = pct.rolling(5).apply(
                lambda x: x.autocorr() if len(x.dropna()) > 2 else 0).iloc[-1] if len(grp) > 5 else 0
            out['gap_trend_10d'] = _safe_last((grp['gap_ratio'] > 0).rolling(10).mean(), 0.5)
            ma5 = grp['close'].rolling(5).mean()
            ma10 = grp['close'].rolling(10).mean()
            ma_cross = (ma5 > ma10).astype(int)
            out['ma_cross_signal'] = _safe_last(ma_cross - ma_cross.shift(5).fillna(0).astype(int), 0)
            out['rsi_momentum'] = _safe_last(grp['rsi_14'] - grp['rsi_14'].shift(5), 0)
            out['macd_cross_accel'] = _safe_last(grp['macd_hist'].diff(3), 0)
            out['sharp_up_days'] = _safe_last((pct > 3.0).rolling(20).sum(), 0)
            out['sharp_down_days'] = _safe_last((pct < -3.0).rolling(20).sum(), 0)
            out['limit_proximity'] = _safe_last(
                (grp['close'] / grp['high_52w'].replace(0, np.nan) - 1).clip(-0.1, 0), 0)
            out['price_channel_pos'] = _safe_last(
                (grp['close'] - grp['low_60d']) / (grp['high_60d'] - grp['low_60d'] + 1e-9), 0.5)

            # vol_price_div_trend: 量价关系趋势
            if len(grp) >= 10:
                x_arr = np.arange(10)
                vs = grp['vol'].iloc[-10:].values
                ps = pct.iloc[-10:].values
                valid = ~(np.isnan(vs) | np.isnan(ps))
                if valid.sum() > 2:
                    out['vol_price_div_trend'] = float(np.polyfit(x_arr[valid], ps[valid] * np.nanmean(vs), 1)[0])
                else:
                    out['vol_price_div_trend'] = 0
            else:
                out['vol_price_div_trend'] = 0

            feature_dfs.append(out)

        if feature_dfs:
            v10_feats = pd.DataFrame(feature_dfs)
            for col in v10_feats.columns:
                if col != 'ts_code':
                    v10_feats[col] = v10_feats[col].fillna(0).replace([np.inf, -np.inf], 0)
            result = result.merge(v10_feats, on='ts_code', how='left')

        # ===== C. Alpha 信号特征 =====
        if not alpha.empty:
            alpha = alpha.sort_values('signal_date')
            alpha_pos = alpha.groupby('ts_code').apply(
                lambda g: pd.Series({
                    'alpha_pos_5d': g['max_boost'].clip(lower=0).rolling(5, min_periods=1).sum().iloc[-1],
                    'alpha_neg_5d': g['max_boost'].clip(upper=0).abs().rolling(5, min_periods=1).sum().iloc[-1],
                }), include_groups=False
            ).reset_index()
            result = result.merge(alpha_pos, on='ts_code', how='left')

    except Exception as e:
        logger.warning(f"V10.0 特征计算失败: {e}")
        import traceback
        logger.warning(traceback.format_exc())

    # 确保所有 V10.0 特征列存在
    v10_cols = [
        'mom_accel_5d', 'mom_accel_10d',
        'vol_regime_change', 'vol_clustering_10d', 'vol_mean_revert',
        'vol_price_div_trend', 'turnover_accel',
        'money_flow_div_5d', 'money_flow_div_10d', 'flow_accel_5d',
        'ret_asymmetry', 'ret_autocorr_5d',
        'gap_trend_10d', 'limit_proximity', 'price_channel_pos',
        'ma_fan_out', 'ma_cross_signal', 'rsi_momentum', 'macd_cross_accel',
        'sharp_up_days', 'sharp_down_days',
        'range_ratio_20d', 'close_to_high_ratio',
        'alpha_pos_5d', 'alpha_neg_5d',
    ]
    for col in v10_cols:
        if col not in result.columns:
            result[col] = 0.0

    elapsed = time.time() - t0
    logger.info(f"V10.0 特征构建完成: {result.shape}, 耗时 {elapsed:.1f}s")
    return result


def _build_features_for_stocks_v8_6(conn, ts_codes, as_of_date=None):
    """V8.6 特征构建 — V6 核心特征 (52 base + 10 rank = 62 总特征)"""
    result = _build_features_for_stocks_v6_3(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result
    # V8.6 uses only V6 base features + 10 core rank features
    v86_features = [
        'pct_chg', 'turnover_rate', 'volume_ratio', 'vol_5d', 'vol_10d', 'vol_20d',
        'ma5_ma10_ratio', 'ma10_ma20_ratio', 'price_ma5_ratio', 'price_ma20_ratio',
        'chg_3d', 'chg_5d', 'chg_10d', 'chg_20d', 'vol_trend', 'pos_52w',
        'rps_20', 'rps_change', 'up_ratio_5d', 'up_ratio_10d', 'vol_pct_corr',
        'ma_pattern', 'macd_diff', 'macd_signal_line', 'macd_hist',
        'rsi_14', 'adx_14',
        'main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_trend',
        'main_streak', 'main_vs_retail', 'lg_ratio', 'main_cum5', 'main_cum10',
        'main_accel_3d', 'smart_div_count', 'main_inflow_ratio', 'main_flow_accel',
        'vol_price_corr_10d', 'gap_ratio', 'gap_retention', 'amihud', 'amihud_ma5',
        'vol_price_divergence', 'vol_price_div_10d', 'ma_spread_stock',
        'rzye_chg', 'rzmre_ratio', 'ind_mom_5d', 'ind_mom_20d',
        'pct_chg_rank', 'turnover_rate_rank', 'volume_ratio_rank', 'rps_20_rank',
        'lg_ratio_rank', 'main_net_ratio_rank', 'pos_52w_rank',
        'chg_5d_rank', 'chg_10d_rank', 'main_cum5_rank',
    ]
    keep = [f for f in ['ts_code'] + v86_features if f in result.columns]
    result = result[keep]
    return result


def _build_features_for_stocks_v9_0(conn, ts_codes, as_of_date=None):
    """V9.0 特征构建 — 复用 V8.0 构建器，移除所有 _rank 横截面排名特征"""
    result = _build_features_for_stocks_v8_0(conn, ts_codes, as_of_date=as_of_date)
    if result.empty:
        return result
    # V9.0: 移除所有 rank 特征（训练时已排除）
    rank_cols = [c for c in result.columns if c.endswith('_rank')]
    if rank_cols:
        result = result.drop(columns=rank_cols)
    return result


def predict_batch(ts_codes, db_conn=None, as_of_date=None):
    """批量预测 — 自动选择最佳可用模型"""
    bundle, version = _load_best_model()
    if bundle is None:
        return {c: {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0, 'model_type': 'no_model'} for c in ts_codes}
    should_close = False
    if db_conn is None:
        engine = _get_engine()
        db_conn = engine.connect()
        should_close = True
    try:
        if version == "v11.0":
            from scripts.predict_v11 import build_features_v11_inference
            feat_df = build_features_v11_inference(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v10.0":
            feat_df = _build_features_for_stocks_v8_0(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v9.0":
            feat_df = _build_features_for_stocks_v9_0(db_conn, ts_codes, as_of_date=as_of_date)
        elif version in ("v8.1", "v8.6", "v8.0"):
            feat_df = _build_features_for_stocks_v8_0(db_conn, ts_codes, as_of_date=as_of_date)
        elif version in ("v6.7", "v6.6"):
            feat_df = _build_features_for_stocks_v6_6(db_conn, ts_codes, as_of_date=as_of_date)
        elif version in ("v6.5", "v6.4"):
            feat_df = _build_features_for_stocks_v6_3(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v6.3":
            feat_df = _build_features_for_stocks_v6_3(db_conn, ts_codes, as_of_date=as_of_date)
        elif version == "v6.2":
            feat_df = _build_features_for_stocks_v6_2(db_conn, ts_codes, as_of_date=as_of_date)
        else:
            feat_df = _build_features_for_stocks_v6(db_conn, ts_codes, as_of_date=as_of_date)
        if feat_df.empty:
            return {c: {'probability': 0.5, 'is_likely_up': False, 'predicted_return': 0, 'model_type': 'no_data'} for c in ts_codes}

        if version in ("v11.0", "v8.1", "v10.0", "v8.6", "v9.0", "v8.0", "v6.7", "v6.6", "v6.5", "v6.4", "v6.2", "v6.3") and 'models' in bundle:
            scores_df = _ensemble_scores(feat_df, bundle)
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
            feat_df["__raw__"] = pred_returns
            scores_df = _ensemble_scores(feat_df, bundle)

        codes_list = feat_df['ts_code'].tolist()
        results = {}
        for i, (_, row) in enumerate(scores_df.iterrows()):
            code = codes_list[i] if i < len(codes_list) else str(i)
            z_score = float(row['z_score'])
            prob = round(float(row['probability']), 3)
            results[code] = {
                'probability': prob,
                'predicted_return': round(z_score, 2),
                'is_likely_up': z_score > 0,
                'model_type': version,
                'model_task': 'lambdarank',
            }
        if len(ts_codes) > 1:
            rank_ic = bundle.get('final_rank_ic', 0)
            model_label = f'{version}集成(IC={rank_ic:.3f})' if version and (version.startswith('v') and '.' in version) else str(version or 'unknown')
            global _last_scan_results
            _last_scan_results = {
                tc: {
                    'ml概率': float(r['probability']),
                    '排序强度': float(r['predicted_return']),
                    'ml看涨': bool(r['is_likely_up']),
                    '模型名称': model_label,
                }
                for tc, r in results.items()
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
    model_label = f'V8.0集成(IC={rank_ic:.3f})' if version == 'v8.0' else (
        f'V6.7集成(IC={rank_ic:.3f})' if version == 'v6.7' else (
        f'V6.6集成(IC={rank_ic:.3f})' if version == 'v6.6' else (
        f'V6.5集成(IC={rank_ic:.3f})' if version == 'v6.5' else (
        f'V6.4集成(IC={rank_ic:.3f})' if version == 'v6.4' else (
        f'V6.3集成(IC={rank_ic:.3f})' if version == 'v6.3' else (
        f'V6.2集成(IC={rank_ic:.3f})' if version == 'v6.2' else f'V6(IC={rank_ic:.3f})'))))))

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
    from sector_rotation import _build_industry_map, get_fund_flow_continuity, get_hot_sectors, get_sector_bonus
    hot_sectors = get_hot_sectors(top_n=8, db_conn=db_conn)
    industry_map = _build_industry_map(list(code_map.keys()), db_conn)  # 批量加载，避免N次SQL
    # 批量扫描结果写入缓存（供个股分析页面直接引用）
    scan_cache = {}
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
        s['排序强度'] = round(pred_ret, 2); s['热点板块'] = sector_name
        s['资金趋势'] = flow['trend']; s['资金连续'] = flow['continuous_inflow']
        s['市场状态'] = f'{model_label}'
        scan_cache[ts_code] = {
            'ml概率': round(prob, 3), '排序强度': round(pred_ret, 2),
            'ml看涨': is_up, '资金趋势': flow['trend'],
            '模型名称': model_label,
        }
    global _last_scan_results
    _last_scan_results = scan_cache
    # 保存预测快照
    _save_prediction_snapshot(stocks_list, version)
    return stocks_list


def _save_prediction_snapshot(predictions, version):
    """保存预测快照到 data/predictions_{date}_{version}.json"""
    today = datetime.now().strftime("%Y%m%d")
    path = os.path.join(BASE_DIR, "data", f"predictions_{today}_{version}.json")

    # 简化预测记录（只保留核心字段）
    simplified = []
    for p in predictions[:200]:  # 只保存前 200 只
        simplified.append({
            "ts_code": p.get("ts_code") or p.get("_ts_code", ""),
            "name": p.get("name", ""),
            "close": p.get("close", 0),
            "ml_prob": p.get("ml概率", 0),
            "ml_bullish": p.get("ml看涨", False),
            "enhanced_score": p.get("增强评分", 0),
            "predicted_return": p.get("预测收益", 0),
        })

    snapshot = {
        "date": today,
        "version": version,
        "n_stocks": len(predictions),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "predictions": simplified,
    }

    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        logger.info(f"预测快照已保存: {path} ({len(simplified)} 只)")
    except Exception as e:
        logger.warning(f"预测快照保存失败: {e}")
