#!/usr/bin/env python3
"""
ML选股模型训练 V8.3 — V6.5 成功参数 + V8.0 特征

修复 v8.0 退化问题:
  - 标签: 原始5日收益(非波动率调整)
  - 分箱: 5档(非10档)
  - 超参数: num_leaves=31, lr=0.05(非64, 0.01)
  - 早停: 30(非50)
"""

import os, sys, json, logging, copy
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import numpy as np, pandas as pd, pymysql, lightgbm as lgb
from scipy.stats import spearmanr
import joblib

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
MODEL_PATH = os.path.join(DATA_DIR, 'ml_stock_model_v8_3.pkl')
FEATURE_CONFIG_PATH = os.path.join(DATA_DIR, 'feature_config_v8_3.json')
MONITOR_HISTORY_PATH = os.path.join(DATA_DIR, 'model_monitor_history.json')

# 排除无交易权限股票
EXCLUDE_PREFIXES = ('68', '83', '87', '43')
NO_PERM_PREFIXES_1 = ('8', '4', '9')

from quant_app.utils.config import get_db_config
DB_CONFIG = get_db_config()


def get_db():
    return pymysql.connect(**DB_CONFIG)


def load_data():
    logger.info("加载最近 600 个交易日数据...")
    conn = get_db()

    # 行情数据
    daily = pd.read_sql("""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               vol, amount, pct_chg, turnover_rate, volume_ratio,
               ma5, ma10, ma20, rps_20, low_52w, high_52w
        FROM daily_price
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)

    # 资金流
    moneyflow = pd.read_sql("""
        SELECT ts_code, trade_date, main_net, net_mf_amount,
               buy_sm_amount, sell_sm_amount, buy_lg_amount, sell_lg_amount
        FROM moneyflow_daily
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)

    # 指数数据（仅用于计算个股相对市场的特征）
    index_data = pd.read_sql("""
        SELECT trade_date, change_pct, close_price
        FROM market_index_daily
        WHERE index_code='000001.SH'
        AND trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)

    # 截面因子：市值、估值
    fundamentals = pd.read_sql("""
        SELECT ts_code, trade_date, pe_ttm, pb, total_mv, circ_mv
        FROM daily_basic
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)

    # 行业信息
    stock_info = pd.read_sql("SELECT ts_code, industry FROM stock_info", conn)

    # Alpha 信号
    alpha_signals = pd.read_sql("""
        SELECT ts_code, signal_date, MAX(score_boost) as max_boost
        FROM alpha_signals
        GROUP BY ts_code, signal_date
    """, conn)
    if not alpha_signals.empty:
        alpha_signals['signal_date'] = pd.to_datetime(alpha_signals['signal_date'])
    logger.info(f"Alpha信号: {len(alpha_signals)} 条 ({alpha_signals['ts_code'].nunique()} 只)")

    # 融资融券
    margin = pd.read_sql("""
        SELECT ts_code, trade_date, rzye, rqye, rzmre
        FROM margin_daily
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)
    if not margin.empty:
        margin['trade_date'] = pd.to_datetime(margin['trade_date'])
        for c in ['rzye', 'rqye', 'rzmre']:
            margin[c] = pd.to_numeric(margin[c], errors='coerce').fillna(0)
    logger.info(f"融资融券: {len(margin)} 条")

    # 龙虎榜数据
    dragon_tiger = pd.read_sql("""
        SELECT ts_code, trade_date, net_buy, buy, sell, exalter as reason
        FROM dragon_tiger
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
          AND net_buy != 0
    """, conn)
    if not dragon_tiger.empty:
        dragon_tiger['trade_date'] = pd.to_datetime(dragon_tiger['trade_date'])
        dragon_tiger['net_buy'] = pd.to_numeric(dragon_tiger['net_buy'], errors='coerce').fillna(0)
        dragon_tiger['buy'] = pd.to_numeric(dragon_tiger['buy'], errors='coerce').fillna(0)
    logger.info(f"龙虎榜: {len(dragon_tiger)} 条 ({dragon_tiger['ts_code'].nunique() if not dragon_tiger.empty else 0} 只)")

    # 龙虎榜机构席位明细
    dragon_tiger_inst = pd.read_sql("""
        SELECT ts_code, trade_date, net_buy, exalter
        FROM dragon_tiger_inst
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
          AND net_buy != 0
          AND (exalter LIKE '%机构%' OR exalter LIKE '%专用%')
    """, conn)
    if not dragon_tiger_inst.empty:
        dragon_tiger_inst['trade_date'] = pd.to_datetime(dragon_tiger_inst['trade_date'])
        dragon_tiger_inst['net_buy'] = pd.to_numeric(dragon_tiger_inst['net_buy'], errors='coerce').fillna(0)
    logger.info(f"龙虎榜机构: {len(dragon_tiger_inst)} 条 ({dragon_tiger_inst['ts_code'].nunique() if not dragon_tiger_inst.empty else 0} 只)")

    # 股东人数变化
    holder_change = pd.read_sql("""
        SELECT ts_code, end_date, holder_num, holder_num_change, holder_change_pct
        FROM holder_change
        WHERE end_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)
    if not holder_change.empty:
        holder_change['end_date'] = pd.to_datetime(holder_change['end_date'])
        for c in ['holder_num', 'holder_num_change', 'holder_change_pct']:
            holder_change[c] = pd.to_numeric(holder_change[c], errors='coerce').fillna(0)
    logger.info(f"股东变化: {len(holder_change)} 条 ({holder_change['ts_code'].nunique() if not holder_change.empty else 0} 只)")

    # ====== V6.3/V6.4 新增数据源 ======

    # 涨停板
    zt_pool = pd.read_sql("""
        SELECT ts_code, trade_date, last_board, seal_amount, open_count
        FROM zt_pool
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)
    if not zt_pool.empty:
        zt_pool['trade_date'] = pd.to_datetime(zt_pool['trade_date'])
        zt_pool['last_board'] = pd.to_numeric(zt_pool['last_board'], errors='coerce').fillna(0).astype(int)
        zt_pool['seal_amount'] = pd.to_numeric(zt_pool['seal_amount'], errors='coerce').fillna(0)
        zt_pool['open_count'] = pd.to_numeric(zt_pool['open_count'], errors='coerce').fillna(0).astype(int)
    logger.info(f"涨停板: {len(zt_pool):,} 条 ({zt_pool['ts_code'].nunique() if not zt_pool.empty else 0} 只)")

    # 行业板块历史行情
    board_ind_hist = pd.read_sql("""
        SELECT board_code, board_name, trade_date, pct_change, amount
        FROM board_industry_hist
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)
    if not board_ind_hist.empty:
        board_ind_hist['trade_date'] = pd.to_datetime(board_ind_hist['trade_date'])
        board_ind_hist['pct_change'] = pd.to_numeric(board_ind_hist['pct_change'], errors='coerce').fillna(0)
    logger.info(f"行业板块历史: {len(board_ind_hist):,} 条")

    # 行业板块成分映射
    board_ind_cons = pd.read_sql("""
        SELECT board_code, ts_code
        FROM board_industry_cons
    """, conn)
    logger.info(f"行业成分映射: {len(board_ind_cons):,} 条")

    # 概念板块成分映射
    board_concept_cons = pd.read_sql("""
        SELECT board_code, ts_code
        FROM board_concept_cons
    """, conn)
    logger.info(f"概念成分映射: {len(board_concept_cons):,} 条")

    # 业绩报表
    earnings = pd.read_sql("""
        SELECT ts_code, report_date, revenue_yoy, net_profit_yoy, roe, gross_margin
        FROM earnings_report
    """, conn)
    if not earnings.empty:
        earnings['report_date'] = pd.to_datetime(earnings['report_date'])
        for c in ['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']:
            earnings[c] = pd.to_numeric(earnings[c], errors='coerce')
    logger.info(f"业绩报表: {len(earnings):,} 条")


    # 大宗交易
    block_trade = pd.read_sql("""
        SELECT ts_code, trade_date, premium_rate, deal_amount, buyer
        FROM block_trade
        WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 600 DAY
    """, conn)
    if not block_trade.empty:
        block_trade["trade_date"] = pd.to_datetime(block_trade["trade_date"])
        block_trade["premium_rate"] = pd.to_numeric(block_trade["premium_rate"], errors="coerce").fillna(0)
        block_trade["deal_amount"] = pd.to_numeric(block_trade["deal_amount"], errors="coerce").fillna(0)
    logger.info(f"大宗交易: {len(block_trade)} 条")

    # 业绩预告
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
    logger.info(f"业绩预告: {len(stock_forecast)} 条")
    daily['trade_date'] = pd.to_datetime(daily['trade_date'])
    moneyflow['trade_date'] = pd.to_datetime(moneyflow['trade_date'])
    index_data['trade_date'] = pd.to_datetime(index_data['trade_date'])
    if not fundamentals.empty:
        fundamentals['trade_date'] = pd.to_datetime(fundamentals['trade_date'])

    dates = sorted(daily['trade_date'].unique())
    min_date = dates[-400] if len(dates) > 400 else dates[0]
    max_date = dates[-1]

    conn.close()

    train_start = dates[-400] if len(dates) > 400 else dates[0]
    logger.info(f"行情: {len(daily):,} 行, {daily['ts_code'].nunique()} 股 | "
                f"资金流: {len(moneyflow):,} | 基本面: {len(fundamentals):,} | "
                f"Alpha信号: {len(alpha_signals)} | 融资融券: {len(margin)} | "
                f"龙虎榜: {len(dragon_tiger)} | 龙虎榜机构: {len(dragon_tiger_inst)} | "
                f"股东变化: {len(holder_change)} | 涨停板: {len(zt_pool):,} | "
                f"行业板块: {len(board_ind_hist):,} | 业绩: {len(earnings):,} | "
                f"大宗交易: {len(block_trade):,} | 业绩预告: {len(stock_forecast):,} | "
                f"窗口: {len(dates)}个交易日 / 训练期 {train_start} ~ {dates[-1]}")
    return (daily, moneyflow, index_data, fundamentals, stock_info, alpha_signals,
            margin, dragon_tiger, dragon_tiger_inst, holder_change,
            zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
            block_trade, stock_forecast,
            min_date, max_date)


def build_features(daily, moneyflow, index_data, fundamentals, stock_info, alpha_signals,
                   margin, dragon_tiger, dragon_tiger_inst, holder_change,
                   zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
                   block_trade, stock_forecast,
                   min_date, max_date):
    """
    V6.4 特征构建 — V6.2 全部特征 + V6.3 新增涨停板/真实行业动量/概念热度/业绩因子
    """
    logger.info("构建特征 V6.2...")

    # === 预处理融资融券 ===
    margin_dict = {}
    if not margin.empty:
        for ts_code, mg in margin.groupby('ts_code'):
            margin_dict[ts_code] = mg.sort_values('trade_date')

    # === 预处理基本面 ===
    fund_dict = {}
    if not fundamentals.empty:
        for ts_code, fg in fundamentals.groupby('ts_code'):
            fund_dict[ts_code] = fg.sort_values('trade_date')

    # === 行业映射 ===
    ind_map = {}
    if not stock_info.empty:
        for _, row in stock_info.iterrows():
            ind_map[row['ts_code']] = row.get('industry', '')

    # === 预处理资金流 ===
    mf_dict = {}
    for ts_code, mf in moneyflow.groupby('ts_code'):
        mf_dict[ts_code] = mf.sort_values('trade_date')

    # === 预处理龙虎榜（按股票分组，按trade_date排序） ===
    dt_dict = {}
    if not dragon_tiger.empty:
        for ts_code, dtg in dragon_tiger.groupby('ts_code'):
            dt_dict[ts_code] = dtg.sort_values('trade_date').reset_index(drop=True)

    # === 预处理龙虎榜机构明细 ===
    dti_dict = {}
    if not dragon_tiger_inst.empty:
        for ts_code, dtig in dragon_tiger_inst.groupby('ts_code'):
            dti_dict[ts_code] = dtig.sort_values('trade_date').reset_index(drop=True)

    # === 预处理股东人数变化（按股票分组，按end_date排序） ===
    hc_dict = {}
    if not holder_change.empty:
        for ts_code, hcg in holder_change.groupby('ts_code'):
            hc_dict[ts_code] = hcg.sort_values('end_date').reset_index(drop=True)

    # ====== V6.4 新增预处理 ======

    # 涨停板按股票分组
    zt_dict = {}
    if not zt_pool.empty:
        for ts_code, zg in zt_pool.groupby('ts_code'):
            zt_dict[ts_code] = zg.sort_values('trade_date').reset_index(drop=True)

    # 行业板块成分映射: ts_code -> board_code
    ind_board_map = {}
    if not board_ind_cons.empty:
        for _, row in board_ind_cons.iterrows():
            ind_board_map[row['ts_code']] = row['board_code']

    # 概念板块成分映射: ts_code -> [board_code, ...]
    concept_board_map = {}
    if not board_concept_cons.empty:
        for _, row in board_concept_cons.iterrows():
            concept_board_map.setdefault(row['ts_code'], []).append(row['board_code'])

    # 行业板块历史行情: board_code -> {trade_date -> pct_change}
    ind_hist_map = {}
    if not board_ind_hist.empty:
        for _, row in board_ind_hist.iterrows():
            key = row['board_code']
            if key not in ind_hist_map:
                ind_hist_map[key] = {}
            ind_hist_map[key][row['trade_date']] = row['pct_change']

    # 业绩报表: ts_code -> sorted by report_date
    earn_dict = {}
    if not earnings.empty:
        for ts_code, eg in earnings.groupby('ts_code'):
            earn_dict[ts_code] = eg.sort_values('report_date')

    # 概念板块每日平均涨幅（从 daily_price + concept_cons 计算）
    logger.info("预计算概念板块每日动量...")
    concept_daily_mom = pd.DataFrame()
    if not board_concept_cons.empty and not daily.empty:
        daily_code = daily[['ts_code', 'trade_date', 'pct_chg']].copy()
        daily_code['trade_date'] = pd.to_datetime(daily_code['trade_date']).values.astype('datetime64[us]')
        concept_df = board_concept_cons.copy()
        merged = daily_code.merge(concept_df, on='ts_code', how='inner')
        concept_daily_mom = merged.groupby(['board_code', 'trade_date'])['pct_chg'].mean().reset_index()
        concept_daily_mom.columns = ['board_code', 'trade_date', 'concept_board_pct']

    results = []

    # === 预处理大宗交易（leak-fixed: 按日期对齐） ===
    bt_dict = {}
    if not block_trade.empty:
        for ts_code, btg in block_trade.groupby("ts_code"):
            bt_dict[ts_code] = btg.sort_values("trade_date").reset_index(drop=True)

    # === 预处理业绩预告（leak-fixed: merge_asof） ===
    fc_dict = {}
    if not stock_forecast.empty:
        for ts_code, fcg in stock_forecast.groupby("ts_code"):
            fcg = fcg.sort_values("report_date").reset_index(drop=True)
            fcg["trade_date"] = fcg["report_date"]
            fc_dict[ts_code] = fcg

    for ts_code, group in daily.groupby('ts_code'):
        if ts_code[:2] in EXCLUDE_PREFIXES or ts_code[:1] in NO_PERM_PREFIXES_1:
            continue
        group = group.sort_values('trade_date').reset_index(drop=True)
        if len(group) < 60:
            continue

        g = group.copy()
        g['trade_date'] = pd.to_datetime(g['trade_date']).values.astype('datetime64[us]')
        industry = ind_map.get(ts_code, '')

        # ================================================================
        # 所有 rolling 特征统一 shift(1) — 防未来数据泄漏
        # ================================================================

        # --- 现有 V6 特征 ---
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

        # V4 派生
        g['vol_price_divergence'] = g['chg_5d'] * g['vol_trend']
        g['vol_price_div_10d'] = g['chg_10d'] * g['vol_pct_corr']
        g['ma_spread_stock'] = g['price_ma5_ratio'] - g['price_ma20_ratio']

        # --- V6.2 新增特征 ---

        # 1. 量比异常: amount / MA5(amount)
        g['amount_ma5'] = g['amount'].shift(1).rolling(5).mean()
        g['amount_ma5_ratio'] = g['amount'] / g['amount_ma5'].replace(0, np.nan)

        # 2. 短期价格位置: (close - low_Nd) / (high_Nd - low_Nd)
        g['low_10d'] = g['close'].shift(1).rolling(10).min()
        g['high_10d'] = g['close'].shift(1).rolling(10).max()
        g['low_20d'] = g['close'].shift(1).rolling(20).min()
        g['high_20d'] = g['close'].shift(1).rolling(20).max()
        g['pos_10d'] = (g['close'] - g['low_10d']) / (g['high_10d'] - g['low_10d']).replace(0, np.nan)
        g['pos_20d'] = (g['close'] - g['low_20d']) / (g['high_20d'] - g['low_20d']).replace(0, np.nan)

        # 3. 振幅(10日): (最高-最低)/均价
        g['amplitude_10d'] = (g['high_10d'] - g['low_10d']) / g['close'].shift(1).rolling(10).mean().replace(0, np.nan)

        # 4. 换手率比: turnover_rate / MA20(turnover_rate)
        g['turnover_rate_ma20'] = g['turnover_rate'].shift(1).rolling(20).mean()
        g['turnover_ratio'] = g['turnover_rate'] / g['turnover_rate_ma20'].replace(0, np.nan)

        # 5. 动量分歧: 短期动量 - 长期动量
        g['mom_divergence'] = g['chg_5d'] - g['chg_20d']

        # 6. 收益率偏度与峰度 (20日)
        g['ret_skew_20d'] = g['pct_chg'].shift(1).rolling(20).skew()
        g['ret_kurt_20d'] = g['pct_chg'].shift(1).rolling(20).kurt()

        # 7. max/min ret over 5d (shift(1))
        g['ret_max_5d'] = g['pct_chg'].shift(1).rolling(5).max()
        g['ret_min_5d'] = g['pct_chg'].shift(1).rolling(5).min()

        # --- 资金流特征 (shift(1)) ---
        if ts_code in mf_dict:
            mf = mf_dict[ts_code]
            g = g.merge(mf[['trade_date', 'main_net', 'net_mf_amount',
                           'buy_sm_amount', 'sell_sm_amount', 'buy_lg_amount', 'sell_lg_amount']],
                       on='trade_date', how='left')
            g['amount_est'] = g['vol'] * g['close'] * 100
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

        # --- 融资融券 (shift(1)) ---
        if ts_code in margin_dict:
            mg = margin_dict[ts_code]
            g = g.merge(mg[['trade_date', 'rzye', 'rqye', 'rzmre']], on='trade_date', how='left')
            g['rzye'] = g['rzye'].fillna(0)
            g['rqye'] = g['rqye'].fillna(0)
            g['rzmre'] = g['rzmre'].fillna(0)
            g['margin_total'] = g['rzye'] + g['rqye']
            g['rzye_chg'] = g['rzye'].diff()
            g['rzmre_ratio'] = g['rzmre'] / g['amount_est'].replace(0, np.nan)
            g[['rzye_chg', 'rzmre_ratio']] = g[['rzye_chg', 'rzmre_ratio']].shift(1)
        else:
            g['rzye_chg'] = np.nan
            g['rzmre_ratio'] = np.nan

        # --- 截面因子：市值 + 估值 (每日更新) ---
        if ts_code in fund_dict:
            fd = fund_dict[ts_code]
            g = g.merge(fd[['trade_date', 'pe_ttm', 'pb', 'total_mv', 'circ_mv']], on='trade_date', how='left')
            g['ln_mv'] = np.log(g['total_mv'].replace(0, np.nan))
            g['ln_circ_mv'] = np.log(g['circ_mv'].replace(0, np.nan))
        else:
            for c in ['pe_ttm', 'pb', 'total_mv', 'circ_mv']:
                g[c] = np.nan
            g['ln_mv'] = np.nan
            g['ln_circ_mv'] = np.nan

        # --- V6.2: 不再加入指数特征（对截面排序无贡献） ---

        # --- 龙虎榜特征 ---
        if ts_code in dt_dict:
            dt = dt_dict[ts_code]
            # 按日聚合净买入
            dt_daily = dt.groupby('trade_date', as_index=False)['net_buy'].agg(['sum', 'count'])
            dt_daily.columns = ['trade_date', 'dragon_net_buy_30d', 'dragon_count_30d']
            g = g.merge(dt_daily, on='trade_date', how='left')
            g[['dragon_net_buy_30d', 'dragon_count_30d']] = g[['dragon_net_buy_30d', 'dragon_count_30d']].fillna(0)
            # rolling 30d + shift(1) 防泄漏
            g['dragon_net_buy_30d'] = g['dragon_net_buy_30d'].shift(1).rolling(30, min_periods=1).sum()
            g['dragon_count_30d'] = g['dragon_count_30d'].shift(1).rolling(30, min_periods=1).sum()
        else:
            g['dragon_net_buy_30d'] = 0.0
            g['dragon_count_30d'] = 0.0

        # --- 龙虎榜机构特征 ---
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

        # --- 股东集中度特征（端到端合并） ---
        if ts_code in hc_dict:
            hc = hc_dict[ts_code][['end_date', 'holder_change_pct', 'holder_num_change']].copy()
            hc = hc.rename(columns={'end_date': 'trade_date'})
            # 用 merge_asof 获取每个交易日前最新的股东数据
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
            # 计算连续减少期数
            g['holder_consecutive_decline'] = 0
            neg_mask = g['holder_num_change'] < 0
            # 用分组滚动计数
            decline_count = 0
            decline_series = []
            for v in neg_mask.values:
                if v:
                    decline_count += 1
                else:
                    decline_count = 0
                decline_series.append(decline_count)
            g['holder_consecutive_decline'] = decline_series
            g['holder_consecutive_decline'] = g['holder_consecutive_decline'].shift(1).fillna(0).astype(int)
        else:
            g['holder_change_pct'] = 0.0
            g['holder_num_change'] = 0
            g['holder_consecutive_decline'] = 0

        # ====== V6.4 新增特征（来自 V6.3） ======

        # 1. 涨停板特征
        if ts_code in zt_dict:
            zt = zt_dict[ts_code]
            zt_flags = zt[['trade_date', 'last_board', 'seal_amount', 'open_count']].copy()
            zt_flags['is_zt'] = 1
            g = g.merge(zt_flags[['trade_date', 'is_zt', 'last_board', 'seal_amount', 'open_count']],
                       on='trade_date', how='left')
            g['is_zt'] = g['is_zt'].fillna(0).astype(int)
            g['last_board'] = g['last_board'].fillna(0).astype(int)
            g['seal_amount'] = g['seal_amount'].fillna(0)
            g['open_count'] = g['open_count'].fillna(0).astype(int)
            g['zt_count_30d'] = g['is_zt'].shift(1).rolling(30, min_periods=1).sum()
            g['zt_max_board_30d'] = g['last_board'].shift(1).rolling(30, min_periods=1).max()
            g['zt_seal_amount_30d'] = g['seal_amount'].shift(1).rolling(30, min_periods=1).sum()
        else:
            g['zt_count_30d'] = 0
            g['zt_max_board_30d'] = 0
            g['zt_seal_amount_30d'] = 0.0

        # 2. 真实行业动量 (board_industry_hist)
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

        # 3. 概念板块热度
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
            else:
                g['concept_mom_avg'] = 0
                g['concept_mom_5d'] = 0
                g['concept_mom_10d'] = 0
            g['concept_count'] = len(concepts)
        else:
            g['concept_mom_avg'] = 0
            g['concept_mom_5d'] = 0
            g['concept_mom_10d'] = 0
            g['concept_count'] = 0

        # 4. 业绩因子 (earnings_report)
        if ts_code in earn_dict:
            ed = earn_dict[ts_code][['report_date', 'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']]
            ed = ed.rename(columns={'report_date': 'trade_date'})
            g_sorted = g[['trade_date']].sort_values('trade_date').drop_duplicates()
            g_sorted['trade_date'] = g_sorted['trade_date'].values.astype('datetime64[us]')
            ed_sorted = ed.sort_values('trade_date')
            ed_sorted['trade_date'] = ed_sorted['trade_date'].values.astype('datetime64[us]')
            merged = pd.merge_asof(g_sorted, ed_sorted, on='trade_date', direction='backward')
            merged = merged.set_index('trade_date')
            g = g.merge(merged[['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']],
                       left_on='trade_date', right_index=True, how='left')
            g['revenue_yoy'] = g['revenue_yoy'].fillna(0)
            g['net_profit_yoy'] = g['net_profit_yoy'].fillna(0)
            g['roe'] = g['roe'].fillna(0)
            g['gross_margin'] = g['gross_margin'].fillna(0)
        else:
            for c in ['revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin']:
                g[c] = 0.0

        # ====== V8.0 新增特征 ======

        # 1. 短期反转: 昨日涨跌幅的反号
        g["ret_1d_reversal"] = -g["pct_chg"].shift(1) / 100.0

        # 2. 缩量上涨天数: 过去10天中价格上涨且量比<1的天数
        g["volume_div_days_10d"] = ((g["pct_chg"].shift(1) > 0) & (g["volume_ratio"].shift(1) < 1.0)).rolling(10).sum()

        # 3. 换手率变异系数: 20日换手率标准差/均值
        g["turnover_std"] = g["turnover_rate"].shift(1).rolling(20).std()
        g["turnover_mean"] = g["turnover_rate"].shift(1).rolling(20).mean()
        g["turnover_std_ratio"] = g["turnover_std"] / g["turnover_mean"].replace(0, np.nan)

        # 4. 大宗交易溢价率排名（泄漏修复：按日期对齐）
        if ts_code in bt_dict:
            bt_stock = bt_dict[ts_code]
            bt_aligned = bt_stock.set_index("trade_date").reindex(g["trade_date"].values)
            has_trade = bt_aligned["premium_rate"].notna().astype(int)
            g["bt_count_30d"] = has_trade.rolling(30, min_periods=1).sum().shift(1).fillna(0).values
            premium = bt_aligned["premium_rate"].fillna(0)
            rolling_sum = premium.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            rolling_count = has_trade.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            g["bt_premium_avg_30d"] = (rolling_sum / rolling_count.replace(0, np.nan)).fillna(0).values
            # 大宗交易溢价加权（deal_amount 加权）
            deal_amt = bt_aligned["deal_amount"].fillna(0)
            w_sum = (premium * deal_amt).rolling(30, min_periods=1).sum().shift(1).fillna(0)
            w_div = deal_amt.rolling(30, min_periods=1).sum().shift(1).fillna(0)
            g["bt_premium_weighted_30d"] = (w_sum / w_div.replace(0, np.nan)).fillna(0).values
        else:
            g["bt_count_30d"] = 0.0
            g["bt_premium_avg_30d"] = 0.0
            g["bt_premium_weighted_30d"] = 0.0

        # 5. 业绩预告特征（泄漏修复：merge_asof 向前匹配）
        if ts_code in fc_dict:
            fc_stock = fc_dict[ts_code]
            g_temp = g[["trade_date"]].copy()
            g_temp["trade_date"] = g_temp["trade_date"].values.astype("datetime64[us]")
            fc_for_merge = fc_stock[["trade_date", "forecast_type", "net_profit_max"]].rename(
                columns={"trade_date": "fc_date"}
            ).sort_values("fc_date")
            fc_for_merge["fc_date"] = fc_for_merge["fc_date"].values.astype("datetime64[us]")
            merged = pd.merge_asof(
                g_temp.sort_values("trade_date"),
                fc_for_merge,
                left_on="trade_date", right_on="fc_date", direction="backward"
            )
            type_map_bt = {"预增": 2, "扭亏": 2, "略增": 1, "续盈": 1,
                          "略减": -1, "预减": -2, "首亏": -2, "续亏": -3}
            merged["forecast_type_code"] = merged["forecast_type"].map(type_map_bt).fillna(0).astype(int)
            merged["forecast_is_positive"] = merged["forecast_type"].isin(
                ("预增", "扭亏", "略增", "续盈")
            ).astype(int)
            merged["forecast_days_since"] = (merged["trade_date"] - merged["fc_date"]).dt.days.fillna(30).astype(int)
            merged["forecast_net_profit_max"] = merged["net_profit_max"].fillna(0)
            merged = merged.set_index("trade_date")
            g["forecast_type_code"] = merged["forecast_type_code"].values
            g["forecast_is_positive"] = merged["forecast_is_positive"].values
            g["forecast_days_since"] = merged["forecast_days_since"].values
            g["forecast_net_profit_max"] = merged["forecast_net_profit_max"].values
        else:
            g["forecast_type_code"] = 0
            g["forecast_is_positive"] = 0
            g["forecast_days_since"] = 30
            g["forecast_net_profit_max"] = 0.0

        # --- V8.3 回归目标：原始5日收益（恢复v6.5验证过的配置） ---
        g["target_5d"] = g["close"].shift(-5) / g["close"] - 1

        valid = g.dropna(subset=["target_5d"])
        valid = valid[valid["trade_date"] >= pd.Timestamp(min_date)]
        if len(valid) < 10:
            continue
        results.append(valid)

    if not results:
        logger.warning("无有效样本")
        return pd.DataFrame(), {}, []

    result = pd.concat(results, ignore_index=True)

    # === 行业超额收益目标 ===
    logger.info("计算行业超额收益 alpha_5d...")
    ind_df = stock_info[['ts_code', 'industry']].dropna()
    result = result.merge(ind_df, on='ts_code', how='left')
    result['industry'] = result['industry'].fillna('OTHER')
    result['industry_avg_5d'] = result.groupby(['trade_date', 'industry'])['target_5d'].transform('mean')
    result['alpha_5d'] = result['target_5d'] - result['industry_avg_5d']

    alpha_mean = result['alpha_5d'].mean() * 100
    alpha_std = result['alpha_5d'].std() * 100
    logger.info(f"alpha_5d 分布: 均值={alpha_mean:.2f}%, std={alpha_std:.2f}%")

    # === Alpha 情绪特征 ===
    logger.info("添加 Alpha 情绪特征...")
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
    if use_alpha_features:
        logger.info(f"Alpha 覆盖率: {alpha_coverage:.1%} >= 5%, 加入训练")
    else:
        logger.info(f"Alpha 覆盖率: {alpha_coverage:.1%} < 5%, 不加入训练")

    # === 行业动量特征 ===
    logger.info("添加行业动量特征...")
    result = result.sort_values(['ts_code', 'trade_date'])
    result['ind_pct_avg'] = result.groupby(['trade_date', 'industry'])['pct_chg'].transform('mean')
    result['ind_pct_avg'] = result.groupby('industry')['ind_pct_avg'].shift(1)
    result['ind_mom_5d'] = result.groupby('industry')['ind_pct_avg'].transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    )
    result['ind_mom_20d'] = result.groupby('industry')['ind_pct_avg'].transform(
        lambda x: x.rolling(20, min_periods=1).mean()
    )

    # === 特征列定义 ===
    # V6.2: 移除 idx_* 指数特征（对截面排序无贡献）
    feature_cols = [
        # 量价基础
        'pct_chg', 'turnover_rate', 'volume_ratio',
        'vol_5d', 'vol_10d', 'vol_20d',
        'ma5_ma10_ratio', 'ma10_ma20_ratio', 'price_ma5_ratio', 'price_ma20_ratio',
        'chg_3d', 'chg_5d', 'chg_10d', 'chg_20d', 'vol_trend', 'pos_52w',
        'rps_20', 'rps_change', 'up_ratio_5d', 'up_ratio_10d', 'vol_pct_corr',
        'ma_pattern',
        # MACD/RSI/ADX
        'macd_diff', 'macd_signal_line', 'macd_hist',
        'rsi_14', 'adx_14',
        # 资金流
        'main_net_ratio', 'main_net_ma5', 'main_net_ma10', 'main_trend',
        'main_streak', 'main_vs_retail', 'lg_ratio', 'main_cum5', 'main_cum10',
        'main_accel_3d', 'smart_div_count',
        'main_inflow_ratio', 'main_flow_accel',
        # Alpha因子
        'vol_price_corr_10d', 'gap_ratio', 'gap_retention',
        'amihud', 'amihud_ma5',
        # V4 派生
        'vol_price_divergence', 'vol_price_div_10d', 'ma_spread_stock',
        # 融资融券
        'rzye_chg', 'rzmre_ratio',
        # 行业动量 + 结构
        'ind_mom_5d', 'ind_mom_20d',
        # --- V6.2 新增特征 ---
        'amount_ma5_ratio',
        'pos_10d', 'pos_20d',
        'amplitude_10d',
        'turnover_ratio',
        'mom_divergence',
        'ret_skew_20d', 'ret_kurt_20d',
        'ret_max_5d', 'ret_min_5d',
        # --- V6.2 龙虎榜特征 ---
        'dragon_net_buy_30d', 'dragon_count_30d',
        'dti_net_buy_30d', 'dti_count_30d',
        # --- V6.2 股东集中度特征 ---
        'holder_change_pct', 'holder_num_change', 'holder_consecutive_decline',
        # ===== V6.5 新增特征（来自 V6.3，去零贡献） =====
        # 涨停板
        'zt_count_30d', 'zt_max_board_30d', 'zt_seal_amount_30d',
        # 真实行业动量
        'ind_board_pct_5d', 'ind_board_pct_10d', 'ind_board_pct_20d',
        # 概念热度
        'concept_count', 'concept_mom_avg', 'concept_mom_5d', 'concept_mom_10d',
        # 业绩因子
        'revenue_yoy', 'net_profit_yoy', 'roe', 'gross_margin',
        # V8.0 新特征
        'ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
        'bt_count_30d', 'bt_premium_avg_30d', 'bt_premium_weighted_30d',
        'forecast_type_code', 'forecast_is_positive', 'forecast_days_since', 'forecast_net_profit_max',
    ]

    # 移除零贡献特征（截面因子等）
    _DROP_FEATURES = {'ln_mv', 'pe_ttm', 'pb', 'ln_circ_mv',
                      'is_st', 'ln_list_age', 'net_margin',
                      'zt_open_count_30d', 'dragon_has_institution_30d'}
    feature_cols = [c for c in feature_cols if c not in _DROP_FEATURES]

    if use_alpha_features:
        feature_cols.extend(['max_boost', 'alpha_pos_5d', 'alpha_neg_5d'])

    for col in feature_cols:
        if col not in result.columns:
            result[col] = np.nan

    # 横截面排名特征
    logger.info("添加横截面排名特征...")
    rank_features = [
        'pct_chg', 'turnover_rate', 'volume_ratio', 'rps_20',
        'lg_ratio', 'main_net_ratio', 'pos_52w',
        'chg_5d', 'chg_10d', 'main_cum5',
        # V6.2 新增排名特征
        'amount_ma5_ratio', 'pos_10d', 'turnover_ratio', 'mom_divergence',
        # 龙虎榜排名特征
        'dragon_net_buy_30d', 'dragon_count_30d',
        # V6.4 新增排名特征
        'zt_count_30d', 'zt_max_board_30d',
        'ind_board_pct_5d', 'ind_board_pct_10d',
        'concept_count', 'concept_mom_5d',
        'net_profit_yoy', 'revenue_yoy',
        'ret_1d_reversal', 'volume_div_days_10d', 'turnover_std_ratio',
        'bt_count_30d', 'bt_premium_avg_30d',
        'forecast_type_code', 'forecast_is_positive', 'forecast_net_profit_max',
    ]
    for col in rank_features:
        if col in result.columns:
            result[f'{col}_rank'] = result.groupby('trade_date')[col].rank(pct=True)
            feature_cols.append(f'{col}_rank')

    # 填充 NaN
    global_medians = {}
    for col in feature_cols:
        med = result[col].median()
        global_medians[col] = float(med) if not np.isnan(med) else 0.0
        result[col] = result[col].fillna(global_medians[col])

    logger.info(f"构建完成: {len(result):,} 样本, {result['ts_code'].nunique()} 股, {len(feature_cols)} 特征")

    y = result['alpha_5d'].values
    logger.info(f"alpha_5d: 均值={y.mean()*100:.2f}%, 中位={np.median(y)*100:.2f}%, "
                f"std={y.std()*100:.2f}%, 正超额占比={(y>0).mean()*100:.1f}%")

    return result, global_medians, feature_cols


def safe_qcut(s, n_bins=5):
    """安全的 qcut：用 rank(method='first') 保证唯一值，避免重复边界报错"""
    return pd.qcut(s.rank(method='first'), n_bins, labels=list(range(n_bins)))


def walk_forward_train(df, feature_cols):
    """
    Walk-Forward 5-fold 验证 — LambdaRank
    评估: Spearman Rank IC, 日频 IC, IC IR, Top-Bottom spread
    """
    logger.info("Walk-Forward 滚动验证...")

    df_sorted = df.sort_values('trade_date').copy()
    dates = sorted(df_sorted['trade_date'].unique())
    window_size = len(dates) // 7

    cv_results = []

    for fold in range(5):
        train_end_idx = (fold + 1) * window_size
        val_start_idx = train_end_idx
        val_end_idx = min((fold + 2) * window_size, len(dates))

        if val_start_idx >= len(dates) or val_end_idx <= val_start_idx:
            break

        train_dates = dates[:train_end_idx]
        val_dates = dates[val_start_idx:val_end_idx]

        train_mask = df_sorted['trade_date'].isin(train_dates)
        val_mask = df_sorted['trade_date'].isin(val_dates)

        X_train = df_sorted.loc[train_mask, feature_cols].values
        y_train = df_sorted.loc[train_mask, 'alpha_5d'].values
        X_val = df_sorted.loc[val_mask, feature_cols].values
        y_val = df_sorted.loc[val_mask, 'alpha_5d'].values

        if len(X_train) < 500 or len(X_val) < 50:
            continue

        # LambdaRank 离散化 — 5档分箱(v6.5验证过的配置)
        train_df = df_sorted.loc[train_mask].copy()
        val_df = df_sorted.loc[val_mask].copy()
        train_df['label_rank'] = train_df.groupby('trade_date')['alpha_5d'].transform(
            lambda x: safe_qcut(x, 5)
        )
        val_df['label_rank'] = val_df.groupby('trade_date')['alpha_5d'].transform(
            lambda x: safe_qcut(x, 5)
        )
        y_train_lr = train_df['label_rank'].fillna(2).astype(int).values
        y_val_lr = val_df['label_rank'].fillna(2).astype(int).values

        train_group = train_df.groupby('trade_date').size().to_numpy()
        val_group = val_df.groupby('trade_date').size().to_numpy()

        # V6.2: 时间衰减样本权重
        max_date = train_df['trade_date'].max()
        train_df['days_ago'] = (max_date - train_df['trade_date']).dt.days
        train_df['weight'] = np.exp(-0.005 * train_df['days_ago'])
        sample_weight = train_df['weight'].values

        td = lgb.Dataset(X_train, label=y_train_lr, group=train_group, weight=sample_weight)
        vd = lgb.Dataset(X_val, label=y_val_lr, group=val_group, reference=td)

        params = {
            'objective': 'lambdarank',
            'ndcg_eval_at': [10, 20, 50],
            'label_gain': [0, 1, 2, 3, 4],
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
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
            callbacks=[lgb.early_stopping(30), lgb.log_evaluation(0)]
        )

        vp = model.predict(X_val)

        # Rank IC
        rank_ic = spearmanr(y_val, vp)[0]

        # Top-Bottom spread
        n_top = max(10, int(len(vp) * 0.20))
        top_idx = np.argsort(vp)[-n_top:]
        bottom_idx = np.argsort(vp)[:n_top]
        top_avg_ret = y_val[top_idx].mean() * 100
        bottom_avg_ret = y_val[bottom_idx].mean() * 100
        spread = top_avg_ret - bottom_avg_ret

        # 日频 IC
        val_df_cv = df_sorted.loc[val_mask].copy()
        val_df_cv['pred'] = vp
        daily_ic = val_df_cv.groupby('trade_date').apply(
            lambda x: spearmanr(x['alpha_5d'], x['pred'])[0], include_groups=False
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
            'spread_bps': spread,
        })

        r = cv_results[-1]
        logger.info(f"  Fold {fold+1}: RankIC={r['rank_ic']:.3f}, "
                    f"日频IC={r['mean_daily_ic']:.3f}, ICIR={r['ic_ir']:.2f}, "
                    f"Spread={r['spread_bps']:.1f}bp")

    if not cv_results:
        logger.warning("Walk-Forward 无有效 fold")
        return None, None

    avg_ic = np.mean([r['rank_ic'] for r in cv_results])
    avg_daily_ic = np.mean([r['mean_daily_ic'] for r in cv_results])
    avg_icir = np.mean([r['ic_ir'] for r in cv_results])
    avg_spread = np.mean([r['spread_bps'] for r in cv_results])

    logger.info(f"  Walk-Forward 平均: RankIC={avg_ic:.3f}, 日频IC={avg_daily_ic:.3f}, "
                f"ICIR={avg_icir:.2f}, Spread={avg_spread:.1f}bp")

    return cv_results, None


def train_ensemble(df, feature_cols, n_models=5):
    """
    训练集成模型 — n 个不同种子的 LambdaRank
    保存多个模型到 bundle['models'] 列表
    """
    logger.info(f"训练 {n_models} 个集成模型...")

    df_sorted = df.sort_values('trade_date')
    max_date = df_sorted['trade_date'].max()

    # 时间衰减权重
    df_temp = df_sorted.copy()
    df_temp['days_ago'] = (max_date - df_temp['trade_date']).dt.days
    df_temp['weight'] = np.exp(-0.005 * df_temp['days_ago'])
    sample_weight = df_temp['weight'].values

    # 离散化标签 — 5档分箱
    df_temp['label_rank'] = df_temp.groupby('trade_date')['alpha_5d'].transform(
        lambda x: pd.qcut(x, 5, labels=[0,1,2,3,4], duplicates='drop')
    )
    y_lr = df_temp['label_rank'].fillna(2).astype(int).values
    X = df_sorted[feature_cols].values
    group = df_sorted.groupby('trade_date').size().to_numpy()

    models = []
    base_params = {
        'objective': 'lambdarank',
        'ndcg_eval_at': [10, 20, 50],
        'label_gain': [0, 1, 2, 3, 4],
        'boosting_type': 'gbdt',
        'num_leaves': 31,
        'learning_rate': 0.05,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'min_child_samples': 200,
            'lambda_l2': 0.01,
        'verbose': -1,
    }

    base_ds = lgb.Dataset(X, label=y_lr, group=group, weight=sample_weight)

    for i in range(n_models):
        params = dict(base_params)
        params['seed'] = 42 + i * 7  # 42, 49, 56, 63, 70
        params['feature_fraction'] = [0.7, 0.75, 0.8][i % 3]  # 0.70, 0.75, 0.80 diversified

        model = lgb.train(
            params, base_ds, num_boost_round=1500,
            callbacks=[lgb.log_evaluation(0)]
        )
        models.append(model)
        logger.info(f"  模型 {i+1}/{n_models} 训练完成 (seed={params['seed']})")

    # 集成预测
    all_preds = np.zeros((len(X), n_models))
    for i, model in enumerate(models):
        all_preds[:, i] = model.predict(X)
    ensemble_pred = np.mean(all_preds, axis=1)

    # 特征重要性 (取平均)
    imp = {}
    for model in models:
        mi = dict(zip(feature_cols, model.feature_importance()))
        for k, v in mi.items():
            imp[k] = imp.get(k, 0) + v / n_models
    imp = dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))

    # 评估集成效果
    final_rank_ic = spearmanr(df_sorted['alpha_5d'].values, ensemble_pred)[0]

    daily_ic = df_sorted.groupby('trade_date').apply(
        lambda x: spearmanr(x['alpha_5d'].values,
                            np.mean([m.predict(x[feature_cols].values) for m in models], axis=0))[0]
        if len(x) > 5 else np.nan, include_groups=False
    )
    mean_daily_ic = daily_ic.mean()
    daily_ic_std = daily_ic.std()
    ic_ir = mean_daily_ic / (daily_ic_std + 1e-9)

    logger.info(f"  集成模型: RankIC={final_rank_ic:.3f}, 日频IC={mean_daily_ic:.3f}, ICIR={ic_ir:.2f}")

    logger.info("  特征重要性 Top 15:")
    for k, v in list(imp.items())[:15]:
        logger.info(f"    {k}: {v:.0f}")

    return models, imp, {
        'final_rank_ic': final_rank_ic,
        'mean_daily_ic': mean_daily_ic,
        'ic_ir': ic_ir,
    }


def append_monitor_record(record):
    records = []
    if os.path.exists(MONITOR_HISTORY_PATH):
        try:
            with open(MONITOR_HISTORY_PATH, 'r') as f:
                records = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            records = []
    records.append(record)
    with open(MONITOR_HISTORY_PATH, 'w') as f:
        json.dump(records, f, indent=2, default=str)
    logger.info(f"监控记录已保存: {MONITOR_HISTORY_PATH}")


def main():
    start = datetime.now()

    # Step 1: 加载数据
    (daily, moneyflow, index_data, fundamentals, stock_info, alpha_signals,
     margin, dragon_tiger, dragon_tiger_inst, holder_change,
     zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
     block_trade, stock_forecast,
     min_date, max_date) = load_data()

    # Step 2: 构建特征
    features, global_medians, feature_cols = build_features(
        daily, moneyflow, index_data, fundamentals, stock_info, alpha_signals,
        margin, dragon_tiger, dragon_tiger_inst, holder_change,
        zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
        block_trade, stock_forecast,
        min_date, max_date
    )
    if features.empty:
        logger.error("特征构建失败")
        return

    # Step 3: Walk-Forward 验证
    logger.info(f"\n{'='*60}")
    logger.info("Walk-Forward 5-fold 验证")
    logger.info(f"{'='*60}")
    cv_results, _ = walk_forward_train(features, feature_cols)

    # Step 4: 训练集成模型
    logger.info(f"\n{'='*60}")
    logger.info("训练集成模型 (5-model ensemble)")
    logger.info(f"{'='*60}")
    models, importance, ensemble_metrics = train_ensemble(features, feature_cols, n_models=5)

    # Step 5: 保存
    os.makedirs(DATA_DIR, exist_ok=True)

    bundle = {
        'models': models,  # 列表：用于 ensemble 预测
        'feature_cols': feature_cols,
        'cv_results': cv_results or [],
        'importance': importance,
        'global_medians': global_medians,
        'model_type': 'lambdarank_ensemble',
        'trained_at': datetime.now().isoformat(),
        'version': 'v8.3',
        'features_shifted': True,
        'label': 'v8_0_vol_adj_multi_horizon_industry_neutral_raw',
        'n_samples': len(features),
        'n_stocks': int(features['ts_code'].nunique()),
        'n_features': len(feature_cols),
        'data_range': f"{features['trade_date'].min().date()} ~ {features['trade_date'].max().date()}",
        'ensemble_n_models': len(models),
        'final_rank_ic': float(ensemble_metrics['final_rank_ic']),
        'final_mean_daily_ic': float(ensemble_metrics['mean_daily_ic']),
        'final_ic_ir': float(ensemble_metrics['ic_ir']),
        'inference': 'ensemble_mean',  # 推理时取所有模型预测的平均值
    }

    if cv_results:
        bundle['avg_rank_ic'] = float(np.mean([r['rank_ic'] for r in cv_results]))
        bundle['avg_daily_ic'] = float(np.mean([r['mean_daily_ic'] for r in cv_results]))
        bundle['avg_ic_ir'] = float(np.mean([r['ic_ir'] for r in cv_results]))
        bundle['avg_spread_bps'] = float(np.mean([r['spread_bps'] for r in cv_results]))

    joblib.dump(bundle, MODEL_PATH)
    logger.info(f"模型保存: {MODEL_PATH}")

    # Step 6: 特征配置
    config = {
        'feature_cols': feature_cols,
        'global_medians': global_medians,
        'model_path': str(MODEL_PATH),
        'model_type': 'lambdarank_ensemble',
        'trained_at': bundle['trained_at'],
        'version': 'v8.3',
    }
    with open(FEATURE_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, default=str)

    # Step 7: 监控记录
    monitor_record = {
        'trained_at': bundle['trained_at'],
        'version': 'v8.3',
        'n_samples': bundle['n_samples'],
        'n_stocks': bundle['n_stocks'],
        'n_features': bundle['n_features'],
        'final_rank_ic': bundle.get('final_rank_ic', 0),
        'final_mean_daily_ic': bundle.get('final_mean_daily_ic', 0),
        'final_ic_ir': bundle.get('final_ic_ir', 0),
        'avg_rank_ic': bundle.get('avg_rank_ic', 0),
        'avg_daily_ic': bundle.get('avg_daily_ic', 0),
        'avg_ic_ir': bundle.get('avg_ic_ir', 0),
        'avg_spread_bps': bundle.get('avg_spread_bps', 0),
        'feature_importance_top20': dict(list(importance.items())[:20]),
        'data_range': bundle['data_range'],
        'ensemble_n_models': len(models),
    }
    append_monitor_record(monitor_record)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"\n{'='*60}")
    logger.info(f"完成! 耗时: {elapsed:.1f}s")
    logger.info(f"{'='*60}")

    if cv_results:
        logger.info(f"Walk-Forward 均值:")
        logger.info(f"  Rank IC:   {bundle['avg_rank_ic']:.3f}")
        logger.info(f"  日频 IC:   {bundle['avg_daily_ic']:.3f}")
        logger.info(f"  IC IR:     {bundle['avg_ic_ir']:.2f}")
        logger.info(f"  Spread:    {bundle['avg_spread_bps']:.1f}bp")
    logger.info(f"全量数据 (5 模型集成):")
    logger.info(f"  Rank IC:   {bundle['final_rank_ic']:.3f}")
    logger.info(f"  日频 IC:   {bundle['final_mean_daily_ic']:.3f}")
    logger.info(f"  IC IR:     {bundle['final_ic_ir']:.2f}")


if __name__ == '__main__':
    main()
