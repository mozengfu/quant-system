"""
研报特征 — 卖方覆盖 + 一致预期变化的领先信号

A 股经验:
  - 首次覆盖当周, 主升浪概率 +6~9%
  - 评级上调当日 + 次日, 表现明显强于大盘
  - 研报数量从 0 → ≥3 家 (拐点) 是机构开始关注的信号

特征 (per stock, per day):
  - research_count_5d:   近 5 日研报数量
  - research_count_20d:  近 20 日研报数量
  - research_first_cov:  近 20 日是否首次覆盖 (1/0)
  - research_upgrade:    近 5 日是否有"上调"评级 (基于 report_type + 标题正则)
  - research_inst_div:   覆盖券商数量 (近 20 日 distinct)
  - research_ind_hot:    所属行业研报数 (板块热度)
"""
import logging
import re

import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

UPGRADE_PATTERNS = [r'上调', r'买入', r'增持', r'强推', r'推荐', r'优于大市']


def _is_upgrade(title: str) -> bool:
    if not title: return False
    for p in UPGRADE_PATTERNS:
        if re.search(p, title):
            return True
    return False


def build_research_features(ts_codes: list[str], as_of_date: str, conn=None, lookback_days: int = 30) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    should_close = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        should_close = True
    try:
        placeholders = ','.join(['%s'] * len(ts_codes))
        sql = f"""
            SELECT trade_date, ts_code, report_type, title, inst_csname, ind_name
            FROM research_report
            WHERE trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
              AND ts_code IN ({placeholders})
        """
        df = pd.read_sql(sql, conn, params=(as_of_date, lookback_days, as_of_date, *ts_codes),
                         parse_dates=['trade_date'])

        # 板块研报热度 (近 20 日各行业研报数)
        ind_hot = {}
        if not df.empty and 'ind_name' in df.columns:
            df_ind = df.dropna(subset=['ind_name'])
            near20 = df_ind[df_ind['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=20)]
            ind_hot = near20['ind_name'].value_counts().to_dict()

        results = []
        for code in ts_codes:
            row = {'ts_code': code}
            sub = df[df['ts_code'] == code].sort_values('trade_date', ascending=False)
            if sub.empty:
                row.update({k: 0 for k in ['research_count_5d','research_count_20d','research_first_cov',
                                            'research_upgrade_5d','research_inst_count_20d','research_ind_hot']})
                results.append(row)
                continue
            today = sub[sub['trade_date'] == pd.Timestamp(as_of_date)]
            near5 = sub[sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)]
            near20 = sub[sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=20)]
            row['research_count_5d'] = int(len(near5))
            row['research_count_20d'] = int(len(near20))
            # 首次覆盖近似: 20 日内第 1 篇且 5 日内无
            row['research_first_cov'] = int(len(near20) > 0 and len(near5) == 0 and len(near20) <= 2)
            # 上调评级 (5 日内)
            row['research_upgrade_5d'] = int(near5['title'].fillna('').apply(_is_upgrade).any())
            # 覆盖券商数 (20 日)
            row['research_inst_count_20d'] = int(near20['inst_csname'].nunique())
            # 行业热度: 找 ind_name
            ind_name = sub.dropna(subset=['ind_name'])['ind_name']
            if not ind_name.empty:
                row['research_ind_hot'] = int(ind_hot.get(ind_name.iloc[0], 0))
            else:
                row['research_ind_hot'] = 0
            results.append(row)
        return pd.DataFrame(results).set_index('ts_code')
    finally:
        if should_close:
            conn.close()
