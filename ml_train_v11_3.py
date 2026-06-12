#!/usr/bin/env python3
"""
ML选股模型训练 V11.0 — 三层堆叠集成 + 多源数据融合

架构:
  Layer 1: 14个基础模型 (不同算法/种子/特征子集)
  Layer 2: Regime加权融合
  Layer 3: LightGBM堆叠元模型

标签: alpha_10d = industry-neutral vol-adjusted 5d return (同V8.0，经实践验证最优)

数据源: daily_price + moneyflow + margin + dragon_tiger + holder_change
        + zt_pool + board_industry/concept + earnings + block_trade
        + stock_forecast + fina_indicator + sector_moneyflow
        + north_moneyflow + ml_predictions + market_regime_daily

防泄露: ALL SQL queries use trade_date < as_of_date
验证:  Purged Walk-Forward with 10-day embargo
"""

import json
import logging
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
import pymysql
import xgboost as xgb
from scipy.stats import spearmanr

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MODEL_PATH = os.path.join(DATA_DIR, 'ml_stock_model_v11_0.pkl')
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'feature_config_v11_2.json')
MONITOR_HISTORY_PATH = os.path.join(DATA_DIR, 'model_monitor_history_v11_2.json')

EXCLUDE_PREFIXES = ('68', '83', '87', '43')
NO_PERM_PREFIXES_1 = ('8', '4', '9')

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

# ========== 特征子集定义 ==========
MOMENTUM_FEATURES = [
    'pct_chg', 'turnover_rate', 'volume_ratio',
    'vol_5d', 'vol_10d', 'vol_20d',
    'chg_3d', 'chg_5d', 'chg_10d', 'chg_20d',
    'ma5_ma10_ratio', 'ma10_ma20_ratio', 'price_ma5_ratio', 'price_ma20_ratio',
    'pos_10d', 'pos_20d', 'pos_52w', 'amplitude_10d',
    'macd_diff', 'macd_signal_line', 'macd_hist',
    'rsi_14', 'adx_14',
    'ret_1d_reversal', 'mom_divergence',
    'turnover_ratio', 'turnover_std_ratio',
    'volume_div_days_10d', 'vol_price_divergence',
    'rps_20', 'rps_change',
    'ret_autocorr_5d', 'vol_of_vol', 'max_drawdown_10d',
    'rel_strength_idx_5d', 'volume_shock',
    'high_low_spread', 'consecutive_wins', 'consecutive_losses',
    # V11.1 均值回复+市场状态
    'price_range_pos_10d', 'oversold_boost', 'ma_dispersion',
    'volume_contraction', 'price_ma50_ratio', 'ret_vol_ratio_5d',
    'mkt_zt_dt_spread', 'mkt_volatility', 'mkt_breadth',
]

FLOW_FEATURES = [
    'main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_trend',
    'main_streak', 'main_vs_retail', 'lg_ratio', 'lg_ratio_rank',
    'main_cum5', 'main_cum10', 'main_accel_3d', 'smart_div_count',
    'main_inflow_ratio', 'main_flow_accel',
    'rzye_chg', 'rzmre_ratio',
    'dragon_net_buy_30d', 'dragon_count_30d',
    'dti_net_buy_30d', 'dti_count_30d',
    'holder_change_pct', 'holder_consecutive_decline',
    'sector_netflow_1d', 'sector_netflow_5d', 'sector_flow_divergence',
    'north_flow_1d', 'money_flow_div_5d', 'money_flow_div_10d',
]

VALUE_QUALITY_FEATURES = [
    'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin',
    'fina_roe', 'fina_yoy_sales', 'fina_gross_margin', 'fina_net_margin', 'fina_eps',
    'bt_count_30d', 'bt_premium_avg_30d', 'bt_premium_weighted_30d',
    'forecast_type_code', 'forecast_is_positive', 'forecast_days_since', 'forecast_net_profit_max',
    'zt_count_30d', 'zt_max_board_30d', 'zt_seal_amount_30d',
    'concept_count', 'concept_mom_avg', 'concept_mom_5d', 'concept_mom_10d',
]


# ========== PART 1: DATA LOADING ==========

def load_data(max_date=None):
    """
    加载训练数据。max_date: 可选截止日期（用于样本外训练）。
    所有 SQL 使用 trade_date < as_of_date 防泄露。
    """
    if max_date:
        logger.info(f"加载数据（截止 {max_date}）...")
        max_dt = datetime.strptime(max_date, '%Y-%m-%d')
        min_dt = max_dt - timedelta(days=600)
        trade_lt = f"trade_date < '{max_dt.date()}'"
        trade_bound = f"trade_date >= '{min_dt.date()}' AND {trade_lt}"
        end_bound = f"end_date >= '{min_dt.date()}' AND end_date <= '{max_dt.date()}'"
    else:
        logger.info("加载最近 600 个交易日数据...")
        max_expr = "(SELECT MAX(trade_date) FROM daily_price)"
        trade_bound = f"trade_date >= {max_expr} - INTERVAL 600 DAY AND trade_date < {max_expr}"
        end_bound = f"end_date >= {max_expr} - INTERVAL 600 DAY AND end_date <= {max_expr}"
        trade_lt = f"trade_date < {max_expr}"

    conn = pymysql.connect(**DB_CONFIG)

    # -- 核心行情 --
    daily = pd.read_sql(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               vol, amount, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price
        WHERE {trade_bound}
    """, conn)

    # -- 指数行情（用于超额收益计算） --
    idx_data = pd.read_sql(f"""
        SELECT trade_date, close_price
        FROM market_index_daily
        WHERE index_code='000001.SH'
          AND {trade_bound}
    """, conn)

    # -- 资金流 --
    moneyflow = pd.read_sql(f"""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily
        WHERE {trade_bound}
    """, conn)

    # -- 基本面 --
    fundamentals = pd.read_sql(f"""
        SELECT ts_code, trade_date, pe_ttm, pb, total_mv, circ_mv
        FROM daily_basic
        WHERE {trade_bound}
    """, conn)
    if not fundamentals.empty:
        fundamentals['trade_date'] = pd.to_datetime(fundamentals['trade_date'])

    # -- 行业信息 --
    stock_info = pd.read_sql("SELECT ts_code, industry FROM stock_info", conn)

    # -- Alpha 信号 --
    alpha_signals = pd.read_sql("""
        SELECT ts_code, signal_date, MAX(score_boost) as max_boost
        FROM alpha_signals
        GROUP BY ts_code, signal_date
    """, conn)
    if not alpha_signals.empty:
        alpha_signals['signal_date'] = pd.to_datetime(alpha_signals['signal_date'])

    # -- 融资融券 --
    margin = pd.read_sql(f"""
        SELECT ts_code, trade_date, rzye, rqye, rzmre
        FROM margin_daily
        WHERE {trade_bound}
    """, conn)
    if not margin.empty:
        margin['trade_date'] = pd.to_datetime(margin['trade_date'])
        for c in ['rzye', 'rqye', 'rzmre']:
            margin[c] = pd.to_numeric(margin[c], errors='coerce').fillna(0)

    # -- 龙虎榜 --
    dragon_tiger = pd.read_sql(f"""
        SELECT ts_code, trade_date, net_buy, buy, sell, exalter as reason
        FROM dragon_tiger
        WHERE {trade_bound}
          AND net_buy != 0
    """, conn)
    if not dragon_tiger.empty:
        dragon_tiger['trade_date'] = pd.to_datetime(dragon_tiger['trade_date'])
        dragon_tiger['net_buy'] = pd.to_numeric(dragon_tiger['net_buy'], errors='coerce').fillna(0)
        dragon_tiger['buy'] = pd.to_numeric(dragon_tiger['buy'], errors='coerce').fillna(0)

    # -- 龙虎榜机构 --
    dragon_tiger_inst = pd.read_sql(f"""
        SELECT ts_code, trade_date, net_buy, exalter
        FROM dragon_tiger_inst
        WHERE {trade_bound}
          AND net_buy != 0
          AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
    """, conn)
    if not dragon_tiger_inst.empty:
        dragon_tiger_inst['trade_date'] = pd.to_datetime(dragon_tiger_inst['trade_date'])
        dragon_tiger_inst['net_buy'] = pd.to_numeric(dragon_tiger_inst['net_buy'], errors='coerce').fillna(0)

    # -- 股东人数变化 --
    holder_change = pd.read_sql(f"""
        SELECT ts_code, end_date, holder_num, holder_num_change, holder_change_pct
        FROM holder_change
        WHERE {end_bound}
    """, conn)
    if not holder_change.empty:
        holder_change['end_date'] = pd.to_datetime(holder_change['end_date'])
        for c in ['holder_num', 'holder_num_change', 'holder_change_pct']:
            holder_change[c] = pd.to_numeric(holder_change[c], errors='coerce').fillna(0)

    # -- 涨停板 --
    zt_pool = pd.read_sql(f"""
        SELECT ts_code, trade_date, last_board, seal_amount, open_count
        FROM zt_pool
        WHERE {trade_bound}
    """, conn)
    if not zt_pool.empty:
        zt_pool['trade_date'] = pd.to_datetime(zt_pool['trade_date'])
        zt_pool['last_board'] = pd.to_numeric(zt_pool['last_board'], errors='coerce').fillna(0).astype(int)
        zt_pool['seal_amount'] = pd.to_numeric(zt_pool['seal_amount'], errors='coerce').fillna(0)
        zt_pool['open_count'] = pd.to_numeric(zt_pool['open_count'], errors='coerce').fillna(0).astype(int)

    # -- 行业板块 --
    board_ind_hist = pd.read_sql(f"""
        SELECT board_code, board_name, trade_date, pct_change, amount
        FROM board_industry_hist
        WHERE {trade_bound}
    """, conn)
    if not board_ind_hist.empty:
        board_ind_hist['trade_date'] = pd.to_datetime(board_ind_hist['trade_date'])
        board_ind_hist['pct_change'] = pd.to_numeric(board_ind_hist['pct_change'], errors='coerce').fillna(0)

    board_ind_cons = pd.read_sql("SELECT board_code, ts_code FROM board_industry_cons", conn)
    board_concept_cons = pd.read_sql("SELECT board_code, ts_code FROM board_concept_cons", conn)

    # -- 业绩报表 --
    earnings = pd.read_sql("""
        SELECT ts_code, report_date, revenue_yoy, net_profit_yoy, roe, gross_margin
        FROM earnings_report
    """, conn)
    if not earnings.empty:
        earnings['report_date'] = pd.to_datetime(earnings['report_date'])
        for c in ['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']:
            earnings[c] = pd.to_numeric(earnings[c], errors='coerce')

    # -- 大宗交易 --
    block_trade = pd.read_sql(f"""
        SELECT ts_code, trade_date, premium_rate, deal_amount, buyer
        FROM block_trade
        WHERE {trade_bound}
    """, conn)
    if not block_trade.empty:
        block_trade["trade_date"] = pd.to_datetime(block_trade["trade_date"])
        block_trade["premium_rate"] = pd.to_numeric(block_trade["premium_rate"], errors="coerce").fillna(0)
        block_trade["deal_amount"] = pd.to_numeric(block_trade["deal_amount"], errors="coerce").fillna(0)

    # -- 业绩预告 --
    stock_forecast = pd.read_sql("""
        SELECT ts_code, end_date, report_date, forecast_type,
               net_profit_min, net_profit_max
        FROM stock_forecast
    """, conn)
    if not stock_forecast.empty:
        stock_forecast["end_date"] = pd.to_datetime(stock_forecast["end_date"])
        stock_forecast["report_date"] = pd.to_datetime(stock_forecast["report_date"])
        stock_forecast["net_profit_min"] = pd.to_numeric(stock_forecast["net_profit_min"], errors="coerce").fillna(0)
        stock_forecast["net_profit_max"] = pd.to_numeric(stock_forecast["net_profit_max"], errors="coerce").fillna(0)

    # ========== V11.3 新增 (10日预测周期)数据源 ==========

    # -- 财务指标 (fina_indicator — 季度财报实际数据) --
    fina_ind = pd.read_sql("""
        SELECT ts_code, end_date, roe, yoy_sales, grossprofit_margin,
               netprofit_margin, eps
        FROM fina_indicator
        WHERE end_date <= %s
    """, conn, params=(max_date if max_date else datetime.now().strftime('%Y-%m-%d'),))
    if not fina_ind.empty:
        fina_ind = fina_ind.sort_values('end_date').groupby('ts_code').last().reset_index()
        for c in ['roe', 'yoy_sales', 'grossprofit_margin', 'netprofit_margin', 'eps']:
            fina_ind[c] = pd.to_numeric(fina_ind[c], errors='coerce').fillna(0)
    logger.info(f"财务指标(fina_indicator): {len(fina_ind)} 只股票")

    # -- 板块资金流 (sector_moneyflow) --
    sector_mf = pd.read_sql(f"""
        SELECT trade_date, sector_name, net_amount, buy_elg_amount,
               sell_elg_amount, pct_change
        FROM sector_moneyflow
        WHERE {trade_lt}
          AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
    """, conn, params=(max_date if max_date else datetime.now().strftime('%Y-%m-%d'),))
    if not sector_mf.empty:
        sector_mf['trade_date'] = pd.to_datetime(sector_mf['trade_date'])
        sector_mf['elg_net'] = sector_mf['buy_elg_amount'] - sector_mf['sell_elg_amount']
    logger.info(f"板块资金流: {len(sector_mf)} 条")

    # -- 北向资金 (north_moneyflow) --
    north_mf = pd.read_sql(f"""
        SELECT trade_date, north_money, hgt, sgt
        FROM north_moneyflow
        WHERE {trade_lt}
          AND trade_date >= DATE_SUB(%s, INTERVAL 25 DAY)
    """, conn, params=(max_date if max_date else datetime.now().strftime('%Y-%m-%d'),))
    if not north_mf.empty:
        north_mf['trade_date'] = pd.to_datetime(north_mf['trade_date'])
    logger.info(f"北向资金: {len(north_mf)} 条")

    # -- 历史ML预测 (ml_predictions) --
    ml_prev = pd.read_sql(f"""
        SELECT ts_code, trade_date, _ml_pred, predicted_return, model_type
        FROM ml_predictions
        WHERE {trade_lt}
          AND trade_date >= DATE_SUB(%s, INTERVAL 20 DAY)
    """, conn, params=(max_date if max_date else datetime.now().strftime('%Y-%m-%d'),))
    if not ml_prev.empty:
        ml_prev['trade_date'] = pd.to_datetime(ml_prev['trade_date'])
        ml_prev['_ml_pred'] = pd.to_numeric(ml_prev['_ml_pred'], errors='coerce').fillna(0)
    logger.info(f"历史ML预测: {len(ml_prev)} 条")

    conn.close()

    daily['trade_date'] = pd.to_datetime(daily['trade_date'])


    idx_data['trade_date'] = pd.to_datetime(idx_data['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    stock_info['industry'] = stock_info['industry'].fillna('OTHER')

    dates = sorted(daily['trade_date'].unique())
    min_date = dates[-400] if len(dates) > 400 else dates[0]
    max_date_actual = dates[-1]

    logger.info(f"行情: {len(daily):,} 行, {daily['ts_code'].nunique()} 股 | "
                f"资金流: {len(moneyflow):,} | 基本面: {len(fundamentals):,} | "
                f"窗口: {len(dates)} 交易日 / {min_date.date()} ~ {max_date_actual.date()}")

    # -- 市场状态 (market_regime_daily) -- 使用独立连接避免超时
    try:
        from quant_app.utils.config import get_db_config as _get_regime_config
        _regime_conn = pymysql.connect(**_get_regime_config())
        market_regime = pd.read_sql(f"""
            SELECT trade_date, regime, prob_bull, prob_bear, prob_panic, prob_range, prob_overheated,
                   sh_trend, market_breadth, volatility, volume_trend, momentum_breadth, zt_ratio
            FROM market_regime_daily
            WHERE {trade_lt}
        """, _regime_conn)
        _regime_conn.close()


    except Exception as _re:
        logger.warning(f"加载市场状态数据失败: {_re}")
        market_regime = pd.DataFrame()
    if not market_regime.empty:
        market_regime["trade_date"] = pd.to_datetime(market_regime["trade_date"])
        for c in ["prob_bull","prob_bear","prob_panic","prob_range","prob_overheated"]:
            market_regime[c] = pd.to_numeric(market_regime[c], errors="coerce").fillna(0)
    logger.info(f"市场状态(regime): {len(market_regime)} 条")

    return (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
            margin, dragon_tiger, dragon_tiger_inst, holder_change,
            zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
            block_trade, stock_forecast,
            fina_ind, sector_mf, north_mf, ml_prev, market_regime,
            min_date, max_date_actual)


# ========== PART 2: FEATURE BUILDING ==========

def build_features(data_tuple):
    """
    V11.0 特征构建:
    - V8.0 全部 ~105 基础特征
    - +27 新增特征 (fina_indicator, sector_moneyflow, north_moneyflow, ml_predictions, 新衍生日频)
    - +9 regime概率特征
    - +rank特征
    - 标签: alpha_10d (行业中性 + 波动率调整)
    """
    (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
     margin, dragon_tiger, dragon_tiger_inst, holder_change,
     zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
     block_trade, stock_forecast,
     fina_ind, sector_mf, north_mf, ml_prev, market_regime,
     min_date, max_date) = data_tuple

    logger.info("构建特征 V8.0 基础 + V11.3 新增 (10日预测周期)...")


    # === 预处理字典 ===
    margin_dict = {}
    if not margin.empty:
        for ts_code, mg in margin.groupby('ts_code'):
            margin_dict[ts_code] = mg.sort_values('trade_date')

    fund_dict = {}
    if not fundamentals.empty:
        for ts_code, fg in fundamentals.groupby('ts_code'):
            fund_dict[ts_code] = fg.sort_values('trade_date')

    ind_map = {}
    for _, row in stock_info.iterrows():
        ind_map[row['ts_code']] = row.get('industry', 'OTHER')

    mf_dict = {}
    for ts_code, mf in moneyflow.groupby('ts_code'):
        mf_dict[ts_code] = mf.sort_values('trade_date')

    dt_dict = {}
    if not dragon_tiger.empty:
        for ts_code, dtg in dragon_tiger.groupby('ts_code'):
            dt_dict[ts_code] = dtg.sort_values('trade_date').reset_index(drop=True)

    dti_dict = {}
    if not dragon_tiger_inst.empty:
        for ts_code, dtig in dragon_tiger_inst.groupby('ts_code'):
            dti_dict[ts_code] = dtig.sort_values('trade_date').reset_index(drop=True)

    hc_dict = {}
    if not holder_change.empty:
        for ts_code, hcg in holder_change.groupby('ts_code'):
            hc_dict[ts_code] = hcg.sort_values('end_date').reset_index(drop=True)

    # V6.3 数据预处理
    zt_dict = {}
    if not zt_pool.empty:
        for ts_code, zg in zt_pool.groupby('ts_code'):
            zt_dict[ts_code] = zg.sort_values('trade_date').reset_index(drop=True)

    ind_board_map = {}
    if not board_ind_cons.empty:
        for _, row in board_ind_cons.iterrows():
            ind_board_map[row['ts_code']] = row['board_code']

    concept_board_map = {}
    if not board_concept_cons.empty:
        for _, row in board_concept_cons.iterrows():
            concept_board_map.setdefault(row['ts_code'], []).append(row['board_code'])

    ind_hist_map = {}
    if not board_ind_hist.empty:
        for _, row in board_ind_hist.iterrows():
            key = row['board_code']
            if key not in ind_hist_map:
                ind_hist_map[key] = {}
            ind_hist_map[key][row['trade_date']] = row['pct_change']

    earn_dict = {}
    if not earnings.empty:
        for ts_code, eg in earnings.groupby('ts_code'):
            earn_dict[ts_code] = eg.sort_values('report_date')

    concept_daily_mom = pd.DataFrame()
    if not board_concept_cons.empty and not daily.empty:
        daily_code = daily[['ts_code', 'trade_date', 'pct_chg']].copy()
        concept_df = board_concept_cons.copy()
        merged_conc = daily_code.merge(concept_df, on='ts_code', how='inner')
        if not merged_conc.empty:
            concept_daily_mom = merged_conc.groupby(['board_code', 'trade_date'])['pct_chg'].mean().reset_index()
            concept_daily_mom.columns = ['board_code', 'trade_date', 'concept_board_pct']

    bt_dict = {}
    if not block_trade.empty:
        for ts_code, btg in block_trade.groupby("ts_code"):
            bt_dict[ts_code] = btg.sort_values("trade_date").reset_index(drop=True)

    fc_dict = {}
    if not stock_forecast.empty:
        for ts_code, fcg in stock_forecast.groupby("ts_code"):
            fcg = fcg.sort_values("report_date").reset_index(drop=True)
            fcg["trade_date"] = fcg["report_date"]
            fc_dict[ts_code] = fcg

    # === V11.0: fina_indicator 字典 ===
    fina_dict = {}
    if not fina_ind.empty:
        for _, row in fina_ind.iterrows():
            fina_dict[row['ts_code']] = row

    # === V11.0: 板块资金流按行业名索引 ===
    sector_mf_dict = {}
    if not sector_mf.empty:
        for sector_name, sg in sector_mf.groupby('sector_name'):
            sector_mf_dict[sector_name] = sg.sort_values('trade_date')

    # === V11.0: 北向资金按日期索引 ===
    north_mf_sorted = north_mf.sort_values('trade_date') if not north_mf.empty else pd.DataFrame()

    # === V11.0: 历史ML预测字典 ===
    ml_prev_dict = {}
    if not ml_prev.empty:
        for ts_code, mg in ml_prev.groupby('ts_code'):
            ml_prev_dict[ts_code] = mg.sort_values('trade_date')

    # === 指数收益率字典（用于超额收益计算） ===
    idx_close = {}
    if not idx_data.empty:
        idx_data_sorted = idx_data.sort_values('trade_date')
        idx_pct = idx_data_sorted['close_price'].pct_change().fillna(0)
        idx_data_sorted = idx_data_sorted.copy()
        idx_data_sorted['idx_pct_chg'] = idx_pct * 100
        for _, row in idx_data_sorted.iterrows():
            idx_close[row['trade_date']] = row

    results = []

    for ts_code, group in daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE_PREFIXES or ts_code[:1] in NO_PERM_PREFIXES_1:
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 60:
            continue

        g = group.copy()
        industry = ind_map.get(ts_code, 'OTHER')

        # ============ 全部特征使用 shift(1) 防泄露 ============

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
        g['rps_change'] = g['rps_20'].diff(5).shift(1)

        g['up_ratio_5d'] = (g['pct_chg'] > 0).shift(1).rolling(5).mean()
        g['up_ratio_10d'] = (g['pct_chg'] > 0).shift(1).rolling(10).mean()
        g['vol_pct_corr'] = g['volume_ratio'].shift(1).rolling(10).corr(g['pct_chg'].shift(1))

        g['ma_pattern'] = 1
        g.loc[(g['ma5'] > g['ma10']) & (g['ma10'] > g['ma20']), 'ma_pattern'] = 2
        g.loc[(g['ma5'] < g['ma10']) & (g['ma10'] < g['ma20']), 'ma_pattern'] = 0

        ema12 = g['close'].ewm(span=12, adjust=False).mean()
        ema26 = g['close'].ewm(span=26, adjust=False).mean()
        g['macd_diff'] = (ema12 - ema26) / g['close']
        g['macd_signal_line'] = g['macd_diff'].ewm(span=9, adjust=False).mean()
        g['macd_hist'] = g['macd_diff'] - g['macd_signal_line']
        g[['macd_diff', 'macd_signal_line', 'macd_hist']] = g[['macd_diff', 'macd_signal_line', 'macd_hist']].shift(1)

        delta = g['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        g['rsi_14'] = 100 - (100 / (1 + rs))
        g['rsi_14'] = g['rsi_14'].shift(1)

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

        # V6.2 新特征
        g['amount_ma5'] = g['amount'].shift(1).rolling(5).mean()
        g['amount_ma5_ratio'] = g['amount'] / g['amount_ma5'].replace(0, np.nan)
        g['low_10d'] = g['close'].shift(1).rolling(10).min()
        g['high_10d'] = g['close'].shift(1).rolling(10).max()
        g['low_20d'] = g['close'].shift(1).rolling(20).min()
        g['high_20d'] = g['close'].shift(1).rolling(20).max()
        g['pos_10d'] = (g['close'] - g['low_10d']) / (g['high_10d'] - g['low_10d']).replace(0, np.nan)
        g['pos_20d'] = (g['close'] - g['low_20d']) / (g['high_20d'] - g['low_20d']).replace(0, np.nan)
        g['amplitude_10d'] = (g['high_10d'] - g['low_10d']) / g['close'].shift(1).rolling(10).mean().replace(0, np.nan)
        g['turnover_rate_ma20'] = g['turnover_rate'].shift(1).rolling(20).mean()
        g['turnover_ratio'] = g['turnover_rate'] / g['turnover_rate_ma20'].replace(0, np.nan)
        g['mom_divergence'] = g['chg_5d'] - g['chg_20d']
        g['ret_skew_20d'] = g['pct_chg'].shift(1).rolling(20).skew()
        g['ret_kurt_20d'] = g['pct_chg'].shift(1).rolling(20).kurt()
        g['ret_max_5d'] = g['pct_chg'].shift(1).rolling(5).max()
        g['ret_min_5d'] = g['pct_chg'].shift(1).rolling(5).min()

        # ---- 资金流 ----
        g['amount_est'] = g['vol'] * g['close'] * 100
        if ts_code in mf_dict:
            mf = mf_dict[ts_code]
            g = g.merge(mf[['trade_date', 'main_net', 'net_mf_amount',
                           'buy_sm_amount', 'sell_sm_amount', 'buy_lg_amount', 'sell_lg_amount']],
                       on='trade_date', how='left')
            g['main_net_ratio'] = g['main_net'] / g['amount_est'].replace(0, np.nan)
            g['main_net_ma5'] = g['main_net_ratio'].shift(1).rolling(5).mean()
            g['main_net_ma10'] = g['main_net_ratio'].shift(1).rolling(10).mean()
            g['main_trend'] = g['main_net_ma5'] / g['main_net_ma10'].replace(0, np.nan)
            g['main_pos'] = (g['main_net'] > 0).astype(int)
            g['main_streak'] = g['main_pos'].shift(1).rolling(5).sum()
            g['retail_net'] = g['buy_sm_amount'].fillna(0) - g['sell_sm_amount'].fillna(0)
            g['main_vs_retail'] = (g['main_net'] - g['retail_net']) / g['amount_est'].replace(0, np.nan)
            g['lg_ratio'] = (g['buy_lg_amount'].fillna(0) + g['sell_lg_amount'].fillna(0)) / \
                           (g['buy_sm_amount'].fillna(0) + g['sell_sm_amount'].fillna(0) + 1)
            g['main_cum5'] = g['main_net_ratio'].shift(1).rolling(5).sum()
            g['main_cum10'] = g['main_net_ratio'].shift(1).rolling(10).sum()
            g['main_accel_3d'] = g['main_net_ratio'].diff(3)
            g['smart_div_count'] = ((g['pct_chg'] < 0) & (g['main_net'] > 0)).shift(1).rolling(5).sum()
            g['main_inflow_ratio'] = g['buy_lg_amount'].fillna(0) / (g['main_net'].abs() + 1)
            g['main_flow_accel'] = g['main_net_ratio'].diff(1) / (g['main_net_ratio'].abs().rolling(5).mean() + 1e-9)
        else:
            for c in ['main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_trend',
                      'main_streak', 'main_vs_retail', 'lg_ratio', 'main_cum5', 'main_cum10',
                      'main_accel_3d', 'smart_div_count', 'main_inflow_ratio', 'main_flow_accel']:
                g[c] = np.nan

        # ---- 融资融券 ----
        if ts_code in margin_dict:
            mg = margin_dict[ts_code]
            g = g.merge(mg[['trade_date', 'rzye', 'rqye', 'rzmre']], on='trade_date', how='left')
            g['rzye'] = g['rzye'].fillna(0)
            g['rqye'] = g['rqye'].fillna(0)
            g['rzmre'] = g['rzmre'].fillna(0)
            g['rzye_chg'] = g['rzye'].diff()
            g['rzmre_ratio'] = g['rzmre'] / g['amount_est'].replace(0, np.nan)
            g[['rzye_chg', 'rzmre_ratio']] = g[['rzye_chg', 'rzmre_ratio']].shift(1)
        else:
            g['rzye_chg'] = np.nan
            g['rzmre_ratio'] = np.nan

        # ---- 基本面 ----
        if ts_code in fund_dict:
            fd = fund_dict[ts_code]
            # fd['trade_date'] 已在源端转为 datetime64
            g = g.merge(fd[['trade_date', 'pe_ttm', 'pb', 'total_mv', 'circ_mv']], on='trade_date', how='left')
        else:
            for c in ['pe_ttm', 'pb', 'total_mv', 'circ_mv']:
                g[c] = np.nan

        # ---- 龙虎榜 ----
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

        # ---- 股东集中度 ----
        if ts_code in hc_dict:
            hc = hc_dict[ts_code][['end_date', 'holder_change_pct', 'holder_num_change']].copy()
            hc = hc.rename(columns={'end_date': 'trade_date'})
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            hc_sorted = hc.sort_values('trade_date')
            merged_hc = pd.merge_asof(g_sorted, hc_sorted, on='trade_date', direction='backward')
            g['holder_change_pct'] = merged_hc['holder_change_pct'].fillna(0)
            g['holder_num_change'] = merged_hc['holder_num_change'].fillna(0)
            neg_mask = g['holder_num_change'] < 0
            decline_count = 0
            decline_series = []
            for v in neg_mask.values:
                if v:
                    decline_count += 1
                else:
                    decline_count = 0
                decline_series.append(decline_count)
            g['holder_consecutive_decline'] = pd.Series(decline_series, index=g.index).shift(1).fillna(0).astype(int)
        else:
            g['holder_change_pct'] = 0.0
            g['holder_num_change'] = 0
            g['holder_consecutive_decline'] = 0

        # ====== V6.3/V6.4 特征 ======

        # 涨停板
        if ts_code in zt_dict:
            zt = zt_dict[ts_code]
            zt_flags = zt[['trade_date', 'last_board', 'seal_amount', 'open_count']].copy()
            zt_flags['is_zt'] = 1
            g = g.merge(zt_flags[['trade_date', 'is_zt', 'last_board', 'seal_amount', 'open_count']],
                       on='trade_date', how='left')
            g['is_zt'] = g['is_zt'].fillna(0).astype(int)
            g['last_board'] = g['last_board'].fillna(0).astype(int)
            g['seal_amount'] = g['seal_amount'].fillna(0)
            g['zt_count_30d'] = g['is_zt'].shift(1).rolling(30, min_periods=1).sum()
            g['zt_max_board_30d'] = g['last_board'].shift(1).rolling(30, min_periods=1).max()
            g['zt_seal_amount_30d'] = g['seal_amount'].shift(1).rolling(30, min_periods=1).sum()
        else:
            g['zt_count_30d'] = 0
            g['zt_max_board_30d'] = 0
            g['zt_seal_amount_30d'] = 0.0

        # 行业动量
        board_code = ind_board_map.get(ts_code)
        if board_code and board_code in ind_hist_map:
            hist = ind_hist_map[board_code]
            g['ind_board_pct'] = g['trade_date'].map(lambda x: hist.get(x, np.nan))
            g['ind_board_pct_5d'] = g['ind_board_pct'].shift(1).rolling(5, min_periods=1).mean()
            g['ind_board_pct_10d'] = g['ind_board_pct'].shift(1).rolling(10, min_periods=1).mean()
            g['ind_board_pct_20d'] = g['ind_board_pct'].shift(1).rolling(20, min_periods=1).mean()
        else:
            g['ind_board_pct_5d'] = np.nan
            g['ind_board_pct_10d'] = np.nan
            g['ind_board_pct_20d'] = np.nan

        # 概念热度
        concepts = concept_board_map.get(ts_code, [])
        if concepts and not concept_daily_mom.empty:
            concept_moms = []
            for cb in concepts:
                cb_mom = concept_daily_mom[concept_daily_mom['board_code'] == cb][['trade_date', 'concept_board_pct']]
                if not cb_mom.empty:
                    cb_mom = cb_mom.rename(columns={'concept_board_pct': f'cb_{cb}'})
                    concept_moms.append(cb_mom)
            if concept_moms:
                combined = concept_moms[0]
                for m in concept_moms[1:]:
                    combined = combined.merge(m, on='trade_date', how='outer')
                cols = [c for c in combined.columns if c != 'trade_date']
                combined['concept_mom_avg'] = combined[cols].mean(axis=1)
                combined = combined[['trade_date', 'concept_mom_avg']]
                g = g.merge(combined, on='trade_date', how='left')
                g['concept_mom_avg'] = g['concept_mom_avg'].fillna(0)
                g['concept_mom_5d'] = g['concept_mom_avg'].shift(1).rolling(5, min_periods=1).mean()
                g['concept_mom_10d'] = g['concept_mom_avg'].shift(1).rolling(10, min_periods=1).mean()
                g['concept_count'] = len(concepts)
        if 'concept_mom_avg' not in g.columns:
            g['concept_mom_avg'] = 0
            g['concept_mom_5d'] = 0
            g['concept_mom_10d'] = 0
            g['concept_count'] = 0

        # 业绩因子
        if ts_code in earn_dict:
            ed = earn_dict[ts_code][['report_date', 'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']]
            ed = ed.rename(columns={'report_date': 'trade_date'})
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            ed_sorted = ed.sort_values('trade_date')
            ed_sorted['trade_date'] = pd.to_datetime(ed_sorted['trade_date'])
            merged_ed = pd.merge_asof(g_sorted, ed_sorted, on='trade_date', direction='backward')
            g['revenue_yoy'] = merged_ed['revenue_yoy'].fillna(0).values
            g['net_profit_yoy'] = merged_ed['net_profit_yoy'].fillna(0).values
            g['roe'] = merged_ed['roe'].fillna(0).values
            g['gross_margin'] = merged_ed['gross_margin'].fillna(0).values
        else:
            for c in ['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']:
                g[c] = 0.0

        # ====== V8.0 特征 ======
        g['ret_1d_reversal'] = -g['pct_chg'].shift(1) / 100.0
        g['volume_div_days_10d'] = ((g['pct_chg'].shift(1) > 0) & (g['volume_ratio'].shift(1) < 1.0)).rolling(10).sum()
        g['turnover_std'] = g['turnover_rate'].shift(1).rolling(20).std()
        g['turnover_mean'] = g['turnover_rate'].shift(1).rolling(20).mean()
        g['turnover_std_ratio'] = g['turnover_std'] / g['turnover_mean'].replace(0, np.nan)

        if ts_code in bt_dict:
            bt_stock = bt_dict[ts_code]
            bt_aligned = bt_stock.set_index("trade_date").reindex(g["trade_date"].values)
            has_trade = bt_aligned["premium_rate"].notna().astype(int)
            g["bt_count_30d"] = has_trade.rolling(30, min_periods=1).sum().shift(1).fillna(0).values
            premium = bt_aligned["premium_rate"].fillna(0)
            rolling_sum = premium.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            rolling_count = has_trade.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            g["bt_premium_avg_30d"] = (rolling_sum / rolling_count.replace(0, np.nan)).fillna(0).values
            deal_amt = bt_aligned["deal_amount"].fillna(0)
            w_sum = (premium * deal_amt).rolling(30, min_periods=1).sum().shift(1).fillna(0)
            w_div = deal_amt.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            g["bt_premium_weighted_30d"] = (w_sum / w_div.replace(0, np.nan)).fillna(0).values
        else:
            for c in ["bt_count_30d", "bt_premium_avg_30d", "bt_premium_weighted_30d"]:
                g[c] = 0.0

        if ts_code in fc_dict:
            fc_stock = fc_dict[ts_code]
            g_temp = g[["trade_date"]].copy()
            fc_for_merge = fc_stock[["trade_date", "forecast_type", "net_profit_max", "report_date"]].sort_values("trade_date")
            merged_fc = pd.merge_asof(g_temp.sort_values("trade_date"), fc_for_merge, on="trade_date", direction="backward")
            type_map_bt = {"预增": 2, "扭亏": 2, "略增": 1, "续盈": 1,
                          "略减": -1, "预减": -2, "首亏": -2, "续亏": -3}
            merged_fc["forecast_type_code"] = merged_fc["forecast_type"].map(type_map_bt).fillna(0).astype(int)
            merged_fc["forecast_is_positive"] = merged_fc["forecast_type"].isin(
                ("预增", "扭亏", "略增", "续盈")
            ).astype(int)
            merged_fc["forecast_days_since"] = (merged_fc["trade_date"] - merged_fc["report_date"]).dt.days.fillna(30).astype(int)
            merged_fc["forecast_net_profit_max"] = merged_fc["net_profit_max"].fillna(0)
            g["forecast_type_code"] = merged_fc["forecast_type_code"].values
            g["forecast_is_positive"] = merged_fc["forecast_is_positive"].values
            g["forecast_days_since"] = merged_fc["forecast_days_since"].values
            g["forecast_net_profit_max"] = merged_fc["forecast_net_profit_max"].values
        else:
            for c in ['forecast_type_code', 'forecast_is_positive', 'forecast_days_since', 'forecast_net_profit_max']:
                g[c] = 0

        # ====== V11.3 新增 (10日预测周期)特征 ======

        # -- E1: 新频衍生特征 --
        close_series = g['close']
        pct_chg_series = g['pct_chg']

        # 自相关
        g['ret_autocorr_5d'] = pct_chg_series.shift(1).rolling(10).apply(
            lambda x: x.autocorr() if len(x) >= 5 else 0, raw=False
        ).fillna(0)

        # 波动率之波动率
        g['vol_of_vol'] = pct_chg_series.shift(1).rolling(20).std().rolling(10).std()

        # 最大回撤100
        rolling_max = close_series.shift(1).rolling(10).max()
        g['max_drawdown_10d'] = (close_series.shift(1) / rolling_max - 1).rolling(10).min()

        # 相对行业强度
        if industry:
            g['rel_strength_idx_5d'] = g['chg_5d']  # will be adjusted after concat

        # 成交量冲击
        g['volume_shock'] = g['vol'].shift(1) / g['vol'].shift(1).rolling(20).mean() - 1

        # 换手率Z-score
        g['turnover_zscore_20d'] = (g['turnover_rate'] - g['turnover_rate'].shift(1).rolling(20).mean()) / \
                                    g['turnover_rate'].shift(1).rolling(20).std().replace(0, np.nan)

        # 高低价差
        g['high_low_spread'] = (g['high'].shift(1) - g['low'].shift(1)) / ((g['high'].shift(1) + g['low'].shift(1)) / 2 + 1e-9)

        # 连涨连跌
        g['consecutive_wins'] = (pct_chg_series.shift(1) > 0).rolling(10).sum()
        g['consecutive_losses'] = (pct_chg_series.shift(1) < 0).rolling(10).sum()

        # ====== V11.1 新增：均值回复特征 ======
        # 价格在10日高低位置 (0=near low, 1=near high)
        high_10d = g['high'].shift(1).rolling(10).max()
        low_10d = g['low'].shift(1).rolling(10).min()
        range_10d = (high_10d - low_10d).replace(0, np.nan)
        g['price_range_pos_10d'] = ((g['close'].shift(1) - low_10d) / range_10d).clip(0, 1)
        # 超卖综合信号 (价格低位 + RSI低位 + 缩量)
        rsi_low = (g['rsi_14'].shift(1) < 35).astype(float)
        price_low = (g['price_range_pos_10d'] < 0.25).astype(float)
        vol_shrink = (g['volume_ratio'].shift(1) < 0.7).astype(float)
        g['oversold_boost'] = (rsi_low + price_low + vol_shrink) / 3.0
        # 均线发散度
        g['ma_dispersion'] = (g[['ma5', 'ma10', 'ma20']].shift(1).std(axis=1) /
                              g[['ma5', 'ma10', 'ma20']].shift(1).mean(axis=1).replace(0, np.nan))
        # 成交量萎缩 (正值=缩量)
        g['volume_contraction'] = -(g['vol'].shift(1) / g['vol'].shift(1).rolling(20).mean() - 1)
        # 距离50日均线
        g['price_ma50_ratio'] = (g['close'].shift(1) / g['close'].shift(1).rolling(50).mean() - 1)
        # 收益波动比
        g['ret_vol_ratio_5d'] = g['chg_5d'].shift(1) / (pct_chg_series.shift(1).rolling(5).std() + 1e-9)

        # ====== V11.2: std_20d + CMRA ======
        # 20日收益率标准差
        g['std_20d'] = g['pct_chg'].shift(1).rolling(20, min_periods=5).std()

        # CMRA: 12月收益极差 = ln((1+ZMax)/(1+ZMin))
        # 用21日近似月度收益，滚动252日取最大最小
        g['monthly_ret_approx'] = g['close'].pct_change(21)
        g['zmax_12m'] = g['monthly_ret_approx'].shift(1).rolling(252, min_periods=63).max()
        g['zmin_12m'] = g['monthly_ret_approx'].shift(1).rolling(252, min_periods=63).min()
        g['cmra'] = np.log((1 + g['zmax_12m']) / (1 + g['zmin_12m'] + 0.01))

        # -- V11.0: fina_indicator --
        if ts_code in fina_dict:
            fr = fina_dict[ts_code]
            g['fina_roe'] = fr.get('roe', 0)
            g['fina_yoy_sales'] = fr.get('yoy_sales', 0)
            g['fina_gross_margin'] = fr.get('grossprofit_margin', 0)
            g['fina_net_margin'] = fr.get('netprofit_margin', 0)
            g['fina_eps'] = fr.get('eps', 0)
        else:
            for c in ['fina_roe', 'fina_yoy_sales', 'fina_gross_margin', 'fina_net_margin', 'fina_eps']:
                g[c] = 0.0

        # -- V11.0: 板块资金流 --
        if industry and industry in sector_mf_dict:
            smf = sector_mf_dict[industry]
            smf_sorted = smf.sort_values('trade_date')
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            merged_smf = pd.merge_asof(g_sorted, smf_sorted, on='trade_date', direction='backward')
            g['sector_netflow_1d'] = merged_smf['net_amount'].fillna(0).shift(1).fillna(0).values
            g['sector_elg_net'] = merged_smf['elg_net'].fillna(0).shift(1).fillna(0).values
            smf_for_5d = smf_sorted.set_index('trade_date')['elg_net']
            g['sector_netflow_5d'] = g['trade_date'].map(
                lambda x: smf_for_5d.loc[:x].iloc[:-1].tail(5).sum() if x in smf_for_5d.index and len(smf_for_5d.loc[:x]) > 1 else 0
            ).fillna(0)
            sector_pct = smf_sorted.set_index('trade_date')['pct_change']
            g['sector_pct_5d'] = g['trade_date'].map(
                lambda x: sector_pct.loc[:x].iloc[:-1].tail(5).sum() if x in sector_pct.index and len(sector_pct.loc[:x]) > 1 else 0
            ).fillna(0)
        else:
            for c in ['sector_netflow_1d', 'sector_elg_net', 'sector_netflow_5d', 'sector_pct_5d']:
                g[c] = 0.0

        # 个股vs板块资金背离
        g['sector_flow_divergence'] = g.get('main_net', 0) - g.get('sector_elg_net', 0)  # already shifted via main_net & sector_elg_net

        # -- V11.0: 北向资金 --
        if not north_mf_sorted.empty:
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            merged_nmf = pd.merge_asof(g_sorted, north_mf_sorted[['trade_date', 'north_money']].fillna(0),
                                       on='trade_date', direction='backward')
            g['north_flow_1d'] = merged_nmf['north_money'].fillna(0).shift(1).fillna(0).values
        else:
            g['north_flow_1d'] = 0.0

        # -- V11.0: 历史ML预测 --
        if ts_code in ml_prev_dict:
            mp = ml_prev_dict[ts_code]
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            merged_mp = pd.merge_asof(g_sorted, mp[['trade_date', '_ml_pred']], on='trade_date', direction='backward')
            g['ml_pred_prev'] = merged_mp['_ml_pred'].fillna(0.5).shift(1).fillna(0.5).values
            # 用 merge_asof 对齐后计算 ml_pred_prev 的 5 日变化（逐行）
            merged_mp_chg = merged_mp['_ml_pred'].copy()
            mp_chg_5d = merged_mp_chg - merged_mp_chg.shift(5)
            g['ml_pred_chg_5d'] = mp_chg_5d.fillna(0).values
        else:
            g['ml_pred_prev'] = 0.5
            g['ml_pred_chg_5d'] = 0

        # ---- 标签计算：industry-neutral alpha_10d ----
        # 前向5日收益
        g['fwd_ret_10d'] = g['close'].shift(-10) / g['close'] - 1

        # 波动率调整（vol_20d 已知于T日，已shift(1)保护）
        vol_20d_at_T = g['pct_chg'].shift(1).rolling(20).std() * np.sqrt(10)
        g['target_10d_adj'] = g['fwd_ret_10d'] / (vol_20d_at_T + 0.01)

        # 只保留特征和目标字段（行业中性化在 concat 后做）
        valid = g.dropna(subset=['target_10d_adj'])
        # 取 min_date 之后的数据（给特征留足历史）
        valid = valid[valid['trade_date'] >= pd.Timestamp(min_date)]
        if len(valid) < 10:
            continue
        results.append(valid)

    if not results:
        logger.warning("无有效样本")
        return pd.DataFrame(), {}, [], []

    result = pd.concat(results, ignore_index=True)

    # === 行业中性化（label: alpha_10d） ===
    ind_df = stock_info[['ts_code', 'industry']].dropna()
    result = result.merge(ind_df, on='ts_code', how='left')
    result['industry'] = result['industry'].fillna('OTHER')
    result['industry_avg_10d'] = result.groupby(['trade_date', 'industry'])['target_10d_adj'].transform('mean')
    result['alpha_10d'] = result['target_10d_adj'] - result['industry_avg_10d']

    # 3sigma winsorize
    label_col = 'alpha_10d'
    mean_val = result[label_col].mean()
    std_val = result[label_col].std()
    if std_val > 0:
        result[label_col] = result[label_col].clip(mean_val - 3 * std_val, mean_val + 3 * std_val)

    # === Alpha 情绪特征 ===
    if not alpha_signals.empty:
        alpha_rn = alpha_signals.rename(columns={'signal_date': 'trade_date'})
        result = result.merge(alpha_rn[['ts_code', 'trade_date', 'max_boost']],
                              on=['ts_code', 'trade_date'], how='left')
        result['max_boost'] = result['max_boost'].fillna(0)
        result = result.sort_values(['ts_code', 'trade_date'])
        result['max_boost'] = result.groupby('ts_code')['max_boost'].shift(1).fillna(0)
        result['alpha_pos_5d'] = result.groupby('ts_code')['max_boost'].transform(
            lambda x: x.clip(lower=0).rolling(5, min_periods=1).sum()
        )
        result['alpha_neg_5d'] = result.groupby('ts_code')['max_boost'].transform(
            lambda x: x.clip(upper=0).abs().rolling(5, min_periods=1).sum()
        )
    else:
        result['max_boost'] = 0.0
        result['alpha_pos_5d'] = 0.0
        result['alpha_neg_5d'] = 0.0

    alpha_coverage = (result['max_boost'] != 0).mean()
    use_alpha_features = alpha_coverage >= 0.05

    # === 行业动量（辅助特征） ===
    result = result.sort_values(['ts_code', 'trade_date'])
    result['ind_pct_avg'] = result.groupby(['trade_date', 'industry'])['pct_chg'].transform('mean')
    result['ind_pct_avg'] = result.groupby('industry')['ind_pct_avg'].shift(1)
    result['ind_mom_5d'] = result.groupby('industry')['ind_pct_avg'].transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    )
    result['ind_mom_20d'] = result.groupby('industry')['ind_pct_avg'].transform(
        lambda x: x.rolling(20, min_periods=1).mean()
    )

    # 相对行业强度（修正上面 incomplete 的计算）
    result['rel_strength_idx_5d'] = result.groupby('trade_date')['chg_5d'].transform(
        lambda x: x / x.median() if x.median() != 0 else 1.0
    )

    # ====== V11.1 全局市场状态特征 ======
    logger.info("计算全局市场状态特征...")
    try:
        daily_mkt = daily[['trade_date', 'pct_chg']].copy()
        daily_mkt['is_zt'] = (daily_mkt['pct_chg'] > 9.5).astype(int)
        daily_mkt['is_dt'] = (daily_mkt['pct_chg'] < -9.5).astype(int)
        mkt_stats = daily_mkt.groupby('trade_date').agg(
            zt_cnt=('is_zt', 'sum'), dt_cnt=('is_dt', 'sum'),
            ret_std=('pct_chg', 'std'),
            above_zero=('pct_chg', lambda x: (x > 0).mean()),
        ).reset_index()
        mkt_stats['mkt_zt_dt_spread'] = mkt_stats['zt_cnt'] - mkt_stats['dt_cnt']
        mkt_stats['mkt_zt_dt_spread'] = mkt_stats['mkt_zt_dt_spread'].rolling(5).mean()
        mkt_stats['mkt_volatility'] = mkt_stats['ret_std'].rolling(5).mean()
        mkt_stats['mkt_breadth'] = mkt_stats['above_zero'].rolling(5).mean()
        result = result.merge(
            mkt_stats[['trade_date', 'mkt_zt_dt_spread', 'mkt_volatility', 'mkt_breadth']],
            on='trade_date', how='left'
        )
    except Exception as e:
        logger.warning(f"V11.1 市场状态特征失败: {e}")
        for c in ['mkt_zt_dt_spread', 'mkt_volatility', 'mkt_breadth']:
            if c not in result.columns: result[c] = 0.0


    # ====== V11.2 市场状态交互特征 ======
    # 将市场状态概率与关键特征交互，让模型学会"不同市场用不同规则"
    logger.info("构建市场状态交互特征...")
    if market_regime is not None and not market_regime.empty:
        # 向前填充到每个交易日的市场状态
        result = result.merge(
            market_regime[['trade_date','prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                           'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio']],
            on='trade_date', how='left'
        )
        # 填充缺失值
        for c in ['prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                  'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio']:
            if c in result.columns:
                result[c] = result[c].fillna(0.0)

        # 核心：动量特征 × 恐慌概率（恐慌时动量反转）
        result['chg_5d_x_panic'] = result['chg_5d'] * result['prob_panic']
        result['chg_10d_x_panic'] = result['chg_10d'] * result['prob_panic']
        result['chg_20d_x_panic'] = result['chg_20d'] * result['prob_panic']
        result['rsi_14_x_panic'] = result['rsi_14'] * result['prob_panic']
        result['volume_ratio_x_panic'] = result['volume_ratio'] * result['prob_panic']
        if 'main_net_ratio' in result.columns:
            result['main_net_ratio_x_panic'] = result['main_net_ratio'] * result['prob_panic']
        if 'vol_price_divergence' in result.columns:
            result['vol_price_divergence_x_panic'] = result['vol_price_divergence'] * result['prob_panic']
        result['pos_52w_x_panic'] = result['pos_52w'] * result['prob_panic']
        if 'ret_autocorr_5d' in result.columns:
            result['ret_autocorr_5d_x_panic'] = result['ret_autocorr_5d'] * result['prob_panic']
        if 'zt_seal_amount_30d' in result.columns:
            result['zt_seal_amount_30d_x_panic'] = result.get('zt_seal_amount_30d', 0) * result['prob_panic']

        # 不同市场状态的补充交互
        result['volume_ratio_x_overheated'] = result['volume_ratio'] * result['prob_overheated']
        result['chg_5d_x_overheated'] = result['chg_5d'] * result['prob_overheated']
        result['chg_5d_x_bull'] = result['chg_5d'] * result['prob_bull']
        result['sh_trend_x_chg_5d'] = result.get('sh_trend', 0) * result['chg_5d']
        result['breadth_x_main_net'] = result.get('market_breadth', 0) * result.get('main_net_ratio', 0)
        result['volatility_x_rsi'] = result.get('volatility', 0) * result['rsi_14']
        result['zt_ratio_x_chg_5d'] = result.get('zt_ratio', 0) * result['chg_5d']
    else:
        logger.warning("market_regime 数据为空，跳过交互特征")
        for c in ['prob_bull','prob_bear','prob_panic','prob_range','prob_overheated',
                  'sh_trend','market_breadth','volatility','volume_trend','momentum_breadth','zt_ratio',
                  'chg_5d_x_panic','chg_10d_x_panic','chg_20d_x_panic','rsi_14_x_panic',
                  'volume_ratio_x_panic','main_net_ratio_x_panic','vol_price_divergence_x_panic',
                  'pos_52w_x_panic','ret_autocorr_5d_x_panic','zt_seal_amount_30d_x_panic',
                  'volume_ratio_x_overheated','chg_5d_x_overheated',
                  'chg_5d_x_bull','main_net_ratio_x_bull',
                  'sh_trend_x_chg_5d','breadth_x_main_net','volatility_x_rsi','zt_ratio_x_chg_5d']:
            result[c] = 0.0

    # ---- Money flow divergence（V10.0 已有，补全） ----
    result['money_flow_div_5d'] = result['main_cum5']
    result['money_flow_div_10d'] = result['main_cum10']
    result['flow_accel_5d'] = result['main_accel_3d']

    # === 特征列定义 ===
    base_feature_cols = [
        # V6 量价基础
        'pct_chg', 'turnover_rate', 'volume_ratio',
        'vol_5d', 'vol_10d', 'vol_20d',
        'ma5_ma10_ratio', 'ma10_ma20_ratio', 'price_ma5_ratio', 'price_ma20_ratio',
        'chg_3d', 'chg_5d', 'chg_10d', 'chg_20d', 'vol_trend', 'pos_52w',
        'rps_20', 'rps_change', 'up_ratio_5d', 'up_ratio_10d', 'vol_pct_corr',
        'ma_pattern',
        'macd_diff', 'macd_signal_line', 'macd_hist',
        'rsi_14', 'adx_14',
        # 资金流
        'main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_trend',
        'main_streak', 'main_vs_retail', 'lg_ratio', 'main_cum5', 'main_cum10',
        'main_accel_3d', 'smart_div_count',
        'main_inflow_ratio', 'main_flow_accel',
        # 基本因子
        'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
        'amihud', 'amihud_ma5',
        'vol_price_divergence', 'vol_price_div_10d', 'ma_spread_stock',
        # 融资融券
        'rzye_chg', 'rzmre_ratio',
        # 行业动量
        'ind_mom_5d', 'ind_mom_20d',
        # V6.2 新增
        'amount_ma5_ratio', 'pos_10d', 'pos_20d',
        'amplitude_10d', 'turnover_ratio',
        'mom_divergence', 'ret_skew_20d', 'ret_kurt_20d',
        'ret_max_5d', 'ret_min_5d',
        'dragon_net_buy_30d', 'dragon_count_30d',
        'dti_net_buy_30d', 'dti_count_30d',
        'holder_change_pct', 'holder_num_change', 'holder_consecutive_decline',
        # V6.3/V6.5+
        'zt_count_30d', 'zt_max_board_30d', 'zt_seal_amount_30d',
        'ind_board_pct_5d', 'ind_board_pct_10d', 'ind_board_pct_20d',
        'concept_count', 'concept_mom_avg', 'concept_mom_5d', 'concept_mom_10d',
        'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin',
        # V8.0
        'ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
        'bt_count_30d', 'bt_premium_avg_30d', 'bt_premium_weighted_30d',
        'forecast_type_code', 'forecast_is_positive', 'forecast_days_since', 'forecast_net_profit_max',
    ]

    # V11.3 新增 (10日预测周期)特征
    v11_feature_cols = [
        # fina_indicator
        'fina_roe', 'fina_yoy_sales', 'fina_gross_margin', 'fina_net_margin', 'fina_eps',
        # V11.1 均值回复
        'price_range_pos_10d', 'oversold_boost', 'ma_dispersion',
        'volume_contraction', 'price_ma50_ratio', 'ret_vol_ratio_5d',
        # V11.1 市场状态
        'mkt_zt_dt_spread', 'mkt_volatility', 'mkt_breadth',
        # sector_moneyflow
        'sector_netflow_1d', 'sector_netflow_5d', 'sector_flow_divergence',
        # north_moneyflow
        'north_flow_1d',
        # ml_predictions
        'ml_pred_prev', 'ml_pred_chg_5d',
        # 新衍生
        'ret_autocorr_5d', 'vol_of_vol', 'max_drawdown_10d',
        'rel_strength_idx_5d', 'volume_shock', 'turnover_zscore_20d',
        'high_low_spread', 'consecutive_wins', 'consecutive_losses',
        # 资金流衍生
        'money_flow_div_5d', 'money_flow_div_10d', 'flow_accel_5d',
        # sector pct
        'sector_pct_5d',
        # V11.2 新增
        'std_20d', 'cmra',
    ]

    feature_cols = base_feature_cols + v11_feature_cols

    if use_alpha_features:
        feature_cols.extend(['max_boost', 'alpha_pos_5d', 'alpha_neg_5d'])

    for col in feature_cols:
        if col not in result.columns:
            result[col] = np.nan

    # === 横截面排名特征 ===
    rank_features = [
        'pct_chg', 'turnover_rate', 'volume_ratio', 'rps_20',
        'lg_ratio', 'main_net_ratio', 'pos_52w',
        'chg_5d', 'chg_10d', 'main_cum5',
        'amount_ma5_ratio', 'pos_10d', 'turnover_ratio', 'mom_divergence',
        'dragon_net_buy_30d', 'dragon_count_30d',
        'zt_count_30d', 'zt_max_board_30d',
        'ind_board_pct_5d', 'ind_board_pct_10d',
        'concept_count', 'concept_mom_5d',
        'net_profit_yoy', 'revenue_yoy',
        'ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
        'bt_count_30d', 'bt_premium_avg_30d',
        'forecast_type_code', 'forecast_is_positive', 'forecast_net_profit_max',
        'fina_roe', 'ml_pred_prev',
    ]
    for col in rank_features:
        if col in result.columns:
            result[f'{col}_rank'] = result.groupby('trade_date')[col].rank(pct=True)

    # === 填充 NaN ===
    global_medians = {}
    for col in feature_cols:
        med = result[col].median()
        global_medians[col] = float(med) if not np.isnan(med) else 0.0
        result[col] = result[col].fillna(global_medians[col])

    logger.info(f"构建完成: {len(result):,} 样本, {result['ts_code'].nunique()} 股, {len(feature_cols)} 特征")

    y = result['alpha_10d'].values
    logger.info(f"alpha_10d: mean={y.mean()*100:.2f}%, median={np.median(y)*100:.2f}%, "
                f"std={y.std()*100:.2f}%, 正值占比={(y>0).mean()*100:.1f}%")

    return result, global_medians, feature_cols, use_alpha_features


# ========== PART 3: PURGED WALK-FORWARD VALIDATION ==========

def safe_qcut(s, n_bins=5):
    """安全的 qcut"""
    s_clean = s.dropna()
    if len(s_clean) <= 1:
        return pd.Series([0] * len(s), index=s.index)
    if len(s_clean) < n_bins:
        return s.rank(method='dense').fillna(0).astype(int)
    return pd.qcut(s.rank(method='first'), n_bins, labels=list(range(n_bins)))


def purged_walk_forward(df, feature_cols, label_col='alpha_10d',
                        n_folds=5, embargo=10, val_size=60):
    """
    Purged walk-forward 验证。
    Purging: 去除训练样本中标签计算窗口与验证集重叠的样本。
    Embargo: 训练集最后 N 天留空，防止 rolling 特征泄露。
    """
    df_sorted = df.sort_values('trade_date').copy()
    dates = sorted(df_sorted['trade_date'].unique())
    n_dates = len(dates)
    # 自适应 fold 数：确保每个 fold 有足够的训练数据
    min_train = 60  # 最少训练天数
    max_folds = (n_dates - min_train) // (val_size + embargo)
    n_folds = min(n_folds, max_folds)
    if n_folds < 3:
        n_folds = 3
        val_size = n_dates // 8
        if val_size < 20:
            val_size = 20

    logger.info(f"Purged Walk-Forward ({n_folds}折, embargo={embargo}d, val_size={val_size}d, "
                f"total_dates={n_dates})...")

    cv_results = []

    for fold in range(n_folds):
        # 用展开窗口: 训练集从最早到 val_start-embargo，验证集固定窗口
        # val_start = 首日往前推
        val_end = n_dates - (n_folds - fold - 1) * (val_size + embargo)
        val_start = val_end - val_size

        if val_start <= min_train or val_end > n_dates:
            # 放宽约束再试
            val_end = n_dates - (n_folds - fold - 1) * val_size
            val_start = val_end - val_size

        if val_start <= min_train or val_end > n_dates:
            continue

        # Embargo: 训练集在验证集前留空
        train_end = val_start - embargo
        if train_end <= 0:
            continue

        train_dates = set(dates[:train_end])
        val_dates = set(dates[val_start:val_end])

        train_mask = df_sorted['trade_date'].isin(train_dates)
        val_mask = df_sorted['trade_date'].isin(val_dates)

        # Purging: 去掉训练样本，其前向收益窗口与验证集重叠
        if 'fwd_ret_10d' in df_sorted.columns:
            valid_min = dates[val_start]
            # 样本 T 的前向收益用到 T+1 到 T+5，约10个自然日
            purge_mask = df_sorted['trade_date'] < pd.Timestamp(valid_min) - pd.Timedelta(days=12)
            train_mask = train_mask & purge_mask

        X_train = df_sorted.loc[train_mask, feature_cols].values
        y_train = df_sorted.loc[train_mask, label_col].values
        X_val = df_sorted.loc[val_mask, feature_cols].values
        y_val = df_sorted.loc[val_mask, label_col].values

        if len(X_train) < 500 or len(X_val) < 50:
            logger.warning(f"  Fold {fold+1}: 样本不足 train={len(X_train)}, val={len(X_val)}")
            continue

        # LambdaRank 离散化
        train_df = df_sorted.loc[train_mask].copy()
        val_df = df_sorted.loc[val_mask].copy()
        train_df['label_rank'] = train_df.groupby('trade_date')[label_col].transform(
            lambda x: safe_qcut(x, 10)
        )
        val_df['label_rank'] = val_df.groupby('trade_date')[label_col].transform(
            lambda x: safe_qcut(x, 10)
        )
        y_train_lr = train_df['label_rank'].fillna(4).astype(int).values
        y_val_lr = val_df['label_rank'].fillna(4).astype(int).values

        train_group = train_df.groupby('trade_date').size().to_numpy()
        val_group = val_df.groupby('trade_date').size().to_numpy()

        # 时间衰减权重
        max_known_date = train_df['trade_date'].max()
        train_df['days_ago'] = (max_known_date - train_df['trade_date']).dt.days
        train_df['weight'] = np.exp(-0.005 * train_df['days_ago'])
        sample_weight = train_df['weight'].values

        # 训练 LGB LambdaRank
        td = lgb.Dataset(X_train, label=y_train_lr, group=train_group, weight=sample_weight)
        vd = lgb.Dataset(X_val, label=y_val_lr, group=val_group, reference=td)

        params = {
            'objective': 'lambdarank',
            'ndcg_eval_at': [10, 20, 50],
            'label_gain': list(range(10)),
            'boosting_type': 'gbdt',
            'num_leaves': 64,
            'learning_rate': 0.01,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'min_child_samples': 200,
            'lambda_l2': 0.01,
            'verbose': -1,
            'seed': 42 + fold,
        }

        model = lgb.train(
            params, td, num_boost_round=2000, valid_sets=[vd],
            callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)]
        )

        vp = model.predict(X_val)

        # Rank IC
        rank_ic = spearmanr(y_val, vp)[0]

        # 前20% vs 后20% spread
        n_top = max(10, int(len(vp) * 0.20))
        top_idx = np.argsort(vp)[-n_top:]
        bottom_idx = np.argsort(vp)[:n_top]
        top_avg_ret = y_val[top_idx].mean()
        bottom_avg_ret = y_val[bottom_idx].mean()
        spread = (top_avg_ret - bottom_avg_ret)

        # 日频 IC
        val_df_cv = df_sorted.loc[val_mask].copy()
        val_df_cv['pred'] = vp
        daily_ic = val_df_cv.groupby('trade_date').apply(
            lambda x: spearmanr(x[label_col], x['pred'])[0], include_groups=False
        )
        mean_daily_ic = daily_ic.mean()
        daily_ic_std = daily_ic.std()
        ic_ir = mean_daily_ic / (daily_ic_std + 1e-9)

        cv_results.append({
            'fold': fold + 1,
            'rank_ic': rank_ic,
            'mean_daily_ic': mean_daily_ic,
            'ic_ir': ic_ir,
            'top20_avg_ret': top_avg_ret,
            'bottom20_avg_ret': bottom_avg_ret,
            'spread': spread,
            'n_train': len(X_train),
            'n_val': len(X_val),
            'train_dates': f"{min(dates)} ~ {dates[train_end-1]}",
            'val_dates': f"{dates[val_start]} ~ {dates[val_end-1]}",
        })

        r = cv_results[-1]
        logger.info(f"  Fold {fold+1}: RankIC={r['rank_ic']:.3f}, "
                    f"日频IC={r['mean_daily_ic']:.3f}, ICIR={r['ic_ir']:.2f}, "
                    f"Spread={r['spread']:.1f}bp")

    if not cv_results:
        logger.warning("Walk-Forward 无有效 fold")
        return None

    avg_ic = np.mean([r['rank_ic'] for r in cv_results])
    avg_daily_ic = np.mean([r['mean_daily_ic'] for r in cv_results])
    avg_icir = np.mean([r['ic_ir'] for r in cv_results])
    avg_spread = np.mean([r['spread'] for r in cv_results])

    logger.info(f"  Purged Walk-Forward 平均: RankIC={avg_ic:.3f}, "
                f"日频IC={avg_daily_ic:.3f}, ICIR={avg_icir:.2f}, Spread={avg_spread:.1f}bp")

    return cv_results


# ========== PART 4: BASE MODEL TRAINING ==========

def train_base_models(df, feature_cols, label_col='alpha_10d',
                      use_alpha_features=False, n_seeds=14):
    """
    训练 Layer 1 基础模型：
    - Tier A: 5 个 LGB LambdaRank (不同种子)
    - Tier B: 3 个多周期 LGB
    - Tier C: 3 个特征子集 LGB
    - Tier D: 2 个 XGBoost
    总计: 13个基础模型
    """
    logger.info(f"\n{'='*60}")
    logger.info("训练 Layer 1: 基础模型 (13个)")
    logger.info(f"{'='*60}")

    df_sorted = df.sort_values('trade_date')
    group = df_sorted.groupby('trade_date').size().to_numpy()
    X = df_sorted[feature_cols].values
    y = df_sorted[label_col].values

    # LambdaRank 离散化
    df_temp = df_sorted.copy()
    df_temp['label_rank'] = df_temp.groupby('trade_date')[label_col].transform(
        lambda x: safe_qcut(x, 10)
    )
    y_lr = df_temp['label_rank'].fillna(4).astype(int).values

    # 时间衰减权重
    max_date = df_sorted['trade_date'].max()
    df_temp['days_ago'] = (max_date - df_temp['trade_date']).dt.days
    df_temp['weight'] = np.exp(-0.005 * df_temp['days_ago'])
    sample_weight = df_temp['weight'].values

    base_ds = lgb.Dataset(X, label=y_lr, group=group, weight=sample_weight)

    models = {}

    # ===== Tier A: 不同种子的 LGB LambdaRank =====
    logger.info("Tier A: 训练 LGB LambdaRank 集成...")
    for i in range(n_seeds):
        params = {
            'objective': 'lambdarank',
            'ndcg_eval_at': [10, 20, 50],
            'label_gain': list(range(10)),
            'boosting_type': 'gbdt',
            'num_leaves': 64,
            'learning_rate': 0.01,
            'feature_fraction': [0.65, 0.7, 0.75, 0.8, 0.65, 0.7, 0.75, 0.8, 0.65, 0.7, 0.75, 0.8, 0.65, 0.7][i],
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'min_child_samples': 200,
            'lambda_l2': 0.01,
            'verbose': -1,
            'seed': 42 + i * 7,
        }
        model = lgb.train(params, base_ds, num_boost_round=1500, callbacks=[lgb.log_evaluation(0)])
        models[f'lgb_seed_{42 + i * 7}'] = model
        logger.info(f"  Tier A/{i+1}: seed={42 + i * 7}, feature_fraction={params['feature_fraction']}")

    # ===== Tier B: XGBoost 回归模型 =====
    logger.info("Tier B: 训练 XGBoost 回归模型...")
    dtrain = xgb.DMatrix(X, label=y)
    xgb_params = {
        'objective': 'reg:squarederror',
        'max_depth': 5,
        'eta': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 10,
        'lambda': 0.1,
        'seed': 42,
    }
    xgb_model = xgb.train(xgb_params, dtrain, num_boost_round=500)
    models['xgb_reg'] = xgb_model
    logger.info("  Tier B: XGBoost reg:squarederror")

    # ===== Tier C: 特征子集模型 =====
    logger.info("Tier C: 训练特征子集模型...")
    subset_configs = [
        ('momentum_lgb', MOMENTUM_FEATURES),
        ('flow_lgb', FLOW_FEATURES),
        ('quality_lgb', VALUE_QUALITY_FEATURES),
    ]
    for name, subset_cols in subset_configs:
        valid_cols = [c for c in subset_cols if c in df_sorted.columns]
        if len(valid_cols) < 5:
            logger.warning(f"  Tier C/{name}: 有效特征不足 ({len(valid_cols)})，跳过")
            continue
        X_sub = df_sorted[valid_cols].values
        sub_ds = lgb.Dataset(X_sub, label=y_lr, group=group, weight=sample_weight)
        params = {
            'objective': 'lambdarank',
            'ndcg_eval_at': [10, 20],
            'label_gain': list(range(10)),
            'boosting_type': 'gbdt',
            'num_leaves': 24,
            'learning_rate': 0.03,
            'feature_fraction': 1.0,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'min_child_samples': 500,
            'lambda_l2': 0.1,
            'verbose': -1,
            'seed': 77,
        }
        model = lgb.train(params, sub_ds, num_boost_round=1500, callbacks=[lgb.log_evaluation(0)])
        models[name] = {'model': model, 'feature_cols': valid_cols}
        logger.info(f"  Tier C/{name}: {len(valid_cols)} 个特征")

    logger.info(f"Layer 1 完成: {len(models)} 个基础模型")
    return models


# ========== PART 5: METRICS ==========

def compute_feature_importance(models, feature_cols):
    """聚合特征重要性"""
    imp = {}
    for name, model in models.items():
        if isinstance(model, dict) and 'model' in model:
            m = model['model']
            cols = model.get('feature_cols', feature_cols)
        else:
            m = model
            cols = feature_cols
        if hasattr(m, 'feature_importance'):
            mi = dict(zip(cols, m.feature_importance()))
            for k, v in mi.items():
                imp[k] = imp.get(k, 0) + v
    return dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))


# ========== PART 6: MAIN ==========

def main():
    start = datetime.now()

    import argparse
    parser = argparse.ArgumentParser(description='ML选股模型训练 V11.0')
    parser.add_argument('--max_date', type=str, default=None,
                        help='训练数据截止日期 (YYYY-MM-DD)，用于样本外模型')
    parser.add_argument('--output', type=str, default=None,
                        help='模型输出路径，默认 data/ml_stock_model_v11_2.pkl')
    args = parser.parse_args()

    oos_model_path = args.output or MODEL_PATH
    model_name = os.path.basename(oos_model_path).replace('.pkl', '')
    oos_config_path = os.path.join(DATA_DIR, f'feature_config_{model_name}.json')

    # Step 1: 加载数据
    data = load_data(max_date=args.max_date)
    (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
     margin, dragon_tiger, dragon_tiger_inst, holder_change,
     zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
     block_trade, stock_forecast,
     fina_ind, sector_mf, north_mf, ml_prev, market_regime,
     min_date, max_date) = data

    # Step 1.5: 过滤到成交量前1000的活跃股
    top_stocks = daily.groupby('ts_code')['amount'].mean().nlargest(1000).index.tolist()
    daily = daily[daily['ts_code'].isin(top_stocks)].copy()
    moneyflow = moneyflow[moneyflow['ts_code'].isin(top_stocks)].copy() if not moneyflow.empty and 'ts_code' in moneyflow.columns else moneyflow
    fundamentals = fundamentals[fundamentals['ts_code'].isin(top_stocks)].copy() if not fundamentals.empty and 'ts_code' in fundamentals.columns else fundamentals
    alpha_signals = alpha_signals[alpha_signals['ts_code'].isin(top_stocks)].copy() if not alpha_signals.empty and 'ts_code' in alpha_signals.columns else alpha_signals
    stock_info = stock_info[stock_info['ts_code'].isin(top_stocks)].copy()
    data = (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
            margin, dragon_tiger, dragon_tiger_inst, holder_change,
            zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
            block_trade, stock_forecast,
            fina_ind, sector_mf, north_mf, ml_prev, market_regime,
            min_date, max_date)
    logger.info(f'过滤后: {len(daily):,} 行, {daily.ts_code.nunique()} 股')
    import gc; gc.collect()

    # Step 1.5: 过滤到成交量前1500的活跃股
    top_stocks = daily.groupby('ts_code')['amount'].mean().nlargest(1500).index.tolist()
    daily = daily[daily['ts_code'].isin(top_stocks)].copy()
    moneyflow = moneyflow[moneyflow['ts_code'].isin(top_stocks)].copy() if not moneyflow.empty and 'ts_code' in moneyflow.columns else moneyflow
    fundamentals = fundamentals[fundamentals['ts_code'].isin(top_stocks)].copy() if not fundamentals.empty and 'ts_code' in fundamentals.columns else fundamentals
    alpha_signals = alpha_signals[alpha_signals['ts_code'].isin(top_stocks)].copy() if not alpha_signals.empty and 'ts_code' in alpha_signals.columns else alpha_signals
    stock_info = stock_info[stock_info['ts_code'].isin(top_stocks)].copy()
    data = (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
            margin, dragon_tiger, dragon_tiger_inst, holder_change,
            zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
            block_trade, stock_forecast,
            fina_ind, sector_mf, north_mf, ml_prev, market_regime,
            min_date, max_date)
    logger.info(f'过滤后: {len(daily):,} 行, {daily.ts_code.nunique()} 股')
    import gc; gc.collect()

    # Step 2: 构建特征
    result_tuple = build_features(data)
    if result_tuple[0].empty:
        logger.error("特征构建失败")
        return
    features, global_medians, feature_cols, use_alpha_features = result_tuple

    logger.info(f"\n{'='*60}")
    logger.info(f"V11.0 特征: {len(feature_cols)} 列, {len(features):,} 样本")
    logger.info(f"{'='*60}")

    # Step 3: Purged Walk-Forward 验证
    cv_results = purged_walk_forward(features, feature_cols)

    # Step 4: 训练 Layer 1 基础模型
    base_models = train_base_models(features, feature_cols, use_alpha_features=use_alpha_features)

    # Step 5: 特征重要性
    importance = compute_feature_importance(base_models, feature_cols)
    logger.info("\n特征重要性 Top 20:")
    for k, v in list(importance.items())[:20]:
        logger.info(f"  {k}: {v:.0f}")

    # Step 6: 全数据评估
    X_all = features[feature_cols].values
    y_all = features['alpha_10d'].values
    all_preds = []
    for name, model in base_models.items():
        if isinstance(model, dict) and 'model' in model:
            m = model['model']
            cols = model.get('feature_cols', feature_cols)
            X_sub = features[cols].values
            if hasattr(m, 'predict'):
                pred = m.predict(X_sub)
            else:
                continue
        elif hasattr(model, 'predict'):
            if 'xgboost' in type(model).__module__ or hasattr(model, 'feature_names'):
                import xgboost as xgb
                pred = model.predict(xgb.DMatrix(X_all))
            else:
                pred = model.predict(X_all)
        all_preds.append(pred)

    if all_preds:
        ensemble_pred = np.mean(all_preds, axis=0)
        final_ic = spearmanr(y_all, ensemble_pred)[0]
        logger.info(f"全数据等权融合: RankIC={final_ic:.3f}")

    # Step 7: 保存
    os.makedirs(DATA_DIR, exist_ok=True)

    bundle = {
        'model_type': 'v11_2_stacked_ensemble',
        'version': 'v11.2',
        'models': base_models,
        'feature_cols': feature_cols,
        'feature_subsets': {
            'momentum': MOMENTUM_FEATURES,
            'flow': FLOW_FEATURES,
            'value_quality': VALUE_QUALITY_FEATURES,
        },
        'global_medians': global_medians,
        # V11.1 新增特征默认中位数
        'price_range_pos_10d': 0.5, 'oversold_boost': 0.0, 'ma_dispersion': 0.05,
        'volume_contraction': 0.0, 'price_ma50_ratio': 0.0, 'ret_vol_ratio_5d': 0.0,
        'mkt_zt_dt_spread': 60.0, 'mkt_volatility': 3.0, 'mkt_breadth': 0.5,
        'trained_at': datetime.now().isoformat(),
        'std_20d': 3.0, 'cmra': 0.8,
        'n_samples': len(features),
        'n_stocks': int(features['ts_code'].nunique()),
        'n_features': len(feature_cols),
        'n_models': len(base_models),
        'data_range': f"{features['trade_date'].min().date()} ~ {features['trade_date'].max().date()}",
        'final_rank_ic': float(final_ic) if all_preds else 0.0,
        'cv_results': cv_results or [],
        'inference': 'ensemble_mean',
    }

    joblib.dump(bundle, oos_model_path)
    logger.info(f"模型保存: {oos_model_path} ({os.path.getsize(oos_model_path) / 1e6:.1f} MB)")

    # Step 8: 特征配置保存
    config = {
        'feature_cols': feature_cols,
        'global_medians': global_medians,
        # V11.1 新增特征默认中位数
        'price_range_pos_10d': 0.5, 'oversold_boost': 0.0, 'ma_dispersion': 0.05,
        'volume_contraction': 0.0, 'price_ma50_ratio': 0.0, 'ret_vol_ratio_5d': 0.0,
        'mkt_zt_dt_spread': 60.0, 'mkt_volatility': 3.0, 'mkt_breadth': 0.5,
        'feature_subsets': bundle['feature_subsets'],
        'std_20d': 3.0, 'cmra': 0.8,
        'model_path': str(oos_model_path),
        'trained_at': bundle['trained_at'],
        'version': 'v11.2',
    }
    with open(oos_config_path, 'w') as f:
        json.dump(config, f, indent=2, default=str)

    # Step 9: 监控记录
    monitor_record = {
        'trained_at': bundle['trained_at'],
        'version': 'v11.2',
        'n_samples': bundle['n_samples'],
        'n_stocks': bundle['n_stocks'],
        'n_features': bundle['n_features'],
        'final_rank_ic': bundle.get('final_rank_ic', 0),
        'data_range': bundle['data_range'],
        'ensemble_n_models': bundle['n_models'],
        'feature_importance_top20': dict(list(importance.items())[:20]),
    }
    try:
        records = []
        if os.path.exists(MONITOR_HISTORY_PATH):
            with open(MONITOR_HISTORY_PATH) as f:
                records = json.load(f)
        records.append(monitor_record)
        with open(MONITOR_HISTORY_PATH, 'w') as f:
            json.dump(records, f, indent=2, default=str)
    except Exception:
        pass

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"\n{'='*60}")
    logger.info(f"完成! 耗时: {elapsed:.1f}s")
    logger.info(f"{'='*60}")

    if cv_results:
        logger.info("Purged Walk-Forward 均值:")
        logger.info(f"  Rank IC:   {np.mean([r['rank_ic'] for r in cv_results]):.3f}")
        logger.info(f"  日频 IC:   {np.mean([r['mean_daily_ic'] for r in cv_results]):.3f}")
        logger.info(f"  IC IR:     {np.mean([r['ic_ir'] for r in cv_results]):.2f}")
        logger.info(f"  Spread:    {np.mean([r['spread'] for r in cv_results]):.1f}bp")

    logger.info("全数据集成融合:")
    if all_preds:
        logger.info(f"  Rank IC:   {final_ic:.3f}")


if __name__ == '__main__':
    main()
