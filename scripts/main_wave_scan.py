#!/usr/bin/env python3
"""
主升浪策略 V1 - 接入 sim_trading 框架

策略逻辑 (基于回测最优):
  - Stage 1+2+3 三段漏斗: 大盘 -> 板块 -> 个股
  - 持仓期: 3 个交易日
  - 止损: -3%
  - 仓位: 单票 20% 资金 (允许 3-5 个并发)
  - 写入 sim_signals 表, 复用 sim_trading.execute_buy / execute_sell

用法:
  python3 scripts/main_wave_scan.py scan    # 盘后选股 + 检查持仓
  python3 scripts/main_wave_scan.py status  # 持仓 + 账户
"""
import json
import logging
import os
import sys
import warnings

import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger(__name__)

from quant_app.pipeline.topdown_predictor import TopDownPredictor
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
STRATEGY_NAME = "main_wave_v1"

# 策略参数 (来自 2026-04-09 OOS 验证最优: hold=3, 无止损, +47% 年化)
TOP_N = 5                    # 每次选股数量
HOLD_DAYS = 3                # 持仓 3 个交易日
STOP_LOSS_PCT = 0.0          # 无止损 (OOS 验证: 加止损反而降低收益)
POSITION_PCT = 0.20          # 单票 20% 仓位 (5 并发)
MIN_MW_PROB = 0.0            # 接受所有 topdown 推荐 (mw_prob >= 0 即可)


# ========== 持仓/账户管理 (复用 sim_signals) ==========
def get_db_conn():
    return pymysql.connect(**DB_CONFIG)


def get_holding_positions(strategy=None):
    """从 sim_positions 拿当前持仓, 可按 strategy 过滤"""
    conn = get_db_conn()
    cur = conn.cursor()
    if strategy:
        cur.execute("""
            SELECT id, ts_code, stock_name, shares, cost_price, buy_date, strategy, stop_loss, take_profit
            FROM sim_positions WHERE shares > 0 AND strategy = %s
        """, (strategy,))
    else:
        cur.execute("""
            SELECT id, ts_code, stock_name, shares, cost_price, buy_date, strategy, stop_loss, take_profit
            FROM sim_positions WHERE shares > 0
        """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


def get_account():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sim_account WHERE id=1")
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    cur.close(); conn.close()
    if row is None: return None
    return dict(zip(cols, row))


def get_cash():
    acc = get_account()
    return float(acc['cash']) if acc else 0.0


def get_latest_trade_date():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    d = cur.fetchone()[0]
    cur.close(); conn.close()
    return d


def get_today_prices(ts_codes, trade_date):
    if not ts_codes: return {}
    conn = get_db_conn()
    cur = conn.cursor()
    placeholders = ','.join(['%s'] * len(ts_codes))
    cur.execute(f"""
        SELECT ts_code, open, high, low, close, pct_chg
        FROM daily_price
        WHERE trade_date = %s AND ts_code IN ({placeholders})
    """, (trade_date, *ts_codes))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {r[0]: {'open': float(r[1]) if r[1] else 0,
                    'high': float(r[2]) if r[2] else 0,
                    'low': float(r[3]) if r[3] else 0,
                    'close': float(r[4]) if r[4] else 0,
                    'pct_chg': float(r[5]) if r[5] else 0} for r in rows}


def count_trading_days_between(start_date, end_date):
    """计算两个日期之间有多少个交易日"""
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM daily_price
        WHERE trade_date > %s AND trade_date <= %s
    """, (start_date, end_date))
    n = cur.fetchone()[0]
    cur.close(); conn.close()
    return n


# ========== 买卖执行 ==========
def execute_buy(ts_code, name, price, mw_prob, market_state, today):
    """通过 SQL 直接写 sim_signals 和 sim_positions, 简化版"""
    conn = get_db_conn()
    cur = conn.cursor()
    cash = get_cash()
    target_value = cash * POSITION_PCT
    # A 股 100 股一手
    shares = int(target_value / price / 100) * 100
    if shares < 100:
        logger.info(f"  {name}({ts_code}) 资金不足: cash={cash:.0f}, target={target_value:.0f}, price={price:.2f}")
        return False
    cost = price * shares
    # 记录信号
    cur.execute("""
        INSERT INTO sim_signals
        (signal_date, ts_code, stock_name, signal_type, price, shares, strategy,
         ml_prob, enhanced_score, market_state, reason, status, created_at)
        VALUES (%s, %s, %s, 'mw_buy', %s, %s, %s, %s, %s, %s,
                '主升浪策略买入 (持仓3日, 无止损)', '已执行', NOW())
    """, (today, ts_code, name, price, shares, STRATEGY_NAME,
          mw_prob, mw_prob * 100, market_state))
    # 写持仓
    cur.execute("""
        INSERT INTO sim_positions
        (ts_code, stock_name, shares, cost_price, total_cost, buy_date, buy_time, strategy,
         stop_loss, take_profit, ml_prob, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, NOW())
    """, (ts_code, name, shares, price, price * shares, today, STRATEGY_NAME,
          price * (1 + STOP_LOSS_PCT), price * 1.10, mw_prob))
    # 减现金
    cur.execute("UPDATE sim_account SET cash = cash - %s WHERE id=1", (cost,))
    conn.commit()
    cur.close(); conn.close()
    logger.info(f"  BUY  {name}({ts_code}) {shares}股 @{price:.2f} mw={mw_prob:.3f}")
    return True


def execute_sell(position_id, ts_code, name, shares, price, reason, today):
    conn = get_db_conn()
    cur = conn.cursor()
    cost_price = 0.0
    cur.execute("SELECT cost_price FROM sim_positions WHERE id=%s", (position_id,))
    row = cur.fetchone()
    if row: cost_price = float(row[0])
    pnl = (price - cost_price) * shares
    pnl_pct = (price / cost_price - 1) * 100 if cost_price > 0 else 0
    # 记录信号
    cur.execute("""
        INSERT INTO sim_signals
        (signal_date, ts_code, stock_name, signal_type, price, shares, strategy,
         ml_prob, enhanced_score, market_state, reason, status, created_at)
        VALUES (%s, %s, %s, 'mw_sell', %s, %s, %s, 0, %s, '', %s, '已平仓', NOW())
    """, (today, ts_code, name, price, shares, STRATEGY_NAME, pnl_pct, reason))
    # 清持仓
    cur.execute("DELETE FROM sim_positions WHERE id=%s", (position_id,))
    # 加现金
    cur.execute("UPDATE sim_account SET cash = cash + %s WHERE id=1", (price * shares,))
    conn.commit()
    cur.close(); conn.close()
    logger.info(f"  SELL {name}({ts_code}) {shares}股 @{price:.2f} {reason} pnl={pnl:+.0f} ({pnl_pct:+.2f}%)")
    return pnl


# ========== 核心 scan ==========
def scan():
    logger.info("=== 主升浪策略 V1 scan 开始 ===")
    latest_date = get_latest_trade_date()
    today = str(latest_date)
    logger.info(f"  最新交易日: {today}")

    # 1) 检查现有持仓: 止损 / 持仓满 3 日清仓
    logger.info("--- 检查现有持仓 ---")
    positions = get_holding_positions(strategy=STRATEGY_NAME)
    for pos in positions:
        pid, code, name = pos['id'], pos['ts_code'], pos['stock_name']
        shares = int(pos['shares'])
        cost = float(pos['cost_price'])
        stop = float(pos['stop_loss']) if pos['stop_loss'] else cost * (1 + STOP_LOSS_PCT)
        # 持仓天数
        days_held = count_trading_days_between(pos['buy_date'], today)
        prices = get_today_prices([code], today)
        p = prices.get(code)
        if not p:
            logger.info(f"  {name}: 无今日价格, 跳过")
            continue
        # 触发条件
        if p['low'] <= stop:
            # 止损 (按 stop_price 卖, 考虑滑点)
            exit_p = stop * 0.9985
            execute_sell(pid, code, name, shares, exit_p, f"止损({abs(STOP_LOSS_PCT*100):.0f}个点)", today)
        elif days_held >= HOLD_DAYS:
            # 满 N 日清仓 (按今日开盘卖, 考虑滑点)
            exit_p = p['open'] * 0.9985
            execute_sell(pid, code, name, shares, exit_p, f"持仓满{HOLD_DAYS}日", today)
        else:
            logger.info(f"  {name} 持仓{days_held}/{HOLD_DAYS}日, 今日 {p['pct_chg']:+.2f}%, 继续持有")

    # 2) 选股: TopDownPredictor
    logger.info("--- 选股 (TopDown) ---")
    conn = get_db_conn()
    p = TopDownPredictor(conn=conn)
    try:
        result = p.run_predict(today)
    except Exception as e:
        logger.error(f"TopDown 预测失败: {e}")
        conn.close()
        return
    conn.close()
    if result.get('gate') != 'ok':
        logger.info(f"  TopDown gate={result.get('gate')}, 跳过建仓")
        logger.info(f"  market: {result.get('market', {})}")
        return
    candidates = result.get('candidates', [])[:TOP_N]
    market_state = f"{result['market']['direction']}_p{result['market']['prob']:.2f}"
    logger.info(f"  Topdown: market={result['market']['direction']}, sectors={len(result['hot_sectors'])}, cands={result['candidate_pool_size']}")

    # 3) 过滤 (mw_prob + 已有持仓 + 涨跌停)
    held_codes = {p['ts_code'] for p in get_holding_positions(strategy=STRATEGY_NAME)}
    today_prices = get_today_prices([c['ts_code'] for c in candidates], today)
    to_buy = []
    for c in candidates:
        code = c['ts_code']
        if code in held_codes:
            continue
        if c['main_wave_prob'] < MIN_MW_PROB:
            continue
        p_data = today_prices.get(code)
        if not p_data or p_data['open'] <= 0:
            continue
        # 涨停一字板: 开盘 >= 昨收 * 1.098 → 买不进
        # (近似: pct_chg > 9.5)
        if p_data['pct_chg'] > 9.5:
            continue
        to_buy.append((code, c['name'], p_data['open'], c['main_wave_prob']))

    logger.info(f"  可建仓: {len(to_buy)} (过滤 {len(candidates) - len(to_buy)} 个)")

    # 4) 执行建仓 (按 cash 比例)
    for code, name, price, mw_prob in to_buy:
        if len(get_holding_positions(strategy=STRATEGY_NAME)) >= 5:  # 最多 5 个并发
            break
        if get_cash() < 10000:  # 至少留 1 万
            logger.info("  现金不足, 停止建仓")
            break
        execute_buy(code, name, price, mw_prob, market_state, today)

    # 5) 刷新账户净值
    try:
        from sim_trading import update_account_value
        update_account_value()
    except Exception as e:
        logger.warning(f"update_account_value 失败: {e}")
    logger.info("=== scan 完成 ===")


def status():
    acc = get_account()
    if not acc:
        print("账户未初始化, 请先运行: python3 sim_trading.py init")
        return
    positions = get_holding_positions(strategy=STRATEGY_NAME)
    # 今日市值
    if positions:
        codes = [p['ts_code'] for p in positions]
        prices = get_today_prices(codes, str(get_latest_trade_date()))
        total_mv = 0
        for p in positions:
            pr = prices.get(p['ts_code'], {})
            total_mv += pr.get('close', p['cost_price']) * p['shares']
    else:
        total_mv = 0
    print(json.dumps({
        "strategy": STRATEGY_NAME,
        "cash": round(float(acc['cash']), 2),
        "market_value": round(total_mv, 2),
        "total_value": round(float(acc['cash']) + total_mv, 2),
        "positions": [{
            "ts_code": p['ts_code'],
            "name": p['stock_name'],
            "shares": p['shares'],
            "cost": float(p['cost_price']),
            "buy_date": str(p['buy_date']),
            "stop_loss": float(p['stop_loss']) if p['stop_loss'] else None,
        } for p in positions],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="主升浪策略 V1")
    parser.add_argument("action", choices=["scan", "status"], help="scan=扫描, status=持仓")
    args = parser.parse_args()
    if args.action == "scan":
        scan()
    elif args.action == "status":
        status()
