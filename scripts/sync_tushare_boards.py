#!/usr/bin/env python3
"""
Tushare 板块数据同步脚本
替代被东财封禁的 AKShare 板块接口

数据源:
- 概念板块: Tushare ths_index(type='N') — 同花顺概念
- 行业板块: Tushare index_classify(L1) — 申万一级行业（31个）
- 成分股: ths_member / index_member
- 日线: ths_daily (概念), 成分股聚合 (行业)
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 加载 .env 文件
env_path = Path(__file__).parent.parent / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(Path(__file__).parent.parent))
import pymysql
import tushare as ts

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', force=True)
logger = logging.getLogger(__name__)

# 确保 stdout 无缓冲
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)


def get_pro():
    """获取 Tushare Pro API"""
    token = os.environ.get('TUSHARE_TOKEN', '')
    if not token:
        raise ValueError("TUSHARE_TOKEN 未设置")
    ts.set_token(token)
    return ts.pro_api()


def get_db():
    from quant_app.utils.config import get_db_config
    return pymysql.connect(**get_db_config())


def safe_float(v):
    """安全转浮点数"""
    import math
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        if math.isnan(v) or math.isinf(v):
            return 0
        return float(v)
    s = str(v).strip()
    if not s or s in ('N/A', '-', 'nan', 'NaN', 'NA', 'None'):
        return 0
    try:
        s = s.replace('%', '').replace('亿', '').replace('万', '').replace(',', '')
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return 0
        return f
    except (ValueError, TypeError):
        return 0


# ==================== 概念板块 ====================

def sync_concept_boards():
    """同步同花顺概念板块列表"""
    logger.info("同步概念板块列表 (Tushare ths_index)")
    pro = get_pro()

    df = pro.ths_index(exchange='A', type='N')
    if df is None or df.empty:
        logger.error("ths_index 返回空")
        return False

    # 过滤宽基指数（样本股/成份股类）
    df = df[~df['name'].str.contains('样本股|成份股|成分股|指数')].copy()
    logger.info(f"  概念板块: {len(df)} 个")

    # 确保 is_latest 列存在
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE quant_db.board_concept ADD COLUMN is_latest BOOLEAN DEFAULT TRUE")
    except Exception:
        pass
    conn.commit()
    cursor.close()
    conn.close()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE quant_db.board_concept SET is_latest = FALSE")

    for _, row in df.iterrows():
        code = str(row.get('ts_code', ''))
        name = str(row.get('name', ''))
        if not code:
            continue

        sql = """INSERT INTO quant_db.board_concept
            (board_code, board_name, latest_price, change_pct, total_mv,
             turnover_rate, up_count, down_count, lead_stock, lead_stock_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            board_name=VALUES(board_name), is_latest=TRUE"""
        cursor.execute(sql, (
            code, name, 0, 0, 0, 0, 0, 0, '', 0
        ))

    conn.commit()
    cursor.close()
    conn.close()
    logger.info(f"  概念板块同步完成: {len(df)} 个")
    return True


def sync_concept_cons():
    """同步概念板块成分股"""
    logger.info("同步概念板块成分股 (Tushare ths_member)")
    pro = get_pro()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT board_code, board_name FROM quant_db.board_concept WHERE is_latest = TRUE")
    boards = cursor.fetchall()
    cursor.close()
    conn.close()

    total = 0
    failed = 0
    for i, (code, name) in enumerate(boards):
        try:
            df = pro.ths_member(ts_code=code)
            if df is None or df.empty:
                continue

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE quant_db.board_concept_cons SET is_latest = FALSE WHERE board_code = %s",
                (code,)
            )

            for _, row in df.iterrows():
                ts_code = str(row.get('con_code', ''))
                if not ts_code:
                    continue

                sql = """INSERT INTO quant_db.board_concept_cons
                    (board_code, ts_code, stock_name, latest_price, pct_change, is_latest)
                    VALUES (%s,%s,%s,%s,%s,TRUE)
                    ON DUPLICATE KEY UPDATE
                    stock_name=VALUES(stock_name), is_latest=TRUE"""
                cursor.execute(sql, (
                    code, ts_code, str(row.get('con_name', '')), 0, 0
                ))

            conn.commit()
            cursor.close()
            conn.close()
            total += len(df)

            if (i + 1) % 50 == 0:
                logger.info(f"  概念成分股: {i+1}/{len(boards)} boards, {total} stocks")

            time.sleep(0.3)  # Tushare 限频

        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning(f"  {code} {name} 成分股同步失败: {e}")
            elif failed == 4:
                logger.warning("  ... (后续失败不再逐个打印)")

    logger.info(f"  概念成分股完成: {total} stocks, {failed} failed")


def sync_concept_hist(days=120):
    """同步概念板块历史行情（增量）"""
    logger.info("同步概念板块历史行情 (Tushare ths_daily)")
    pro = get_pro()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT board_code, board_name FROM quant_db.board_concept WHERE is_latest = TRUE")
    boards = cursor.fetchall()

    cursor.execute("SELECT MAX(trade_date) FROM quant_db.board_concept_hist")
    latest = cursor.fetchone()[0]
    if latest:
        start_date = (latest + timedelta(days=1)).strftime('%Y%m%d')
        logger.info(f"  增量同步: 从 {latest} 开始")
    else:
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        logger.info(f"  全量同步: 从 {start_date} 开始")

    end_date = datetime.now().strftime('%Y%m%d')
    cursor.close()
    conn.close()

    total = 0
    failed = 0
    for i, (code, name) in enumerate(boards):
        try:
            df = pro.ths_daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                continue

            conn = get_db()
            cursor = conn.cursor()

            for _, row in df.iterrows():
                trade_date = str(row.get('trade_date', ''))
                if len(trade_date) == 8:
                    trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

                sql = """INSERT INTO quant_db.board_concept_hist
                    (trade_date, board_code, board_name, open, close, high, low,
                     pct_change, change_amount, volume, amount, turnover_rate)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                    board_name=VALUES(board_name), open=VALUES(open), close=VALUES(close),
                    high=VALUES(high), low=VALUES(low), pct_change=VALUES(pct_change),
                    change_amount=VALUES(change_amount), volume=VALUES(volume),
                    amount=VALUES(amount), turnover_rate=VALUES(turnover_rate)"""
                cursor.execute(sql, (
                    trade_date, code, name,
                    safe_float(row.get('open', 0)), safe_float(row.get('close', 0)),
                    safe_float(row.get('high', 0)), safe_float(row.get('low', 0)),
                    safe_float(row.get('pct_change', 0)), safe_float(row.get('change', 0)),
                    safe_float(row.get('vol', 0)),
                    safe_float(row.get('vol', 0)) * safe_float(row.get('avg_price', 0)) / 100,  # amount(万元)=vol(手)*100*avg_price/10000
                    safe_float(row.get('turnover_rate', 0))
                ))

            conn.commit()
            cursor.close()
            conn.close()
            total += len(df)

            if (i + 1) % 50 == 0:
                logger.info(f"  概念历史: {i+1}/{len(boards)} boards, {total} rows")

            time.sleep(0.5) # Tushare限频安全值 (0.3s会卡200/min限频)

        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning(f"  {code} {name} 历史同步失败: {e}")
            elif failed == 4:
                logger.warning("  ... (后续失败不再逐个打印)")

    logger.info(f"  概念历史行情完成: {total} rows, {failed} failed")


# ==================== 行业板块 ====================

def sync_industry_boards():
    """同步申万一级行业板块列表 (31个)"""
    logger.info("同步行业板块列表 (Tushare index_classify SW L1)")
    pro = get_pro()

    df = pro.index_classify(level='L1', src='SW2021')
    if df is None or df.empty:
        logger.error("index_classify 返回空")
        return False

    logger.info(f"  申万一级行业: {len(df)} 个")

    # 确保 is_latest 列存在
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("ALTER TABLE quant_db.board_industry ADD COLUMN is_latest BOOLEAN DEFAULT TRUE")
    except Exception:
        pass
    conn.commit()
    cursor.close()
    conn.close()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE quant_db.board_industry SET is_latest = FALSE")

    for _, row in df.iterrows():
        code = str(row.get('index_code', ''))
        name = str(row.get('industry_name', ''))
        if not code:
            continue

        sql = """INSERT INTO quant_db.board_industry
            (board_code, board_name, latest_price, change_pct, total_mv,
             turnover_rate, up_count, down_count, lead_stock, lead_stock_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            board_name=VALUES(board_name), is_latest=TRUE"""
        cursor.execute(sql, (
            code, name, 0, 0, 0, 0, 0, 0, '', 0
        ))

    conn.commit()
    cursor.close()
    conn.close()
    logger.info(f"  行业板块同步完成: {len(df)} 个")
    return True


def sync_industry_cons():
    """同步申万行业成分股"""
    logger.info("同步行业板块成分股 (Tushare index_member)")
    pro = get_pro()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT board_code, board_name FROM quant_db.board_industry WHERE is_latest = TRUE")
    boards = cursor.fetchall()
    cursor.close()
    conn.close()

    total = 0
    failed = 0
    for i, (code, name) in enumerate(boards):
        try:
            df = pro.index_member(id=code)
            if df is None or df.empty:
                continue

            # 只保留当前成分股 (out_date 为 None)
            df = df[df['out_date'].isna()].groupby('con_code', as_index=False).last()

            if df.empty:
                continue

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE quant_db.board_industry_cons SET is_latest = FALSE WHERE board_code = %s",
                (code,)
            )

            for _, row in df.iterrows():
                ts_code = str(row.get('con_code', ''))
                if not ts_code:
                    continue

                sql = """INSERT INTO quant_db.board_industry_cons
                    (board_code, ts_code, stock_name, latest_price, pct_change, is_latest)
                    VALUES (%s,%s,%s,%s,%s,TRUE)
                    ON DUPLICATE KEY UPDATE
                    stock_name=VALUES(stock_name), is_latest=TRUE"""
                cursor.execute(sql, (
                    code, ts_code, '', 0, 0
                ))

            conn.commit()
            cursor.close()
            conn.close()
            total += len(df)

            if (i + 1) % 10 == 0:
                logger.info(f"  行业成分股: {i+1}/{len(boards)} boards, {total} stocks")

            time.sleep(0.5) # Tushare限频安全值 (0.3s会卡200/min限频)

        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning(f"  {code} {name} 成分股同步失败: {e}")
            elif failed == 4:
                logger.warning("  ... (后续失败不再逐个打印)")

    logger.info(f"  行业成分股完成: {total} stocks, {failed} failed")


# ==================== 主流程 ====================

def main():
    logger.info("=" * 60)
    logger.info("Tushare 板块数据同步开始")
    logger.info("=" * 60)

    start_time = time.time()

    # 1. 概念板块列表
    logger.info("\n[Step 1] 同步概念板块列表")
    concept_ok = sync_concept_boards()

    # 2. 行业板块列表
    logger.info("\n[Step 2] 同步行业板块列表")
    industry_ok = sync_industry_boards()

    # 3. 概念板块成分股
    if concept_ok:
        logger.info("\n[Step 3] 同步概念板块成分股")
        sync_concept_cons()
    else:
        logger.warning("\n[Step 3] 跳过概念成分股（概念列表未同步）")

    # 4. 行业板块成分股
    if industry_ok:
        logger.info("\n[Step 4] 同步行业板块成分股")
        sync_industry_cons()
    else:
        logger.warning("\n[Step 4] 跳过行业成分股（行业列表未同步）")

    # 5. 概念板块历史行情
    if concept_ok:
        logger.info("\n[Step 5] 同步概念板块历史行情")
        sync_concept_hist(days=120)
    else:
        logger.warning("\n[Step 5] 跳过概念历史（概念列表未同步）")

    # 注意: 申万行业日线 (index_daily) 积分不够返回空
    # 行业历史行情需要通过成分股日线聚合计算，此处暂不实现
    logger.info("\n[Step 6] 跳过行业历史行情（Tushare index_daily 需要更高积分）")

    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}")
    logger.info(f"同步完成，耗时 {elapsed/60:.1f} 分钟")
    logger.info(f"{'='*60}")


def _topup_only():
    """只补全历史行情落后的板块（凌晨被限频截断的子集）"""
    logger.info("=== _topup_only模式 ===")
    pro = get_pro()
    conn = get_db()
    cursor = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute("""
        SELECT b.board_code, b.board_name, MAX(h.trade_date) AS latest_date
        FROM quant_db.board_concept b
        LEFT JOIN quant_db.board_concept_hist h ON b.board_code = h.board_code
        WHERE b.is_latest = TRUE
        GROUP BY b.board_code, b.board_name
        HAVING MAX(h.trade_date) < %s OR MAX(h.trade_date) IS NULL
        ORDER BY MAX(h.trade_date), b.board_code
    """, (today,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    logger.info(f"落后板块: {len(rows)} 个")
    boards = rows
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(trade_date) FROM quant_db.board_concept_hist")
    latest_all = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    if latest_all:
        start_date = (latest_all + timedelta(days=1)).strftime("%Y%m%d")
    else:
        start_date = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
    logger.info(f"start_date={start_date}, end_date={end_date}")
    total =0
    failed =0
    for i, (code, name, latest) in enumerate(boards):
        try:
            df = pro.ths_daily(ts_code=code, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                continue
            conn = get_db()
            cur = conn.cursor()
            rows_added =0
            for _, row in df.iterrows():
                td = str(row.get("trade_date", ""))
                if len(td) ==8:
                    td = f"{td[:4]}-{td[4:6]}-{td[6:]}"
                sql = """INSERT INTO quant_db.board_concept_hist
                 (trade_date, board_code, board_name, open, close, high, low,
                 pct_change, change_amount, volume, amount, turnover_rate)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE
                 board_name=VALUES(board_name), open=VALUES(open), close=VALUES(close),
                 high=VALUES(high), low=VALUES(low), pct_change=VALUES(pct_change),
                 change_amount=VALUES(change_amount), volume=VALUES(volume),
                 amount=VALUES(amount), turnover_rate=VALUES(turnover_rate)"""
                cur.execute(sql, (td, code, name,
                    safe_float(row.get("open",0)), safe_float(row.get("close",0)),
                    safe_float(row.get("high",0)), safe_float(row.get("low",0)),
                    safe_float(row.get("pct_change",0)), safe_float(row.get("change",0)),
                    safe_float(row.get("vol",0)),
                    safe_float(row.get("vol",0)) * safe_float(row.get("avg_price",0)) /100,
                    safe_float(row.get("turnover_rate",0))))
                rows_added +=1
            conn.commit()
            cur.close()
            conn.close()
            total += rows_added
            logger.info(f" [{i+1}/{len(boards)}] {code} {name}: +{rows_added} rows (was latest={latest})")
            time.sleep(0.5)
        except Exception as e:
            failed +=1
            logger.warning(f" [{i+1}/{len(boards)}] {code} {name}: FAIL {e}")
            time.sleep(1.0)
    logger.info(f"=== _topup_only 完成: {total} rows, {failed} failed ===")
if __name__ == '__main__':
    import sys
    if len(sys.argv) >1 and sys.argv[1] == "--topup":
        _topup_only()
    else:
        main()
