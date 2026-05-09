#!/usr/bin/env python3
"""
AI模拟炒股 — TOP5推荐跟踪系统

功能：
1. 每天记录AI精选TOP5推荐（含各维度得分）
2. 追踪后续1/3/5/10日实际收益率
3. 统计模型胜率、平均收益、得分vs收益相关性
4. 提供"AI选股到底准不准"的客观数据

MySQL 表结构：
  quant_db.ai_sim_recommendations - 每日推荐记录
  quant_db.ai_sim_performance     - 后续表现追踪

策略：V4规则初筛 + ML V6.5负向过滤（2026-05-09 更新）
回测（2025-10-01~2026-04-30）：收益+20.25%，胜率67.9%，回撤9.64%
"""

import os, sys, json, logging, pymysql
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

INITIAL_CAPITAL = 100000  # 初始资金 10 万
POSITION_PER_STOCK = 0.18  # 每只股票分配 18%（5只 = 90%仓位，留10%现金）

# ========== 数据库表 ==========
def init_tables(conn):
    """创建模拟组合表"""
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_recommendations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            recommend_date DATE NOT NULL,
            rec_rank TINYINT NOT NULL,
            ts_code VARCHAR(20) NOT NULL,
            name VARCHAR(50),
            industry VARCHAR(50),
            price DECIMAL(10,2),
            ml_score DECIMAL(8,4),
            rank_pct DECIMAL(5,2),
            tech_score DECIMAL(5,2),
            total_score DECIMAL(5,2),
            tech_reasons TEXT,
            market_regime VARCHAR(20),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_date_rank (recommend_date, rec_rank)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_performance (
            id INT AUTO_INCREMENT PRIMARY KEY,
            recommendation_id INT NOT NULL,
            ts_code VARCHAR(20) NOT NULL,
            recommend_date DATE NOT NULL,
            entry_price DECIMAL(10,2),
            -- 1/3/5/10日收盘价
            close_1d DECIMAL(10,2),
            close_3d DECIMAL(10,2),
            close_5d DECIMAL(10,2),
            close_10d DECIMAL(10,2),
            -- 对应收益率
            ret_1d DECIMAL(8,4),
            ret_3d DECIMAL(8,4),
            ret_5d DECIMAL(8,4),
            ret_10d DECIMAL(8,4),
            -- 最高/最低收益（持有期间）
            max_ret DECIMAL(8,4),
            min_ret DECIMAL(8,4),
            -- 状态
            is_stop_loss TINYINT DEFAULT 0,
            stop_loss_date DATE,
            stop_loss_price DECIMAL(10,2),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_recs_date (recommendation_id),
            INDEX idx_code (ts_code),
            INDEX idx_date (recommend_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_summary (
            id INT AUTO_INCREMENT PRIMARY KEY,
            stat_date DATE NOT NULL UNIQUE,
            total_recs INT,
            -- 1日/3日/5日胜率
            win_rate_1d DECIMAL(5,2),
            win_rate_3d DECIMAL(5,2),
            win_rate_5d DECIMAL(5,2),
            -- 平均收益
            avg_ret_1d DECIMAL(8,4),
            avg_ret_3d DECIMAL(8,4),
            avg_ret_5d DECIMAL(8,4),
            -- 累计收益（等权持有）
            cum_ret_1d DECIMAL(8,4),
            cum_ret_3d DECIMAL(8,4),
            cum_ret_5d DECIMAL(8,4),
            -- 得分相关性（total_score vs ret_5d）
            score_return_corr DECIMAL(5,3),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    
    # 模拟账户表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_account (
            id INT AUTO_INCREMENT PRIMARY KEY,
            initial_capital DECIMAL(12,2) DEFAULT 100000,
            cash DECIMAL(12,2) DEFAULT 100000,
            market_value DECIMAL(12,2) DEFAULT 0,
            total_value DECIMAL(12,2) DEFAULT 100000,
            total_pnl DECIMAL(12,2) DEFAULT 0,
            total_pnl_pct DECIMAL(8,2) DEFAULT 0,
            max_drawdown DECIMAL(8,2) DEFAULT 0,
            trade_count INT DEFAULT 0,
            win_count INT DEFAULT 0,
            win_rate DECIMAL(5,2) DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("SELECT COUNT(*) FROM ai_sim_account")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO ai_sim_account (initial_capital, cash) VALUES (%s, %s)", (INITIAL_CAPITAL, INITIAL_CAPITAL))
    
    # 模拟持仓表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_positions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            name VARCHAR(50),
            buy_date DATE NOT NULL,
            buy_price DECIMAL(10,2),
            shares INT,
            cost_amount DECIMAL(12,2),
            current_price DECIMAL(10,2),
            market_value DECIMAL(12,2),
            pnl DECIMAL(12,2),
            pnl_pct DECIMAL(8,2),
            days_held INT,
            status VARCHAR(20) DEFAULT 'holding',
            sell_date DATE,
            sell_price DECIMAL(10,2),
            sell_reason VARCHAR(100),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    
    # 模拟交易记录表
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_sim_trade_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            trade_date DATE NOT NULL,
            ts_code VARCHAR(20) NOT NULL,
            name VARCHAR(50),
            action VARCHAR(10),
            price DECIMAL(10,2),
            shares INT,
            amount DECIMAL(12,2),
            pnl DECIMAL(12,2),
            reason VARCHAR(200),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_date (trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    
    conn.commit()
    cur.close()
    logger.info("AI模拟组合表初始化完成（含账户/持仓/交易记录）")


# ========== 核心功能 ==========
def record_daily_top5(conn):
    """记录今日V4+ML选股TOP5 + 模拟买入"""
    from quant_app.services.strategy_service import generate_v4_ml_top5

    today = datetime.now().strftime('%Y-%m-%d')

    # 检查今天是否已记录
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ai_sim_recommendations WHERE recommend_date = %s", (today,))
    if cur.fetchone()[0] > 0:
        logger.info(f"今日({today})已记录，跳过")
        cur.close()
        return False

    top5 = generate_v4_ml_top5(conn)
    if not top5:
        logger.warning("今日无TOP5推荐")
        cur.close()
        return False

    # 判断市场状态
    try:
        from market_state import get_market_state
        ms = get_market_state(conn)
        regime = ms.get('state', 'unknown')
    except Exception as e:
        logger.warning(f"市场状态获取失败: {e}")
        regime = 'unknown'

    # 获取账户信息
    cur.execute("SELECT cash, total_value, trade_count FROM ai_sim_account LIMIT 1")
    acc_row = cur.fetchone()
    cash = float(acc_row[0]) if acc_row else INITIAL_CAPITAL
    total_value = float(acc_row[1]) if acc_row else INITIAL_CAPITAL
    trade_count = int(acc_row[2]) if acc_row else 0

    # 卖出到期的持仓（持有≥5个交易日）
    sell_expired_positions(conn, cur, today)

    cur.execute("DELETE FROM ai_sim_performance WHERE recommend_date = %s", (today,))

    # 重新读取账户余额
    cur.execute("SELECT cash FROM ai_sim_account LIMIT 1")
    cash = float(cur.fetchone()[0])

    buy_count = 0
    for s in top5:
        ts_code = s['ts_code']

        # 记录推荐
        cur.execute("""
            INSERT INTO ai_sim_recommendations
            (recommend_date, rec_rank, ts_code, name, industry, price, ml_score, rank_pct,
             tech_score, total_score, tech_reasons, market_regime)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            today, s['rank'], ts_code, s['name'], s.get('industry', ''),
            float(s['price']),
            float(s.get('ml_score', 0)),
            0,  # rank_pct 不再使用
            s.get('v4_score', s.get('total_score', 0)),  # tech_score = v4_score
            s['total_score'],
            json.dumps(s.get('reasons', []), ensure_ascii=False), regime
        ))

        rec_id = cur.lastrowid

        # 创建性能追踪
        cur.execute("""
            INSERT INTO ai_sim_performance
            (recommendation_id, ts_code, recommend_date, entry_price)
            VALUES (%s, %s, %s, %s)
        """, (rec_id, ts_code, today, float(s['price'])))

        # 模拟买入（如果账户有足够资金且当前无此持仓）
        cur.execute("SELECT COUNT(*) FROM ai_sim_positions WHERE ts_code=%s AND status='holding'", (ts_code,))
        if cur.fetchone()[0] == 0:
            price = float(s['price'])
            if price and price > 0:
                buy_amount = INITIAL_CAPITAL * POSITION_PER_STOCK  # 固定每只18%初始资金
                shares = int(buy_amount / price / 100) * 100  # 向下取整到100的倍数
                if shares > 0:
                    cost = shares * price
                    if cost <= cash:
                        cur.execute("""
                            INSERT INTO ai_sim_positions
                            (ts_code, name, buy_date, buy_price, shares, cost_amount, current_price, market_value, pnl, pnl_pct, days_held, status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'holding')
                        """, (ts_code, s['name'], today, price, shares, cost, price, cost, 0, 0, 0))

                        cur.execute("""
                            INSERT INTO ai_sim_trade_log
                            (trade_date, ts_code, name, action, price, shares, amount, pnl, reason)
                            VALUES (%s, %s, %s, 'BUY', %s, %s, %s, 0, %s)
                        """, (today, ts_code, s['name'], price, shares, cost,
                              f"V4+ML Top#{s['rank']} V4得分{s['total_score']}"))

                        cash -= cost
                        trade_count += 1
                        buy_count += 1
    
    # 更新账户
    cur.execute("SELECT SUM(market_value) FROM ai_sim_positions WHERE status='holding'")
    mv = float(cur.fetchone()[0] or 0)
    
    total_val = cash + mv
    
    cur.execute("""
        UPDATE ai_sim_account SET 
            cash=%s, market_value=%s, total_value=%s,
            total_pnl=total_value-initial_capital,
            total_pnl_pct=(total_value-initial_capital)/initial_capital*100,
            trade_count=%s
        WHERE id=1
    """, (cash, mv, total_val, trade_count))
    
    conn.commit()
    cur.close()
    logger.info(f"记录今日TOP5完成: {len(top5)}只, 模拟买入{buy_count}只, 现金={cash:.0f}, 市值={mv:.0f}")
    return True


def sell_expired_positions(conn, cur, today):
    """卖出持有≥5个交易日的持仓（按收盘价）"""
    cur.execute("SELECT id, ts_code, name, buy_price, shares FROM ai_sim_positions WHERE status='holding'")
    positions = cur.fetchall()
    
    for pos_id, ts_code, name, buy_price, shares in positions:
        code = ts_code.split('.')[0]
        exchange = ts_code.split('.')[1].upper() if '.' in ts_code else ''
        
        # 找买入后第5个交易日的收盘价
        buy_date_str = today.replace('-', '')
        cur.execute("""
            SELECT trade_date, close FROM daily_price 
            WHERE ts_code=%s AND trade_date > (SELECT buy_date FROM ai_sim_positions WHERE id=%s)
            ORDER BY trade_date ASC LIMIT 5
        """, (f"{code}.{exchange}", pos_id))
        
        rows = cur.fetchall()
        if len(rows) >= 5:
            sell_date = rows[-1][0]
            sell_price = float(rows[-1][1]) if rows[-1][1] else float(buy_price)
            sell_amount = shares * sell_price
            cost = float(buy_price) * shares
            pnl = sell_amount - cost
            
            cur.execute("""
                UPDATE ai_sim_positions 
                SET status='sold', sell_date=%s, sell_price=%s, sell_reason='持有5日到期',
                    current_price=%s, market_value=%s, pnl=%s, 
                    pnl_pct=%s
                WHERE id=%s
            """, (sell_date, sell_price, sell_price, sell_amount, pnl, pnl/cost*100 if cost>0 else 0, pos_id))
            
            cur.execute("""
                INSERT INTO ai_sim_trade_log 
                (trade_date, ts_code, name, action, price, shares, amount, pnl, reason)
                VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, '持有5日到期')
            """, (sell_date, ts_code, name, sell_price, shares, sell_amount, pnl))
            
            # 退回现金
            cur.execute("UPDATE ai_sim_account SET cash=cash+%s, trade_count=trade_count+1 WHERE id=1", (sell_amount,))
            
            # 更新胜率
            if pnl > 0:
                cur.execute("UPDATE ai_sim_account SET win_count=win_count+1 WHERE id=1")
    
    # 止损：检查持仓是否跌破-5%
    cur.execute("SELECT id, ts_code, name, buy_price, shares FROM ai_sim_positions WHERE status='holding'")
    for pos_id, ts_code, name, buy_price, shares in cur.fetchall():
        code = ts_code.split('.')[0]
        exchange = ts_code.split('.')[1].upper() if '.' in ts_code else ''
        
        cur.execute("""
            SELECT trade_date, close FROM daily_price 
            WHERE ts_code=%s AND trade_date > (SELECT buy_date FROM ai_sim_positions WHERE id=%s)
            ORDER BY trade_date DESC LIMIT 1
        """, (f"{code}.{exchange}", pos_id))
        
        row = cur.fetchone()
        if row:
            current_price = float(row[1]) if row[1] else float(buy_price)
            pnl_pct = (current_price - float(buy_price)) / float(buy_price) * 100
            
            if pnl_pct <= -5.0:  # 止损
                sell_amount = shares * current_price
                cost = float(buy_price) * shares
                pnl = sell_amount - cost
                
                cur.execute("""
                    UPDATE ai_sim_positions 
                    SET status='sold', sell_date=CURDATE(), sell_price=%s, sell_reason='止损(-5%%)',
                        current_price=%s, market_value=%s, pnl=%s, pnl_pct=%s
                    WHERE id=%s
                """, (current_price, current_price, sell_amount, pnl, pnl_pct, pos_id))
                
                cur.execute("""
                    INSERT INTO ai_sim_trade_log 
                    (trade_date, ts_code, name, action, price, shares, amount, pnl, reason)
                    VALUES (CURDATE(), %s, %s, 'SELL', %s, %s, %s, %s, '止损(-5%%)')
                """, (ts_code, name, current_price, shares, sell_amount, pnl))
                
                cur.execute("UPDATE ai_sim_account SET cash=cash+%s, trade_count=trade_count+1 WHERE id=1", (sell_amount,))
            else:
                # 更新当前价格和市值
                cur.execute("""
                    UPDATE ai_sim_positions 
                    SET current_price=%s, market_value=%s, pnl=%s, pnl_pct=%s,
                        days_held=DATEDIFF(CURDATE(), buy_date)
                    WHERE id=%s
                """, (current_price, shares*current_price, shares*(current_price-float(buy_price)), pnl_pct, pos_id))


def update_performance(conn, days_back=30):
    """更新已推荐股票的后续表现"""
    cur = conn.cursor()
    
    # 获取需要更新的推荐（最近N天，还未到10日的也更新已有天数）
    start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    today = datetime.now().strftime('%Y-%m-%d')
    
    cur.execute("""
        SELECT p.id, p.ts_code, p.recommend_date, p.entry_price
        FROM ai_sim_performance p
        WHERE p.recommend_date >= %s AND p.recommend_date <= %s
    """, (start_date, today))
    
    rows = cur.fetchall()
    updated = 0
    
    for rec_id, ts_code, rec_date, entry_price in rows:
        rec_date_str = rec_date.strftime('%Y%m%d') if hasattr(rec_date, 'strftime') else str(rec_date).replace('-', '')[:8]
        
        days_passed = (datetime.now().date() - (rec_date if hasattr(rec_date, 'date') else datetime.strptime(str(rec_date), '%Y-%m-%d').date())).days
        if days_passed < 1 or days_passed > 15:
            continue
        
        code_short = ts_code.split('.')[0]
        exchange = ts_code.split('.')[1].upper() if '.' in ts_code else ''
        
        updates = {}
        for target_day in [1, 3, 5, 10]:
            if days_passed >= target_day:
                close_val = _get_close_at_day(conn, code_short, exchange, rec_date_str, target_day)
                if close_val and float(entry_price) > 0:
                    ret = (close_val - float(entry_price)) / float(entry_price)
                    updates[f'close_{target_day}d'] = close_val
                    updates[f'ret_{target_day}d'] = ret
        
        # 持有期间最高/最低收益
        max_ret, min_ret = _get_max_min_ret(conn, code_short, exchange, rec_date_str, min(days_passed, 10))
        if max_ret is not None:
            updates['max_ret'] = max_ret
            updates['min_ret'] = min_ret
        
        # 止损判断（跌破-5%）
        if updates.get('min_ret') and updates['min_ret'] <= -0.05 and not updates.get('is_stop_loss'):
            updates['is_stop_loss'] = 1
            # 找止损日
            stop_date = _find_stop_loss_date(conn, code_short, exchange, rec_date_str, float(entry_price))
            if stop_date:
                updates['stop_loss_date'] = stop_date
                stop_price = _get_close_on_date(conn, code_short, exchange, stop_date.replace('-', ''))
                if stop_price:
                    updates['stop_loss_price'] = stop_price
        
        if updates:
            set_parts = []
            vals = []
            for k, v in updates.items():
                set_parts.append(f"{k} = %s")
                vals.append(v)
            vals.append(rec_id)
            
            sql = f"UPDATE ai_sim_performance SET {', '.join(set_parts)} WHERE id = %s"
            cur.execute(sql, vals)
            updated += 1
    
    conn.commit()
    cur.close()
    logger.info(f"性能更新完成: {updated}条记录")


def _get_close_at_day(conn, code, exchange, start_date, days_after):
    """获取start_date之后第N个交易日的收盘价"""
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_price 
        WHERE ts_code = %s 
        AND trade_date > %s 
        ORDER BY trade_date ASC
        LIMIT 1 OFFSET %s
    """, (f"{code}.{exchange}", start_date, days_after - 1))
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] else None


def _get_max_min_ret(conn, code, exchange, start_date, max_days):
    """获取持有期间最高/最低收益率"""
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_price 
        WHERE ts_code = %s 
        AND trade_date > %s 
        ORDER BY trade_date ASC
        LIMIT %s
    """, (f"{code}.{exchange}", start_date, max_days))
    rows = cur.fetchall()
    cur.close()
    
    if not rows:
        return None, None
    
    closes = [float(r[0]) for r in rows if r[0]]
    if not closes:
        return None, None
    
    entry = closes[0] if len(closes) > 0 else 1
    max_ret = max((c - entry) / entry for c in closes)
    min_ret = min((c - entry) / entry for c in closes)
    return max_ret, min_ret


def _find_stop_loss_date(conn, code, exchange, start_date, entry_price):
    """找到止损日期（首次跌破-5%的日期）"""
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, close FROM daily_price 
        WHERE ts_code = %s 
        AND trade_date > %s 
        ORDER BY trade_date ASC
        LIMIT 15
    """, (f"{code}.{exchange}", start_date))
    rows = cur.fetchall()
    cur.close()
    
    for row in rows:
        if row[1] and entry_price > 0:
            ret = (float(row[1]) - entry_price) / entry_price
            if ret <= -0.05:
                dt = row[0]
                return dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
    return None


def _get_close_on_date(conn, code, exchange, date_str):
    """获取指定日期收盘价"""
    cur = conn.cursor()
    cur.execute("""
        SELECT close FROM daily_price 
        WHERE ts_code = %s AND trade_date = %s
    """, (f"{code}.{exchange}", date_str))
    row = cur.fetchone()
    cur.close()
    return float(row[0]) if row and row[0] else None


def compute_summary(conn):
    """计算统计摘要"""
    cur = conn.cursor()
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            AVG(CASE WHEN ret_1d > 0 THEN 1 ELSE 0 END) * 100 as wr_1d,
            AVG(CASE WHEN ret_3d > 0 THEN 1 ELSE 0 END) * 100 as wr_3d,
            AVG(CASE WHEN ret_5d > 0 THEN 1 ELSE 0 END) * 100 as wr_5d,
            AVG(ret_1d) as ar_1d,
            AVG(ret_3d) as ar_3d,
            AVG(ret_5d) as ar_5d,
            SUM(ret_1d) / COUNT(*) * 100 as cr_1d,
            SUM(ret_3d) / COUNT(*) * 100 as cr_3d,
            SUM(ret_5d) / COUNT(*) * 100 as cr_5d
        FROM ai_sim_performance 
        WHERE recommend_date < DATE_SUB(%s, INTERVAL 1 DAY)
    """, (today,))
    
    row = cur.fetchone()
    if not row or row[0] == 0:
        cur.close()
        return
    
    total, wr_1d, wr_3d, wr_5d, ar_1d, ar_3d, ar_5d, cr_1d, cr_3d, cr_5d = row
    
    # 计算得分相关性
    cur.execute("""
        SELECT r.total_score, p.ret_5d 
        FROM ai_sim_recommendations r
        JOIN ai_sim_performance p ON r.id = p.recommendation_id
        WHERE p.ret_5d IS NOT NULL AND r.recommend_date < DATE_SUB(%s, INTERVAL 5 DAY)
    """, (today,))
    corr_rows = cur.fetchall()
    
    corr = 0
    if len(corr_rows) >= 3:
        scores = [float(r[0]) for r in corr_rows]
        returns = [float(r[1]) for r in corr_rows]
        n = len(scores)
        mean_s = sum(scores) / n
        mean_r = sum(returns) / n
        cov = sum((s - mean_s) * (r - mean_r) for s, r in zip(scores, returns)) / n
        std_s = (sum((s - mean_s) ** 2 for s in scores) / n) ** 0.5
        std_r = (sum((r - mean_r) ** 2 for r in returns) / n) ** 0.5
        if std_s > 0 and std_r > 0:
            corr = cov / (std_s * std_r)
    
    cur.execute("""
        INSERT INTO ai_sim_summary 
        (stat_date, total_recs, win_rate_1d, win_rate_3d, win_rate_5d,
         avg_ret_1d, avg_ret_3d, avg_ret_5d, cum_ret_1d, cum_ret_3d, cum_ret_5d,
         score_return_corr)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            total_recs=VALUES(total_recs), win_rate_1d=VALUES(win_rate_1d),
            win_rate_3d=VALUES(win_rate_3d), win_rate_5d=VALUES(win_rate_5d),
            avg_ret_1d=VALUES(avg_ret_1d), avg_ret_3d=VALUES(avg_ret_3d),
            avg_ret_5d=VALUES(avg_ret_5d), cum_ret_1d=VALUES(cum_ret_1d),
            cum_ret_3d=VALUES(cum_ret_3d), cum_ret_5d=VALUES(cum_ret_5d),
            score_return_corr=VALUES(score_return_corr)
    """, (today, total, wr_1d, wr_3d, wr_5d, ar_1d, ar_3d, ar_5d, cr_1d, cr_3d, cr_5d, corr))
    
    conn.commit()
    cur.close()
    logger.info(f"统计摘要更新完成: {total}条记录, 5日胜率={wr_5d:.1f}%, 得分相关性={corr:.3f}")


def get_performance_report(conn):
    """获取性能报告（含账户/持仓/交易/推荐）"""
    cur = conn.cursor()
    
    # 账户信息
    cur.execute("SELECT * FROM ai_sim_account LIMIT 1")
    acc = cur.fetchone()
    acc_cols = ['id','initial_capital','cash','market_value','total_value',
                'total_pnl','total_pnl_pct','max_drawdown','trade_count','win_count','win_rate']
    acc_data = dict(zip(acc_cols, acc)) if acc else {'initial_capital': INITIAL_CAPITAL, 'cash': INITIAL_CAPITAL, 'total_value': INITIAL_CAPITAL}
    
    # 当前持仓
    cur.execute("""
        SELECT ts_code, name, buy_date, buy_price, shares, cost_amount, 
               current_price, market_value, pnl, pnl_pct, days_held, status
        FROM ai_sim_positions WHERE status='holding' ORDER BY buy_date DESC
    """)
    pos_rows = cur.fetchall()
    pos_cols = ['ts_code','name','buy_date','buy_price','shares','cost_amount',
                'current_price','market_value','pnl','pnl_pct','days_held','status']
    
    # 交易记录
    cur.execute("""
        SELECT trade_date, ts_code, name, action, price, shares, amount, pnl, reason
        FROM ai_sim_trade_log ORDER BY trade_date DESC, id DESC LIMIT 30
    """)
    trade_rows = cur.fetchall()
    trade_cols = ['trade_date','ts_code','name','action','price','shares','amount','pnl','reason']
    
    # 推荐及表现
    cur.execute("""
        SELECT r.recommend_date, r.rec_rank, r.ts_code, r.name, r.industry,
               r.price, r.ml_score, r.tech_score, r.total_score,
               p.ret_1d, p.ret_3d, p.ret_5d, p.is_stop_loss
        FROM ai_sim_recommendations r
        LEFT JOIN ai_sim_performance p ON r.id = p.recommendation_id
        ORDER BY r.recommend_date DESC, r.rec_rank ASC
        LIMIT 30
    """)
    recs = cur.fetchall()
    rec_cols = ['recommend_date','rec_rank','ts_code','name','industry','price',
                'ml_score','tech_score','total_score','ret_1d','ret_3d','ret_5d','is_stop_loss']
    
    # 最新摘要
    cur.execute("SELECT * FROM ai_sim_summary ORDER BY stat_date DESC LIMIT 1")
    summary = cur.fetchone()
    
    cur.close()
    
    return {
        'account': {k: float(v) if isinstance(v, Decimal) else v for k, v in acc_data.items()} if acc_data else {'initial_capital': INITIAL_CAPITAL, 'cash': INITIAL_CAPITAL, 'total_value': INITIAL_CAPITAL},
        'positions': [{k: float(v) if isinstance(v, Decimal) else v for k, v in p.items()} for p in [dict(zip(pos_cols, r)) for r in pos_rows]],
        'trades': [{k: float(v) if isinstance(v, Decimal) else v for k, v in t.items()} for t in [dict(zip(trade_cols, r)) for r in trade_rows]],
        'summary': list(summary) if summary else None,
        'recommendations': [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in [dict(zip(rec_cols, r)) for r in recs]],
    }


def main():
    """主流程：记录今日推荐 + 更新历史表现 + 计算统计"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        init_tables(conn)
        record_daily_top5(conn)
        update_performance(conn)
        compute_summary(conn)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
