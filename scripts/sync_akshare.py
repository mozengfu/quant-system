#!/usr/bin/env python3
"""
AKShare 数据补充同步脚本
从 akshare 拉取 Tushare 缺失的数据，写入 quant_db

缺失数据清单：
  P0: 涨停板池、概念/行业板块历史行情
  P1: 板块成分股映射、北向资金持仓个股排行
  P2: 业绩报表、大宗交易
"""
import sys
import os
import time
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import akshare as ak
import pandas as pd
import pymysql
import warnings
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def ak_retry(func, *args, max_retries=5, delay=5, **kwargs):
    """带指数退避重试的 akshare 调用包装器
    默认 5 次重试，延迟 5/10/20/40/80 秒指数退避，应对东财频率限制
    """
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                ConnectionResetError,
                BrokenPipeError) as e:
            if attempt < max_retries:
                wait = min(delay * (2 ** attempt), 120)  # 最长 120s
                logger.warning(f"  网络异常 ({type(e).__name__}), {wait}s后重试 ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise
        except Exception:
            raise  # 非网络错误直接抛出


def get_db():
    from quant_app.utils.config import get_db_config
    return pymysql.connect(**get_db_config())


def create_tables():
    """创建缺失的数据表"""
    conn = get_db()
    cursor = conn.cursor()
    
    tables = {
        # P0: 涨停板池
        'zt_pool': """
            CREATE TABLE IF NOT EXISTS quant_db.zt_pool (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                close DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                turnover_rate DECIMAL(10,2),
                volume_ratio DECIMAL(10,2),
                amount DECIMAL(15,2),
                circ_mv DECIMAL(15,2),
                total_mv DECIMAL(20,2),
                seal_amount DECIMAL(15,2),
                seal_ratio DECIMAL(10,4),
                open_count INT,
                last_board INT DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_code (trade_date, ts_code),
                INDEX idx_date (trade_date),
                INDEX idx_code (ts_code),
                INDEX idx_board (last_board)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P0: 概念板块列表
        'board_concept': """
            CREATE TABLE IF NOT EXISTS quant_db.board_concept (
                id INT AUTO_INCREMENT PRIMARY KEY,
                board_code VARCHAR(20) NOT NULL UNIQUE,
                board_name VARCHAR(50) NOT NULL,
                latest_price DECIMAL(10,2),
                change_pct DECIMAL(10,2),
                total_mv DECIMAL(20,2),
                turnover_rate DECIMAL(10,2),
                up_count INT,
                down_count INT,
                lead_stock VARCHAR(50),
                lead_stock_pct DECIMAL(10,2),
                is_latest BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_name (board_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P0: 概念板块历史行情
        'board_concept_hist': """
            CREATE TABLE IF NOT EXISTS quant_db.board_concept_hist (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                board_code VARCHAR(20) NOT NULL,
                board_name VARCHAR(50),
                open DECIMAL(10,2),
                close DECIMAL(10,2),
                high DECIMAL(10,2),
                low DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                change_amount DECIMAL(10,2),
                volume DECIMAL(15,2),
                amount DECIMAL(15,2),
                turnover_rate DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_board (trade_date, board_code),
                INDEX idx_date (trade_date),
                INDEX idx_board (board_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P0: 行业板块列表
        'board_industry': """
            CREATE TABLE IF NOT EXISTS quant_db.board_industry (
                id INT AUTO_INCREMENT PRIMARY KEY,
                board_code VARCHAR(20) NOT NULL UNIQUE,
                board_name VARCHAR(50) NOT NULL,
                latest_price DECIMAL(10,2),
                change_pct DECIMAL(10,2),
                total_mv DECIMAL(20,2),
                turnover_rate DECIMAL(10,2),
                up_count INT,
                down_count INT,
                lead_stock VARCHAR(50),
                lead_stock_pct DECIMAL(10,2),
                is_latest BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_name (board_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P0: 行业板块历史行情
        'board_industry_hist': """
            CREATE TABLE IF NOT EXISTS quant_db.board_industry_hist (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                board_code VARCHAR(20) NOT NULL,
                board_name VARCHAR(50),
                open DECIMAL(10,2),
                close DECIMAL(10,2),
                high DECIMAL(10,2),
                low DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                change_amount DECIMAL(10,2),
                volume DECIMAL(15,2),
                amount DECIMAL(15,2),
                turnover_rate DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_board (trade_date, board_code),
                INDEX idx_date (trade_date),
                INDEX idx_board (board_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P1: 概念板块成分股
        'board_concept_cons': """
            CREATE TABLE IF NOT EXISTS quant_db.board_concept_cons (
                id INT AUTO_INCREMENT PRIMARY KEY,
                board_code VARCHAR(20) NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                stock_name VARCHAR(50),
                latest_price DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                is_latest BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_board_code (board_code, ts_code),
                INDEX idx_board (board_code),
                INDEX idx_code (ts_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P1: 行业板块成分股
        'board_industry_cons': """
            CREATE TABLE IF NOT EXISTS quant_db.board_industry_cons (
                id INT AUTO_INCREMENT PRIMARY KEY,
                board_code VARCHAR(20) NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                stock_name VARCHAR(50),
                latest_price DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                is_latest BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_board_code (board_code, ts_code),
                INDEX idx_board (board_code),
                INDEX idx_code (ts_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P1: 北向资金持仓个股
        'hsgt_hold_stock': """
            CREATE TABLE IF NOT EXISTS quant_db.hsgt_hold_stock (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                close DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                hold_shares DECIMAL(15,2),
                hold_ratio DECIMAL(10,4),
                hold_mv DECIMAL(15,2),
                `rank` INT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_code (trade_date, ts_code),
                INDEX idx_date (trade_date),
                INDEX idx_code (ts_code),
                INDEX idx_rank (`rank`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P2: 业绩报表
        'earnings_report': """
            CREATE TABLE IF NOT EXISTS quant_db.earnings_report (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                report_date VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                eps DECIMAL(10,4),
                revenue DECIMAL(15,2),
                revenue_yoy DECIMAL(10,2),
                net_profit DECIMAL(15,2),
                net_profit_yoy DECIMAL(10,2),
                roe DECIMAL(10,2),
                navps DECIMAL(10,4),
                gross_margin DECIMAL(10,2),
                net_margin DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_code_report (ts_code, report_date),
                INDEX idx_code (ts_code),
                INDEX idx_report (report_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P2: 大宗交易
        'block_trade': """
            CREATE TABLE IF NOT EXISTS quant_db.block_trade (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                close DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                deal_price DECIMAL(10,2),
                deal_volume INT,
                deal_amount DECIMAL(15,2),
                premium_rate DECIMAL(10,2),
                buyer VARCHAR(100),
                seller VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_code (trade_date, ts_code, deal_price),
                INDEX idx_date (trade_date),
                INDEX idx_code (ts_code),
                INDEX idx_amount (deal_amount)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P2+: 业绩预告
        'stock_forecast': """
            CREATE TABLE IF NOT EXISTS quant_db.stock_forecast (
                id INT AUTO_INCREMENT PRIMARY KEY,
                ts_code VARCHAR(20) NOT NULL,
                end_date VARCHAR(20) NOT NULL,
                report_date VARCHAR(20) NOT NULL,
                forecast_type VARCHAR(20),
                net_profit_min DECIMAL(15,2),
                net_profit_max DECIMAL(15,2),
                change_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_ts_code_end (ts_code, end_date),
                INDEX idx_code (ts_code),
                INDEX idx_end_date (end_date),
                INDEX idx_type (forecast_type)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P3: 强势涨停池（连板股）
        'zt_pool_strong': """
            CREATE TABLE IF NOT EXISTS quant_db.zt_pool_strong (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                last_board INT,
                seal_amount DECIMAL(15,2),
                open_count INT,
                seal_ratio DECIMAL(10,4),
                turnover_rate DECIMAL(10,2),
                amount DECIMAL(15,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_code (trade_date, ts_code),
                INDEX idx_date (trade_date),
                INDEX idx_board (last_board)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        # P3: 炸板池
        'zt_pool_zbgc': """
            CREATE TABLE IF NOT EXISTS quant_db.zt_pool_zbgc (
                id INT AUTO_INCREMENT PRIMARY KEY,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                name VARCHAR(50),
                close DECIMAL(10,2),
                pct_change DECIMAL(10,2),
                seal_amount DECIMAL(15,2),
                amount DECIMAL(15,2),
                turnover_rate DECIMAL(10,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_date_code (trade_date, ts_code),
                INDEX idx_date (trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    }
    
    for name, ddl in tables.items():
        cursor.execute(ddl)
        logger.info(f"Table {name} ready")
    
    conn.commit()
    cursor.close()
    conn.close()
    logger.info("All tables created/verified")


# ==================== 同步函数 ====================

def sync_zt_pool(trade_date: str, dry_run=False):
    """P0: 涨停板池 + 强势涨停 + 炸板池"""
    logger.info(f"同步涨停板 {trade_date}")
    try:
        # 涨停池
        df = ak.stock_zt_pool_em(date=trade_date)
        if df is None or df.empty:
            logger.info(f"  {trade_date} 无涨停数据")
            return 0
        
        df['trade_date'] = pd.to_datetime(trade_date)
        df['代码'] = df['代码'].apply(lambda x: f"{x.zfill(6)}.{'SZ' if x.startswith(('0','3')) else 'SH'}")
        
        cols_map = {
            '代码': 'ts_code', '名称': 'name', '涨跌幅': 'pct_change',
            '最新价': 'close', '换手率': 'turnover_rate', '成交额': 'amount',
            '流通市值': 'circ_mv', '总市值': 'total_mv',
            '封板资金': 'seal_amount', '涨速': None, '首次封板时间': None,
            '封板占比': 'seal_ratio', '开板次数': 'open_count',
            '连板数': 'last_board', '量比': 'volume_ratio'
        }
        
        df = df.rename(columns={k: v for k, v in cols_map.items() if v})
        
        conn = get_db()
        cursor = conn.cursor()
        
        for _, row in df.iterrows():
            ts_code = row.get('ts_code', '')
            if not ts_code:
                continue
            sql = """INSERT INTO quant_db.zt_pool 
                (trade_date, ts_code, name, close, pct_change, turnover_rate, volume_ratio,
                 amount, circ_mv, total_mv, seal_amount, seal_ratio, open_count, last_board)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), close=VALUES(close), pct_change=VALUES(pct_change),
                turnover_rate=VALUES(turnover_rate), volume_ratio=VALUES(volume_ratio),
                amount=VALUES(amount), circ_mv=VALUES(circ_mv), total_mv=VALUES(total_mv),
                seal_amount=VALUES(seal_amount), seal_ratio=VALUES(seal_ratio),
                open_count=VALUES(open_count), last_board=VALUES(last_board)"""
            cursor.execute(sql, (
                trade_date, ts_code, row.get('name',''), row.get('close',0),
                row.get('pct_change',0), row.get('turnover_rate',0), row.get('volume_ratio',0),
                row.get('amount',0), row.get('circ_mv',0), row.get('total_mv',0),
                row.get('seal_amount',0), row.get('seal_ratio',0),
                row.get('open_count',0), row.get('last_board',1)
            ))
        
        # 强势涨停（连板 >= 2）
        strong = df[df.get('last_board', pd.Series([0]*len(df))) >= 2]
        for _, row in strong.iterrows():
            ts_code = row.get('ts_code', '')
            sql = """INSERT INTO quant_db.zt_pool_strong
                (trade_date, ts_code, name, last_board, seal_amount, open_count,
                 seal_ratio, turnover_rate, amount)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), last_board=VALUES(last_board),
                seal_amount=VALUES(seal_amount), open_count=VALUES(open_count),
                seal_ratio=VALUES(seal_ratio), turnover_rate=VALUES(turnover_rate),
                amount=VALUES(amount)"""
            cursor.execute(sql, (
                trade_date, ts_code, row.get('name',''), row.get('last_board',1),
                row.get('seal_amount',0), row.get('open_count',0),
                row.get('seal_ratio',0), row.get('turnover_rate',0), row.get('amount',0)
            ))
        
        # 炸板池
        try:
            dt_df = ak.stock_zt_pool_zbgc_em(date=trade_date)
            if dt_df is not None and not dt_df.empty:
                dt_df['代码'] = dt_df['代码'].apply(lambda x: f"{x.zfill(6)}.{'SZ' if x.startswith(('0','3')) else 'SH'}")
                for _, row in dt_df.iterrows():
                    ts_code = row.get('代码', '')
                    sql = """INSERT INTO quant_db.zt_pool_zbgc
                        (trade_date, ts_code, name, close, pct_change, seal_amount, amount, turnover_rate)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                        name=VALUES(name), close=VALUES(close), pct_change=VALUES(pct_change),
                        seal_amount=VALUES(seal_amount), amount=VALUES(amount),
                        turnover_rate=VALUES(turnover_rate)"""
                    cursor.execute(sql, (
                        trade_date, ts_code, row.get('名称',''), row.get('最新价',0),
                        row.get('涨跌幅',0), row.get('封板资金',0), row.get('成交额',0),
                        row.get('换手率',0)
                    ))
        except Exception as e:
            logger.warning(f"  炸板池同步失败: {e}")
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  涨停: {len(df)}只, 强势: {len(strong)}只")
        return len(df)
    except Exception as e:
        logger.error(f"  涨停板同步失败: {e}")
        return 0


def sync_board_lists():
    """P0: 概念/行业板块列表 — 返回 (concept_ok, industry_ok)"""
    logger.info("同步板块列表")
    
    # 确保 is_latest 列存在
    conn = get_db()
    cursor = conn.cursor()
    for tbl in ['board_concept', 'board_industry']:
        try:
            cursor.execute(f"ALTER TABLE quant_db.{tbl} ADD COLUMN is_latest BOOLEAN DEFAULT TRUE")
        except Exception:
            pass  # 列已存在
    conn.commit()
    cursor.close()
    conn.close()
    
    concept_ok = False
    # 概念板块
    try:
        df = ak_retry(ak.stock_board_concept_name_em)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE quant_db.board_concept SET is_latest = FALSE")
        
        for _, row in df.iterrows():
            sql = """INSERT INTO quant_db.board_concept
                (board_code, board_name, latest_price, change_pct, total_mv,
                 turnover_rate, up_count, down_count, lead_stock, lead_stock_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                board_name=VALUES(board_name), latest_price=VALUES(latest_price),
                change_pct=VALUES(change_pct), total_mv=VALUES(total_mv),
                turnover_rate=VALUES(turnover_rate), up_count=VALUES(up_count),
                down_count=VALUES(down_count), lead_stock=VALUES(lead_stock),
                lead_stock_pct=VALUES(lead_stock_pct), is_latest=TRUE"""
            cursor.execute(sql, (
                str(row.get('板块代码','')), str(row.get('板块名称','')),
                row.get('最新价',0), row.get('涨跌幅',0), row.get('总市值',0),
                row.get('换手率',0), row.get('上涨家数',0), row.get('下跌家数',0),
                str(row.get('领涨股票','')), row.get('领涨股票-涨跌幅',0)
            ))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  概念板块: {len(df)}个")
        concept_ok = True
    except Exception as e:
        logger.error(f"  概念板块列表同步失败: {e}")
    
    industry_ok = False
    # 行业板块
    try:
        df = ak_retry(ak.stock_board_industry_name_em)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE quant_db.board_industry SET is_latest = FALSE")
        
        for _, row in df.iterrows():
            sql = """INSERT INTO quant_db.board_industry
                (board_code, board_name, latest_price, change_pct, total_mv,
                 turnover_rate, up_count, down_count, lead_stock, lead_stock_pct)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                board_name=VALUES(board_name), latest_price=VALUES(latest_price),
                change_pct=VALUES(change_pct), total_mv=VALUES(total_mv),
                turnover_rate=VALUES(turnover_rate), up_count=VALUES(up_count),
                down_count=VALUES(down_count), lead_stock=VALUES(lead_stock),
                lead_stock_pct=VALUES(lead_stock_pct), is_latest=TRUE"""
            cursor.execute(sql, (
                str(row.get('板块代码','')), str(row.get('板块名称','')),
                row.get('最新价',0), row.get('涨跌幅',0), row.get('总市值',0),
                row.get('换手率',0), row.get('上涨家数',0), row.get('下跌家数',0),
                str(row.get('领涨股票','')), row.get('领涨股票-涨跌幅',0)
            ))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  行业板块: {len(df)}个")
        industry_ok = True
    except Exception as e:
        logger.error(f"  行业板块列表同步失败: {e}")
    
    return concept_ok, industry_ok


def sync_board_hist(board_type: str, days: int = 120):
    """P0: 板块历史行情 (board_type: concept/industry) — 增量同步"""
    table = 'board_concept_hist' if board_type == 'concept' else 'board_industry_hist'
    fetch_func = ak.stock_board_concept_hist_em if board_type == 'concept' else ak.stock_board_industry_hist_em
    list_table = 'board_concept' if board_type == 'concept' else 'board_industry'
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 获取板块列表
    cursor.execute(f"SELECT board_code, board_name FROM quant_db.{list_table}")
    boards = cursor.fetchall()
    
    # 获取已同步的最新日期（增量）
    cursor.execute(f"SELECT MAX(trade_date) FROM quant_db.{table}")
    latest = cursor.fetchone()[0]
    if latest:
        start_date = (latest + timedelta(days=1)).strftime('%Y%m%d')
        logger.info(f"  {board_type} hist 增量同步: 从 {latest} 开始")
    else:
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
        logger.info(f"  {board_type} hist 全量同步: 从 {start_date} 开始")
    
    end_date = datetime.now().strftime('%Y%m%d')
    cursor.close()
    conn.close()
    
    total = 0
    failed = 0
    for i, (code, name) in enumerate(boards):
        try:
            df = ak_retry(fetch_func, symbol=name, start_date=start_date, end_date=end_date)
            if df is None or df.empty:
                continue
            
            conn = get_db()
            cursor = conn.cursor()
            
            for _, row in df.iterrows():
                trade_date = str(row.get('日期', ''))
                if len(trade_date) == 8:
                    trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
                
                sql = f"""INSERT INTO quant_db.{table}
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
                    row.get('开盘',0), row.get('收盘',0), row.get('最高',0), row.get('最低',0),
                    row.get('涨跌幅',0), row.get('涨跌额',0), row.get('成交量',0),
                    row.get('成交额',0), row.get('换手率',0)
                ))
            
            conn.commit()
            cursor.close()
            conn.close()
            total += len(df)
            
            if (i + 1) % 50 == 0:
                logger.info(f"  {board_type} hist: {i+1}/{len(boards)} boards, {total} rows")
            
            time.sleep(0.1)  # 降低限频
            
            # 每 10 个板块冷却 2 秒，避免触发东财频率限制
            if (i + 1) % 10 == 0:
                time.sleep(2)
        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning(f"  {code} {name} 历史行情同步失败: {e}")
            elif failed == 4:
                logger.warning(f"  ... (后续失败不再逐个打印)")
    
    logger.info(f"  {board_type} hist 完成: {total} rows, {failed} failed")


def sync_board_cons(board_type: str):
    """P1: 板块成分股映射"""
    table = 'board_concept_cons' if board_type == 'concept' else 'board_industry_cons'
    fetch_func = ak.stock_board_concept_cons_em if board_type == 'concept' else ak.stock_board_industry_cons_em
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"SELECT board_code, board_name FROM quant_db.{'board_concept' if board_type == 'concept' else 'board_industry'}")
    boards = cursor.fetchall()
    cursor.close()
    conn.close()
    
    total = 0
    for i, (code, name) in enumerate(boards):
        try:
            df = ak_retry(fetch_func, symbol=name)
            if df is None or df.empty:
                continue
            
            # 标记旧数据
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(f"UPDATE quant_db.{table} SET is_latest = FALSE WHERE board_code = %s", (code,))
            
            for _, row in df.iterrows():
                raw_code = str(row.get('代码', ''))
                ts_code = f"{raw_code.zfill(6)}.{'SZ' if raw_code.startswith(('0','3')) else 'SH'}"
                
                sql = f"""INSERT INTO quant_db.{table}
                    (board_code, ts_code, stock_name, latest_price, pct_change, is_latest)
                    VALUES (%s,%s,%s,%s,%s,TRUE)
                    ON DUPLICATE KEY UPDATE
                    stock_name=VALUES(stock_name), latest_price=VALUES(latest_price),
                    pct_change=VALUES(pct_change), is_latest=TRUE"""
                cursor.execute(sql, (
                    code, ts_code, str(row.get('名称','')),
                    safe_float(row.get('最新价', 0)), safe_float(row.get('涨跌幅', 0))
                ))
            
            conn.commit()
            cursor.close()
            conn.close()
            total += len(df)
            
            if (i + 1) % 100 == 0:
                logger.info(f"  {board_type} cons: {i+1}/{len(boards)} boards, {total} stocks")
            
            time.sleep(0.1)
        except Exception as e:
            logger.warning(f"  {code} {name} 成分股同步失败: {e}")
    
    logger.info(f"  {board_type} cons 完成: {total} stocks")


def sync_hsgt_hold(trade_date: str):
    """P1: 北向资金持仓个股 - 用沪深港通每日统计替代（持仓排行太慢）"""
    logger.info(f"同步北向资金统计 {trade_date}")
    try:
        dt_fmt = trade_date.replace('-', '')
        df = ak.stock_hsgt_hist_em(symbol='北向资金')
        if df is None or df.empty:
            logger.info(f"  无北向数据")
            return 0

        df['date_str'] = df['日期'].astype(str).str.replace('-', '')
        df = df[df['date_str'] == dt_fmt]
        if df.empty:
            logger.info(f"  {trade_date} 无数据")
            return 0

        row = df.iloc[0]
        conn = get_db()
        cursor = conn.cursor()

        net_amt = safe_float(row.get('当日成交净买额', 0))
        buy_amt = safe_float(row.get('买入成交额', 0))
        sell_amt = safe_float(row.get('卖出成交额', 0))

        sql = """INSERT INTO quant_db.hsgt_hold_stock
            (trade_date, ts_code, name, close, pct_change,
             hold_shares, hold_ratio, hold_mv, `rank`)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
            hold_shares=VALUES(hold_shares), hold_ratio=VALUES(hold_ratio),
            hold_mv=VALUES(hold_mv)"""

        cursor.execute(sql, (
            trade_date, '000000.NORTH', '北向资金',
            0, 0, net_amt, 0, buy_amt + sell_amt, 0
        ))

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  北向资金: 净买额{net_amt/1e8:.1f}亿")
        return 1
    except Exception as e:
        logger.error(f"  北向资金同步失败: {e}")
        return 0


def sync_hsgt_stock_hold():
    """P1+: 北向资金个股持仓明细 — 东方财富个股排行（全市场约2700只）"""
    logger.info("同步北向资金个股持仓...")
    try:
        df = ak.stock_hsgt_hold_stock_em(market='北向', indicator='今日排行')
        if df is None or df.empty:
            logger.info("  无北向个股持仓数据")
            return 0

        # 获取数据日期（从返回的日期列取最新）
        api_dates = df['日期'].dropna().unique()
        if len(api_dates) == 0:
            logger.info("  无日期信息，跳过")
            return 0
        data_date = max(api_dates)
        if hasattr(data_date, 'strftime'):
            trade_date = data_date.strftime('%Y-%m-%d')
        else:
            trade_date = str(data_date)[:10]

        conn = get_db()
        cursor = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            raw_code = str(row.get('代码', ''))
            ts_code = f"{raw_code.zfill(6)}.{'SZ' if raw_code.startswith(('0','3','1')) else 'SH'}"

            hold_ratio = safe_float(row.get('今日持股-占流通股比', 0)) / 100.0
            hold_ratio_total = safe_float(row.get('今日持股-占总股本比', 0)) / 100.0
            hold_shares = safe_float(row.get('今日持股-股数', 0)) * 10000  # 万股→股
            hold_mv = safe_float(row.get('今日持股-市值', 0)) * 10000  # 万元→元
            est_chg = safe_float(row.get('今日增持估计-市值增幅', 0))
            close = safe_float(row.get('今日收盘价', 0))
            pct = safe_float(row.get('今日涨跌幅', 0))

            sql = """INSERT INTO quant_db.hsgt_hold_stock
                (trade_date, ts_code, name, close, pct_change,
                 hold_shares, hold_ratio, hold_mv, `rank`)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), close=VALUES(close), pct_change=VALUES(pct_change),
                hold_shares=VALUES(hold_shares), hold_ratio=VALUES(hold_ratio),
                hold_mv=VALUES(hold_mv), `rank`=VALUES(`rank`)"""
            cursor.execute(sql, (
                trade_date, ts_code, str(row.get('名称', '')),
                close, pct,
                hold_shares, hold_ratio, hold_mv,
                int(row.get('序号', 0))
            ))
            count += 1

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  北向个股持仓: {count}条 (日期 {trade_date})")
        return count
    except Exception as e:
        logger.error(f"  北向个股持仓同步失败: {e}")
        return 0


def sync_earnings(report_period: str = '20251231'):
    """P2: 业绩报表"""
    logger.info(f"同步业绩报表 {report_period}")
    try:
        df = ak.stock_yjbb_em(date=report_period)
        if df is None or df.empty:
            logger.info(f"  无业绩数据")
            return 0
        
        conn = get_db()
        cursor = conn.cursor()
        count = 0
        
        for _, row in df.iterrows():
            raw_code = str(row.get('股票代码', ''))
            ts_code = f"{raw_code.zfill(6)}.{'SZ' if raw_code.startswith(('0','3')) else 'SH'}"
            
            sql = """INSERT INTO quant_db.earnings_report
                (ts_code, report_date, name, eps, revenue, revenue_yoy,
                 net_profit, net_profit_yoy, roe, navps, gross_margin, net_margin)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), eps=VALUES(eps), revenue=VALUES(revenue),
                revenue_yoy=VALUES(revenue_yoy), net_profit=VALUES(net_profit),
                net_profit_yoy=VALUES(net_profit_yoy), roe=VALUES(roe),
                navps=VALUES(navps), gross_margin=VALUES(gross_margin),
                net_margin=VALUES(net_margin)"""
            cursor.execute(sql, (
                ts_code, report_period, str(row.get('股票简称','')),
                safe_float(row.get('每股收益', 0)),
                safe_float(row.get('营业总收入-营业总收入', 0)),
                safe_float(row.get('营业总收入-同比增长', 0)),
                safe_float(row.get('净利润-净利润', 0)),
                safe_float(row.get('净利润-同比增长', 0)),
                safe_float(row.get('净资产收益率', 0)),
                safe_float(row.get('每股净资产', 0)),
                safe_float(row.get('销售毛利率', 0)),
                0  # net_margin not available in yjbb
            ))
            count += 1
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  业绩报表: {count}条")
        return count
    except Exception as e:
        logger.error(f"  业绩报表同步失败: {e}")
        return 0


def sync_block_trade(start_date: str, end_date: str):
    """P2: 大宗交易 — 从 AKShare 获取，从 daily_price 计算折溢率"""
    logger.info(f"同步大宗交易 {start_date} ~ {end_date}")
    try:
        df = ak.stock_dzjy_mrmx(start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            logger.info(f"  无大宗交易数据")
            return 0

        conn = get_db()
        cursor = conn.cursor()
        count = 0

        # 批量获取这些股票的对应日期收盘价，用于计算折溢率
        date_filter = f"'{start_date}'" if start_date else "'2000-01-01'"
        cursor.execute(f"""
            SELECT ts_code, trade_date, close
            FROM daily_price
            WHERE trade_date >= %s AND trade_date <= %s
        """, (start_date, end_date))
        close_map = {}
        for r in cursor.fetchall():
            close_map[(r[0], r[1].strftime('%Y-%m-%d') if hasattr(r[1], 'strftime') else str(r[1])[:10])] = float(r[2])

        for _, row in df.iterrows():
            raw_code = str(row.get('证券代码', ''))
            ts_code = f"{raw_code.zfill(6)}.{'SZ' if raw_code.startswith(('0','3')) else 'SH'}"

            trade_date = str(row.get('交易日期', ''))
            if len(trade_date) == 8:
                trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            elif '-' in trade_date and len(trade_date) == 10:
                pass  # already YYYY-MM-DD
            else:
                continue

            deal_price = safe_float(row.get('成交价', 0))
            deal_volume = int(safe_float(row.get('成交量', 0)))
            deal_amount = safe_float(row.get('成交额', 0))
            buyer = str(row.get('买方营业部', ''))
            seller = str(row.get('卖方营业部', ''))

            # 计算折溢率：(成交价 / 收盘价 - 1) * 100
            close_price = close_map.get((ts_code, trade_date), 0)
            premium_rate = 0
            if close_price > 0 and deal_price > 0:
                premium_rate = round((deal_price / close_price - 1) * 100, 2)

            sql = """INSERT INTO quant_db.block_trade
                (trade_date, ts_code, name, close, pct_change,
                 deal_price, deal_volume, deal_amount, premium_rate, buyer, seller)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE
                name=VALUES(name), deal_price=VALUES(deal_price),
                deal_volume=VALUES(deal_volume), deal_amount=VALUES(deal_amount),
                premium_rate=VALUES(premium_rate),
                buyer=VALUES(buyer), seller=VALUES(seller)"""
            cursor.execute(sql, (
                trade_date, ts_code, str(row.get('证券简称', '')),
                close_price, 0,  # close from daily_price, pct_change not in dzjy_mrmx
                deal_price, deal_volume, deal_amount, premium_rate,
                buyer, seller
            ))
            count += 1

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  大宗交易: {count}条")
        return count
    except Exception as e:
        logger.error(f"  大宗交易同步失败: {e}")
        return 0


def sync_forecast(start_period: str = '20240101', end_period: str = '20261231'):
    """P2+: 业绩预告 — 按股票逐只获取（Tushare 限频 200次/分钟）"""
    logger.info(f"同步业绩预告 {start_period} ~ {end_period}")
    try:
        import tushare as ts
        ts.set_token(os.environ.get('TUSHARE_TOKEN', ''))
        pro = ts.pro_api()

        # 获取所有上市股票
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT ts_code FROM stock_info")
        codes = [r[0] for r in cursor.fetchall()]
        cursor.close()
        conn.close()
        logger.info(f"  共 {len(codes)} 只股票")

        conn = get_db()
        cursor = conn.cursor()
        total = 0
        for i, code in enumerate(codes):
            try:
                df = pro.forecast(ts_code=code, start_date=start_period, end_date=end_period)
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    ts_code = str(row.get('ts_code', code))
                    end_date = str(row.get('end_date', ''))[:10]
                    ann_date = str(row.get('ann_date', ''))[:10]
                    ftype = str(row.get('type', ''))
                    np_min = safe_float(row.get('net_profit_min', 0))
                    np_max = safe_float(row.get('net_profit_max', 0))
                    reason = str(row.get('change_reason', '') or '')

                    sql = """INSERT INTO quant_db.stock_forecast
                        (ts_code, end_date, report_date, forecast_type,
                         net_profit_min, net_profit_max, change_reason)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                        forecast_type=VALUES(forecast_type),
                        net_profit_min=VALUES(net_profit_min),
                        net_profit_max=VALUES(net_profit_max),
                        change_reason=VALUES(change_reason)"""
                    cursor.execute(sql, (ts_code, end_date, ann_date, ftype, np_min, np_max, reason))
                    total += 1
                if (i + 1) % 200 == 0:
                    conn.commit()
                    logger.info(f"  进度: {i+1}/{len(codes)}, {total} 条")
            except Exception as e:
                logger.warning(f"  {code} 失败: {e}")
                time.sleep(1)
                continue
            time.sleep(0.3)  # Tushare 限频 ~200次/分钟

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  业绩预告完成: {total} 条")
        return total
    except Exception as e:
        logger.error(f"  业绩预告同步失败: {e}")
        return 0


def backfill_block_trade_premium():
    """一次性回填历史记录中缺失的 premium_rate 和 close"""
    logger.info("回填大宗交易折溢率...")
    try:
        conn = get_db()
        cursor = conn.cursor()

        # 找出 premium_rate=0 且 deal_price>0 的记录
        cursor.execute("""
            SELECT bt.id, bt.ts_code, DATE(bt.trade_date) as td, bt.deal_price
            FROM block_trade bt
            WHERE bt.premium_rate = 0 AND bt.deal_price > 0
        """)
        rows = cursor.fetchall()
        if not rows:
            logger.info("  无需要回填的记录")
            cursor.close()
            conn.close()
            return 0

        # 批量获取收盘价
        date_clauses = set()
        for r in rows:
            date_clauses.add(str(r[2]))
        date_list = sorted(date_clauses)
        close_map = {}
        for d in date_list:
            cursor.execute("""
                SELECT ts_code, close FROM daily_price WHERE trade_date = %s
            """, (d,))
            for cr in cursor.fetchall():
                close_map[(cr[0], d)] = float(cr[1])

        updated = 0
        for r in rows:
            ts_code, td, deal_price = r[1], str(r[2]), float(r[3])
            close_price = close_map.get((ts_code, td), 0)
            if close_price > 0 and deal_price > 0:
                premium = round((deal_price / close_price - 1) * 100, 2)
                cursor.execute(
                    "UPDATE block_trade SET premium_rate=%s, close=%s WHERE id=%s",
                    (premium, close_price, r[0])
                )
                updated += 1

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"  回填大宗交易折溢率: {updated}条")
        return updated
    except Exception as e:
        logger.error(f"  回填大宗交易折溢率失败: {e}")
        return 0


def safe_float(v):
    """安全转浮点数，处理带单位字符串、NaN、NA"""
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
        # 去掉百分号、亿、万等单位
        s = s.replace('%', '').replace('亿', '').replace('万', '').replace(',', '')
        f = float(s)
        if math.isnan(f) or math.isinf(f):
            return 0
        return f
    except (ValueError, TypeError):
        return 0


# ==================== 主流程 ====================

def main():
    logger.info("=" * 60)
    logger.info("AKShare 数据补充同步开始")
    logger.info("=" * 60)
    
    start_time = time.time()
    
    # 1. 建表
    logger.info("\n[Step 1] 创建数据表")
    create_tables()
    
    # 2. 板块列表
    logger.info("\n[Step 2] 同步板块列表")
    concept_ok, industry_ok = sync_board_lists()
    
    today = datetime.now()
    
    # 3. 涨停板（最近30天）
    logger.info("\n[Step 3] 同步涨停板（最近30天）")
    for i in range(30, -1, -1):
        d = (today - timedelta(days=i)).strftime('%Y-%m-%d')
        day_name = (today - timedelta(days=i)).strftime('%Y%m%d')
        # 跳过非交易日（周一到周五）
        if (today - timedelta(days=i)).weekday() >= 5:
            continue
        sync_zt_pool(day_name)
        time.sleep(0.5)
    
    # 4. 板块历史行情（每日增量同步）— 依赖 Step 2 成功
    is_workday = today.weekday() < 5
    if is_workday and concept_ok and industry_ok:
        logger.info("\n[Step 4] 同步板块历史行情")
        sync_board_hist('concept', days=120)
        sync_board_hist('industry', days=120)
    elif not concept_ok or not industry_ok:
        logger.warning("\n[Step 4] 跳过板块历史行情（板块列表未成功同步，避免空列表级联报错）")
    else:
        logger.info("\n[Step 4] 非交易日,跳过板块历史行情")

    # 5. 板块成分股（每日增量同步）
    if is_workday:
        logger.info("\n[Step 5] 同步板块成分股")
        sync_board_cons('concept')
        sync_board_cons('industry')
    else:
        logger.info("\n[Step 5] 跳过板块成分股（非周五）")
    
    # 6. 北向资金持仓（最近30个交易日）+ 个股持仓
    logger.info("\n[Step 6] 同步北向资金持仓")
    for i in range(30, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        dt_str = d.strftime('%Y-%m-%d')
        sync_hsgt_hold(dt_str)
        time.sleep(0.5)
    if is_workday:
        sync_hsgt_stock_hold()

    # 7. 业绩报表（最近2年报）
    logger.info("\n[Step 7] 同步业绩报表")
    sync_earnings('20251231')
    sync_earnings('20250630')
    sync_earnings('20241231')
    sync_forecast('20250101', '20261231')

    # 8. 大宗交易（最近90天）+ 回填折溢率
    logger.info("\n[Step 8] 同步大宗交易")
    start_d = (today - timedelta(days=90)).strftime('%Y%m%d')
    end_d = today.strftime('%Y%m%d')
    sync_block_trade(start_d, end_d)
    backfill_block_trade_premium()
    
    elapsed = time.time() - start_time
    logger.info(f"\n{'='*60}")
    logger.info(f"同步完成，耗时 {elapsed/60:.1f} 分钟")
    logger.info(f"{'='*60}")


if __name__ == '__main__':
    main()
