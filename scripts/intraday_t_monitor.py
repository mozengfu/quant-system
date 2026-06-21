#!/usr/bin/env python3
"""
日内做T监控进程 — 实时信号评估 + 执行
=====================================

运行方式:
  python3 scripts/intraday_t_monitor.py                    # 实盘监控 (9:35-11:30 / 13:00-14:50)
  python3 scripts/intraday_t_monitor.py --mode sim         # 模拟盘
  python3 scripts/intraday_t_monitor.py --mode dryrun      # 只打日志, 不执行

定时任务 (crontab):
  */1 9-11 * * 1-5 /usr/bin/python3 /path/scripts/intraday_t_monitor.py --mode sim
  */1 13-14 * * 1-5 /usr/bin/python3 /path/scripts/intraday_t_monitor.py --mode sim

信号逻辑:
  1. 加载所有底仓 (排除 T 仓位)
  2. 逐只拉实时行情
  3. 先检查: 是否有当日已开 T 仓位 → 判断 buy_back / force_close
  4. 无 T 仓位 → 判断 sell_high
  5. 所有操作先过风控 (R1-R9)
  6. 执行 (dryrun 只打日志)
"""

import logging
import os
import sys
import time
from datetime import datetime, time as dtime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "logs", "intraday_t.log"
        ))
    ]
)


from quant_app.trading.modes.sim_executor import SimExecutor
from quant_app.trading.config import trading_config
from quant_app.services.notification_service import send_feishu

from scripts.intraday_t_strategy import (
    IntradayState, TConfig,
    load_base_positions, load_open_t_positions, log_t_action,
    count_t_actions_today, get_realtime_quote, get_hs300_quote, calc_t_shares,
    should_sell_high, should_buy_back, check_risk, is_blocked_subject, DEFAULTS
)
from scripts.sim_trading import get_market_params

logger = logging.getLogger("intraday_t_monitor")

# 内存熔断: ts_code -> bool, 当日跳过该股
_skip_rest_of_day: dict[str, bool] = {}


def is_trading_window() -> bool:
    """当前是否在T策略的交易时段"""
    now = datetime.now()
    # 非交易日
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    t = h * 60 + m
    return (9 * 60 + 35) <= t <= (11 * 60 + 30) or (13 * 60) <= t <= (14 * 60 + 50)


def is_force_close_window() -> bool:
    """14:40-14:50 强制还原窗口"""
    now = datetime.now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    return t >= (14 * 60 + 40) and t <= (14 * 60 + 50)


def resolve_market(ts_code: str) -> str:
    """ts_code -> 'sh' / 'sz' / 'bj'"""
    code = ts_code.split(".")[0]
    if code.startswith(("60", "688")):
        return "sh"
    elif code.startswith(("8", "4")):
        return "bj"
    return "sz"


def build_state_from_position(pos: dict, quote: dict) -> IntradayState:
    return IntradayState(
        ts_code=pos["ts_code"],
        base_position_id=pos["id"],
        base_shares=int(pos["shares"]),
        cost_price=float(pos["cost_price"]),
        stop_loss=float(pos.get("stop_loss", 0) or 0),
        take_profit=float(pos.get("take_profit", 0) or 0),
        name=pos.get("stock_name", ""),
        market=pos.get("market", ""),
        cur_price=quote["price"],
        prev_close=quote["prev_close"],
        open_price=quote.get("open", 0),
        high=quote.get("high", 0),
        low=quote.get("low", 0),
        volume=quote.get("volume", 0),
        amount=quote.get("amount", 0),
        cum_volume=quote.get("volume", 0),
        cum_amount=quote.get("amount", 0),
    )


def run_on_positions(executor, cfg: TConfig, mode: str):
    """核心循环: 遍历底仓, 评估信号, 执行 T"""
    hs300 = get_hs300_quote()
    hs300_change = round(hs300["change_pct"], 2) if hs300 else None

    # 1. 加载底仓
    positions = load_base_positions()
    if not positions:
        logger.info("no base positions, skip")
        return

    # 2. 加载当日已开 T 仓位 (用于 buy_back/force_close 判断)
    t_positions_map = {}
    for tp in load_open_t_positions():
        t_positions_map[tp["ts_code"]] = tp

    for pos in positions:
        ts_code = pos["ts_code"]
        market = resolve_market(ts_code)

        # 内存熔断
        if _skip_rest_of_day.get(ts_code, False):
            continue

        # 标的过滤
        name = pos.get("stock_name", "")
        if is_blocked_subject(name, ts_code):
            continue

        # 当日T次数
        t_count_today = count_t_actions_today(ts_code)

        # 拉行情
        quote = get_realtime_quote(ts_code.split(".")[0], market)
        if not quote or quote["price"] <= 0:
            logger.debug("skip %s: no quote", ts_code)
            continue

        state = build_state_from_position(pos, quote)
        state.t_action_count = t_count_today

        # 检查 T 仓位状态
        tp = t_positions_map.get(ts_code)
        if tp:
            state.t_position_id = tp["id"]
            state.t_open_price = float(tp["cost_price"])
            state.t_open_shares = int(tp["shares"])

        # --- 强制还原 (force_close) ---
        if is_force_close_window() and state.t_position_id is not None:
            try:
                shares = state.t_open_shares
                price = quote["price"]
                pnl = (price - state.t_open_price) * shares if state.t_open_price else 0
                pnl_pct = (price - state.t_open_price) / state.t_open_price * 100 if state.t_open_price else 0
                if mode != "dryrun":
                    order = executor.sell(state.t_position_id, ts_code, price, shares,
                                          reason="intraday_t_force_close")
                    if order and order.status != "rejected":
                        log_t_action(
                            direction="force_close",
                            ts_code=ts_code, stock_name=name,
                            base_position_id=pos["id"],
                            t_position_id=state.t_position_id,
                            shares=shares, price=price,
                            vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                            intraday_pct=state.intraday_pct, target_pct=None,
                            pnl=pnl, pnl_pct=pnl_pct,
                            reason="force close T position",
                            status="filled", executor_mode=mode,
                        )
                        logger.info("force_close: %s %d shares @ %.3f (pnl=%.2f)", name, shares, price, pnl)
                else:
                    log_t_action(
                        direction="force_close", ts_code=ts_code, stock_name=name,
                        base_position_id=pos["id"],
                        t_position_id=state.t_position_id,
                        shares=shares, price=price,
                        vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                        intraday_pct=state.intraday_pct, target_pct=None,
                        pnl=0, pnl_pct=0, reason="dryrun force close",
                        status="filled", executor_mode=mode,
                    )
                    logger.info("[DRYRUN] force_close: %s %d shares @ %.3f", name, shares, price)
                state.t_action_count += 1
            except Exception as e:
                logger.error("force_close failed %s: %s", ts_code, e)
            continue

        # --- 风控 ---
        risk_passed, risk_reason = check_risk(state, cfg, hs300_change)
        if not risk_passed:
            # 防抖熔断
            state.t_skip_streak += 1
            if state.t_skip_streak >= cfg.skip_debounce:
                _skip_rest_of_day[ts_code] = True
                logger.warning("debounce meltdown for %s (skip streak %d)", ts_code, state.t_skip_streak)
            logger.debug("risk block %s: %s", ts_code, risk_reason)
            continue
        else:
            state.t_skip_streak = 0  # 重置防抖

        # --- 有 T 仓位: 判断低吸买回 ---
        if state.t_position_id is not None and state.t_action_count < cfg.max_t_per_day:
            signal, reason = should_buy_back(state, cfg)
            if signal:
                t_shares = calc_t_shares(state.base_shares, cfg.t_ratio)
                if t_shares < cfg.t_shares_min:
                    logger.debug("buy_back skip %s: shares %d < min %d", ts_code, t_shares, cfg.t_shares_min)
                    continue
                price = quote["price"]
                pnl = (state.t_open_price - price) * t_shares if state.t_open_price else 0
                pnl_pct = (state.t_open_price - price) / state.t_open_price * 100 if state.t_open_price else 0
                if mode != "dryrun":
                    try:
                        order = executor.buy(
                            ts_code=ts_code, name=name, market=market,
                            price=price, quantity=t_shares,
                            strategy="intraday_t_buyback",
                            reason="intraday_t_buyback",
                        )
                        if order and order.status not in ("rejected",):
                            # 卖 T 仓 (还原底仓)
                            executor.sell(state.t_position_id, ts_code, price, t_shares,
                                          reason="intraday_t_revert")
                            log_t_action(
                                direction="buy_back", ts_code=ts_code, stock_name=name,
                                base_position_id=pos["id"],
                                t_position_id=state.t_position_id,
                                shares=t_shares, price=price,
                                vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                                intraday_pct=state.intraday_pct, target_pct=0.3,
                                pnl=pnl, pnl_pct=pnl_pct,
                                reason=f"T buy back success, pnl={pnl:.2f}",
                                status="filled", executor_mode=mode,
                            )
                            logger.info("T buy_back done: %s %d shares @ %.3f (pnl=%.2f)", name, t_shares, price, pnl)
                    except Exception as e:
                        logger.error("buy_back exec failed %s: %s", ts_code, e)
                else:
                    log_t_action(
                        direction="buy_back", ts_code=ts_code, stock_name=name,
                        base_position_id=pos["id"],
                        t_position_id=state.t_position_id,
                        shares=t_shares, price=price,
                        vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                        intraday_pct=state.intraday_pct, target_pct=0.3,
                        pnl=0, pnl_pct=0, reason="dryrun buy back",
                        status="filled", executor_mode=mode,
                    )
                    logger.info("[DRYRUN] T buy_back: %s %d shares @ %.3f", name, t_shares, price)
                state.t_action_count += 1
            else:
                logger.debug("no buy_back signal %s: %s", ts_code, reason)
            continue

        # --- 无 T 仓位: 判断高抛卖 ---
        if state.t_position_id is None and state.t_action_count < cfg.max_t_per_day:
            signal, reason = should_sell_high(state, cfg)
            if not signal:
                logger.debug("no sell_high signal %s: %s", ts_code, reason)
                continue
            t_shares = calc_t_shares(state.base_shares, cfg.t_ratio)
            if t_shares < cfg.t_shares_min:
                logger.debug("sell_high skip %s: shares %d < min %d", ts_code, t_shares, cfg.t_shares_min)
                continue
            # 底仓剩余检查
            if state.base_shares - t_shares < 100:
                logger.debug("sell_high skip %s: remaining base %d < 100", ts_code, state.base_shares - t_shares)
                continue
            price = quote["price"]
            if mode != "dryrun":
                try:
                    order = executor.partial_sell(
                        position_id=pos["id"], ts_code=ts_code, price=price,
                        quantity=t_shares, reason="intraday_t_sell_high",
                    )
                    if order and order.status not in ("rejected",):
                        log_t_action(
                            direction="sell_high", ts_code=ts_code, stock_name=name,
                            base_position_id=pos["id"],
                            t_position_id=None,
                            shares=t_shares, price=price,
                            vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                            intraday_pct=state.intraday_pct, target_pct=0.3,
                            pnl=0, pnl_pct=0,
                            reason="intraday T sell high",
                            status="filled", executor_mode=mode,
                        )
                        logger.info("sell_high: %s %d shares @ %.3f", name, t_shares, price)
                except Exception as e:
                    logger.error("sell_high exec failed %s: %s", ts_code, e)
            else:
                log_t_action(
                    direction="sell_high", ts_code=ts_code, stock_name=name,
                    base_position_id=pos["id"],
                    t_position_id=None,
                    shares=t_shares, price=price,
                    vwap=state.vwap, pct_from_vwap=state.pct_from_vwap,
                    intraday_pct=state.intraday_pct, target_pct=0.3,
                    pnl=0, pnl_pct=0, reason="dryrun sell high",
                    status="filled", executor_mode=mode,
                )
                logger.info("[DRYRUN] sell_high: %s %d shares @ %.3f", name, t_shares, price)
            state.t_action_count += 1


def main(mode: str = "dryrun", once: bool = False, shared_executor=None):
    """日内做T监控主入口
    
    Args:
        mode: "sim" | "real" | "dryrun"
        once: True = 单次执行不循环
        shared_executor: 外部传入的 executor (调度器调用时复用)
    """
    cfg = TConfig()
    logger.info("=" * 60)
    logger.info("intraday_t_monitor started, mode=%s, once=%s", mode, once)
    logger.info("config: sell_band=%s buy_band=%s t_ratio=%s max_t_per_day=%s",
                cfg.sell_vwap_band, cfg.buy_vwap_band, cfg.t_ratio, cfg.max_t_per_day)

    executor = shared_executor
    if executor is None:
        executor = SimExecutor() if mode in ("sim", "dryrun") else None

    while True:
        now = datetime.now()
        if now.weekday() >= 5:
            logger.info("weekend, sleep 10m")
            time.sleep(600)
            continue

        # 强制还原: 任何时间只要有未平 T 仓位就执行
        if is_force_close_window() or (not is_trading_window()):
            t_pos = load_open_t_positions()
            if t_pos:
                logger.info("force close window: %d open T positions", len(t_pos))
                run_on_positions(executor, cfg, mode)

            if now.hour >= 15:
                logger.info("market closed, sleep 10m")
                time.sleep(600)
                continue

            time.sleep(30)
            continue

        if not is_trading_window():
            if now.hour < 9:
                time.sleep(120)
            else:
                time.sleep(60)
            continue

        run_on_positions(executor, cfg, mode)

        if once:
            logger.info("once mode, done.")
            break

        time.sleep(30)

    logger.info("intraday_t_monitor stopped")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="日内做T监控")
    parser.add_argument("--mode", choices=["sim", "real", "dryrun"], default="dryrun",
                        help="sim=模拟盘, real=实盘, dryrun=只打日志 (默认)")
    parser.add_argument("--once", action="store_true", help="单次运行, 不循环")
    args = parser.parse_args()
    main(mode=args.mode, once=args.once)
