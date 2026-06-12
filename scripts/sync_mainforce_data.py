#!/usr/bin/env python3
"""
主力资金数据同步脚本
从 Tushare API 拉取历史资金流向、板块资金流向、股东变化、龙虎榜数据并入库
"""
import logging
import os
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()
import pymysql
import tushare as ts

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

# ========== 配置 ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
DB_CONFIG = {
    'host': 'localhost',
    'unix_socket': '/tmp/mysql.sock',
    'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db',
    'connect_timeout': 10,
    'charset': 'utf8mb4',
}

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def safe_float(v, default=0.0):
    """NaN 安全浮点转换：处理 pandas NaN / None / 各类异常值"""
    try:
        if v is None:
            return default
        if isinstance(v, float):
            return default if (v != v) else v  # v != v 检测 NaN
        return float(v)
    except (ValueError, TypeError):
        return default

def get_trade_cal(start_date, end_date):
    """获取交易日历"""
    try:
        df = pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date, is_open='1')
        if df is not None and len(df) > 0:
            return sorted(df['cal_date'].tolist())
    except Exception as e:
        logger.warning(f"获取交易日历失败: {e}")
    return []

def batch_insert(conn, sql, rows, batch_size=500):
    """批量插入"""
    cursor = conn.cursor()
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        cursor.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total

def sync_moneyflow_daily():
    """同步个股资金流向（从2025-01-01至今）"""
    logger.info("=== 开始同步个股资金流向 ===")
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # 获取交易日历
    start_date = "20250101"
    end_date = datetime.now().strftime("%Y%m%d")
    trade_dates = get_trade_cal(start_date, end_date)
    logger.info(f"交易日数量: {len(trade_dates)} ({start_date} ~ {end_date})")

    total_inserted = 0
    for i, td in enumerate(trade_dates):
        try:
            df = pro.moneyflow(trade_date=td)
            if df is None or len(df) == 0:
                continue

            rows = []
            for _, row in df.iterrows():
                # main_net = 大单净入 + 特大单净入
                buy_lg = float(row.get('buy_lg_amount', 0) or 0)
                sell_lg = float(row.get('sell_lg_amount', 0) or 0)
                buy_elg = float(row.get('buy_elg_amount', 0) or 0)
                sell_elg = float(row.get('sell_elg_amount', 0) or 0)
                main_net = (buy_lg - sell_lg) + (buy_elg - sell_elg)

                rows.append((
                    row.get('ts_code', ''),
                    td,
                    float(row.get('buy_sm_amount', 0) or 0),
                    float(row.get('sell_sm_amount', 0) or 0),
                    float(row.get('buy_md_amount', 0) or 0),
                    float(row.get('sell_md_amount', 0) or 0),
                    buy_lg,
                    sell_lg,
                    buy_elg,
                    sell_elg,
                    float(row.get('net_mf_amount', 0) or 0),
                    main_net,
                ))

            sql = """INSERT IGNORE INTO moneyflow_daily 
                (ts_code, trade_date, buy_sm_amount, sell_sm_amount, buy_md_amount, sell_md_amount,
                 buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount, net_mf_amount, main_net)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""

            count = batch_insert(conn, sql, rows)
            total_inserted += count

            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_dates)} 日期, 累计插入 {total_inserted} 条")
                time.sleep(0.2)

        except Exception as e:
            logger.warning(f"  资金流向同步失败 {td}: {e}")
            time.sleep(0.3)

    conn.close()
    logger.info(f"=== 个股资金流向同步完成，共插入 {total_inserted} 条 ===")
    return total_inserted

def sync_sector_moneyflow():
    """同步板块资金流向"""
    logger.info("=== 开始同步板块资金流向 ===")
    conn = pymysql.connect(**DB_CONFIG)

    start_date = "20250101"
    end_date = datetime.now().strftime("%Y%m%d")
    trade_dates = get_trade_cal(start_date, end_date)
    logger.info(f"交易日数量: {len(trade_dates)}")

    total_inserted = 0
    for i, td in enumerate(trade_dates):
        try:
            df = pro.moneyflow_ind_ths(trade_date=td)
            if df is None or len(df) == 0:
                continue

            rows = []
            for _, row in df.iterrows():
                rows.append((
                    td,
                    str(row.get('industry', '') or row.get('name', '')),
                    float(row.get('net_amount', 0) or 0),
                    float(row.get('buy_elg_amount', 0) or 0),
                    float(row.get('sell_elg_amount', 0) or 0),
                    float(row.get('pct_change', 0) or 0),
                ))

            sql = """INSERT IGNORE INTO sector_moneyflow 
                (trade_date, sector_name, net_amount, buy_elg_amount, sell_elg_amount, pct_change)
                VALUES (%s, %s, %s, %s, %s, %s)"""

            count = batch_insert(conn, sql, rows)
            total_inserted += count

            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_dates)} 日期, 累计插入 {total_inserted} 条")
                time.sleep(0.2)

        except Exception as e:
            logger.warning(f"  板块资金流向同步失败 {td}: {e}")
            time.sleep(0.3)

    conn.close()
    logger.info(f"=== 板块资金流向同步完成，共插入 {total_inserted} 条 ===")
    return total_inserted

def sync_holder_change():
    """
    同步股东变化（全量）

    Tushare stk_holdernumber 只返回 holder_num（股东人数），
    不返回 holder_num_change 和 holder_change_pct。
    本函数按 end_date 排序后自行计算变化量和变化率。
    """
    logger.info("=== 开始同步股东变化 ===")
    conn = pymysql.connect(**DB_CONFIG)

    # 先获取所有股票列表
    basic_df = pro.stock_basic(exchange="", list_status="L", fields="ts_code")
    codes = basic_df['ts_code'].tolist()
    logger.info(f"股票总数: {len(codes)}")

    total_inserted = 0
    for i, ts_code in enumerate(codes):
        try:
            # 只请求实际存在的字段：ts_code, end_date, holder_num
            df = pro.stk_holdernumber(ts_code=ts_code)
            if df is None or len(df) == 0:
                continue

            # 按 end_date 排序，计算变化量和变化率
            df = df.sort_values('end_date').reset_index(drop=True)

            rows = []
            prev_holder_num = None
            for _, row in df.iterrows():
                end_date_val = row.get('end_date', '')
                if not end_date_val:
                    continue
                # Convert to date format
                end_date_str = str(end_date_val)
                if len(end_date_str) == 8:
                    end_date_str = f"{end_date_str[:4]}-{end_date_str[4:6]}-{end_date_str[6:8]}"

                import math
                hn = row.get('holder_num', 0) or 0
                holder_num = int(hn) if not (isinstance(hn, float) and math.isnan(hn)) else 0

                # 自行计算变化量和变化率
                if prev_holder_num is not None and prev_holder_num > 0:
                    holder_change = holder_num - prev_holder_num
                    change_pct = round((holder_num - prev_holder_num) / prev_holder_num * 100, 2)
                else:
                    holder_change = 0
                    change_pct = 0.0

                rows.append((ts_code, end_date_str, holder_num, holder_change, change_pct))
                prev_holder_num = holder_num

            sql = """INSERT IGNORE INTO holder_change
                (ts_code, end_date, holder_num, holder_num_change, holder_change_pct)
                VALUES (%s, %s, %s, %s, %s)"""

            count = batch_insert(conn, sql, rows)
            total_inserted += count

            if (i + 1) % 100 == 0:
                logger.info(f"  进度: {i+1}/{len(codes)} 股票, 累计插入 {total_inserted} 条")
                time.sleep(0.5)

        except Exception as e:
            logger.warning(f"  股东变化同步失败 {ts_code}: {e}")
            if "频率超限" in str(e) or "rate" in str(e).lower():
                time.sleep(3)
            elif "NaN" in str(e):
                pass  # skip NaN conversion errors
            else:
                time.sleep(0.15)  # default: ~400 calls/min, under 500 limit

    conn.close()
    logger.info(f"=== 股东变化同步完成，共插入 {total_inserted} 条 ===")
    return total_inserted

def sync_dragon_tiger():
    """同步龙虎榜（从2025-01-01至今）"""
    logger.info("=== 开始同步龙虎榜 ===")
    conn = pymysql.connect(**DB_CONFIG)

    start_date = "20250101"
    end_date = datetime.now().strftime("%Y%m%d")
    trade_dates = get_trade_cal(start_date, end_date)
    logger.info(f"交易日数量: {len(trade_dates)}")

    total_inserted = 0
    for i, td in enumerate(trade_dates):
        try:
            # top_list 龙虎榜每日数据
            df = pro.top_list(trade_date=td)
            if df is None or len(df) == 0:
                continue

            rows = []
            for _, row in df.iterrows():
                rows.append((
                    row.get('ts_code', ''),
                    td,
                    str(row.get('name', '')),
                    safe_float(row.get('close', 0)),
                    safe_float(row.get('pct_change', 0)),
                    safe_float(row.get('net_amount', 0)),      # 实际字段名: net_amount
                    safe_float(row.get('l_buy', 0)),            # 实际字段名: l_buy
                    safe_float(row.get('l_sell', 0)),           # 实际字段名: l_sell
                    str(row.get('reason', '') or ''),           # 实际字段名: reason（原exalter）
                ))

            if rows:
                sql = """INSERT IGNORE INTO dragon_tiger 
                    (ts_code, trade_date, name, close, pct_change, net_buy, buy, sell, exalter)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                count = batch_insert(conn, sql, rows)
                total_inserted += count

            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_dates)} 日期, 累计插入 {total_inserted} 条")
                time.sleep(0.2)

        except Exception as e:
            logger.warning(f"  龙虎榜同步失败 {td}: {e}")
            time.sleep(0.3)

    conn.close()
    logger.info(f"=== 龙虎榜同步完成，共插入 {total_inserted} 条 ===")
    return total_inserted


def sync_dragon_tiger_inst():
    """
    同步龙虎榜机构席位明细（top_inst）
    从每个交易日拉取龙虎榜上榜股票的买卖前五席位明细
    用于识别机构、深股通等席位的净买入情况
    """
    logger.info("=== 开始同步龙虎榜机构席位明细 ===")
    conn = pymysql.connect(**DB_CONFIG)

    start_date = "20250101"
    end_date = datetime.now().strftime("%Y%m%d")
    trade_dates = get_trade_cal(start_date, end_date)
    logger.info(f"交易日数量: {len(trade_dates)}")

    total_inserted = 0
    for i, td in enumerate(trade_dates):
        try:
            df = pro.top_inst(trade_date=td)
            if df is None or len(df) == 0:
                continue

            rows = []
            for _, row in df.iterrows():
                exalter = str(row.get('exalter', '') or '')
                # 截断超长营业部名称
                if len(exalter) > 100:
                    exalter = exalter[:100]
                rows.append((
                    row.get('ts_code', ''),
                    td,
                    exalter,
                    safe_float(row.get('buy', 0)),
                    safe_float(row.get('sell', 0)),
                    safe_float(row.get('net_buy', 0)),
                    str(row.get('side', '') or ''),
                    str(row.get('reason', '') or ''),
                ))

            if rows:
                sql = """INSERT IGNORE INTO dragon_tiger_inst
                    (ts_code, trade_date, exalter, buy, sell, net_buy, side, reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
                count = batch_insert(conn, sql, rows)
                total_inserted += count

            if (i + 1) % 20 == 0:
                logger.info(f"  进度: {i+1}/{len(trade_dates)} 日期, 累计插入 {total_inserted} 条")
                time.sleep(0.2)

        except Exception as e:
            logger.warning(f"  龙虎榜席位同步失败 {td}: {e}")
            time.sleep(0.3)

    conn.close()
    logger.info(f"=== 龙虎榜机构席位明细同步完成，共插入 {total_inserted} 条 ===")
    return total_inserted


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="同步主力资金数据到MySQL")
    parser.add_argument('--only', choices=['moneyflow', 'sector', 'holder', 'dragon_tiger', 'dragon_tiger_inst'],
                        help='只同步指定类型（不指定则同步全部）')
    args = parser.parse_args()

    start_time = time.time()

    funcs = {
        'moneyflow': sync_moneyflow_daily,
        'sector': sync_sector_moneyflow,
        'holder': sync_holder_change,
        'dragon_tiger': sync_dragon_tiger,
        'dragon_tiger_inst': sync_dragon_tiger_inst,
    }

    results = {}
    if args.only:
        if args.only in funcs:
            results[args.only] = funcs[args.only]()
    else:
        for name, func in funcs.items():
            results[name] = func()

    elapsed = time.time() - start_time
    logger.info(f"\n=== 全部同步完成，耗时 {elapsed/60:.1f} 分钟 ===")
    for k, v in results.items():
        logger.info(f"  {k}: {v} 条")
