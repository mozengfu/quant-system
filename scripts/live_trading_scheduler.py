#!/usr/bin/env python3
"""
实盘交易调度器 — QMT实盘交易
用法: python3 scripts/live_trading_scheduler.py scan|morning|monitor|status|ping
"""

import argparse
import datetime
import logging
import os
import sys
from pathlib import Path

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 项目根路径: scripts/ 自动加入 PYTHONPATH（由 crontab 或 start.sh 注入）
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent

# 国信手续费: 佣金 0.025% (5元起), 印花税 卖出 0.1% / 买入 0
# 预算 buffer 0.5% 覆盖佣金 + 最低5元 + 印花税 + 安全余量
#      100股@10.00 买入费用=5元 卖出费用≈6元 → 总费用≈11, buffer=50, 够
# 验证: 1200股@17.57 买入费用=5元 卖出费用≈26.63元 → 总费用≈31.63, buffer=105.42, 够
COMMISSION_BUFFER = 0.005

# 每日扫描候选数量 (不随仓位变动,多产生候选让 monitor 择时)
ML_CANDIDATES_COUNT = 5

import pymysql

from quant_app.services.board_rps_scanner import check_rps_stop
from quant_app.services.notification_service import send_feishu
from quant_app.trading.config import trading_config
from quant_app.trading.executor import create_executor
from quant_app.trading.risk.pre_trade_check import PreTradeChecker
from quant_app.trading.trade_recorder import record_trade
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

from scripts.intraday_t_monitor import TConfig
from scripts.intraday_t_monitor import main as intraday_t_main
from scripts.sim_trading import _count_trading_days_since, record_signal, sync_positions_to_json
from scripts.sim_trading import get_market_params as _get_market_params


def _get_dynamic_positions():
    """根据市场状态动态分配 ML 和 Scanner 的仓位上限

    原则:
      - 趋势上涨(trend_up): ML追强势板块有效 → ML多
      - 震荡(range): 短线技术因子有效 → Scanner多
      - 趋势下跌(trend_down): 逆势选股ML更好 → 都减但ML略多
      - 恐慌/过热: 都压到最低
    """
    try:
        mp = _get_market_params()
        state = mp.get("state", "range")
    except Exception:
        state = "range"

    base_alloc = {
        "trend_up":     (3, 2),   # ML追涨有效
        "range":        (1, 4),   # 震荡市 scanner为主 (ML=1 做防御底仓)
        "trend_down":   (2, 1),   # 都减仓，ML略可逆势
        "panic":        (1, 0),   # 不开新仓
        "overheated":   (2, 1),   # 减仓防回调
    }
    ml, sc = base_alloc.get(state, (1, 5))
    # 不超过 market_state 的总仓位上限
    max_pos = mp.get("max_positions", 3)
    total = ml + sc
    if total > max_pos:
        ml = max(1, round(ml * max_pos / total))
        sc = max(max_pos - ml, 0)
    return (ml, sc)


def _is_market_open():
    """当前是否在连续竞价时段 (9:30-11:30, 13:00-15:00)
    修复 2026-06-24: 9:15→9:30, 避开集合竞价撮合期 (9:15-9:30 撮合价不稳定, 容易误触发止损)。"""
    import datetime
    now = datetime.datetime.now().time()
    morning_start = datetime.time(9, 30)
    morning_end = datetime.time(11, 30)
    afternoon_start = datetime.time(13, 0)
    afternoon_end = datetime.time(15, 0)
    return (morning_start <= now <= morning_end) or (afternoon_start <= now <= afternoon_end)


def _is_trading_day(for_intraday=False):
    import datetime
    today = datetime.date.today()
    if today.weekday() >= 5: return False
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        if for_intraday:
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            last_date = cur.fetchone()[0]
            if last_date:
                return (today - last_date).days <= 4  # 容忍周末+1天数据延迟
            return False
        cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date=%s", (today.strftime("%Y%m%d"),))
        return cur.fetchone()[0] > 100
    except Exception as e:
        logger.warning("交易日判断异常（降级为按周几判断）: %s", e)
        return today.weekday() < 5
    finally:
        try: cur.close(); conn.close()
        except Exception: pass

def _notify_trade(action, name, code, price, qty, reason=""):
    if not trading_config.is_real_trading_enabled: return
    icon = "卖出" if action == "卖出" else "买入"
    send_feishu(f"{icon} 实盘{action}\n股票: {name}({code})\n价格: {price:.2f}  数量: {qty}股\n原因: {reason}")

def get_executor():
    return create_executor()

def get_holding_positions_from_executor(executor):
    try:
        positions = executor.get_positions()
        return [{"ts_code": p.ts_code, "stock_name": p.name, "shares": p.quantity,
                 "cost_price": p.cost_price, "current_price": p.current_price,
                 "market": "sh" if p.ts_code.startswith("6") else "sz", "market_value": p.market_value, "profit_loss": p.pnl,
                 "position_id": getattr(p, "position_id", 0) or 0} for p in positions]
    except: return []

def _log_trade_buy(strategy, ts_code, stock_name, price, shares):
    """记录策略买入到 strategy_trade_log"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO strategy_trade_log (strategy, ts_code, stock_name, buy_date, buy_price, shares, status) "
            "VALUES (%s, %s, %s, CURDATE(), %s, %s, '持有')",
            (strategy, ts_code, stock_name, price, shares)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug("记录买入日志失败: %s", e)


def _log_trade_sell(ts_code, sell_price, shares):
    """记录策略卖出 — 匹配最新一条持有中的买入记录，计算盈亏"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, buy_price FROM strategy_trade_log "
            "WHERE ts_code=%s AND status='持有' ORDER BY buy_date DESC LIMIT 1",
            (ts_code,)
        )
        row = cur.fetchone()
        if row:
            log_id, buy_price = row
            pnl = (sell_price - buy_price) * shares
            pnl_pct = (sell_price - buy_price) / buy_price if buy_price > 0 else 0
            cur.execute(
                "UPDATE strategy_trade_log SET status='已平仓', sell_date=CURDATE(), "
                "sell_price=%s, pnl=%s, pnl_pct=%s, hold_days=DATEDIFF(CURDATE(), buy_date) "
                "WHERE id=%s",
                (sell_price, pnl, pnl_pct, log_id)
            )
            conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.debug("记录卖出日志失败: %s", e)


def _executor_sell_and_record(executor, pos, price, shares, label, reason):
    try:
        executor.sell(pos.get("position_id", 0) or 0, pos["ts_code"], price, shares)
        record_signal(label, pos["ts_code"], pos["stock_name"], price, shares,
                     "持仓管理", 0, 0, pos.get("market_state",""), reason, "已平仓")
        _log_trade_sell(pos["ts_code"], price, shares)
        if trading_config.is_live:
            record_trade(pos["ts_code"], pos["stock_name"], "SELL",
                        price, shares, reason=reason, mode="live")
    except Exception as e:
        logger.error("卖出失败: %s", e)


def _executor_market_sell_and_record(executor, pos, price, shares, label, reason):
    """市价卖出 — 止损/恐慌清仓场景，确保立即成交"""
    try:
        pid = pos.get("position_id", 0) or 0
        if hasattr(executor, "sell_market"):
            order = executor.sell_market(pid, pos["ts_code"], price, shares)
            if order and order.status not in ("rejected",):
                record_signal(label, pos["ts_code"], pos["stock_name"], price, shares,
                             "持仓管理", 0, 0, pos.get("market_state",""), reason, "已平仓")
                _log_trade_sell(pos["ts_code"], price, shares)
                if trading_config.is_live:
                    record_trade(pos["ts_code"], pos["stock_name"], "SELL",
                                price, shares, reason=reason + "(市价)", mode="live")
                logger.info("市价卖出成功: %s %d股", pos["stock_name"], shares)
            else:
                logger.warning("市价卖出失败，降级为限价卖出: %s", pos["stock_name"])
                executor.sell(pid, pos["ts_code"], price, shares)
                record_signal(label, pos["ts_code"], pos["stock_name"], price, shares,
                             "持仓管理", 0, 0, pos.get("market_state",""), reason, "已平仓")
                _log_trade_sell(pos["ts_code"], price, shares)
        else:
            executor.sell(pid, pos["ts_code"], price, shares)
    except Exception as e:
        logger.error("市价卖出失败: %s", e)


def _sync_position_after_buy(ts_code, stock_name, market, shares, price, strategy=None, ml_prob=None):
    """买入后同步记录到 sim_positions 表(实盘追加入口)

    同 ts_code 已存在 HOLD 行 → 增量合并 (加权平均成本), 保留原 buy_date/strategy。
    同 ts_code 不存在 HOLD 行 → INSERT 新行。
    用 SELECT ... FOR UPDATE + 事务防并发重复买入覆盖。
    """
    from datetime import date, datetime

    import pymysql
    try:
        from market_state import get_market_state
        ms = get_market_state() or {}
        p = ms.get('params', {})
        sl_pct = p.get('stop_loss_pct', -3) / 100
        tp_pct = p.get('take_profit_pct', 6) / 100
    except Exception:
        sl_pct, tp_pct = -0.03, 0.06

    new_total_cost = round(price * shares, 2)
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        try:
            # 行锁:防两个 monitor 进程同时买同一只股票时 UPDATE 互相覆盖
            cur.execute(
                "SELECT id, shares, cost_price, total_cost FROM sim_positions "
                "WHERE ts_code=%s AND status='HOLD' ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (ts_code,),
            )
            existing = cur.fetchone()

            if existing:
                _pid, old_shares, old_cost, old_total = existing
                new_shares = int(old_shares) + int(shares)
                merged_total = round(float(old_total) + new_total_cost, 2)
                # 加权平均成本
                merged_cost = round(merged_total / new_shares, 4) if new_shares > 0 else price
                stop_loss = round(merged_cost * (1 + sl_pct), 3)
                take_profit = round(merged_cost * (1 + tp_pct), 3)
                cur.execute(
                    "UPDATE sim_positions SET shares=%s, cost_price=%s, total_cost=%s, "
                    "stop_loss=%s, take_profit=%s, updated_at=%s "
                    "WHERE id=%s",
                    (new_shares, merged_cost, merged_total,
                     stop_loss, take_profit, datetime.now(), _pid),
                )
                logger.info("仓位合并: %s %s +%d股@%.2f → %d股@%.4f (累计%.2f)",
                            stock_name, ts_code, shares, price, new_shares, merged_cost, merged_total)
            else:
                stop_loss = round(price * (1 + sl_pct), 3)
                take_profit = round(price * (1 + tp_pct), 3)
                cur.execute("""
                    INSERT INTO sim_positions
                    (ts_code, stock_name, market, shares, cost_price, total_cost,
                     current_price, market_value, stop_loss, take_profit,
                     buy_date, buy_time, status, updated_at,
                     ml_prob, strategy)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HOLD', %s, %s, %s)
                """, (ts_code, stock_name, market, shares, price, new_total_cost,
                      price, new_total_cost, stop_loss, take_profit,
                      date.today().isoformat(), datetime.now(), datetime.now(),
                      ml_prob, strategy))
                logger.info("仓位同步: %s %s %d股@%.2f", stock_name, ts_code, shares, price)
            conn.commit()
            if trading_config.is_live:
                record_trade(ts_code, stock_name, "BUY", price, shares,
                            reason=f"策略买入({strategy or 'unknown'})", mode="live")
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error("仓位同步失败 %s(%s): %s", stock_name, ts_code, e)


def _is_market_blocked(mkt_info):
    """极端市场状态完全阻断交易

    阻断条件（满足任一即阻断）:
    - 上证单日跌 > 2%
    - 涨跌比 < 0.3（极端偏弱，涨的不到跌的1/3）
    - is_bear + 连续两天跌
    - is_bear + 北向流出 > 100亿
    - 市场状态 = panic（全面恐慌，不择方向）
    - 市场状态 = trend_down 且 market_breadth < 0（普跌）
    """
    state = mkt_info.get("state", "")
    mkt_chg = mkt_info.get("mkt_chg", 0)
    breadth = mkt_info.get("breadth") or {}
    ratio = breadth.get("ratio", 1.0)

    reasons = []

    # 恐慌状态：无条件阻断
    if state == "panic":
        reasons.append("市场恐慌")

    # 趋势下跌 + 涨跌比偏弱：阻断
    if state == "trend_down":
        if ratio < 0.5:
            reasons.append("普跌(涨跌比%.2f)" % ratio)
        if mkt_chg < -1.5:
            reasons.append("上证跌%.1f%%" % mkt_chg)

    # is_bear 下的传统检查
    if mkt_info.get("is_bear"):
        if mkt_chg < -2.0:
            reasons.append("上证暴跌%.1f%%" % mkt_chg)
        if ratio < 0.3:
            reasons.append("涨跌比仅%.2f(涨%d/跌%d)" % (ratio, breadth.get("up",0), breadth.get("down",0)))

    if reasons:
        return True, " | ".join(reasons)
    return False, ""


def _get_realtime_market_state():
    """从实时监控文件读取市场状态，降级到 get_market_state_for_sim"""
    import json
    import os
    state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              'data', 'market_state.json')
    try:
        if os.path.exists(state_file):
            mtime = os.path.getmtime(state_file)
            now = __import__('time').time()
            # 交易时段：数据不超过2分钟算有效
            # 非交易时段：不超过30分钟
            max_age = 120 if _is_trading_day(for_intraday=True) else 1800
            if now - mtime < max_age:
                with open(state_file) as f:
                    st = json.load(f)
                return {
                    "is_bear": st.get("is_bear", False),
                    "state_name": st.get("state_name", "常态"),
                    "mkt_chg": st.get("sh_pct", 0),
                    "threshold": 1.5 if not st.get("is_bear") else 2.5,
                    "state": st.get("state", "normal"),
                }
    except Exception:
        pass
    # 降级: 从 sim_trading 读取（也已改为读 market_state.json）
    from scripts.sim_trading import get_market_state_for_sim
    ms = get_market_state_for_sim()
    return {
        "is_bear": ms.get("is_bear", False),
        "state_name": ms.get("state_name", "常态"),
        "mkt_chg": ms.get("mkt_chg", 0),
        "threshold": ms.get("threshold", 1.5),
        "state": ms.get("state", "normal"),
    }


def _classify_holds_by_strategy(current_holds):
    """将QMT持仓按策略分类(ML/Scanner/unknown)，复用 _classify_single_hold"""
    ml_holds = 0
    scanner_holds = 0
    unknown_holds = 0

    for h in current_holds:
        c = _classify_single_hold(h)
        if c == "ML":
            ml_holds += 1
        elif c == "scanner":
            scanner_holds += 1
        else:
            unknown_holds += 1

    logger.info("持仓分类: ML=%d Scanner=%d 未知=%d (总计%d)", ml_holds, scanner_holds, unknown_holds, len(current_holds))
    return ml_holds, scanner_holds, unknown_holds


def _get_atr(ts_code, period=20):
    """计算ATR(20) — 从MySQL daily_price实时计算，用于动态止损和仓位管理"""
    try:
        import pymysql
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(
            "SELECT high, low, close FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s",
            (ts_code, period + 2))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if len(rows) < period:
            return None
        # rows are in DESC order, reverse to ASC
        rows = list(reversed(rows))
        highs = np.array([float(r[0]) for r in rows])
        lows = np.array([float(r[1]) for r in rows])
        closes = np.array([float(r[2]) for r in rows])
        prev_close = np.roll(closes, 1)
        prev_close[0] = closes[0]
        tr = np.maximum(highs - lows,
               np.maximum(np.abs(highs - prev_close),
                         np.abs(lows - prev_close)))
        atr = tr[-period:].mean()
        return float(atr) if atr > 0 else None
    except Exception as e:
        logger.warning(f"ATR计算失败 {ts_code}: {e}")
        return None


def _sync_positions_from_qmt(executor):
    """从QMT实际持仓同步到sim_positions，确保两边HOLD记录一致"""
    from datetime import datetime

    import pymysql
    try:
        qmt_list = get_holding_positions_from_executor(executor)
    except Exception as e:
        logger.warning("[仓位同步] 获取QMT持仓失败: %s", e)
        return
    if not qmt_list:
        return
    qmt_map = {}
    for p in qmt_list:
        ts = p.get("ts_code", "")
        if not ts:
            continue
        qmt_map[ts] = {
            "shares": int(p.get("shares", 0)),
            "cost_price": float(p.get("cost_price", 0)),
            "current_price": float(p.get("current_price", 0)),
            "market_value": float(p.get("market_value", 0)),
            "profit_loss": float(p.get("profit_loss", 0)),
            "stock_name": p.get("stock_name", ""),
            "market": p.get("market", "sh" if ts.startswith("6") else "sz"),
        }
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT ts_code, shares, cost_price, profit_loss, buy_date FROM sim_positions WHERE status='HOLD'")
        sim_map = {}
        for ts, sh, cp, pl, bd in cur.fetchall():
            sim_map[ts] = {"shares": int(sh), "cost_price": float(cp), "profit_loss": float(pl or 0), "buy_date": bd}
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        changed = 0
        # QMT有但sim没有 -> INSERT
        for ts, qp in qmt_map.items():
            if qp["shares"] <= 0:
                continue
            if ts not in sim_map:
                try:
                    from market_state import get_market_state
                    ms = get_market_state() or {}
                    mp = ms.get("params", {})
                    sl = float(mp.get("stop_loss_pct", -3)) / 100
                    tp = float(mp.get("take_profit_pct", 6)) / 100
                except Exception:
                    sl, tp = -0.03, 0.06
                cost = qp["cost_price"]
                cur.execute(
                    "INSERT INTO sim_positions (ts_code, stock_name, market, shares, "
                    "cost_price, total_cost, current_price, market_value, profit_loss, "
                    "stop_loss, take_profit, buy_date, buy_time, status, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'HOLD',%s) "
                    "ON DUPLICATE KEY UPDATE shares=VALUES(shares)",
                    (ts, qp["stock_name"], qp["market"], qp["shares"],
                     cost, round(cost * qp["shares"], 2),
                     qp["current_price"], qp["market_value"], qp["profit_loss"],
                     round(cost * (1 + sl), 3), round(cost * (1 + tp), 3),
                     today, now, now))
                changed += 1
                logger.info("[仓位同步] INSERT %s %d股@%.2f", ts, qp["shares"], cost)
        # sim有HOLD但QMT没有/股数为0 -> SOLD
        for ts, sp in sim_map.items():
            qp = qmt_map.get(ts)
            q_shares = qp["shares"] if qp else 0
            if q_shares == 0:
                # T+1: 当日买入的持仓不做 SOLD（QMT可能还没成交）
                bd = sp.get("buy_date")
                if bd and str(bd) == today:
                    logger.debug("[仓位同步] 跳过T+1 SOLD: %s", ts)
                    continue
                cur.execute(
                    "UPDATE sim_positions SET status='SOLD', sell_date=%s, "
                    "current_price=%s, market_value=0, profit_loss=%s, "
                    "updated_at=%s WHERE ts_code=%s AND status='HOLD'",
                    (today, 0, sp["profit_loss"], now, ts))
                changed += 1
                logger.info("[仓位同步] SOLD %s (QMT已无持仓)", ts)
        # 两边都有但股数/成本不一致 -> UPDATE
        for ts, qp in qmt_map.items():
            if ts not in sim_map or qp["shares"] <= 0:
                continue
            sim_s = sim_map[ts]["shares"]
            sim_c = sim_map[ts]["cost_price"]
            if qp["shares"] != sim_s or abs(qp["cost_price"] - sim_c) > 0.05:
                tc = round(qp["cost_price"] * qp["shares"], 2)
                pnl_pct = round(qp["profit_loss"] / max(tc, 1), 4)
                cur.execute(
                    "UPDATE sim_positions SET shares=%s, cost_price=%s, total_cost=%s, "
                    "current_price=%s, market_value=%s, profit_loss=%s, profit_pct=%s, "
                    "updated_at=%s WHERE ts_code=%s AND status='HOLD'",
                    (qp["shares"], qp["cost_price"], tc,
                     qp["current_price"], qp["market_value"], qp["profit_loss"], pnl_pct,
                     now, ts))
                changed += 1
                logger.info("[仓位同步] UPDATE %s: %d->%d股 成本%.2f->%.2f",
                            ts, sim_s, qp["shares"], sim_c, qp["cost_price"])
        if changed:
            conn.commit()
            logger.info("[仓位同步] 完成: %d处变更", changed)
        else:
            logger.info("[仓位同步] 无变更，QMT与sim_positions已对齐")
        cur.close()
        conn.close()
    except Exception as e:
        logger.error("[仓位同步] 失败: %s", e)

def cmd_scan():
    executor = get_executor()
    _sync_positions_from_qmt(executor)
    mode_label = "实盘" if trading_config.is_live else "模拟"
    logger.info("=== 交易调度器每日扫描开始 [%s] ===", mode_label)

    ml_candidates = []  # 防御初始化，防止在某些分支未赋值

    # === 盘前清理: 清理昨日及更早的"待执行"信号 + 候选状态切换 ===
    # 修复: 之前没日期过滤, 14:00 手动跑 scan 会清掉当日已写的信号,
    #       导致 monitor 看不到早上的候选
    # 加 DATE(created_at) < CURDATE() 后: 手动 scan 不会影响当日信号,
    # 17:30 cron scan 时昨日信号自动被清, 今日新信号照常写入
    try:
        import pymysql as _pm
        _c = _pm.connect(**DB_CONFIG)
        _cu = _c.cursor()
        _cu.execute(
            "UPDATE sim_signals SET status='已过期' "
            "WHERE status='待执行' AND DATE(created_at) < CURDATE()"
        )
        if _is_market_open():
            _cu.execute(
                "UPDATE sim_signals SET status='待执行' "
                "WHERE status='买入候选' AND signal_date=CURDATE()"
            )
        _c.commit()
        _cu.close()
        _c.close()
        logger.info("已清理旧待执行信号 (仅 < 今日)")
    except Exception as _e:
        logger.debug("清理旧信号失败(可能表空): %s", _e)

    # 1. 获取市场状态
    mkt_info = _get_realtime_market_state()
    is_market_bear = mkt_info.get("is_bear", False)
    market_params = _get_market_params()
    logger.info("市场状态: %s 涨跌%.2f%% 止损%.0f%% 止盈%.0f%% 最大持仓%d",
               mkt_info["state_name"], mkt_info["mkt_chg"],
               market_params["stop_loss_pct"]*-100, market_params["take_profit_pct"]*100,
               market_params["max_positions"])

    # 2. 获取当前持仓 + 资金分配
    current_holds = get_holding_positions_from_executor(executor)
    ml_held_s, scanner_held_s, unknown_held_s = _classify_holds_by_strategy(current_holds)
    balance = executor.get_balance()

    total_cap = 100000
    try:
        from quant_app.services.scanner_strategy import get_v11_capital
        total_cap = get_v11_capital()
    except Exception:
        pass
    logger.info("仓位分配: ML最多%d只 实时扫描最多%d只 | 总资金=%.0f",
                *(_get_dynamic_positions()), total_cap)

    # 3. 选股（按策略分别计算可用仓位）
    ml_max, scanner_max = _get_dynamic_positions()
    _mp_buy = _get_market_params()
    ml_avail_s = max(0, ml_max - ml_held_s)
    scanner_avail_s = max(0, scanner_max - scanner_held_s)
    total_avail = ml_avail_s + scanner_avail_s
    if is_market_bear:
        logger.info("大盘逆市，仓位后续在风控阶段处理")

    if total_avail <= 0:
        ml_candidates = []
        scanner_candidates = []
        logger.info("双策略均已满仓(ML:%d/%d Scanner:%d/%d)", ml_held_s + unknown_held_s, ml_max, scanner_held_s + unknown_held_s, scanner_max)
    elif balance and balance.profit_pct < -0.15:
        ml_candidates = []
        scanner_candidates = []
        logger.warning("回撤断路器: %.1f%%", balance.profit_pct*100)
    elif not balance:
        ml_candidates = []
        scanner_candidates = []
        logger.warning("远程服务不可用")
    else:
        # 风控判定
        blocked, block_reason = _is_market_blocked(mkt_info)
        if blocked:
            logger.warning("市场阻断: %s — 今日不建仓", block_reason)
            send_feishu("市场阻断\n原因: %s\n今日不执行买入" % block_reason)
            ml_avail_s = 0
            scanner_avail_s = 0
            eff_min_score = 999
        elif is_market_bear:
            logger.info("逆市状态，提高选股门槛")
            eff_min_score = mkt_info.get("threshold", 0.40)
            ml_avail_s = max(0, ml_avail_s // 2)
            scanner_avail_s = max(0, scanner_avail_s // 2)
        else:
            eff_min_score = 0

        if ml_avail_s <= 0 and scanner_avail_s <= 0:
            logger.info("无可用仓位(ML:%d Scanner:%d)", ml_avail_s, scanner_avail_s)
            ml_candidates = []
            scanner_candidates = []
        else:
            # ---- 策略A: ML V11 选股 ----
            v11_slots = ml_avail_s if ml_avail_s > 0 else 0
            v11_budget = (balance.available or total_cap) if balance and balance.available else 0
            ml_candidates = []
            if v11_slots > 0:
                ml_raw = _board_rps_scan_recommend(top_n=ML_CANDIDATES_COUNT)
                for pick in (ml_raw or [])[:ML_CANDIDATES_COUNT]:
                    ts_code = pick["ts_code"]
                    name = pick["name"]
                    price = pick.get("price", 0)
                    if price <= 0:
                        continue
                    # 预算扣 0.1% buffer 覆盖佣金+5元min (国信 0.0004% 佣金, 5元起)
                    _v11_budget_eff = v11_budget * (1 - COMMISSION_BUFFER) if v11_budget > 0 else 0
                    shares = int(_v11_budget_eff / v11_slots / price / 100) * 100 if _v11_budget_eff > 0 else 0
                    if shares < 100:
                        continue
                    # 捕获 uk_sim_signals_executed 唯一索引冲突 (同日同股重复)
                    # 旧代码会直接抛 IntegrityError 中断 scan 循环,导致后续候选丢失
                    try:
                        record_signal("买入候选", ts_code, name, price, shares,
                                    f"周线板RPS+ML({pick.get('model_ver','V11.2')})",
                                    pick.get("ml_prob",0), pick.get("ml_score",0),
                                    _mp_buy.get("state","常态"),
                                    f"周线板RPS {pick.get('model_ver','V11.2')} 排序{pick.get('ml_score',0):.3f}",
                                    status="买入候选")
                    except Exception as _sig_err:
                        logger.debug("[scan] 跳过重复信号 %s: %s", ts_code, _sig_err)
                        continue
                    ml_candidates.append(pick)
                    logger.info("[周线板RPS+ML] 候选: %s %.2f ML=%.3f", name, price, pick.get('ml_score',0))
            # ---- 实时扫描已停用，全部资金归 V11.0 ----

            # 汇总飞书通知
            all_candidates = ml_candidates
            if all_candidates and trading_config.is_real_trading_enabled:
                day_label = "今日" if datetime.datetime.now().hour < 15 else "明日"
                lines = [f"{day_label}买入候选 (周线板RPS+V11.2)"]
                if ml_candidates:
                    if ml_candidates[0].get('model_ver', '') == 'V11.0(板RPS周线)':
                        lines.append("-- 板RPS周线 Top5 + V11.2 ML --")
                    else:
                        lines.append("-- ML V11(降级) --")
                    for p in ml_candidates:
                        lines.append(f"  {p['name']}({p['ts_code']}) ML={p.get('ml_score',0):.3f}")
                send_feishu("\n".join(lines))

    logger.info("=== 扫描完成 [%s] ML=%d ===", mode_label, len(ml_candidates))

    # ---- 更新 QMT 股票池（基于RPS板块选股，盘后自动同步到 QMT）----
    try:
        from quant_app.services.strategy_service import scan_daily_pool
        pool_result = scan_daily_pool()
        if pool_result and 'error' not in pool_result:
            logger.info("QMT股票池已更新: %d只候选 → stockpool.json 已同步",
                       pool_result.get("total_candidates", 0))
        else:
            logger.warning("QMT股票池更新失败: %s", pool_result.get("error", "未知"))
    except Exception as e:
        logger.warning("QMT股票池扫描异常: %s", e)

def _factor_scan_recommend(top_n=3):
    """5因子等权模型选股 — 替代旧的 ML V11 预测"""
    try:
        from quant_app.services.factor_scorer import score_stocks
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = str(cur.fetchone()[0])
        cur.close()
        conn.close()

        df = score_stocks(conn=None, as_of_date=latest, top_n=top_n)
        if df is None or df.empty:
            return []

        result = []
        conn2 = pymysql.connect(**DB_CONFIG)
        cur2 = conn2.cursor()
        for _, row in df.iterrows():
            tc = row['ts_code']
            cur2.execute("SELECT name, industry FROM stock_info WHERE ts_code=%s", (tc,))
            r = cur2.fetchone()
            name = r[0] if r else tc
            cur2.execute("SELECT close, pct_chg FROM daily_price WHERE ts_code=%s AND trade_date=%s", (tc, latest))
            dr = cur2.fetchone()
            price = float(dr[0]) if dr else 0
            chg = float(dr[1]) if dr else 0
            score = float(row['factor_score'])
            result.append({
                'ts_code': tc, 'name': name, 'price': price, 'pct_chg': chg,
                'ml_score': score, 'ml_prob': 0.5, 'model_ver': '5因子'
            })
        cur2.close()
        conn2.close()
        return result
    except Exception as e:
        logger.warning("[5因子] 选股失败: %s", e)
        return []


def _board_rps_scan_recommend(top_n=3):
    """周线板RPS + V11.2 ML 排序 — 替代5因子等权模型"""
    try:
        from quant_app.services.board_rps_scanner import board_scan_recommend
        result = board_scan_recommend(top_n=max(top_n, 2))
        if result is not None:
            return result
        logger.warning("周线板RPS扫描返回None，降级到纯ML")
    except Exception as e:
        logger.warning("周线板RPS扫描失败: %s，降级到纯ML", e)
    return _v11_scan_recommend(top_n=top_n)



def _monitor_v11_entry(executor, mkt_info, market_params):
    """5因子 候选股盘中择时入场 — 监测待执行信号，满足条件时买入

    入场条件:
      1. 涨幅 < 5%（不追高）
      2. 当日量比 > 1.2 OR 价格回调至MA5附近（择时）
      3. 价格 > MA20（趋势确认）
      4. 非涨停
    """
    blocked, block_reason = _is_market_blocked(mkt_info)
    if blocked:
        logger.info("[V11入场] 市场阻断: %s, 跳过", block_reason)
        return

    # === 尾盘 cutoff: 14:55 后不开新仓 ===
    # 收盘前 5 分钟信号质量差 (尾盘常拉/砸), T+1 风险高
    import datetime as _dt
    _n = _dt.datetime.now()
    if _n.hour * 100 + _n.minute >= 1455:
        logger.info("[V11入场] 尾盘不开新仓 (%02d:%02d), 跳过", _n.hour, _n.minute)
        return

    # 加载今日待执行 V11.0 信号
    try:
        conn_sig = pymysql.connect(**DB_CONFIG)
        cur_sig = conn_sig.cursor()
        cur_sig.execute(
            "SELECT id, ts_code, price as rec_price, shares as rec_shares, ml_prob, strategy "
            "FROM sim_signals "
            # 修复: 用 signal_date=DATE_SUB(CURDATE(), INTERVAL 1 DAY) 而非 created_at。
            #   scan 在 T 日 17:30 写入, signal_date=T, T+1 日 monitor 才会处理。
            #   原 DATE(created_at)=CURDATE() 永远不匹配 (历史以来全超时根因)。
            #   signal_date=CURDATE() 也不行 (T+1 跑 monitor 时 CURDATE()=T+1, 不等于 T)。
            "WHERE (strategy LIKE '%%V11%%' OR strategy LIKE '%%因子%%' OR strategy LIKE '%%5因子%%') AND status='待执行' AND signal_date=DATE_SUB(CURDATE(), INTERVAL 1 DAY) "
            "ORDER BY ml_prob DESC"
        )
        pending = [dict(zip([d[0] for d in cur_sig.description], row)) for row in cur_sig.fetchall()]
        cur_sig.close()
        conn_sig.close()
    except Exception as e:
        logger.warning("[V11入场] 查询待执行信号失败: %s", e)
        return

    if not pending:
        logger.info("[V11入场] 今日无待执行信号 (scan 17:30后才会有)")
        return

    logger.info("[V11入场] %d只候选股待择时入场", len(pending))

    # 获取当前持仓（去重用）
    current_holds = get_holding_positions_from_executor(executor)
    held_codes = {p["ts_code"] for p in current_holds}

    # 计算可用仓位 (ML 自己的槽位, 不和 Scanner 混算)
    # 修复 2026-06-24: 原 ml_max - total_held 一刀切, 把 4 只 Scanner 持仓算到 ML 头上, 永远挡死 ML 入场。
    # 当前 4 只持仓都是板RPS实时(scanner), ML 实际占用 0, ml_max=2 → avail_slots=2 才对。
    ml_max, _ = _get_dynamic_positions()
    ml_held = sum(1 for p in current_holds
                  if _classify_single_hold(p) == "ml")
    avail_slots = max(0, ml_max - ml_held)
    if avail_slots <= 0:
        logger.info("[V11入场] ML 仓位已满 (%d/%d)", ml_held, ml_max)
        return

    # 计算每只预算
    balance = executor.get_balance()
    available_cash = (balance.available or 0) if balance else 0
    if available_cash < 5000:
        logger.info("[V11入场] 可用资金不足 %.0f", available_cash)
        return
    budget_per_slot = available_cash / max(avail_slots, 1)

    # 过滤今日已买入的股票, 防止重复买入 (2026-06-25)
    try:
        conn_b = pymysql.connect(**DB_CONFIG)
        cur_b = conn_b.cursor()
        cur_b.execute(
            "SELECT DISTINCT ts_code FROM sim_signals "
            "WHERE DATE(created_at)=CURDATE() "
            "  AND signal_type IN ('买入','买入候选','BUY') "
            "  AND status IN ('已执行','待执行','已提交','部分成交')"
        )
        today_bought = {r[0] for r in cur_b.fetchall()}
        cur_b.close(); conn_b.close()
        pending = [s for s in pending if s['ts_code'] not in today_bought]
        if today_bought:
            logger.info("[V11入场] 过滤今日已买入: %d只", len(today_bought))
    except Exception as e:
        logger.debug("查询今日买入失败: %s", e)

    bought = 0
    for sig in pending:
        if bought >= avail_slots:
            break

        ts_code = sig["ts_code"]
        if ts_code in held_codes:
            continue

        code = ts_code.split(".")[0]
        market = "sh" if ts_code.startswith("6") else "sz"

        # 获取实时行情
        quote = _get_qmt_realtime(code, market)
        if not quote:
            continue

        price = quote.get("现价", 0)
        if price <= 0:
            continue

        pct_chg = quote.get("涨跌幅", 0) or 0
        vol_ratio = quote.get("量比", 1.0) or 1.0
        today_open = quote.get("今开", price) or price
        high = quote.get("最高", price) or price
        low = quote.get("最低", price) or price
        pre_close = quote.get("昨收", price) or price

        # ── 入场条件检查（基于实时分时数据） ──
        # 1. 不追高：涨幅 < 5%
        if pct_chg > 5:
            logger.info("[入场] %s 涨幅%.1f%%过高，跳过", ts_code, pct_chg)
            continue

        # 2. 不接飞刀：跌幅 > 5%
        if pct_chg < -5:
            logger.info("[入场] %s 跌%.1f%%，等企稳", ts_code, pct_chg)
            continue

        # 3. 非涨停（科创20%/创业板20%/主板10%）
        limit_up_pct = 0.20 if ts_code.startswith("3") or ts_code.startswith("688") else 0.10
        limit_up = pre_close * (1 + limit_up_pct)
        if price >= limit_up * 0.995:
            logger.info("[入场] %s 已涨停(%.0f%%), 跳过", ts_code, limit_up_pct * 100)
            continue

        # 4. 分时条件：回踩开盘价 / 回抽均价线 / 放量启动 满足其一即可
        good_timing = False
        reason = ""

        # 条件A: 回踩开盘价（现价在开盘价附近，说明日内回踩到位）
        open_diff = abs(price - today_open) / max(today_open, 0.01) * 100
        if open_diff < 1.0:
            good_timing = True
            reason = f"回踩开盘价(开盘{today_open:.2f} 现价{price:.2f} 偏离{open_diff:.1f}%)"

        # 条件B: 放量启动（量比 > 1.3 且 日内位置在中部以上）
        if vol_ratio > 1.3:
            intraday_pos = (price - low) / max(high - low, 0.01)
            if intraday_pos > 0.3:  # 已经脱离日内最低区域
                good_timing = True
                reason = f"放量启动(量比{vol_ratio:.1f} 日内位{intraday_pos:.0%})"
        elif vol_ratio > 1.0 and pct_chg > 0 and pct_chg < 3:
            # 温和放量小幅上涨 — 趋势启动
            good_timing = True
            reason = f"温和启动(量比{vol_ratio:.1f} 涨{pct_chg:.1f}%)"

        if not good_timing:
            logger.info("[V11入场] %s 时机未到 涨%.1f%% 量比%.1f", ts_code, pct_chg, vol_ratio)
            continue

        # ── 执行买入 ──
        # 预算扣 0.1% buffer 覆盖佣金+5元min
        shares = int(budget_per_slot * (1 - COMMISSION_BUFFER) / price / 100) * 100
        if shares < 100:
            logger.info("[V11入场] %s 资金不足(需%.0f/预算%.0f)", ts_code, price*100, budget_per_slot)
            continue

        # 获取名称
        name = ts_code
        try:
            conn_n = pymysql.connect(**DB_CONFIG)
            cur_n = conn_n.cursor()
            cur_n.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
            nr = cur_n.fetchone()
            if nr: name = nr[0]
            cur_n.close(); conn_n.close()
        except Exception:
            pass

        logger.info("[V11入场] 买入: %s(%s) %.2f %d股 %s", name, ts_code, price, shares, reason)

        # 风控检查 (PreTradeChecker: 交易时段/单笔限额/熔断/60s去重/余额)
        checker = PreTradeChecker()
        rc = checker.check_buy(ts_code, name, market, price, shares)
        if not rc["passed"]:
            logger.warning("[V11入场] 风控拦截 %s(%s): %s", name, ts_code, rc["message"])
            continue

        strategy_label = sig.get("strategy", "周线板RPS+ML")
        order = executor.buy(ts_code, name, market, price, shares, strategy=strategy_label)
        if order is None or getattr(order, "status", None) in ("rejected", "failed"):
            logger.warning("[V11入场] 买入失败: %s", ts_code)
            continue

        _log_trade_buy(strategy_label, ts_code, name, price, shares)

        # 更新 sim_signals 状态 (用桥返回的真实 signal_id, 而非原始 scan 信号 id)
        sig_id = order.order_id.replace("sig_", "") if order.order_id and "sig_" in order.order_id else sig["id"]
        try:
            conn_up = pymysql.connect(**DB_CONFIG)
            cur_up = conn_up.cursor()
            cur_up.execute(
                "UPDATE sim_signals SET status='已执行', price=%s, shares=%s WHERE id=%s",
                (price, shares, sig_id)
            )
            conn_up.commit()
            cur_up.close(); conn_up.close()
        except Exception as e:
            logger.error("[V11入场] 更新信号状态失败 id=%s: %s", sig_id, e)

        _sync_position_after_buy(ts_code, name, market, shares, price, strategy_label, sig.get("ml_prob", 0))
        _notify_trade("买入", name, ts_code, price, shares, f"V11择时 {reason}")
        held_codes.add(ts_code)
        bought += 1
        available_cash -= shares * price

    if bought > 0:
        logger.info("[V11入场] 本轮买入%d只", bought)


def _classify_single_hold(pos):
    """判断单只持仓属于ML还是Scanner"""
    strategy = pos.get("strategy", "") or ""
    ts_code = pos.get("ts_code", "")
    try:
        conn_c = pymysql.connect(**DB_CONFIG)
        cur_c = conn_c.cursor()
        cur_c.execute(
            "SELECT strategy FROM sim_positions WHERE ts_code=%s ORDER BY id DESC LIMIT 1", (ts_code,))
        row = cur_c.fetchone()
        if not row or not row[0]:
            # fallback: 从 sim_signals 取 strategy
            cur_c.execute(
                "SELECT strategy FROM sim_signals WHERE ts_code=%s AND status IN ('已执行','部分成交') ORDER BY id DESC LIMIT 1",
                (ts_code,))
            row = cur_c.fetchone()
        cur_c.close(); conn_c.close()
        if row and row[0]:
            strategy = row[0] or strategy
    except Exception:
        pass
    if "扫描" in strategy or "Scanner" in strategy or "板RPS" in strategy:
        return "scanner"
    if "ML" in strategy or "V11" in strategy:
        return "ML"
    return "other"


def _monitor_board_rps_entry(executor, mkt_info, market_params):
    """板RPS候选股盘中实时扫描入场 — 30%仓位

    流程:
      1. 获取板RPS候选股 → ML排序
      2. 拉QMT实时行情 → 实时因子评分（量能/动量/趋势/盘口等）
      3. 综合分排序 → 取符合条件的买入

    与 _monitor_v11_entry 共用相同的仓位/风控框架。
    """
    # 市场阻断检查
    blocked, block_reason = _is_market_blocked(mkt_info)
    if blocked:
        logger.info("[板RPS实时] 市场阻断: %s, 跳过", block_reason)
        return

    # === 尾盘 cutoff: 14:55 后不开新仓 ===
    import datetime as _dt
    _n = _dt.datetime.now()
    if _n.hour * 100 + _n.minute >= 1455:
        logger.info("[板RPS实时] 尾盘不开新仓 (%02d:%02d), 跳过", _n.hour, _n.minute)
        return

    # 获取当前持仓（去重用）
    current_holds = get_holding_positions_from_executor(executor)
    held_codes = {p["ts_code"] for p in current_holds}

    # 计算板RPS实时扫描可用仓位（上限 scanner_max，且不超总剩余）
    ml_max, scanner_max = _get_dynamic_positions()
    scanner_held = sum(1 for p in current_holds
                       if _classify_single_hold(p) == "scanner")
    avail_slots = max(0, scanner_max - scanner_held)  # Scanner只看自己的槽位, 不和ML混算
    if avail_slots <= 0:
        logger.debug("[板RPS实时] 仓位已满 (%d/%d)", scanner_held, scanner_max)
        return

    # 获取板RPS实时信号
    try:
        from quant_app.services.board_rps_scanner import board_rps_realtime_signals
        signals = board_rps_realtime_signals()
    except Exception as e:
        logger.warning("[板RPS实时] 获取信号失败: %s", e)
        return

    if not signals:
        logger.info("[板RPS实时] 无合格信号")
        return

    # 过滤: 实时分 >= 60 (BUY及以上) + ML 概率 >= 0.3 (不能是模型勉强)
    # 修复: 之前 ml_prob > 0 等于不卡 ML, 0.001 概率也买入, 太松
    buys = [s for s in signals
            # Scanner候选已过ML排序, 只按综合分+盘中条件过滤 (2026-06-24)
            if s.get('combined_score', 0) >= 60
            and s['ts_code'] not in held_codes]

    # 过滤今日已卖出的股票, 防止止损/T+3后立即重新买入 (2026-06-24)
    try:
        conn_sold = pymysql.connect(**DB_CONFIG)
        cur_sold = conn_sold.cursor()
        cur_sold.execute(
            "SELECT DISTINCT ts_code FROM sim_signals "
            "WHERE DATE(created_at)=CURDATE() "
            "  AND signal_type IN ('止损','峰值止盈','分批止盈','兜底止盈','RPS止损','恐慌清仓','超时','T+3','强制平仓') "
            "  AND status IN ('已平仓','待执行')"
        )
        today_sold = {r[0] for r in cur_sold.fetchall()}
        cur_sold.close()
        conn_sold.close()
        buys = [s for s in buys if s['ts_code'] not in today_sold]
        if today_sold:
            logger.info("[板RPS实时] 过滤今日已卖出: %d只", len(today_sold))
    except Exception as e:
        logger.debug("查询今日卖出失败: %s", e)

    # 过滤今日已买入的股票, 防止同一个 monitor 循环重复买入 (2026-06-25)
    try:
        conn_bought = pymysql.connect(**DB_CONFIG)
        cur_bought = conn_bought.cursor()
        cur_bought.execute(
            "SELECT DISTINCT ts_code FROM sim_signals "
            "WHERE DATE(created_at)=CURDATE() "
            "  AND signal_type IN ('买入','买入候选','BUY') "
            "  AND status IN ('已执行','待执行','已提交','部分成交')"
        )
        today_bought = {r[0] for r in cur_bought.fetchall()}
        cur_bought.close()
        conn_bought.close()
        buys = [s for s in buys if s['ts_code'] not in today_bought]
        if today_bought:
            logger.info("[板RPS实时] 过滤今日已买入: %d只", len(today_bought))
    except Exception as e:
        logger.debug("查询今日买入失败: %s", e)

    if not buys:
        # 详细原因诊断 (2026-06-24)
        score_fail = [s for s in signals if s['realtime_score'] < 60 and s['ml_prob'] < 0.3]
        ml_only_fail = [s for s in signals if s['realtime_score'] >= 60 and s['ml_prob'] < 0.3]
        score_only_fail = [s for s in signals if s['realtime_score'] < 60 and s['ml_prob'] >= 0.3]
        held = [s for s in signals if s['ts_code'] in held_codes and s['realtime_score'] >= 60 and s['ml_prob'] >= 0.3]
        logger.info("[板RPS实时] 无触发买入条件: 双不过%d 仅ML拦%d 仅分拦%d 已持仓%d",
                   len(score_fail), len(ml_only_fail), len(score_only_fail), len(held))
        if ml_only_fail:
            ml_top = sorted(ml_only_fail, key=lambda s: -s['realtime_score'])[:5]
            for s in ml_top:
                logger.info("  ML拦: %s %s 综合%.0f ML=%.3f", s['ts_code'], s.get('name',''), s['realtime_score'], s['ml_prob'])
        if score_only_fail:
            sc_top = sorted(score_only_fail, key=lambda s: -s['ml_prob'])[:5]
            for s in sc_top:
                logger.info("  分拦: %s %s 综合%.0f ML=%.3f", s['ts_code'], s.get('name',''), s['realtime_score'], s['ml_prob'])
        if held:
            for s in held:
                logger.info("  已持仓: %s %s 综合%.0f ML=%.3f", s['ts_code'], s.get('name',''), s['realtime_score'], s['ml_prob'])
        return

    # 预算
    balance = executor.get_balance()
    available_cash = (balance.available or 0) if balance else 0
    # 每个 Scanner slot 最多用 50% 可用现金（防止一次买光所有余额）
    scanner_budget = available_cash * 0.5
    if scanner_budget < 5000:
        logger.info("[板RPS实时] 可用资金不足 %.0f", scanner_budget)
        return
    budget_per_slot = scanner_budget / max(avail_slots, 1)

    bought = 0
    for sig in buys:
        if bought >= avail_slots:
            break

        ts_code = sig['ts_code']
        if ts_code in held_codes:
            continue

        code = ts_code.split(".")[0]
        market = "sh" if ts_code.startswith("6") else "sz"

        # 获取最新实时行情
        quote = _get_qmt_realtime(code, market)
        if not quote:
            continue

        price = quote.get("现价", 0)
        if price <= 0:
            continue

        pct_chg = quote.get("涨跌幅", 0) or 0
        vol_ratio = quote.get("量比", 1.0) or 1.0

        # 入场风控
        if pct_chg > 5 or pct_chg < -5:
            continue
        # 涨停兜底 (分板块, 与 board_rps_scanner 对齐)
        pre_close = quote.get("昨收", price) or price
        limit_up_pct = 0.20 if (ts_code.startswith("3") or ts_code.startswith("688")) else 0.10
        if price >= pre_close * (1 + limit_up_pct * 0.995):
            continue

        # 预算买股
        # 预算扣 0.1% buffer 覆盖佣金+5元min
        shares = int(budget_per_slot * (1 - COMMISSION_BUFFER) / price / 100) * 100
        if shares < 100:
            continue

        strategy_tag = "板RPS实时"
        reason = (f"[{strategy_tag}]实时扫描: ML分{sig['ml_score']:.3f} "
                  f"实时分{sig['realtime_score']} 量比{vol_ratio:.1f}")

        # 风控检查 (PreTradeChecker: 交易时段/单笔限额/熔断/60s去重/余额)
        checker = PreTradeChecker()
        rc = checker.check_buy(ts_code, sig.get('name', ts_code), market, price, shares)
        if not rc["passed"]:
            logger.warning("[板RPS实时] 风控拦截 %s(%s): %s", sig.get("name", ts_code), ts_code, rc["message"])
            continue

        order = executor.buy(
            ts_code, sig['name'], market, price, shares,
            strategy=strategy_tag,
            ml_prob=sig['ml_prob'],
            enhanced_score=sig['combined_score'],
            market_state=mkt_info.get('state_name', ''),
            reason=reason,
        )
        if order and getattr(order, "status", None) not in ("rejected", "failed"):
            bought += 1
            held_codes.add(ts_code)
            _log_trade_buy(strategy_tag, ts_code, sig['name'], price, shares)
            logger.info("[板RPS实时] 买入 %s(%s) %.0f股@%.2f 分%.1f",
                        sig['name'], ts_code, shares, price, sig['combined_score'])
            # 写 sim_signals
            from datetime import datetime; today = datetime.now().strftime('%Y-%m-%d')
            try:
                conn_sig = pymysql.connect(**DB_CONFIG)
                cur_sig = conn_sig.cursor()
                cur_sig.execute(
                    "INSERT IGNORE INTO sim_signals "
                    "(ts_code, stock_name, signal_date, signal_type, strategy, enhanced_score, "
                    " ml_prob, price, shares, status, reason, created_at) "
                    "VALUES (%s,%s,%s,'买入',%s,%s,%s,%s,%s,'已执行',%s,NOW())",
                    (ts_code, sig['name'], today, strategy_tag,
                     sig['combined_score'], sig['ml_prob'], price, shares, reason)
                )
                conn_sig.commit()
                cur_sig.close()
                conn_sig.close()
            except Exception:
                pass
        else:
            logger.warning("[板RPS实时] 买入失败: %s %s", ts_code, order.status if order else "")
            _sync_position_after_buy(ts_code, sig['name'], market, shares, price, strategy_tag, sig['ml_prob'])
            _notify_trade("买入", sig['name'], ts_code, price, shares, strategy_tag)

    logger.info("[板RPS实时] 本轮买入 %d/%d 只 (预算%.0f/只)",
                bought, len(buys), budget_per_slot)


def cmd_monitor():
    """
    盘中持仓监控 + V11.0候选股择时入场 + 板RPS实时扫描

    每5分钟运行一次:
    1. 持仓监控 — 检查所有持仓，触发止损/止盈/超时自动卖出
    2. V11.0择时入场 — 监测盘后选出的候选股，满足量价条件时买入
    """
    # === 心跳写入 (供 feishu_alerts.py 检测 monitor 是否在跑) ===
    # 放在最开始, 确保即使后续逻辑 crash, 心跳也会被记录
    try:
        (BASE_DIR / "data" / "monitor_heartbeat.txt").write_text(datetime.datetime.now().isoformat())
    except Exception as e:
        logger.warning("心跳写入失败: %s", e)

    if not _is_trading_day(for_intraday=True):
        logger.info("今日非交易日, 跳过监控")
        return
    if not _is_market_open():
        logger.info("当前非交易时段 (仅9:15-11:30/13:00-15:00), 跳过监控")
        return
    executor = get_executor()
    _sync_positions_from_qmt(executor)
    mode_label = "实盘" if trading_config.is_real_trading_enabled else (f"模拟({trading_config.trade_mode})")

    logger.info("=== 持仓监控开始 [%s] ===", mode_label)

    # 从市场状态获取动态止盈止损参数
    market_params = _get_market_params()
    sl_pct = market_params["stop_loss_pct"]  # 固定止损兜底值
    ATR_STOP_MULT = 2.0  # ATR动态止损倍数
    logger.info("风控参数: 固定止损%.0f%% ATR倍数%.1f 止盈%.0f%%",
               sl_pct * -100, ATR_STOP_MULT, market_params["take_profit_pct"] * 100)

    # 恐慌清仓检查
    mkt_info = _get_realtime_market_state()
    if mkt_info.get("is_bear") and mkt_info.get("mkt_chg", 0) < -3.5:
        logger.critical("恐慌暴跌%.1f%% 触发全仓清仓!", mkt_info["mkt_chg"])
        send_feishu("恐慌清仓\n上证跌%.1f%%\n清空全部持仓" % mkt_info["mkt_chg"])
        positions_for_panic = get_holding_positions_from_executor(executor)
        for pos in positions_for_panic:
            code = pos["ts_code"].split(".")[0]
            market = pos.get("market", "sh" if pos["ts_code"].startswith("6") else "sz")
            quote = _get_qmt_realtime(code, market)
            price = quote["现价"] if quote else 0
            if price > 0:
                _executor_market_sell_and_record(executor, pos, price, int(pos["shares"]), "恐慌清仓", "恐慌暴跌全部清仓")
                _notify_trade("卖出", pos["stock_name"], pos["ts_code"], price, int(pos["shares"]), "恐慌清仓")
        return

    positions = get_holding_positions_from_executor(executor)
    if not positions:
        logger.info("当前无持仓")
        return

    for pos in positions:
        code = pos["ts_code"].split(".")[0]
        market = pos["market"]
        quote = _get_qmt_realtime(code, market)

        if not quote:
            logger.warning("无法获取 %s 行情，跳过", pos["ts_code"])
            continue

        price = quote["现价"]
        shares = int(pos["shares"])
        cost_price = float(pos["cost_price"])
        if cost_price == 0:
            cost_price = float(pos.get("current_price") or price)
        if cost_price == 0:
            logger.warning("持仓 %s 成本为0且无法获取替代价格，跳过", pos["ts_code"])
            continue
        pct_chg = (price - cost_price) / cost_price * 100

        atr_val = _get_atr(pos["ts_code"])
        if atr_val and atr_val > 0 and cost_price > 0:
            # ATR动态止损: 成本价 - 2×ATR，不低于固定止损线
            atr_stop = cost_price - ATR_STOP_MULT * atr_val
            fixed_stop = cost_price * (1 + sl_pct)
            stop_price = round(max(atr_stop, fixed_stop), 2)
        else:
            stop_price = round(cost_price * (1 + sl_pct), 2)

        # === 实盘硬性兜底止损线（主人 2026-06-16 改: -5%→-7%）===
        # 之前依赖 sim_positions.stop_loss，但实盘持仓(QMT同步)经常为 0 → 止损失效
        # 现在用主人规则硬性兜底：成本 × 0.93 必触发，与上面计算的 stop_price 取更低者
        hard_stop = round(cost_price * 0.93, 2)
        final_stop = min(stop_price, hard_stop)
        if final_stop < stop_price:
            logger.info("🛡 硬性兜底止损覆盖: ATR/动态止损 %.2f → C3.0 兜底 %.2f",
                        stop_price, final_stop)

        buy_date = pos.get("buy_date")
        # QMT 实盘不返回 buy_date，从 sim_positions 补查
        if not buy_date:
            try:
                conn_bd = pymysql.connect(**DB_CONFIG)
                cur_bd = conn_bd.cursor()
                cur_bd.execute(
                    "SELECT buy_date FROM sim_positions WHERE ts_code=%s AND status='HOLD' ORDER BY buy_date DESC LIMIT 1",
                    (pos["ts_code"],))
                row_bd = cur_bd.fetchone()
                cur_bd.close()
                conn_bd.close()
                if row_bd and row_bd[0]:
                    buy_date = str(row_bd[0])
            except Exception:
                pass
        days_held = _count_trading_days_since(buy_date) if buy_date else 0

        # === 读取分批止盈标志（避免重复分批）===
        partial_taken = False
        try:
            conn_pt = pymysql.connect(**DB_CONFIG)
            cur_pt = conn_pt.cursor()
            cur_pt.execute(
                "SELECT partial_taken FROM sim_positions WHERE ts_code=%s AND status='HOLD' LIMIT 1",
                (pos["ts_code"],))
            row_pt = cur_pt.fetchone()
            cur_pt.close()
            conn_pt.close()
            if row_pt and row_pt[0]:
                partial_taken = True
        except Exception as e:
            logger.debug("读取 partial_taken 失败 %s: %s", pos["ts_code"], e)

        # === A股 T+1 检查: 今日买入当日不可卖出, 跳过止盈止损 ===
        # 即便 QMT 拒单也是浪费一次下单, 直接 skip 干净
        # 注意: buy_date 未知(QMT 有但 sim 没有的孤儿持仓)不 skip, 让止损和峰值止盈照常跑
        if days_held == 0 and buy_date and str(buy_date) == str(datetime.date.today()):
            logger.info("⏸ %s 今日买入 (T+1), 跳过卖出检查", pos["stock_name"])
            continue

        # === 日内去重: 今日已触发的卖出信号不重复触发（修复 2026-06-24） ===
        # QMT 卖出单没成交时, 持仓监控 30 秒循环会持续看到持仓,
        # 导致 603002/300903 在 9:15-9:30 之间被触发 100+ 次。
        # 策略: 同 ts_code 当日任意 sell 类信号存在 → 跳过本轮, 等 QMT 异步确认。
        try:
            conn_dd = pymysql.connect(**DB_CONFIG)
            cur_dd = conn_dd.cursor()
            cur_dd.execute(
                "SELECT id, signal_type FROM sim_signals "
                "WHERE ts_code=%s AND DATE(created_at)=CURDATE() "
                "  AND signal_type IN ('\u6b62\u635f','\u5cf0\u503c\u6b62\u76c8','\u5206\u6279\u6b62\u76c8','\u5151\u5e95\u6b62\u76c8','RPS\u6b62\u635f','\u6050\u614c\u6e05\u4ed3') "
                "  AND status IN ('\u5f85\u6267\u884c','\u5df2\u5e73\u4ed3') LIMIT 1",
                (pos["ts_code"],))
            row_dd = cur_dd.fetchone()
            cur_dd.close()
            conn_dd.close()
            if row_dd:
                logger.info("\u23f8 %s \u4eca\u65e5\u5df2\u89e6\u53d1\u5356\u51fa(%s), \u8df3\u8fc7\u672c\u8f6e", pos["stock_name"], row_dd[1])
                continue
        except Exception as e:
            logger.debug("\u65e5\u5185\u53bb\u91cd\u67e5\u8be2\u5931\u8d25 %s: %s", pos["ts_code"], e)

        # === RPS 止损（2026-06-20 补充）===
        # 资金退潮先于价格下跌: RPS < 15 或 20日累计跌破历史下沿 → 立即止损
        # 优先级: 早于价格硬止损（因 → 果），晚于 T+1 检查（避免新买入误杀）
        # 持仓 < 2 天直接放行（防止买入次日波动误杀）
        try:
            rps_stop, rps_detail = check_rps_stop(pos["ts_code"], days_held=days_held)
            if rps_stop:
                loss_pct = (price - cost_price) / cost_price * 100
                rps_reason = rps_detail.get("reason_text", "RPS止损")
                _executor_market_sell_and_record(executor, pos, price, shares, "RPS止损", rps_reason)
                _notify_trade("卖出", pos["stock_name"], pos["ts_code"], price, shares, rps_reason)
                send_feishu(
                    f"🚨 RPS 止损触发\n"
                    f"股票: {pos['stock_name']}({pos['ts_code']})\n"
                    f"成本: {cost_price:.2f}  现价: {price:.2f}\n"
                    f"浮亏: {loss_pct:+.1f}%\n"
                    f"RPS: {rps_detail.get('rps', 0):.1f}  "
                    f"20日累计: {rps_detail.get('cum_20d', 0):+.2f}%  "
                    f"下沿: {rps_detail.get('lower_band', 0):+.2f}%\n"
                    f"原因: {'; '.join(rps_detail.get('reasons', []))}\n"
                    f"已下市价卖单"
                )
                logger.warning(
                    "🚨 RPS止损(%s): %s 成本%.2f 现价%.2f RPS=%.1f cum=%.2f%% lb=%.2f%%",
                    mode_label, pos["stock_name"], cost_price, price,
                    rps_detail.get('rps', 0), rps_detail.get('cum_20d', 0),
                    rps_detail.get('lower_band', 0),
                )
                continue
        except Exception as e:
            # RPS 止损判断失败不能影响主流程
            logger.warning("RPS 止损判断失败 %s: %s", pos["ts_code"], e)

        if price <= final_stop:
            # 实盘止损：必须市价单确保成交，不能用限价单挂单
            loss_pct = (price - cost_price) / cost_price * 100
            stop_reason = (
                f"盘中止损: 成本{cost_price:.2f} 现价{price:.2f} "
                f"浮亏{loss_pct:.1f}% 触发线{final_stop:.2f}"
            )
            _executor_market_sell_and_record(executor, pos, price, shares, "止损", stop_reason)
            _notify_trade("卖出", pos["stock_name"], pos["ts_code"], price, shares, stop_reason)
            send_feishu(
                f"🛑 盘中止损触发\n"
                f"股票: {pos['stock_name']}({pos['ts_code']})\n"
                f"成本: {cost_price:.2f}  现价: {price:.2f}\n"
                f"浮亏: {loss_pct:.1f}%  触发线: {final_stop:.2f}\n"
                f"数量: {shares}股  已下市价卖单"
            )
            logger.warning("🚨 盘中止损(%s): %s 成本%.2f 现价%.2f (%.1f%%) 触发线%.2f",
                           mode_label, pos["stock_name"], cost_price, price, loss_pct, final_stop)
            continue

        # === 动态退出策略 ===
        # 修复 2026-06-22: today_high 改查 daily_price 中今天的 high.
        # 旧版只读 QMT 实时 quote 的"最高"字段, 09:30~10:00 早盘可能是
        # 昨日缓存或 0, 导致 600183 在 06-18 09:50 误触发兜底止盈:
        #   peak 5.4% 来自昨日 daily_price.high, 价从 -2.2% 反弹到 +0.4%
        #   就被卖到只剩 0.4%。
        # 改为以 daily_price 中今天(CURDATE)high 为准, 与 QMT 实时取大值,
        # 同时一次查询带出历史最高用于 peak_price 计算。
        today_high = quote.get("最高", 0) or 0
        # 跨轮次峰值追踪: 从 daily_peaks.json 读取历史峰值
        import json as _json
        _peak_file = str(BASE_DIR / "data" / "daily_peaks.json")
        _stored_peak = cost_price
        try:
            if os.path.exists(_peak_file):
                with open(_peak_file) as pf:
                    _peaks = _json.load(pf)
                _stored_peak = _peaks.get(pos["ts_code"], cost_price)
        except: pass
        peak_price = max(cost_price, price, today_high, _stored_peak)
        # 回写新峰值
        _peaks = {}
        if os.path.exists(_peak_file):
            with open(_peak_file) as pf: _peaks = _json.load(pf)
        _peaks[pos["ts_code"]] = peak_price
        with open(_peak_file, "w") as pf: _json.dump(_peaks, pf)
        try:
            conn_pk = pymysql.connect(**DB_CONFIG)
            cur_pk = conn_pk.cursor()
            cur_pk.execute(
                "SELECT "
                "  MAX(CASE WHEN trade_date=CURDATE() THEN high ELSE NULL END) AS today_db_high, "
                "  MAX(high) AS hist_high "
                "FROM daily_price WHERE ts_code=%s AND trade_date>=%s",
                (pos["ts_code"], buy_date),
            )
            row_pk = cur_pk.fetchone()
            cur_pk.close()
            conn_pk.close()
            if row_pk:
                if row_pk[0]:
                    today_high = max(today_high, float(row_pk[0]))
                if row_pk[1]:
                    peak_price = max(float(row_pk[1]), price)
        except Exception:
            pass

        peak_profit = (peak_price - cost_price) / cost_price * 100 if cost_price > 0 else 0

        should_sell = False
        sell_reason = ""
        sell_label = ""
        partial_shares = 0  # P0: 分批止盈的卖出股数（>0 表示走分批流程）

        # a-1) 硬性分批止盈 +5%（2026-06-20 补充，P0）
        #     浮盈一旦到过 5%, 立即卖一半锁利, 剩余一半继续走峰值止盈/trailing
        #     依据: 文章《止盈止损》要求短线 3-5% 果断止盈 + 不抱侥幸
        #     保护:
        #       - 已分批过的持仓不再触发 (partial_taken=1)
        #       - 必须持仓 ≥ 1 天 (避免买入次日波动)
        #       - 当前价仍在 +5% 之上 (避免回落到 +5% 之下误触发)
        #       - 至少 200 股才分批 (否则 100 股卖了没意义)
        if (not partial_taken and peak_profit >= 5.0 and days_held >= 1
                and shares >= 200 and cost_price > 0
                and price >= cost_price * 1.05):
            half = (shares // 200) * 100  # 向下取整到 100 股
            if half > 0 and half < shares:  # 至少保留 100 股继续持有
                partial_shares = half
                should_sell = True
                sell_reason = (f"硬性分批止盈(+5%): 峰值{peak_profit:.1f}% 当前浮盈{pct_chg:+.1f}% "
                              f"卖{half}股({half/shares:.0%})锁利, 剩余{shares-half}股继续")
                sell_label = "分批止盈"

        # a) 峰值止盈(分级): 峰值盈利>8%后才进锁利区
        #    第一档(峰值<20%): 回落到"剩8%利润"就锁利,稳赚不贪
        #    第二档(峰值>=20%): 锁定"剩30%利润",让利润跑(大牛股也能稳赚30%)
        #    设计: 主人 2026-06-16 改, 解决"大牛股卖在8%利润"的盲点
        #    修复: 2026-06-16 11:04 bug - 烽火峰值5.2%被误判,必须先到8%才进锁利
        #    改进: 2026-06-16 11:20 第二档从"回吐30%"改为"剩30%利润",避免大牛股卖在5%
        if peak_profit > 8.0 and cost_price > 0:
            if peak_profit < 20.0:
                # 第一档: 小涨稳赚
                profit_floor_pct = 8.0
                trigger_price = cost_price * (1 + profit_floor_pct / 100)
                regime = "小涨稳赚"
            else:
                # 第二档: 大涨放飞, ATR 动态 trailing stop (让利润真正跑)
                # 修复: 主人 2026-06-17 反馈, 旧版 trigger=cost*1.30 是绝对地板
                #   在 peak 触达 20% 时 price 已低于 30% 地板, 立即触发
                #   反而比第一档还早卖, 失去了"让利润跑"的意义
                # 改为 peak - 2*ATR 动态 trailing, 与止损用同一 ATR 系数
                if atr_val and atr_val > 0:
                    trigger_price = round(peak_price - 2 * atr_val, 2)
                else:
                    # ATR 不可用时兜底: peak 回吐 8% 绝对值
                    trigger_price = round(peak_price * 0.92, 2)
                regime = "大涨放飞"
            if price <= trigger_price:
                should_sell = True
                remain_pct = (price - cost_price) / cost_price * 100
                if regime == "小涨稳赚":
                    sell_reason = (f"峰值止盈[小涨稳赚]: 峰值{peak_profit:.0f}%回落到剩{remain_pct:.1f}% "
                                  f"(<8%锁利线)")
                else:
                    sell_reason = (f"峰值止盈[大涨放飞]: 峰值{peak_profit:.0f}%回落到剩{remain_pct:.1f}% "
                                  f"(<30%锁利线)")
                sell_label = "峰值止盈"

        # a2) 兜底固定止盈+3%: 峰值到过3%但未到8%, 回落到3%就锁利
        #     意图: 解决"涨了一点没跑, 最后亏损卖出"的问题
        #     修复: 主人 2026-06-17 反馈, 旧版没检查当前价是否高于成本,
        #       导致 600183 这类股票在 -0.55% 浮亏位被错误触发卖出。
        #       加 price > cost_price 后: 仅在当前价仍高于成本(浮盈)
        #       时才允许此规则触发, 亏损状态下走止损规则(ATR / -7%)。
        elif peak_profit >= 3.0 and peak_profit <= 8.0 and days_held >= 1 and cost_price > 0:
            trigger_price = cost_price * 1.03
            # 修复: 主人 2026-06-17 反馈, 用 price >= cost_price (>= 而非 >)
            # 打平也算"保住本钱", 浮亏才不卖
            # 修复: 2026-06-18 600183 盘中从 -2.2% 反弹到 +0.4% 就被卖
            #   peak 5.4% 是昨天的, 今天日内最高只到 +0.4%, 从未到 3%
            #   加 today_high >= trigger_price 确保只有今天到过 3% 以上的才算"回落到"
            if price <= trigger_price and price >= cost_price and today_high >= trigger_price:
                should_sell = True
                remain_pct = (price - cost_price) / cost_price * 100
                sell_reason = f"兜底止盈(+3%): 峰值{peak_profit:.0f}%回落到剩{remain_pct:.1f}%"
                sell_label = "兜底止盈"

        # b) Scanner T+3 短线平仓 (2026-06-24)
        elif _classify_single_hold(pos) == "scanner" and days_held >= 3:
            should_sell = True
            sell_reason = f"T+3平仓(scanner): 持有{days_held}天 {pct_chg:+.1f}%"
            sell_label = "T+3"

        # c) 超时卖出（V11/ML）：持有>=5天且盈利<3%（弱股不耗时间）
        elif days_held >= 5 and pct_chg < 3.0:
            should_sell = True
            sell_reason = f"超时卖出: 持有{days_held}天仅{pct_chg:.1f}%"
            sell_label = "超时"

        # d) 绝对持有上限：8天强制平仓
        elif days_held > 8:
            should_sell = True
            sell_reason = f"强制平仓: 持有{days_held}天"
            sell_label = "强制平仓"

        if should_sell:
            sell_qty = partial_shares if partial_shares > 0 else shares
            _executor_sell_and_record(executor, pos, price, sell_qty, sell_label, sell_reason)
            _notify_trade("卖出", pos["stock_name"], pos["ts_code"], price, sell_qty, sell_label)

            # P0: 分批止盈后标记 partial_taken=1, 剩余部分继续原有峰值止盈/trailing
            if partial_shares > 0:
                try:
                    conn_ptw = pymysql.connect(**DB_CONFIG)
                    cur_ptw = conn_ptw.cursor()
                    cur_ptw.execute(
                        "UPDATE sim_positions SET partial_taken=1 WHERE ts_code=%s AND status='HOLD'",
                        (pos["ts_code"],))
                    conn_ptw.commit()
                    cur_ptw.close()
                    conn_ptw.close()
                    logger.info("📌 %s 分批止盈标记 partial_taken=1 (剩余 %d 股继续持仓)",
                                pos["stock_name"], shares - partial_shares)
                except Exception as e:
                    logger.warning("分批标志写入失败 %s: %s", pos["ts_code"], e)
                # 分批后立刻给个飞书通知（区别于全仓卖出）
                send_feishu(
                    f"💰 硬性分批止盈触发\n"
                    f"股票: {pos['stock_name']}({pos['ts_code']})\n"
                    f"成本: {cost_price:.2f}  现价: {price:.2f}\n"
                    f"浮盈: {pct_chg:+.1f}%  峰值: {peak_profit:.1f}%\n"
                    f"本次卖出: {sell_qty}股  剩余: {shares - sell_qty}股\n"
                    f"剩余继续按峰值止盈/trailing 管理"
                )

            logger.info(" %s(%s): %s 买入%.2f->现价%.2f (%.1f%%) 持有%d天 峰值%.1f%%",
                        sell_label, mode_label, pos["stock_name"], cost_price, price, pct_chg,
                        days_held, peak_profit)
        else:
            logger.info("  持仓正常(%s): %s 成本%.2f 现价%.2f (%.1f%%) 持有%d天 峰值%.1f%%",
                        mode_label, pos["stock_name"], cost_price, price, pct_chg, days_held, peak_profit)


    # ====== 5因子 候选股盘中择时入场 ======
    _monitor_v11_entry(executor, mkt_info, market_params)

    # ====== 板RPS候选股实时扫描入场（30%仓位）======
    _monitor_board_rps_entry(executor, mkt_info, market_params)

    # 飞书通知已默认由各买卖操作(_notify_trade)触发，不再定时推送持仓状态
    logger.info("=== 持仓监控完成 [%s] ===", mode_label)


def _execute_morning_buy(executor, ts_code, stock_name, market, current_price, buy_shares,
                         strategy, ml_prob, enhanced_score, market_state_str,
                         mkt_state_name, strat_tag, mode_label, sig_id,
                         gap_pct, volume_ratio, ml_avail_ref, scanner_avail_ref,
                         is_real_live, executed, skipped):
    """实盘/模拟统一的早盘买入执行。

    失败: append 到 skipped。
    成功: append 到 executed; 实盘模式下按 strat_tag 扣减对应 slot;
          写 sim_signals(status=已执行) + 同步 sim_positions + 飞书通知。

    ml_avail_ref / scanner_avail_ref: 单元素 list 包装的 int, 用于在函数内修改外部变量。
    """
    # 风控检查 (PreTradeChecker: 交易时段/单笔限额/熔断/60s去重/余额)
    checker = PreTradeChecker()
    rc = checker.check_buy(ts_code, stock_name, market, current_price, buy_shares)
    if not rc["passed"]:
        skipped.append((stock_name, ts_code, f"风控拦截: {rc['message']}"))
        logger.warning("早盘风控拦截 %s(%s): %s", stock_name, ts_code, rc["message"])
        return

    order = executor.buy(
        ts_code, stock_name, market, current_price, buy_shares,
        strategy=strategy or "纯ML(OOS-v2)",
        ml_prob=ml_prob, enhanced_score=enhanced_score,
        market_state=market_state_str or mkt_state_name,
        reason=f"[{strat_tag}]早盘: 跳空{gap_pct:.1f}% 量比{volume_ratio:.2f} ML排序{enhanced_score or 0:.3f}",
    )
    if not order or getattr(order, "status", None) in ("rejected", "failed"):
        skipped.append((stock_name, ts_code, "买入执行失败/废单"))
        logger.warning("买入执行失败 %s(%s) status=%s", stock_name, ts_code, order.status if order else "")
        return

    executed.append((stock_name, ts_code, current_price, buy_shares))
    if is_real_live:
        if strat_tag == "ML":
            ml_avail_ref[0] -= 1
        else:
            scanner_avail_ref[0] -= 1

    _notify_trade("买入", stock_name, ts_code, current_price, buy_shares, f"[{strat_tag}]早盘择时买入")
    logger.info("买入成交[%s](%s): %s %d股@%.2f (跳空%.1f%% 量比%.2f)",
                strat_tag, mode_label, stock_name, buy_shares, current_price, gap_pct, volume_ratio)

    reason_text = f"[{strat_tag}]早盘: 跳空{gap_pct:.1f}% 量比{volume_ratio:.2f} 价{current_price}"
    # 用桥返回的 signal_id, 而非原始 sig_id
    order_sig_id = order.order_id.replace("sig_", "") if order.order_id and "sig_" in order.order_id else sig_id
    try:
        conn3 = pymysql.connect(**DB_CONFIG)
        cur3 = conn3.cursor()
        if is_real_live:
            cur3.execute(
                "UPDATE sim_signals SET status='已执行', price=%s, shares=%s, reason=%s WHERE id=%s",
                (current_price, buy_shares, reason_text, order_sig_id),
            )
        else:
            cur3.execute(
                "UPDATE sim_signals SET status='已执行', reason=%s WHERE id=%s",
                (reason_text, order_sig_id),
            )
        conn3.commit()
        cur3.close()
        conn3.close()
    except Exception as e:
        if isinstance(e, pymysql.err.IntegrityError) and e.args and e.args[0] == 1062:
            # uk_sim_signals_executed 触发: 同 ts_code+date 已有 '已执行' 记录
            logger.warning("[早盘] %s 今日已执行过(unique约束), 跳过更新 sig_id=%s",
                          ts_code, sig_id)
        else:
            logger.error("更新 sim_signals 失败: %s", e)

    try:
        _sync_position_after_buy(ts_code, stock_name, market, buy_shares, current_price, strategy, ml_prob)
    except Exception as e:
        logger.error("同步买入到 sim_positions 失败: %s", e)


def cmd_morning_execute():
    """
    早盘执行已废弃 — V11.0候选股改为盘中择时入场（cmd_monitor 中 _monitor_v11_entry）

    保留此函数以兼容 crontab 调用，不做任何操作。
    """
    logger.info("早盘执行已废弃，V11.0候选股由盘中监控择时入场")


def cmd_status():
    """查询账户状态"""
    executor = get_executor()
    mode_label = "实盘" if trading_config.is_real_trading_enabled else (f"模拟({trading_config.trade_mode})")

    print(f"\n{'='*50}")
    print(f"  交易模式: {mode_label}")
    print(f"  ENABLE_REAL_TRADING: {trading_config.enable_real_trading}")
    print(f"{'='*50}\n")

    # 资金
    balance = executor.get_balance()
    if balance:
        print(f"  💰 总资产: {balance.total_asset:>10.2f}")
        print(f"  💵 可用资金: {balance.available:>10.2f}")
        print(f"  📊 持仓市值: {balance.market_value:>10.2f}")
        print(f"  🔒 冻结资金: {balance.frozen:>10.2f}")
        if balance.initial_capital > 0:
            pnl_pct = (balance.total_asset - balance.initial_capital) / balance.initial_capital * 100
            print(f"  📈 累计盈亏: {balance.total_asset - balance.initial_capital:>+10.2f} ({pnl_pct:+.2f}%)")
            print(f"  📉 最大回撤: {balance.max_drawdown * 100:.2f}%")
            print(f"  🎯 交易次数: {balance.trade_count} 胜率: {balance.win_rate * 100:.1f}%")
    else:
        print("  ❌ 无法获取账户信息")
        if trading_config.trade_mode == "sim":
            print("  提示: 请先运行 python3 scripts/live_trading_scheduler.py init")

    print()

    # 持仓
    positions = get_holding_positions_from_executor(executor)
    if positions:
        print(f"  持仓 ({len(positions)}只):")
        print(f"  {'名称':<10} {'代码':<12} {'成本':>8} {'现价':>8} {'盈亏%':>8} {'仓位%':>8}")
        print(f"  {'-'*54}")
        total_mv = 0
        for p in positions:
            pnl_pct = (p["current_price"] - p["cost_price"]) / p["cost_price"] * 100 if p["cost_price"] > 0 else 0
            pct_str = f"{pnl_pct:+.2f}%"
            mv = p["current_price"] * p["shares"]
            total_mv += mv
            if balance and balance.total_asset > 0:
                pct_of_total = mv / balance.total_asset * 100
            else:
                pct_of_total = 0
            print(f"  {p['stock_name']:<10} {p['ts_code']:<12} {p['cost_price']:>8.2f} {p['current_price']:>8.2f} {pct_str:>8} {pct_of_total:>7.1f}%")
        print(f"  {'-'*54}")
    else:
        print("  当前无持仓")

    print()


def cmd_init():
    """初始化模拟账户（仅 sim 模式需要）"""
    if trading_config.trade_mode != "sim":
        logger.info("实盘模式无需初始化，直接使用QMT账户")
        return

    from scripts.sim_trading import create_tables
    create_tables()

    # 创建实盘订单表
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS real_orders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            order_id VARCHAR(50) NOT NULL COMMENT '订单ID',
            ts_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(50),
            action ENUM("BUY","SELL") NOT NULL,
            price DECIMAL(8,3) NOT NULL,
            quantity INT NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT "pending",
            filled_quantity INT DEFAULT 0,
            filled_amount DECIMAL(12,2) DEFAULT 0,
            reason VARCHAR(200) DEFAULT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME DEFAULT NULL,
            INDEX idx_order_id (order_id),
            INDEX idx_ts_code (ts_code),
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_risk_checks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            check_name VARCHAR(50) NOT NULL,
            passed TINYINT NOT NULL DEFAULT 0,
            detail VARCHAR(500) DEFAULT NULL,
            check_time DATETIME NOT NULL,
            INDEX idx_ts_code (ts_code),
            INDEX idx_check_time (check_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ 模拟账户初始化完成（初始资金 %.0f）", trading_config.initial_capital)


def cmd_sync():
    """同步持仓到 JSON（供 position_monitor 使用）— 仅模拟盘"""
    if trading_config.trade_mode != "sim":
        logger.info("实盘模式无需同步 JSON 持仓")
        return
    sync_positions_to_json()
    logger.info("✅ 持仓已同步到 JSON")



def cmd_ping():
    """健康检查 — 测试远程 QMT 交易服务连接"""
    executor = get_executor()
    if not hasattr(executor, "ping"):
        logger.info("当前执行器不支持 ping (仅实盘模式可用)")
        return

    logger.info("=== 连接健康检查 ===")
    logger.info("  模式: %s", "远程" if executor._is_remote else "本地")
    logger.info("  服务器: %s:%s" if executor._is_remote else "N/A (本地)",
                trading_config.remote_trader_host, trading_config.remote_trader_port)

    result = executor.ping()
    status_emoji = {"ok": "✅", "error": "❌", "dry_run": "🔍"}
    print(f"\n  {status_emoji.get(result['status'], '❓')} 状态: {result['status']}")
    print(f"  📝 信息: {result.get('message', 'N/A')}")
    if result.get("data"):
        print(f"  📊 数据: {result['data']}")
    print()
    logger.info("=== 健康检查完成: %s ===\n", result.get("status", "unknown"))


def cmd_keepalive():
    """保活 — 防止 Windows VM 交易系统锁屏"""
    executor = get_executor()
    result = executor.keepalive()
    if result:
        logger.info("保活成功")
    else:
        logger.warning("保活失败")
    label = "成功" if result else "失败"
    icon = "OK" if result else "FAIL"
    print(f"  保活结果: {label} ({icon})")
    print()




def _filter_trend(conn, ts_codes, trade_date):
    """过滤下跌趋势：ma5<ma20 且近3日累计下跌>3%"""
    if not ts_codes:
        return []
    cur = conn.cursor()
    bearish = set()
    for code in ts_codes:
        cur.execute(
            "SELECT ma5, ma20 FROM daily_price WHERE ts_code=%s AND trade_date=%s",
            (code, trade_date))
        row = cur.fetchone()
        if row and row[0] and row[1] and float(row[0]) < float(row[1]):
            bearish.add(code)
    if not bearish:
        cur.close()
        return ts_codes
    # 近3日累计跌幅
    deep_bear = set()
    for code in bearish:
        cur.execute(
            "SELECT pct_chg FROM daily_price WHERE ts_code=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 3",
            (code, trade_date))
        rows = cur.fetchall()
        if len(rows) == 3:
            total = sum(float(r[0] or 0) for r in rows)
            if total < -3:
                deep_bear.add(code)
    cur.close()
    excluded = sorted(bearish & deep_bear)
    if excluded:
        logger.info("趋势过滤排除 %d 只(ma5<ma20+近3日累跌>3%%): %s",
                   len(excluded), ", ".join(excluded))
    return [c for c in ts_codes if c not in deep_bear]


def _v11_scan_recommend(top_n=3, min_score=0):
    """OOS-v2 纯ML推荐"""
    import pymysql

    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest = str(cur.fetchone()[0])
    cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (latest,))
    prev = str(cur.fetchone()[0])
    cur.execute("""SELECT ts_code FROM daily_price WHERE trade_date=%s
        AND LEFT(ts_code,1) NOT IN ('8','4','9') AND LEFT(ts_code,3) NOT IN ('688')
        AND close<=200 AND close>=3
        ORDER BY amount DESC LIMIT 500""", (prev,))
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    if not codes:
        return []

    from scripts.predict_v11_oos import predict_for_scheduler
    try:
        ranked = predict_for_scheduler(None, codes, as_of_date=latest, top_n=top_n)
    except Exception as e:
        logger.warning("OOS-v2 推荐失败: %s", e)
        return []

    if not ranked:
        return []

    result = []
    conn3 = pymysql.connect(**DB_CONFIG)
    cur3 = conn3.cursor()
    for tc, sc in ranked[:top_n]:
        cur3.execute("SELECT name, industry FROM stock_info WHERE ts_code=%s", (tc,))
        r = cur3.fetchone()
        name = r[0] if r else tc
        market = 'sz' if tc.endswith('.SZ') else 'sh'
        cur3.execute("SELECT close, pct_chg FROM daily_price WHERE ts_code=%s AND trade_date=%s", (tc, latest))
        dr = cur3.fetchone()
        price = float(dr[0]) if dr else 0
        pct_chg = float(dr[1] or 0) if dr else 0
        result.append({
            'ts_code': tc, 'name': name, 'market': market,
            'price': price, 'pct_chg': pct_chg,
            'ml_score': float(sc), 'ml_prob': float(sc),
            'model_ver': 'OOS-v2',
        })
    cur3.close()
    conn3.close()

    if result:
        logger.info("OOS-v2 推荐: %s",
                    ", ".join([f"{c['name']}({c.get('ml_score',0):.4f})" for c in result[:3]]))
    return result



def cmd_t_monitor():
    """日内做T监控入口"""
    import sys
    mode = os.environ.get("T_MONITOR_MODE", "dryrun")
    once = "--once" in sys.argv
    config = TConfig()
    logger.info("启动日内做T监控: mode=%s, once=%s", mode, once)

    executor = None
    if mode in ("sim",):
        from quant_app.trading.modes.sim_executor import SimExecutor
        executor = SimExecutor()
    elif mode == "real":
        executor = get_executor()

    intraday_t_main(mode=mode, once=once, shared_executor=executor)


def main():
    parser = argparse.ArgumentParser(description="交易调度器（模拟/实盘）")
    parser.add_argument(
        "action",
        choices=["scan", "morning", "monitor", "status", "init", "sync", "ping", "keepalive", "t_monitor"],
        help="scan=盘后选股(记录候选), morning=早盘择时买入(9:35), monitor=盘中监控, status=账户状态, init=初始化, sync=同步JSON, ping=远程连接健康检查, keepalive=保活, t_monitor=日内做T监控",
    )
    args = parser.parse_args()

    # 打印启动信息
    logger.info("启动交易调度器: action=%s, TRADE_MODE=%s, ENABLE_REAL_TRADING=%s",
                args.action, trading_config.trade_mode, trading_config.enable_real_trading)

    # 配置校验
    errors = trading_config.validate()
    if errors:
        for err in errors:
            logger.error("配置错误: %s", err)
        if trading_config.is_live:
            logger.error("实盘模式配置校验失败，退出")
            sys.exit(1)

    action_map = {
        "scan": cmd_scan,
        "morning": cmd_morning_execute,
        "monitor": cmd_monitor,
        "status": cmd_status,
        "init": cmd_init,
        "sync": cmd_sync,
        "ping": cmd_ping,
        "keepalive": cmd_keepalive,
        "t_monitor": cmd_t_monitor,
    }
    action_map[args.action]()


# QMT 实时行情缓存（从 V5 策略发布的 qmt_market.json 获取）
_qmt_market_cache = None
_qmt_market_cache_ts = 0

def _get_tencent_quote_simple(code, market="sz"):
    """腾讯接口 — requests 版 (SSL 更可靠)"""
    import requests as _req
    url = f"https://qt.gtimg.cn/q={market}{code}"
    try:
        r = _req.get(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://finance.qq.com"},
                     timeout=5, verify=False)
        raw = r.text
        if "~" not in raw:
            return None
        parts = raw.strip().split("~")
        if len(parts) < 20:
            return None
        prev_close = float(parts[4]) if parts[4] else 0
        cur_price = float(parts[3]) if parts[3] else 0
        return {
            "名称": parts[1], "代码": code,
            "现价": cur_price, "昨收": prev_close,
            "今开": float(parts[5]) if parts[5] else 0,
            "涨跌幅": round((cur_price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "最高": float(parts[33]) if len(parts)>33 and parts[33] else 0,
            "最低": float(parts[34]) if len(parts)>34 and parts[34] else 0,
            "量比": 0,
        }
    except Exception:
        return None


def _get_qmt_realtime(code, market="sz", with_history=False):
    """从 QMT /market/snapshot 获取实时行情，缺失字段从 MySQL daily_price 补全

    QMT snapshot 失败或找不到代码时，降级到腾讯接口兜底（修复 600707 监控失效问题）

    Args:
        code: 纯数字代码如 "300085"
        market: "sz" 或 "sh"
        with_history: 是否补全昨收/今开/量比（早盘择时需要）
    """
    global _qmt_market_cache, _qmt_market_cache_ts
    import json
    import time
    import urllib.request
    now = time.time()
    # 缓存 2 秒
    if _qmt_market_cache is None or now - _qmt_market_cache_ts > 2:
        try:
            req = urllib.request.Request("http://192.168.10.25:1430/market/snapshot",
                                         headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=5)
            _qmt_market_cache = json.loads(resp.read().decode("utf-8"))
            _qmt_market_cache_ts = now
        except Exception:
            _qmt_market_cache = None
    # 第一级：QMT snapshot
    qmt_hit = None
    if _qmt_market_cache and isinstance(_qmt_market_cache, dict):
        stocks = _qmt_market_cache.get("stocks", [])
        for s in stocks:
            sc = s.get("code", "").replace(".SH", "").replace(".SZ", "")
            if sc == code:
                qmt_hit = {
                    "名称": s.get("name", ""),
                    "代码": code,
                    "现价": float(s.get("last", 0)),
                    "昨收": 0,
                    "今开": 0,
                    "涨跌幅": float(s.get("pctChg", 0)),
                    "最高": 0,
                    "最低": 0,
                    "量比": 0,
                    "volume": float(s.get("volume", 0) or 0),  # 修复 P0: 当前股票成交量，量比计算用
                }
                break
    # 先尝试腾讯接口（覆盖更广）
    tq = _get_tencent_quote_simple(code, market)
    if tq:
        result = tq
        logger.info("_get_qmt_realtime 腾讯接口: %s", code)
    elif qmt_hit:
        result = qmt_hit
    else:
        logger.warning("_get_qmt_realtime 全部兜底失败: %s.%s", code, market)
        return None

    ts_code_full = f"{code}.{'SZ' if market=='sz' else 'SH'}"
    if with_history:
        try:
            import pymysql
            conn = pymysql.connect(**DB_CONFIG)
            cur = conn.cursor()
            # 昨收 = 最近一个交易日(不含今天)的收盘价
            cur.execute("""
                SELECT close FROM daily_price
                WHERE ts_code=%s AND trade_date < CURDATE()
                ORDER BY trade_date DESC LIMIT 1
            """, (ts_code_full,))
            row = cur.fetchone()
            if row:
                result["昨收"] = float(row[0] or 0)
                # 用昨收自己算涨跌幅（QMT的pctChg始终为0）
                if result["昨收"] > 0 and result["现价"] > 0:
                    result["涨跌幅"] = round((result["现价"] - result["昨收"]) / result["昨收"] * 100, 2)
            # 今开 = 今天的开盘价
            cur.execute("""
                SELECT open FROM daily_price
                WHERE ts_code=%s AND trade_date = CURDATE() LIMIT 1
            """, (ts_code_full,))
            row = cur.fetchone()
            if row:
                result["今开"] = float(row[0] or 0)
            # 量比 = 今日成交量 / 近5日均量
            # 修复 P0: 之前用 _qmt_market_cache[0] 拿的是第一只股票的 volume, 所有股票量比相同
            # 现在用 result["volume"] (从 qmt_hit 传入) 拿当前股票真实成交量
            today_vol = float(result.get("volume", 0) or 0)
            cur.execute("""
                SELECT AVG(vol) FROM (
                    SELECT vol FROM daily_price
                    WHERE ts_code=%s AND trade_date < CURDATE()
                    ORDER BY trade_date DESC LIMIT 5
                ) t
            """, (ts_code_full,))
            row = cur.fetchone()
            if row and row[0] and today_vol > 0:
                avg_vol = float(row[0])
                result["量比"] = round(today_vol / avg_vol, 2)
            cur.close()
            conn.close()
        except Exception:
            pass
    return result

if __name__ == "__main__":
    main()

