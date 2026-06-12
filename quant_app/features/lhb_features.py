"""
龙虎榜特征 (Lóng Hǔ Bǎng — 公开上榜数据)

A 股特色信号: 游资/机构席位净买入是主升浪最直接的领先指标之一
本研究使用 2 类信号:
  1. 个股级: top_list 当日上榜 + net_amount / amount 占比
  2. 席位级: top_inst 中"游资席位" vs "机构专用" 的区分
     - 游资 (side='0' 假定为非机构, 含拉萨/宁波/上海溧阳等)
     - 机构 (side='1' 假定为机构, 含"机构专用"/"沪股通专用"/知名公募)

核心特征 (per stock, per day):
  - lhb_net_amt_1d:  当日龙虎榜净买入额 (元)
  - lhb_net_pct_1d:  净买入占个股成交比
  - lhb_buy_ratio:   龙虎榜买入额 / 个股成交额
  - lhb_inst_net_1d: 机构席位净买入
  - lhb_hot_seat_net_1d: 知名游资席位净买入 (e.g. 拉萨系/宁波系)
  - lhb_in_top_list_3d: 近 3 日是否上榜 (滚动)
  - lhb_in_top_list_5d: 近 5 日是否上榜
  - lhb_repurchase:   近 5 日上榜后是否再次买入 (回补信号)
"""
import logging

import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

# 知名游资席位白名单 (top_inst.exalter 部分匹配)
HOT_SEATS = [
    '东方财富证券拉萨',  # 拉萨系
    '东方证券绍兴',     # 绍兴系
    '宁波桑田路',       # 宁波系
    '华鑫证券',         # 华鑫系
    '中泰证券上海',     # 中泰上海
    '财通证券杭州',     # 财通杭州
    '国泰君安南京',     # 国君南京
    '招商证券深圳',     # 招商深圳
    '华泰证券深圳',     # 华泰深圳
]


def _is_hot_seat(name: str) -> bool:
    return any(s in (name or '') for s in HOT_SEATS)


def _is_inst_seat(side: str, name: str) -> bool:
    if side == '1':
        return True
    if '机构专用' in (name or ''):
        return True
    if '沪股通专用' in (name or '') or '深股通专用' in (name or ''):
        return True
    if '基金专用' in (name or ''):
        return True
    return False


def build_lhb_features(ts_codes: list[str], as_of_date: str, conn=None, lookback_days: int = 30) -> pd.DataFrame:
    """
    计算 (ts_code, as_of_date) 截止日的龙虎榜特征

    Args:
        ts_codes: 候选股票列表
        as_of_date: 信号日 (T)
        lookback_days: 回看窗口

    Returns:
        DataFrame: index=ts_code, columns=lhb_*
    """
    if not ts_codes:
        return pd.DataFrame()

    should_close = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        should_close = True

    try:
        placeholders = ','.join(['%s'] * len(ts_codes))
        # top_list: 上榜记录
        sql_top = f"""
            SELECT trade_date, ts_code, net_amount, l_buy, l_amount, amount
            FROM top_list
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
        """
        df_top = pd.read_sql(sql_top, conn, params=(*ts_codes, as_of_date, lookback_days, as_of_date),
                             parse_dates=['trade_date'])
        # top_inst: 席位级别
        sql_inst = f"""
            SELECT trade_date, ts_code, exalter, net_buy, side
            FROM top_inst
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
        """
        df_inst = pd.read_sql(sql_inst, conn, params=(*ts_codes, as_of_date, lookback_days, as_of_date),
                              parse_dates=['trade_date'])

        results = []
        for code in ts_codes:
            row = {'ts_code': code}
            # 个股级
            sub = df_top[df_top['ts_code'] == code].sort_values('trade_date', ascending=False)
            if not sub.empty:
                today = sub[sub['trade_date'] == pd.Timestamp(as_of_date)]
                row['lhb_in_top_list_1d'] = int(len(today) > 0)
                row['lhb_in_top_list_3d'] = int((sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=3)).any())
                row['lhb_in_top_list_5d'] = int((sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)).any())
                row['lhb_net_amt_1d'] = float(today['net_amount'].iloc[0]) if len(today) else 0
                row['lhb_buy_ratio_1d'] = (float(today['l_buy'].iloc[0]) / float(today['amount'].iloc[0])
                                            if len(today) and today['amount'].iloc[0] > 0 else 0)
                # 累计近 5 日上榜净买入
                near5 = sub[sub['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)]
                row['lhb_net_amt_5d'] = float(near5['net_amount'].sum())
                row['lhb_appear_count_5d'] = int(len(near5))
                # 上榜后回补: 5 日内出现 2 次以上
                row['lhb_repurchase_5d'] = int(len(near5) >= 2)
            else:
                row.update({k: 0 for k in ['lhb_in_top_list_1d','lhb_in_top_list_3d','lhb_in_top_list_5d',
                                            'lhb_net_amt_1d','lhb_buy_ratio_1d','lhb_net_amt_5d',
                                            'lhb_appear_count_5d','lhb_repurchase_5d']})

            # 席位级
            sub_inst = df_inst[df_inst['ts_code'] == code]
            if not sub_inst.empty:
                today_inst = sub_inst[sub_inst['trade_date'] == pd.Timestamp(as_of_date)]
                if not today_inst.empty:
                    inst_mask = today_inst.apply(lambda r: _is_inst_seat(r['side'], r['exalter']), axis=1)
                    hot_mask = today_inst['exalter'].apply(_is_hot_seat)
                    row['lhb_inst_net_1d'] = float(today_inst.loc[inst_mask, 'net_buy'].sum())
                    row['lhb_hot_seat_net_1d'] = float(today_inst.loc[hot_mask, 'net_buy'].sum())
                else:
                    row['lhb_inst_net_1d'] = 0
                    row['lhb_hot_seat_net_1d'] = 0
                # 5 日累计
                near5_inst = sub_inst[sub_inst['trade_date'] >= pd.Timestamp(as_of_date) - pd.Timedelta(days=5)]
                inst_net_5d = 0.0
                hot_net_5d = 0.0
                for _, r in near5_inst.iterrows():
                    if _is_inst_seat(r['side'], r['exalter']):
                        inst_net_5d += float(r['net_buy'] or 0)
                    if _is_hot_seat(r['exalter']):
                        hot_net_5d += float(r['net_buy'] or 0)
                row['lhb_inst_net_5d'] = inst_net_5d
                row['lhb_hot_seat_net_5d'] = hot_net_5d
            else:
                row['lhb_inst_net_1d'] = 0
                row['lhb_hot_seat_net_1d'] = 0
                row['lhb_inst_net_5d'] = 0
                row['lhb_hot_seat_net_5d'] = 0
            results.append(row)
        return pd.DataFrame(results).set_index('ts_code')
    finally:
        if should_close:
            conn.close()
