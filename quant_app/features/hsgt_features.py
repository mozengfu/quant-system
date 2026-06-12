"""
北向资金特征 (Hong Kong Stock Connect)

5000 积分限制:
  - stock_hsgt_hold (个股级持股变化) 不可用
  - 改用: hsgt_top10 (北向 Top10 活跃股) + moneyflow_hsgt (市场汇总) + stock_hsgt (北向标的名录)
  - 个股级净流入用"是否进入 Top10 + 当日成交活跃度"代理

特征:
  - hsgt_is_target:      是否北向标的 (1/0)
  - hsgt_in_top10_1d:    当日是否进入北向 Top10 活跃
  - hsgt_in_top10_3d:    3 日内累计
  - hsgt_top10_amt_1d:   当日 Top10 成交额
  - hsgt_market_north_5d: 北向资金 5 日累计净流入 (亿元, 板块情绪)
  - hsgt_market_north_20d: 20 日累计
"""
import logging

import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


def build_hsgt_features(ts_codes: list[str], as_of_date: str, conn=None, lookback_days: int = 30) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    HSGT_FEATURES = ['hsgt_is_target','hsgt_in_top10_1d','hsgt_in_top10_3d','hsgt_in_top10_5d',
                     'hsgt_top10_amt_1d','hsgt_top10_count_5d','hsgt_top10_amt_5d',
                     'hsgt_market_north_5d','hsgt_market_north_20d']
    should_close = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        should_close = True
    try:
        # 北向表可能不存在, 降级返回 0
        cur = conn.cursor()
        cur.execute("SHOW TABLES LIKE 'stock_hsgt'")
        if not cur.fetchone():
            logger.warning("stock_hsgt not exist, returning zero features")
            return pd.DataFrame({k: [0]*len(ts_codes) for k in HSGT_FEATURES}, index=ts_codes)
        cur.execute("SHOW TABLES LIKE 'hsgt_top10'")
        if not cur.fetchone():
            return pd.DataFrame({k: [0]*len(ts_codes) for k in HSGT_FEATURES}, index=ts_codes)
        placeholders = ','.join(['%s'] * len(ts_codes))
        # 1) 北向标的名录
        sql_target = f"SELECT ts_code FROM stock_hsgt WHERE ts_code IN ({placeholders})"
        df_target = pd.read_sql(sql_target, conn, params=ts_codes)
        target_set = set(df_target['ts_code'].tolist())

        # 2) 北向 Top10 活跃 (近 lookback_days)
        sql_top10 = """
            SELECT trade_date, ts_code, amount, rank
            FROM hsgt_top10
            WHERE trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
              AND market_type IN ('1','3')   -- 沪股通+深股通
        """
        df_top10 = pd.read_sql(sql_top10, conn, params=(as_of_date, lookback_days, as_of_date),
                               parse_dates=['trade_date'])
        df_top10['amount'] = df_top10['amount'].fillna(0)

        # 3) 北向市场汇总
        sql_mkt = """
            SELECT trade_date, north_money
            FROM moneyflow_hsgt
            WHERE trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
        """
        df_mkt = pd.read_sql(sql_mkt, conn, params=(as_of_date, lookback_days, as_of_date),
                             parse_dates=['trade_date'])
        if not df_mkt.empty:
            df_mkt['north_money'] = pd.to_numeric(df_mkt['north_money'], errors='coerce').fillna(0)
            north_5d = float(df_mkt.sort_values('trade_date', ascending=False).head(5)['north_money'].sum())
            north_20d = float(df_mkt.sort_values('trade_date', ascending=False).head(20)['north_money'].sum())
        else:
            north_5d = north_20d = 0.0

        results = []
        for code in ts_codes:
            row = {'ts_code': code}
            row['hsgt_is_target'] = int(code in target_set)
            sub = df_top10[df_top10['ts_code'] == code].sort_values('trade_date', ascending=False)
            if not sub.empty:
                today = sub[sub['trade_date'] == pd.Timestamp(as_of_date)]
                row['hsgt_in_top10_1d'] = int(len(today) > 0)
                row['hsgt_in_top10_3d'] = int((sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=3)).any())
                row['hsgt_in_top10_5d'] = int((sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)).any())
                row['hsgt_top10_amt_1d'] = float(today['amount'].iloc[0]) / 1e8 if len(today) else 0  # 亿
                # 5 日累计成交额
                near5 = sub[sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)]
                row['hsgt_top10_count_5d'] = int(len(near5))
                row['hsgt_top10_amt_5d'] = float(near5['amount'].sum()) / 1e8
            else:
                row.update({k: 0 for k in ['hsgt_in_top10_1d','hsgt_in_top10_3d','hsgt_in_top10_5d',
                                            'hsgt_top10_amt_1d','hsgt_top10_count_5d','hsgt_top10_amt_5d']})
            row['hsgt_market_north_5d'] = round(north_5d / 1e4, 2)   # 万元 → 亿元
            row['hsgt_market_north_20d'] = round(north_20d / 1e4, 2)
            results.append(row)
        return pd.DataFrame(results).set_index('ts_code')
    finally:
        if should_close:
            conn.close()
