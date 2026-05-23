#!/usr/bin/env python3
"""
检查 Tushare API 实际返回字段名
打印 top_list（龙虎榜）和 stk_holdernumber（股东人数）的完整返回列名和样例值
"""
import os, json
from dotenv import load_dotenv
load_dotenv()

import tushare as ts
import pandas as pd

ts.set_token(os.environ.get("TUSHARE_TOKEN", ""))
pro = ts.pro_api()

def print_df_info(df, title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"  行数: {len(df)}")
    print(f"  列数: {len(df.columns)}")
    print(f"  列名列表: {list(df.columns)}")
    print(f"{'='*70}")

    # 打印每列的非空值和样例
    for col in df.columns:
        non_null = df[col].notna().sum()
        sample = df[col].iloc[0] if non_null > 0 else "ALL_NULL"
        dtype = df[col].dtype

        # 如果是数字列且非空值 > 0，检测是否全为 0
        if pd.api.types.is_numeric_dtype(df[col]):
            if non_null > 0:
                all_zero = (df[col].fillna(0) == 0).all()
                zero_note = " [全为 0]" if all_zero else ""
                print(f"  {col:25s} | {str(dtype):12s} | 非空 {non_null}/{len(df)} | 样例: {sample}{zero_note}")
            else:
                print(f"  {col:25s} | {str(dtype):12s} | ALL_NULL")
        else:
            print(f"  {col:25s} | {str(dtype):12s} | 非空 {non_null}/{len(df)} | 样例: {sample}"[:130])


def check_top_list():
    """检查龙虎榜 top_list 的字段"""
    print("\n\n>>> 龙虎榜 top_list")

    # 尝试几个有数据的交易日
    test_dates = ["20260505", "20260430", "20260429", "20260428", "20260425", "20250430", "20250331"]
    for td in test_dates:
        try:
            df = pro.top_list(trade_date=td)
            if df is not None and len(df) > 0:
                print_df_info(df, f"交易日 {td} ({len(df)} 条)")
                return df
        except Exception as e:
            print(f"  {td}: {e}")
    return None


def check_stk_holdernumber():
    """检查股东人数 stk_holdernumber 的字段"""
    print("\n\n>>> 股东人数 stk_holdernumber")

    # 用不同参数调用看差异
    test_codes = ["000001.SZ", "000002.SZ", "600519.SH"]

    for code in test_codes:
        print(f"\n--- 股票 {code} ---")

        # 1. 不指定 fields，看默认返回
        try:
            df1 = pro.stk_holdernumber(ts_code=code)
            if df1 is not None and len(df1) > 0:
                print_df_info(df1, f"{code} 默认 fields ({len(df1)} 条)")
        except Exception as e:
            print(f"  默认 fields 请求失败: {e}")

        # 2. 指定字段
        try:
            df2 = pro.stk_holdernumber(ts_code=code, fields="ts_code,end_date,holder_num,holder_num_change,holder_change_pct")
            if df2 is not None and len(df2) > 0:
                print_df_info(df2, f"{code} 指定 fields ({len(df2)} 条)")
        except Exception as e:
            print(f"  指定 fields 请求失败: {e}")

        break  # 只查一个股票就够了


def check_daily_holdernumber_sample():
    """检查 stk_holdernumber 的股东人数变化率字段名"""
    print("\n\n>>> 股东人数变化率字段探测")

    # Tushare 文档可能的字段名
    field_sets = [
        ["ts_code", "end_date", "holder_num", "holder_num_change", "change_pct"],
        ["ts_code", "end_date", "holder_num", "holder_num_change", "fchg"],
        ["ts_code", "end_date", "holder_num", "holder_num_change", "holder_change"],
    ]

    for fields in field_sets:
        try:
            df = pro.stk_holdernumber(ts_code="000001.SZ", fields=",".join(fields))
            if df is not None and len(df) > 0:
                print_df_info(df, f"Fields={fields}")
        except Exception as e:
            print(f"  Fields={fields}: {e}")


def query_mysql_check():
    """检查数据库中已有的数据"""
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        config = get_db_config()
        conn = pymysql.connect(**config)
        cur = conn.cursor()

        # 检查 dragon_tiger
        cur.execute("SELECT ts_code, trade_date, net_buy, buy, sell FROM dragon_tiger LIMIT 5")
        rows = cur.fetchall()
        print(f"\n\n>>> dragon_tiger 样例数据:")
        print(f"  列: ts_code, trade_date, net_buy, buy, sell")
        for r in rows:
            print(f"  {r[0]} {r[1]} net_buy={r[2]} buy={r[3]} sell={r[4]}")
        cur.execute("SELECT COUNT(*), SUM(net_buy), SUM(buy), SUM(sell) FROM dragon_tiger")
        cnt, net, buy, sell = cur.fetchone()
        print(f"  总计: {cnt} 条, SUM(net_buy)={net}, SUM(buy)={buy}, SUM(sell)={sell}")

        # 检查 holder_change
        cur.execute("SELECT ts_code, end_date, holder_num, holder_num_change, holder_change_pct FROM holder_change LIMIT 5")
        rows = cur.fetchall()
        print(f"\n>>> holder_change 样例数据:")
        print(f"  列: ts_code, end_date, holder_num, holder_num_change, holder_change_pct")
        for r in rows:
            print(f"  {r[0]} {r[1]} holder_num={r[2]} change={r[3]} pct={r[4]}")
        cur.execute("SELECT COUNT(*), SUM(holder_num_change), SUM(holder_change_pct) FROM holder_change")
        cnt, change, pct = cur.fetchone()
        print(f"  总计: {cnt} 条, SUM(holder_num_change)={change}, SUM(holder_change_pct)={pct}")

        conn.close()
    except Exception as e:
        print(f"\nMySQL 查询失败: {e}")


if __name__ == "__main__":
    check_top_list()
    check_stk_holdernumber()
    check_daily_holdernumber_sample()
    query_mysql_check()
