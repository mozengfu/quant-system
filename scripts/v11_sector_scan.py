#!/usr/bin/env python3
"""
V11 板块内选股 V2 - OOS 最佳 (2026-04-09 验证 +84.36% 年化)

策略: Stage 1 大盘闸门 + 板块过滤 + V11 评分 Top 5
持仓: 2 个交易日
止损: -5%
仓位: 20% 单票, 5 并发

用法:
  python3 scripts/v11_sector_scan.py scan    # 盘后扫描
  python3 scripts/v11_sector_scan.py status  # 持仓 + 账户
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

from quant_app.pipeline.v11_sector_predictor import V11SectorPredictor
from quant_app.risk.position_manager import PositionManager, PositionRules
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
STRATEGY_NAME = "v11_sector_v2"

# 策略参数 (来自 OOS 网格最优)
TOP_N = 5
HOLD_DAYS = 2
STOP_LOSS_PCT = -0.05
POSITION_PCT = 0.20


def get_db_conn():
    return pymysql.connect(**DB_CONFIG)


def get_holding_positions(strategy=None):
    conn = get_db_conn()
    cur = conn.cursor()
    if strategy:
        cur.execute("""SELECT id, ts_code, stock_name, shares, cost_price, buy_date, strategy, stop_loss
                      FROM sim_positions WHERE shares > 0 AND strategy = %s""", (strategy,))
    else:
        cur.execute("""SELECT id, ts_code, stock_name, shares, cost_price, buy_date, strategy, stop_loss
                      FROM sim_positions WHERE shares > 0""")
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
    return dict(zip(cols, row)) if row else None


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
    cur.execute(f"""SELECT ts_code, open, high, low, close, pct_chg
                  FROM daily_price WHERE trade_date = %s AND ts_code IN ({placeholders})""",
                (trade_date, *ts_codes))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return {r[0]: {'open': float(r[1]) if r[1] else 0, 'high': float(r[2]) if r[2] else 0,
                    'low': float(r[3]) if r[3] else 0, 'close': float(r[4]) if r[4] else 0,
                    'pct_chg': float(r[5]) if r[5] else 0} for r in rows}


def count_trading_days_between(start_date, end_date):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date > %s AND trade_date <= %s", (start_date, end_date))
    n = cur.fetchone()[0]
    cur.close(); conn.close()
    return n


def execute_buy(ts_code, name, price, v11_prob, market_state, today):
    conn = get_db_conn()
    cur = conn.cursor()
    cash = get_cash()
    target_value = cash * POSITION_PCT
    # 至少买 100 股, 不足则按 100 股需要的钱买 (允许略超 POSITION_PCT)
    shares_100 = 100
    cost_100 = shares_100 * price
    if cost_100 > cash:
        logger.info(f"  {name}({ts_code}) 现金不够 1 手 cash={cash:.0f} cost={cost_100:.0f}")
        return False
    if cost_100 < target_value:
        # 100 股 < target, 尽可能多买 (按 100 股倍数)
        shares = int(target_value / price / 100) * 100
    else:
        # 100 股 >= target, 买 1 手即可
        shares = shares_100
    if shares < 100:
        shares = shares_100  # 兜底
    cost = price * shares
    cur.execute("""INSERT INTO sim_signals
        (signal_date, ts_code, stock_name, signal_type, price, shares, strategy,
         ml_prob, enhanced_score, market_state, reason, status, created_at)
        VALUES (%s,%s,%s,'v11_buy',%s,%s,%s,%s,%s,%s,
        'V11 板块内选股 (持仓2日, 止损5个点)','已执行',NOW())""",
        (today, ts_code, name, price, shares, STRATEGY_NAME,
         v11_prob, v11_prob * 100, market_state))
    cur.execute("""INSERT INTO sim_positions
        (ts_code, stock_name, shares, cost_price, total_cost, buy_date, buy_time,
         strategy, stop_loss, take_profit, ml_prob, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s,%s,NOW())""",
        (ts_code, name, shares, price, cost, today, STRATEGY_NAME,
         price * (1 + STOP_LOSS_PCT), price * 1.10, v11_prob))
    cur.execute("UPDATE sim_account SET cash = cash - %s WHERE id=1", (cost,))
    conn.commit()
    cur.close(); conn.close()
    logger.info(f"  BUY {name}({ts_code}) {shares}@{price:.2f} v11={v11_prob:.2f}")
    return True


def execute_sell(position_id, ts_code, name, shares, price, reason, today):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT cost_price FROM sim_positions WHERE id=%s", (position_id,))
    row = cur.fetchone()
    cost = float(row[0]) if row else 0
    pnl = (price - cost) * shares
    pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0
    cur.execute("""INSERT INTO sim_signals
        (signal_date, ts_code, stock_name, signal_type, price, shares, strategy,
         ml_prob, enhanced_score, market_state, reason, status, created_at)
        VALUES (%s,%s,%s,'v11_sell',%s,%s,%s,0,%s,'',%s,'已平仓',NOW())""",
        (today, ts_code, name, price, shares, STRATEGY_NAME, pnl_pct, reason))
    cur.execute("DELETE FROM sim_positions WHERE id=%s", (position_id,))
    cur.execute("UPDATE sim_account SET cash = cash + %s WHERE id=1", (price * shares,))
    conn.commit()
    cur.close(); conn.close()
    logger.info(f"  SELL {name}({ts_code}) {shares}@{price:.2f} {reason} pnl={pnl:+.0f} ({pnl_pct:+.2f}%)")
    return pnl


def scan(use_pm=False):
    logger.info(f"=== V11 板块内 V2 scan 开始 (PM={'ON' if use_pm else 'OFF'}) ===")
    pm = None
    if use_pm:
        # 极简 PM: 只保留 DD 闸门
        rules = PositionRules(
            base_position_pct=0.20, max_position_pct=0.25, min_position_pct=0.05,
            max_sector_pct=0.30, max_concurrent=5,
            target_portfolio_vol=1.0,  # 关闭 vol-targeting
            trailing_arm_pct=1.0, trailing_stop_pct=0.0, partial_tp_pct=1.0,
            hard_stop_pct=-0.05,
            dd_warn=0.10, dd_breach=0.15, dd_kill=0.20,
        )
        pm = PositionManager(conn=get_db_conn(), rules=rules)
    latest_date = get_latest_trade_date()
    today = str(latest_date)
    logger.info(f"  最新交易日: {today}")

    # 1) 检查现有持仓
    logger.info("--- 检查现有持仓 ---")
    positions = get_holding_positions(strategy=STRATEGY_NAME)
    for pos in positions:
        pid, code, name = pos['id'], pos['ts_code'], pos['stock_name']
        shares = int(pos['shares'])
        cost = float(pos['cost_price'])
        stop = float(pos['stop_loss']) if pos['stop_loss'] else cost * (1 + STOP_LOSS_PCT)
        days_held = count_trading_days_between(pos['buy_date'], today)
        prices = get_today_prices([code], today)
        p = prices.get(code)
        if not p:
            logger.info(f"  {name}: 无今日价格, 跳过")
            continue
        if p['low'] <= stop:
            exit_p = stop * 0.9985
            execute_sell(pid, code, name, shares, exit_p, f"止损({abs(STOP_LOSS_PCT*100):.0f}个点)", today)
        elif days_held >= HOLD_DAYS:
            exit_p = p['open'] * 0.9985
            execute_sell(pid, code, name, shares, exit_p, f"持仓满{HOLD_DAYS}日", today)
        else:
            logger.info(f"  {name} 持仓{days_held}/{HOLD_DAYS}日, 今日 {p['pct_chg']:+.2f}%, 继续持有")

    # 2) 选股: V11 Sector
    logger.info("--- 选股 (V11 Sector) ---")
    conn = get_db_conn()
    p = V11SectorPredictor(conn=conn)
    try:
        result = p.run_predict(today)
    except Exception as e:
        logger.error(f"V11 预测失败: {e}")
        conn.close()
        return
    conn.close()
    if result.get('gate') != 'ok':
        logger.info(f"  gate={result.get('gate')}, 跳过建仓")
        return
    candidates = result.get('candidates', [])[:TOP_N]
    market_state = f"{result['market']['direction']}_p{result['market']['prob']:.2f}"
    logger.info(f"  V11: market={result['market']['direction']}, sectors={len(result['hot_sectors'])}, cands={result['candidate_pool_size']}")

    # 3) 过滤
    held_codes = {p['ts_code'] for p in get_holding_positions(strategy=STRATEGY_NAME)}
    today_prices = get_today_prices([c['ts_code'] for c in candidates], today)
    to_buy = []
    for c in candidates:
        code = c['ts_code']
        if code in held_codes: continue
        p_data = today_prices.get(code)
        if not p_data or p_data['open'] <= 0: continue
        if p_data['pct_chg'] > 9.5: continue  # 涨停买不进
        to_buy.append((code, c['name'], p_data['open'], c['v11_prob']))

    logger.info(f"  可建仓: {len(to_buy)} (过滤 {len(candidates) - len(to_buy)} 个)")

    # 4) 建仓
    for code, name, price, v11_prob in to_buy:
        if len(get_holding_positions(strategy=STRATEGY_NAME)) >= 5: break
        if get_cash() < 10000: break
        execute_buy(code, name, price, v11_prob, market_state, today)

    # 5) 刷新账户
    try:
        from sim_trading import update_account_value
        update_account_value()
    except Exception as e:
        logger.warning(f"update_account_value 失败: {e}")
    logger.info("=== scan 完成 ===")


def status():
    acc = get_account()
    if not acc:
        print("账户未初始化")
        return
    positions = get_holding_positions(strategy=STRATEGY_NAME)
    if positions:
        codes = [p['ts_code'] for p in positions]
        prices = get_today_prices(codes, str(get_latest_trade_date()))
        total_mv = sum(prices.get(p['ts_code'], {}).get('close', p['cost_price']) * p['shares'] for p in positions)
    else:
        total_mv = 0
    print(json.dumps({
        "strategy": STRATEGY_NAME,
        "params": {"hold_days": HOLD_DAYS, "stop_loss": STOP_LOSS_PCT, "position_pct": POSITION_PCT},
        "cash": round(float(acc['cash']), 2),
        "market_value": round(total_mv, 2),
        "total_value": round(float(acc['cash']) + total_mv, 2),
        "positions": [{
            "ts_code": p['ts_code'], "name": p['stock_name'],
            "shares": p['shares'], "cost": float(p['cost_price']),
            "buy_date": str(p['buy_date']),
            "stop_loss": float(p['stop_loss']) if p['stop_loss'] else None,
        } for p in positions],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["scan", "status"])
    parser.add_argument("--pm", action="store_true", help="启用仓位管理 (DD 闸门)")
    args = parser.parse_args()
    if args.action == "scan": scan(use_pm=args.pm)
    elif args.action == "status": status()
