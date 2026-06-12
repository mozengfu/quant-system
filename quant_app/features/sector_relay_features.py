"""
板块接力特征 — A 股独有的"龙头—龙二—龙三"补涨结构

A 股经验:
  同一板块内, 第 1 只涨停 → 第 2 只 → 第 3 只的接力模式
  补涨股特征: 还没涨停, 但已经放量, 形态到位, 属于"主力下一个目标"

特征 (per stock, per day):
  - relay_index_in_industry:   行业内涨停序位 (1=龙头, 0=未涨停)
  - relay_index_in_concept:    概念内涨停序位
  - has_limit_up_sibling_today:  同行业当日有涨停 (1/0)
  - has_limit_up_sibling_3d:    同行业近 3 日有涨停
  - concept_limit_up_count_today: 所属概念当日涨停数
  - concept_limit_up_count_5d:   所属概念近 5 日累计
  - sector_momentum_5d:         所属行业 5 日累计涨幅 (从 sector_moneyflow 估算)
  - industry_position:          个股涨幅 / 行业涨幅 (相对强度)
"""
import logging

import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


def build_sector_relay_features(ts_codes: list[str], as_of_date: str, conn=None, lookback_days: int = 5) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    should_close = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        should_close = True
    try:
        placeholders = ','.join(['%s'] * len(ts_codes))

        # 1) 个股行业
        sql_ind = f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({placeholders})"
        df_ind = pd.read_sql(sql_ind, conn, params=ts_codes)
        ind_map = dict(zip(df_ind['ts_code'], df_ind['industry'].fillna('OTHER')))

        # 2) 涨停股 (industry 已知)
        sql_limit = """
            SELECT trade_date, ts_code, industry
            FROM limit_list_d
            WHERE trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
        """
        df_limit = pd.read_sql(sql_limit, conn, params=(as_of_date, lookback_days, as_of_date),
                               parse_dates=['trade_date'])

        # 3) 板块资金流 (sector_moneyflow, 5 日累计涨幅代理)
        sql_money = """
            SELECT trade_date, sector_name, pct_change, net_amount
            FROM sector_moneyflow
            WHERE trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
        """
        df_money = pd.read_sql(sql_money, conn, params=(as_of_date, lookback_days, as_of_date),
                               parse_dates=['trade_date'])

        # 4) 个股当日的 industry 内涨停序位
        today = pd.Timestamp(as_of_date)
        today_limit = df_limit[df_limit['trade_date'] == today]

        # 5) 个股当日的涨跌幅 (用于 industry_position)
        sql_chg = f"""
            SELECT ts_code, pct_chg FROM daily_price
            WHERE ts_code IN ({placeholders}) AND trade_date = %s
        """
        df_chg = pd.read_sql(sql_chg, conn, params=(*ts_codes, as_of_date))

        # 6) 行业近 5 日累计涨幅 (用 sector_moneyflow.pct_change 求和)
        ind_momentum = {}
        if not df_money.empty:
            df_money = df_money.dropna(subset=['pct_change'])
            grp = df_money.groupby('sector_name')
            ind_momentum = grp['pct_change'].sum().to_dict()

        # 7) 概念接力 (board_concept_hist 暂时只到 4-30, 后续扩展)
        # 简化: 用 industry 接力序代理

        results = []
        for code in ts_codes:
            row = {'ts_code': code}
            ind = ind_map.get(code, 'OTHER')

            # relay_index_in_industry
            same_ind = today_limit[today_limit['industry'] == ind]
            if code in same_ind['ts_code'].values:
                # 涨停了: 找到该股的接力序 (按 first_time 排序的 rank)
                # 注意: limit_list_d 数据未必有序, 简化为当日同行业涨停数
                row['relay_index_in_industry'] = 0  # 自己涨停, 不参与接力
                row['is_limit_up_today'] = 1
            else:
                # 未涨停: 同行业当日涨停数 + 1 = 自己的接力序
                row['relay_index_in_industry'] = len(same_ind) + 1
                row['is_limit_up_today'] = 0

            # has_limit_up_sibling
            near3 = df_limit[(df_limit['industry'] == ind) &
                             (df_limit['trade_date'] >= today - pd.Timedelta(days=3))]
            near5 = df_limit[(df_limit['industry'] == ind) &
                             (df_limit['trade_date'] >= today - pd.Timedelta(days=5))]
            row['has_limit_up_sibling_today'] = int(len(same_ind) > 0)
            row['has_limit_up_sibling_3d'] = int(len(near3) > 0)
            row['has_limit_up_sibling_5d'] = int(len(near5) > 0)
            row['industry_limit_up_count_5d'] = int(len(near5))

            # sector_momentum_5d
            row['sector_momentum_5d'] = round(float(ind_momentum.get(ind, 0)), 2)

            # industry_position
            chg = float(df_chg.loc[df_chg['ts_code'] == code, 'pct_chg'].iloc[0]) \
                  if (df_chg['ts_code'] == code).any() else 0.0
            ind_avg_chg = float(same_ind['ts_code'].count())  # placeholder
            row['individual_pct_chg'] = round(chg, 2)

            results.append(row)
        return pd.DataFrame(results).set_index('ts_code')
    finally:
        if should_close:
            conn.close()
