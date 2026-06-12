#!/usr/bin/env python3
"""
实盘交易调度器 — QMT实盘交易
用法: python3 scripts/live_trading_scheduler.py scan|morning|monitor|status|ping
"""

import argparse
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

import pymysql

from quant_app.services.notification_service import send_feishu
from quant_app.trading.config import trading_config
from quant_app.trading.executor import create_executor
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

from scripts.sim_trading import _count_trading_days_since, record_signal, sync_positions_to_json
from scripts.sim_trading import get_market_params as _get_market_params


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
                return (today - last_date).days <= 3
            return False
        cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date=%s", (today.strftime("%Y%m%d"),))
        return cur.fetchone()[0] > 100
    except: return today.weekday() < 5
    finally:
        try: cur.close(); conn.close()
        except: pass

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

def _executor_sell_and_record(executor, pos, price, shares, label, reason):
    try:
        executor.sell(pos.get("position_id", 0) or 0, pos["ts_code"], price, shares)
        record_signal(label, pos["ts_code"], pos["stock_name"], price, shares,
                     "持仓管理", 0, 0, pos.get("market_state",""), reason, "已平仓")
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
                logger.info("市价卖出成功: %s %d股", pos["stock_name"], shares)
            else:
                # 市价失败降级为限价卖出
                logger.warning("市价卖出失败，降级为限价卖出: %s", pos["stock_name"])
                executor.sell(pid, pos["ts_code"], price, shares)
                record_signal(label, pos["ts_code"], pos["stock_name"], price, shares,
                             "持仓管理", 0, 0, pos.get("market_state",""), reason, "已平仓")
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
        "state": "normal",
    }


def _classify_holds_by_strategy(current_holds):
    """将QMT持仓按策略分类(ML/Scanner/unknown)，综合sim_positions和sim_signals"""
    ml_holds = 0
    scanner_holds = 0
    unknown_holds = 0

    # 从 sim_positions 获取策略标签
    try:
        import pymysql
        conn = pymysql.connect(**DB_CONFIG)
        cur = conn.cursor()
        for h in current_holds:
            ts_code = h["ts_code"]
            cur.execute(
                "SELECT strategy FROM sim_positions WHERE ts_code=%s AND status='HOLD' ORDER BY id DESC LIMIT 1",
                (ts_code,))
            row = cur.fetchone()
            if row and row[0]:
                s = row[0]
                if '扫描' in s or 'Scanner' in s:
                    scanner_holds += 1
                elif 'ML' in s or 'V11' in s:
                    ml_holds += 1
                else:
                    unknown_holds += 1
            else:
                # 从 sim_signals 推断
                cur.execute(
                    "SELECT signal_type FROM sim_signals WHERE ts_code=%s AND status IN ('已执行','部分成交') ORDER BY id DESC LIMIT 1",
                    (ts_code,))
                row2 = cur.fetchone()
                if row2 and row2[0]:
                    if '扫描' in row2[0]:
                        scanner_holds += 1
                    else:
                        ml_holds += 1
                else:
                    unknown_holds += 1
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("策略分类持仓失败: %s", e)
        unknown_holds = len(current_holds)

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

def cmd_scan():
    executor = get_executor()
    mode_label = "实盘" if trading_config.is_live else "模拟"
    logger.info("=== 交易调度器每日扫描开始 [%s] ===", mode_label)

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

    # 全部资金用于 V11.0（已停用实时扫描策略）
    v11_ratio = 1.0
    scanner_ratio = 0.0
    total_cap = 100000
    try:
        from quant_app.services.scanner_strategy import get_v11_capital
        total_cap = get_v11_capital()
    except Exception:
        pass
    logger.info("资金分配: ML V11=100%% | 总资金=%.0f", total_cap)

    # 3. 选股（按策略分别计算可用仓位）
    _mp_buy = _get_market_params()
    max_pos_s = _mp_buy["max_positions"]
    ml_max_s = max(1, round(max_pos_s * v11_ratio)) if v11_ratio > 0 else 0
    scanner_max_s = max(1, round(max_pos_s * scanner_ratio)) if scanner_ratio > 0 else 0
    ml_unknown_s = max(0, int(unknown_held_s * v11_ratio + 0.5))
    scanner_unknown_s = unknown_held_s - ml_unknown_s
    ml_avail_s = max(0, ml_max_s - ml_held_s - ml_unknown_s)
    scanner_avail_s = max(0, scanner_max_s - scanner_held_s - scanner_unknown_s)
    total_avail = ml_avail_s + scanner_avail_s
    if is_market_bear:
        logger.info("大盘逆市，仓位后续在风控阶段处理")

    if total_avail <= 0:
        logger.info("双策略均已满仓(ML:%d/%d Scanner:%d/%d)", ml_held_s + unknown_held_s, ml_max_s, scanner_held_s + unknown_held_s, scanner_max_s)
    elif balance and balance.profit_pct < -0.15:
        logger.warning("回撤断路器: %.1f%%", balance.profit_pct*100)
    elif not balance:
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
            v11_slots = ml_avail_s if v11_ratio > 0 and ml_avail_s > 0 else 0
            v11_budget = (balance.available or total_cap) * v11_ratio if balance and balance.available else 0
            ml_candidates = []
            if v11_slots > 0:
                ml_raw = _factor_scan_recommend(top_n=max(v11_slots, 2))
                for pick in (ml_raw or [])[:v11_slots]:
                    ts_code = pick["ts_code"]
                    name = pick["name"]
                    price = pick.get("price", 0)
                    if price <= 0:
                        continue
                    shares = int(v11_budget / v11_slots / price / 100) * 100 if v11_budget > 0 else 0
                    if shares < 100:
                        continue
                    record_signal("买入候选", ts_code, name, price, shares,
                                f"纯ML({pick.get('model_ver','V11.2')})",
                                pick.get("ml_prob",0), pick.get("ml_score",0),
                                _mp_buy.get("state","常态"),
                                f"V11 {pick.get('model_ver','V11.2')} 排序{pick.get('ml_score',0):.3f}",
                                status="待执行")
                    ml_candidates.append(pick)
                    logger.info("[ML-V11] 候选: %s %.2f ML=%.3f", name, price, pick.get('ml_score',0))

            # ---- 实时扫描已停用，全部资金归 V11.0 ----

            # 汇总飞书通知
            all_candidates = ml_candidates
            if all_candidates and trading_config.is_real_trading_enabled:
                lines = ["明日买入候选 (V11.0)"]
                if ml_candidates:
                    lines.append("-- ML V11 --")
                    for p in ml_candidates:
                        lines.append(f"  {p['name']}({p['ts_code']}) ML={p.get('ml_score',0):.3f}")
                send_feishu("\n".join(lines))

    logger.info("=== 扫描完成 [%s] ML=%d ===", mode_label, len(ml_candidates))

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

    # 加载今日待执行 V11.0 信号
    try:
        conn_sig = pymysql.connect(**DB_CONFIG)
        cur_sig = conn_sig.cursor()
        cur_sig.execute(
            "SELECT id, ts_code, price as rec_price, shares as rec_shares, score "
            "FROM sim_signals "
            "WHERE (strategy LIKE '%%V11%%' OR strategy LIKE '%%因子%%' OR strategy LIKE '%%5因子%%') AND status='待执行' AND DATE(created_at)=CURDATE() "
            "ORDER BY score DESC"
        )
        pending = [dict(zip([d[0] for d in cur_sig.description], row)) for row in cur_sig.fetchall()]
        cur_sig.close()
        conn_sig.close()
    except Exception as e:
        logger.warning("[V11入场] 查询待执行信号失败: %s", e)
        return

    if not pending:
        return

    logger.info("[V11入场] %d只候选股待择时入场", len(pending))

    # 获取当前持仓（去重用）
    current_holds = get_holding_positions_from_executor(executor)
    held_codes = {p["ts_code"] for p in current_holds}

    # 计算可用仓位
    max_pos = market_params.get("max_positions", 3)
    ml_held = sum(1 for p in current_holds
                  if _classify_single_hold(p) == "ML")
    avail_slots = max(0, max_pos - ml_held)
    if avail_slots <= 0:
        logger.info("[V11入场] ML仓位已满 (%d/%d)", ml_held, max_pos)
        return

    # 计算每只预算
    balance = executor.get_balance()
    available_cash = (balance.available or 0) if balance else 0
    if available_cash < 5000:
        logger.info("[V11入场] 可用资金不足 %.0f", available_cash)
        return
    budget_per_slot = available_cash / max(avail_slots, 1)

    bought = 0
    for sig in pending:
        if bought >= avail_slots:
            break

        ts_code = sig["ts_code"]
        if ts_code in held_codes:
            continue

        code = ts_code.split(".")[0]
        market = "SH" if ts_code.startswith("6") else "SZ"

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

        # 3. 非涨停
        limit_up = pre_close * 1.10
        if price >= limit_up * 0.995:
            logger.info("[入场] %s 已涨停，跳过", ts_code)
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
        shares = int(budget_per_slot / price / 100) * 100
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

        order = executor.buy(ts_code, name, market, price, shares, strategy="5因子择时")
        if order is None or getattr(order, "status", None) in ("rejected", "failed"):
            logger.warning("[V11入场] 买入失败: %s", ts_code)
            continue

        # 更新 sim_signals 状态
        try:
            conn_up = pymysql.connect(**DB_CONFIG)
            cur_up = conn_up.cursor()
            cur_up.execute(
                "UPDATE sim_signals SET status='已执行', price=%s, shares=%s, updated_at=NOW() WHERE id=%s",
                (price, shares, sig["id"])
            )
            conn_up.commit()
            cur_up.close(); conn_up.close()
        except Exception:
            pass

        _sync_position_after_buy(ts_code, name, market, shares, price)
        _notify_trade("买入", name, ts_code, price, shares, f"V11.0盘中择时 {reason}")
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
        cur_c.close(); conn_c.close()
        if row and row[0]:
            strategy = row[0] or strategy
    except Exception:
        pass
    if "ML" in strategy or "V11" in strategy:
        return "ML"
    return "other"


def cmd_monitor():
    """
    盘中持仓监控 + V11.0候选股择时入场

    每5分钟运行一次:
    1. 持仓监控 — 检查所有持仓，触发止损/止盈/超时自动卖出
    2. V11.0择时入场 — 监测盘后选出的候选股，满足量价条件时买入
    """
    if not _is_trading_day(for_intraday=True):
        logger.info("今日非交易日, 跳过监控")
        return
    executor = get_executor()
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
            market = pos.get("market", "SH" if pos["ts_code"].startswith("6") else "SZ")
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

        # === 实盘硬性兜底止损线（C3.0 -5%）===
        # 之前依赖 sim_positions.stop_loss，但实盘持仓(QMT同步)经常为 0 → 止损失效
        # 现在用主人规则硬性兜底：成本 × 0.95 必触发，与上面计算的 stop_price 取更低者
        hard_stop = round(cost_price * 0.95, 2)
        final_stop = min(stop_price, hard_stop)
        if final_stop < stop_price:
            logger.info("🛡 硬性兜底止损覆盖: ATR/动态止损 %.2f → C3.0 兜底 %.2f",
                        stop_price, final_stop)

        buy_date = pos.get("buy_date")
        days_held = _count_trading_days_since(buy_date) if buy_date else 0

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
        # 用今日盘中最高价 + 历史最高价 综合计算峰值
        today_high = quote.get("最高", 0) or 0
        peak_price = max(cost_price, price, today_high)
        try:
            conn_pk = pymysql.connect(**DB_CONFIG)
            cur_pk = conn_pk.cursor()
            cur_pk.execute(
                "SELECT MAX(high) FROM daily_price WHERE ts_code=%s AND trade_date>=%s",
                (pos["ts_code"], buy_date),
            )
            row_pk = cur_pk.fetchone()
            cur_pk.close()
            conn_pk.close()
            if row_pk and row_pk[0]:
                peak_price = max(float(row_pk[0]), price)
        except Exception:
            pass

        peak_profit = (peak_price - cost_price) / cost_price * 100 if cost_price > 0 else 0

        should_sell = False
        sell_reason = ""
        sell_label = ""

        # a) ATR移动止盈：峰值盈利>5%后，回落2×ATR即锁利
        if peak_profit > 5.0 and atr_val and atr_val > 0 and cost_price > 0:
            trail_dist = 2.0 * atr_val / cost_price  # ATR回落比例
            trail_min = 0.03  # 最低3%回落兜底
            trail_dist = max(trail_dist, trail_min)
            if price <= peak_price * (1 - trail_dist):
                should_sell = True
                sell_reason = f"ATR移动止盈: 峰值{peak_profit:.0f}%回落{trail_dist*100:.1f}%"
                sell_label = "移动止盈"
        elif peak_profit > 5.0:
            # ATR不可用时降级为固定3.5%
            if price <= peak_price * 0.965:
                should_sell = True
                sell_reason = f"移动止盈: 峰值{peak_profit:.0f}%回落至{pct_chg:.1f}%"
                sell_label = "移动止盈"

        # b) 超时卖出：持有>=5天且盈利<3%（弱股不耗时间）
        elif days_held >= 5 and pct_chg < 3.0:
            should_sell = True
            sell_reason = f"超时卖出: 持有{days_held}天仅{pct_chg:.1f}%"
            sell_label = "超时"

        # c) 绝对持有上限：8天强制平仓
        elif days_held > 8:
            should_sell = True
            sell_reason = f"强制平仓: 持有{days_held}天"
            sell_label = "强制平仓"

        if should_sell:
            _executor_sell_and_record(executor, pos, price, shares, sell_label, sell_reason)
            _notify_trade("卖出", pos["stock_name"], pos["ts_code"], price, shares, sell_label)
            logger.info(" %s(%s): %s 买入%.2f->现价%.2f (%.1f%%) 持有%d天 峰值%.1f%%",
                        sell_label, mode_label, pos["stock_name"], cost_price, price, pct_chg,
                        days_held, peak_profit)
        else:
            logger.info("  持仓正常(%s): %s 成本%.2f 现价%.2f (%.1f%%) 持有%d天 峰值%.1f%%",
                        mode_label, pos["stock_name"], cost_price, price, pct_chg, days_held, peak_profit)


    # ====== 5因子 候选股盘中择时入场 ======
    _monitor_v11_entry(executor, mkt_info, market_params)

    # 飞书通知：持仓状态（每30分钟一次，避免刷屏）
    if trading_config.is_real_trading_enabled:
        import time as _time
        now_ts = int(_time.time())
        last_notify = getattr(cmd_monitor, "_last_feishu_ts", 0)
        if now_ts - last_notify >= 3600:  # 1小时
            cmd_monitor._last_feishu_ts = now_ts
            # 重新获取最新持仓（包含本轮盘中扫描买入的）
            latest_positions = get_holding_positions_from_executor(executor)
            if latest_positions:
                total_mv = sum(float(p.get("market_value", 0)) for p in latest_positions)
                total_pnl = sum(float(p.get("profit_loss", 0)) for p in latest_positions)
                lines_msg = [f"持仓监控 ({len(latest_positions)}只) 总市值{total_mv:.0f}"]
                for p in latest_positions:
                    name = p.get("stock_name", p.get("name", ""))
                    code = p.get("ts_code", "").split(".")[0]
                    qty = int(p.get("shares", 0))
                    cost = float(p.get("cost_price", 0))
                    cur = float(p.get("current_price", 0))
                    pnl = float(p.get("profit_loss", 0))
                    pnl_str = f"{pnl:+.0f}"
                    lines_msg.append(f"  {name}({code}) {qty}股 成本{cost:.2f} 现价{cur:.2f} {pnl_str}")
                send_feishu("\n".join(lines_msg))
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
    order = executor.buy(
        ts_code, stock_name, market, current_price, buy_shares,
        strategy=strategy or "纯ML(OOS-v2)",
        ml_prob=ml_prob, enhanced_score=enhanced_score,
        market_state=market_state_str or mkt_state_name,
        reason=f"[{strat_tag}]早盘: 跳空{gap_pct:.1f}% 量比{volume_ratio:.2f} ML排序{enhanced_score or 0:.3f}",
    )
    if not order:
        skipped.append((stock_name, ts_code, "买入执行失败"))
        logger.warning("买入执行失败 %s(%s)", stock_name, ts_code)
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
    try:
        conn3 = pymysql.connect(**DB_CONFIG)
        cur3 = conn3.cursor()
        if is_real_live:
            cur3.execute(
                "UPDATE sim_signals SET status='已执行', price=%s, shares=%s, reason=%s WHERE id=%s",
                (current_price, buy_shares, reason_text, sig_id),
            )
        else:
            cur3.execute(
                "UPDATE sim_signals SET status='已执行', reason=%s WHERE id=%s",
                (reason_text, sig_id),
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


def main():
    parser = argparse.ArgumentParser(description="交易调度器（模拟/实盘）")
    parser.add_argument(
        "action",
        choices=["scan", "morning", "monitor", "status", "init", "sync", "ping", "keepalive"],
        help="scan=盘后选股(记录候选), morning=早盘择时买入(9:35), monitor=盘中监控, status=账户状态, init=初始化, sync=同步JSON, ping=远程连接健康检查, keepalive=保活",
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
    }
    action_map[args.action]()


# QMT 实时行情缓存（从 V5 策略发布的 qmt_market.json 获取）
_qmt_market_cache = None
_qmt_market_cache_ts = 0

def _get_tencent_quote_simple(code, market="sz"):
    """腾讯接口兜底拉单只行情 — _get_qmt_realtime 失败时用

    返回 dict 包含 名称/代码/现价/昨收/涨跌幅/最高/最低/今开/量比
    与 _get_qmt_realtime 内部结构一致，便于无缝衔接
    """
    import urllib.request
    symbol = f"{market}{code}"
    url = f"https://qt.gtimg.cn/q={symbol}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.qq.com",
        })
        ctx = __import__("ssl").create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = __import__("ssl").CERT_NONE
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            raw = resp.read().decode("gbk")
        if "=" not in raw or "~" not in raw:
            return None
        parts = raw.strip().split("~")
        if len(parts) < 50:
            return None
        prev_close = float(parts[4]) if parts[4] else 0
        cur_price = float(parts[3]) if parts[3] else 0
        return {
            "名称": parts[1],
            "代码": code,
            "现价": cur_price,
            "昨收": prev_close,
            "今开": float(parts[5]) if parts[5] else 0,
            "涨跌幅": round((cur_price - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "最高": float(parts[33]) if parts[33] else 0,
            "最低": float(parts[34]) if parts[34] else 0,
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
                }
                break
    if qmt_hit:
        result = qmt_hit
    else:
        # 第二级：腾讯接口兜底（修复 600707 等不在 QMT 自选池里的持仓）
        tq = _get_tencent_quote_simple(code, market)
        if not tq:
            logger.warning("_get_qmt_realtime 全部兜底失败: %s.%s", code, market)
            return None
        result = tq
        logger.info("_get_qmt_realtime QMT snapshot 未命中，降级腾讯接口: %s", code)

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
            today_vol = float(_qmt_market_cache.get("stocks", [{}])[0].get("volume", 0)) if _qmt_market_cache else 0
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


