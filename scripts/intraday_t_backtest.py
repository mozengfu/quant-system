#!/usr/bin/env python3
"""
日内做T回测 — 基于历史分钟线数据
==================================
从新浪财经拉取历史分时图 (1min K 线) 数据, 跑 T 策略回测。
输出: 信号统计、盈亏分析、参数调优。

用法:
    python3 scripts/intraday_t_backtest.py --ts_code 600519.SH --days 10
    python3 scripts/intraday_t_backtest.py --ts_code 600519.SH --days 10 --tune
    python3 scripts/intraday_t_backtest.py --ts_code 600519.SH --days 10 --plot  # 待扩展
"""

import argparse
import json
import math
import sys
import os
import time
import urllib.request
import ssl
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql
from quant_app.utils.config import get_db_config


# ============================================================
# 1. 历史分钟线拉取 (新浪财经)
# ============================================================
def fetch_1min_from_sina(ts_code: str, trade_date: str) -> Optional[list[dict]]:
    """从新浪财经拉指定交易日 1min K 线

    Args:
        ts_code: e.g. "600519.SH"
        trade_date: e.g. "2026-06-18"

    Returns:
        list of {time, price, avg_price(分时均价≈VWAP), volume, turnover}
    """
    code_num = ts_code.split(".")[0]
    market = "sh" if code_num.startswith(("60", "688")) else "sz"
    date_str = trade_date.replace("-", "")

    url = f"https://quotes.money.163.com/cjmx/{date_str}/{market}{code_num}.xls"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quotes.money.163.com",
        })
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read()
        # 解析 Excel 格式 (CSV-like)
        text = raw.decode("gbk")
        lines = text.strip().split("\n")
        if len(lines) < 3:
            return None
        bars = []
        for line in lines[2:]:  # 跳过头2行
            parts = line.strip().split("\t")
            if len(parts) < 7:
                continue
            time_str = parts[0].strip()
            if not time_str or len(time_str) < 5:
                continue
            try:
                price = float(parts[2])
                avg_price = float(parts[5]) if parts[5] else 0  # 分时均价
                volume = float(parts[3]) if parts[3] else 0
                turnover = float(parts[4]) if parts[4] else 0
                bars.append({
                    "time": time_str,
                    "price": price,
                    "avg_price": avg_price,
                    "volume": volume,
                    "turnover": turnover,
                })
            except (ValueError, IndexError):
                continue
        return bars if len(bars) > 10 else None
    except Exception as e:
        return None


def fetch_1min_from_tencent(ts_code: str, trade_date: str) -> Optional[list[dict]]:
    """腾讯5分钟K线 (支持历史回测)
    
    Uses tencent mkline API to get 5min K-lines for history backtesting.
    Returns list of {time, price(=close), vwap(computed), volume, turnover_wan, open, high, low}
    """
    code_num = ts_code.split(".")[0]
    market = "sh" if code_num.startswith(("60", "688")) else "sz"
    date_str = trade_date.replace("-", "")
    date_int = int(date_str)

    # Request enough bars: 5min, up to 1000 bars covers ~83 hours
    url = f"http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={market}{code_num},m5,,1000"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        key = f"{market}{code_num}"
        if key not in data.get("data", {}):
            return None
        raw_bars = data["data"][key].get("m5", [])
        if not raw_bars:
            return None

        # Filter bars for this trade_date
        bars = []
        for b in raw_bars:
            bar_time = int(b[0])  # YYYYMMDDHHMM
            bar_date = bar_time // 10000
            if bar_date != date_int:
                continue
            # time string: HHMM
            t = str(bar_time % 10000).zfill(4)
            open_p = float(b[1])
            close_p = float(b[2])
            high_p = float(b[3])
            low_p = float(b[4])
            vol = float(b[5])  # 手
            amount_wan = float(b[7]) if len(b) > 7 else 0  # 万元
            bars.append({
                "time": t,
                "price": close_p,
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "volume": vol,
                "amount_wan": amount_wan,
                "avg_price": 0,  # will compute below
                "turnover": amount_wan * 10000,  # 万元->元
            })
        if not bars:
            return None
        # Use (high+low)/2 as VWAP proxy. This is a reasonable estimate
        # for whether price is "above or below the day's average price zone".
        # Once we have real-time data (vs historical), the real VWAP from turnover/volume will be used.
        cum_vol = 0.0
        cum_price_vol_sum = 0.0  # price * volume
        cum_vol_sum = 0.0
        for b in bars:
            # Use (high+low)/2 as the representative price for this bar
            mid = (b["high"] + b["low"]) / 2
            vol = b["volume"]
            cum_vol += vol
            cum_price_vol_sum += mid * vol * 100  # 手->股, 保持可加性
            cum_vol_sum += vol * 100
            # VWAP proxy = sum(mid*股数)/sum(股数) = 均价
            b["avg_price"] = round(cum_price_vol_sum / cum_vol_sum, 2) if cum_vol_sum > 0 else 0
        return bars
    except Exception as e:
        return None


def get_1min_bars(ts_code: str, trade_date: str) -> Optional[list[dict]]:
    """拉 1min K 线, 腾讯主数据源"""
    return fetch_1min_from_tencent(ts_code, trade_date)


# ============================================================
# 2. T 策略回测
# ============================================================
def simulate_t(strategy, bars: list[dict], base_shares: int = 1000,
               cost_price: float = None,
               trade_date: str = None,
               sell_vwap_band: float = 0.005,
               buy_vwap_band: float = 0.005,
               sell_pct_min: float = 1.5,
               buy_pct_min: float = 1.5,
               pullback_from_high: float = 0.3,
               pullback_from_low: float = 0.3,
               t_ratio: float = 0.33,
               min_t_profit_pct: float = 0.3,
               min_holding_pct: float = 0.5,
               verbose: bool = False) -> dict:
    """在分钟线上模拟 T 策略, 返回统计

    Args:
        bars: list of {time, price, avg_price, volume, turnover}, 按时间升序
    """
    if not bars:
        return {"error": "no data"}

    # 从数据库取前收盘价
    _ts = strategy  # strategy arg is ts_code
    prev_close = None
    try:
        config = get_db_config()
        conn = pymysql.connect(**config)
        cur = conn.cursor()
        d = min(bars, key=lambda x: x["time"])["time"]
        # extract date from first bar's time context - we pass date via param
        # fallback: use daily_price
        if trade_date:
            cur.execute("SELECT pre_close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (strategy, trade_date))
        else:
            cur.execute("SELECT pre_close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (strategy, ""))
        cur.close()
        conn.close()
    except:
        pass
    if prev_close is None:
        # 再试一次
        try:
            config = get_db_config()
            conn = pymysql.connect(**config)
            cur = conn.cursor()
            # 从 trade_date 参数获取, 或者从 bars 推算
            if trade_date:
                cur.execute("SELECT pre_close FROM daily_price WHERE ts_code=%s AND trade_date=%s", (strategy, trade_date))
                row = cur.fetchone()
                if row:
                    prev_close = float(row[0])
            cur.close()
            conn.close()
        except:
            pass
    if prev_close is None:
        prev_close = bars[0]["price"]  # 用开盘
    open_price = bars[0]["price"]
    high = prev_close
    low = prev_close
    cum_volume = 0.0
    cum_amount = 0.0

    state = {
        "t_position_id": None,
        "t_open_price": None,
        "t_open_shares": 0,
        "t_peak_after_sell": 0,
        "t_action_count": 0,
        "t_skip_streak": 0,
    }
    signals = []
    base_remaining = base_shares
    realized_pnl = 0.0

    for bar in bars:
        price = bar["price"]
        avg_price = bar["avg_price"]  # 新浪给的分钟均价≈VWAP
        vol = bar["volume"]
        amt = bar["turnover"]

        # 更新极值 (从盘中累计)
        high = max(high, price)
        low = min(low, price)
        cum_volume += vol
        cum_amount += amt

        # VWAP 估算
        vwap = avg_price if avg_price > 0 else (cum_amount / cum_volume * 100 if cum_volume > 0 else 0)

        # 计算指标
        intraday_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
        pct_from_vwap = (price - vwap) / vwap * 100 if vwap > 0 else 0
        drawdown_from_high = (high - price) / high * 100 if high > 0 else 0
        rebound_from_low = (price - low) / low * 100 if low > 0 else 0
        holding_pct = (price - cost_price) / cost_price * 100 if cost_price else intraday_pct

        # --- 简单风控 ---
        if intraday_pct <= -5 or intraday_pct >= 9.5:  # 跌停/涨停附近不做
            state["t_skip_streak"] += 1
            continue

        # --- 有 T 仓: 判断 buy_back/force_close ---
        if state["t_position_id"] is not None and state["t_action_count"] < 1:
            # buy_back: 价格从卖后峰值回落 >= pullback_from_high % 即触发买回
            peak = state.get("t_peak_after_sell", state["t_open_price"])
            drop_from_peak = (peak - price) / peak * 100 if peak > 0 else 0
            if drop_from_peak >= pullback_from_high:
                target_profit = (state["t_open_price"] - price) / state["t_open_price"] * 100
                if target_profit >= min_t_profit_pct:
                    t_shares = int(base_shares * t_ratio)
                    t_shares = (t_shares // 100) * 100
                    if t_shares >= 100 and base_remaining >= 100:
                        pnl = (state["t_open_price"] - price) * t_shares
                        realized_pnl += pnl
                        base_remaining += t_shares  # 买回后底仓恢复
                        signals.append({
                            "time": bar["time"], "action": "buy_back",
                            "price": price, "vwap": vwap,
                            "pct_from_vwap": round(pct_from_vwap, 2),
                            "intraday_pct": round(intraday_pct, 2),
                            "t_shares": t_shares,
                            "pnl": round(pnl, 2),
                        })
                        state["t_position_id"] = None
                        state["t_open_price"] = None
                        state["t_action_count"] += 1
                        state["t_action_count"] += 1
                        state["t_skip_streak"] = 0
                        continue

            # 时间强制还原 (下午14:40后)
            if bar["time"] >= "14:40" and state["t_position_id"] is not None:
                t_shares = state["t_open_shares"]
                pnl = (price - state["t_open_price"]) * t_shares if state["t_open_price"] else 0
                realized_pnl += pnl
                base_remaining += t_shares
                signals.append({
                    "time": bar["time"], "action": "force_close",
                    "price": price, "vwap": vwap,
                    "pct_from_vwap": round(pct_from_vwap, 2),
                    "intraday_pct": round(intraday_pct, 2),
                    "t_shares": t_shares,
                    "pnl": round(pnl, 2),
                })
                state["t_position_id"] = None
                state["t_open_price"] = None
                state["t_action_count"] += 1
                continue

        # --- 无 T 仓: 判断 sell_high ---
        if state["t_position_id"] is None and state["t_action_count"] < 1:
            # 跳过刚建仓保护
            if abs(holding_pct) < min_holding_pct:
                state["t_skip_streak"] += 1
                continue

            if (vwap > 0 and price >= vwap * (1 + sell_vwap_band)
                    and intraday_pct >= sell_pct_min
                    and drawdown_from_high >= pullback_from_high):
                t_shares = int(base_shares * t_ratio)
                t_shares = (t_shares // 100) * 100
                if t_shares >= 100 and base_remaining - t_shares >= 100:
                    base_remaining -= t_shares
                    signals.append({
                        "time": bar["time"], "action": "sell_high",
                        "price": price, "vwap": vwap,
                        "pct_from_vwap": round(pct_from_vwap, 2),
                        "intraday_pct": round(intraday_pct, 2),
                        "t_shares": t_shares,
                        "pnl": 0,
                    })
                    state["t_position_id"] = 1
                    state["t_open_price"] = price
                    state["t_open_shares"] = t_shares
                    state["t_peak_after_sell"] = price
                    state["t_skip_streak"] = 0
                    continue

        state["t_skip_streak"] += 1

    # --- 收盘清算: 还有未平 T 仓? ---
    if state["t_position_id"] is not None:
        last_price = bars[-1]["price"]
        t_shares = state["t_open_shares"]
        pnl = (last_price - state["t_open_price"]) * t_shares
        realized_pnl += pnl
        signals.append({
            "time": "close", "action": "close_settle",
            "price": last_price, "vwap": 0,
            "pct_from_vwap": 0, "intraday_pct": 0,
            "t_shares": t_shares, "pnl": round(pnl, 2),
        })
        base_remaining += t_shares

    # --- 统计 ---
    count = len(signals)
    sell_highs = [s for s in signals if s["action"] == "sell_high"]
    buy_backs = [s for s in signals if s["action"] == "buy_back"]

    result = {
        "date": "",
        "prev_close": prev_close,
        "open": open_price,
        "high": round(high, 2),
        "low": round(low, 2),
        "close": round(bars[-1]["price"], 2),
        "intraday_range": round((high - low) / low * 100, 2),
        "total_signals": count,
        "sell_high_count": len(sell_highs),
        "buy_back_count": len(buy_backs),
        "force_close_count": len([s for s in signals if s["action"] == "force_close"]),
        "signals": signals,
        "realized_pnl": round(realized_pnl, 2),
        "trade_count": len(sell_highs),
    }
    return result


# ============================================================
# 3. 主流程
# ============================================================
def get_trading_days(ts_code: str, limit: int = 20) -> list[str]:
    """从 daily_price 表取最近 N 个交易日"""
    config = get_db_config()
    conn = pymysql.connect(**config)
    cur = conn.cursor()
    cur.execute(
        """SELECT trade_date FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT %s""",
        (ts_code, limit)
    )
    days = [str(r[0]) for r in cur.fetchall()][::-1]  # 升序
    conn.close()
    return days


def param_scan(bars: list[dict], base_shares: int, cost_price: float) -> list[dict]:
    """参数扫描: 不同参数组合跑一遍, 找最优"""
    params_grid = {
        "sell_vwap_band": [0.003, 0.005, 0.008, 0.01],
        "buy_vwap_band": [0.003, 0.005, 0.008, 0.01],
        "sell_pct_min": [1.0, 1.5, 2.0],
        "buy_pct_min": [1.0, 1.5, 2.0],
        "pullback_from_high": [0.2, 0.3, 0.5],
        "pullback_from_low": [0.2, 0.3, 0.5],
        "t_ratio": [0.2, 0.33, 0.5],
        "min_t_profit_pct": [0.2, 0.3, 0.5],
    }

    results = []
    # 只扫 5 个关键参数, 避免组合爆炸
    for sell_band in params_grid["sell_vwap_band"]:
        for buy_band in params_grid["buy_vwap_band"]:
            for sell_pct in params_grid["sell_pct_min"]:
                for buy_pct in params_grid["buy_pct_min"]:
                    for t_r in params_grid["t_ratio"]:
                        r = simulate_t(
                            "param_scan", bars, base_shares, cost_price,
                            trade_date="",
                            sell_vwap_band=sell_band, buy_vwap_band=buy_band,
                            sell_pct_min=sell_pct, buy_pct_min=buy_pct,
                            pullback_from_high=0.3, pullback_from_low=0.3,
                            t_ratio=t_r, min_t_profit_pct=0.3,
                            verbose=False,
                        )
                        if "error" in r:
                            continue
                        results.append({
                            "sell_band": sell_band,
                            "buy_band": buy_band,
                            "sell_pct": sell_pct,
                            "buy_pct": buy_pct,
                            "t_ratio": t_r,
                            "pnl": r["realized_pnl"],
                            "trades": r["trade_count"],
                            "signals": r["total_signals"],
                        })
    return sorted(results, key=lambda x: -x["pnl"])


def main():
    parser = argparse.ArgumentParser(description="日内做T回测")
    parser.add_argument("--ts_code", default="600519.SH", help="股票代码")
    parser.add_argument("--days", type=int, default=10, help="回测天数")
    parser.add_argument("--shares", type=int, default=1000, help="底仓股数")
    parser.add_argument("--cost", type=float, default=None, help="成本价")
    parser.add_argument("--tune", action="store_true", help="参数扫描优化")
    parser.add_argument("--verbose", action="store_true", help="每笔信号详情")
    args = parser.parse_args()

    print(f"\n=== 日内做T回测: {args.ts_code} 近{args.days}天 ===")
    print(f"底仓: {args.shares}股", end="")

    # 获取交易日列表
    trade_days = get_trading_days(args.ts_code, args.days * 2)[-args.days:]
    print(f", 交易日数: {len(trade_days)}")
    print(f"交易日范围: {trade_days[0]} ~ {trade_days[-1]}")
    print()

    # 成本价
    cost_price = args.cost
    if cost_price is None:
        # 用最近一天收盘价估算
        config = get_db_config()
        conn = pymysql.connect(**config)
        cur = conn.cursor()
        cur.execute(
            "SELECT close FROM daily_price WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1",
            (args.ts_code,)
        )
        row = cur.fetchone()
        conn.close()
        if row:
            cost_price = float(row[0])
            print(f"成本价(自动): {cost_price} (最近收盘)")
        else:
            cost_price = 100

    # 逐日回测
    all_results = []
    failed_days = 0
    for i, day in enumerate(trade_days):
        sys.stdout.write(f"\r  正在回测 {i+1}/{len(trade_days)} ({day})...")
        sys.stdout.flush()
        bars = get_1min_bars(args.ts_code, day)
        if not bars:
            failed_days += 1
            continue
        r = simulate_t(
            args.ts_code, bars, args.shares, cost_price,
            trade_date=day, verbose=args.verbose,
        )
        if "error" in r:
            failed_days += 1
            continue
        r["date"] = day
        all_results.append(r)
        time.sleep(0.2)  # 频率限制

    print(f"\r  {' '*20}\r", end="")

    if failed_days > 0:
        print(f"⚠️  {failed_days} 天分钟线数据拉取失败")

    print(f"成功回测: {len(all_results)} 天\n")

    # 汇总
    total_pnl = sum(r["realized_pnl"] for r in all_results)
    total_signals = sum(r["total_signals"] for r in all_results)
    total_sell_highs = sum(r["sell_high_count"] for r in all_results)
    total_buy_backs = sum(r["buy_back_count"] for r in all_results)
    total_force_closes = sum(r["force_close_count"] for r in all_results)
    trade_days_with_signal = sum(1 for r in all_results if r["sell_high_count"] > 0)
    pnl_days = [r["realized_pnl"] for r in all_results if r["sell_high_count"] > 0]

    print(f"=== 信号汇总 ===")
    print(f"  总信号数: {total_signals}")
    print(f"  高抛: {total_sell_highs} 次, 低吸: {total_buy_backs} 次, 强制还原: {total_force_closes} 次")
    print(f"  有交易日: {trade_days_with_signal}/{len(all_results)} 天")
    print()
    print(f"=== 盈亏 ===")
    print(f"  总盈亏: {total_pnl:+8.2f} 元")
    print(f"  日均信号日: {total_pnl/max(1,trade_days_with_signal):+8.2f} 元/天")
    if pnl_days:
        print(f"  单日盈亏: max={max(pnl_days):+.2f} min={min(pnl_days):+.2f}")
        positive_days = sum(1 for p in pnl_days if p > 0)
        print(f"  胜率: {positive_days}/{len(pnl_days)} = {positive_days/len(pnl_days)*100:.0f}%")
    elif total_signals > 0:
        print(f"  未完成 T (有信号没平仓)")
    print()

    # 每日详情
    print("=== 每日明细 ===")
    print(f"{'日期':<12} {'涨跌幅':>6} {'振幅':>6} {'高抛':>4} {'低吸':>4} {'强还':>4} {'盈亏':>10} {'信号':>20}")
    print("-" * 75)
    for r in all_results:
        sig_str = "; ".join(f"{s['time']} {s['action']} {s['price']}" for s in (r["signals"] if args.verbose else r["signals"][:2]))
        print(f"{r['date']:<12} {r['close']/cost_price*100-100:+6.2f}% {r['intraday_range']:>5.2f}% "
              f"{r['sell_high_count']:>4} {r['buy_back_count']:>4} {r['force_close_count']:>4} "
              f"{r['realized_pnl']:+10.2f}")

    # 参数扫描
    if args.tune and len(all_results) >= 3:
        print(f"\n=== 参数扫描 (合并所有天的分钟线) ===")
        all_bars = []
        for r in all_results:
            days_bars = get_1min_bars(args.ts_code, r["date"])
            if days_bars:
                all_bars.extend(days_bars)
        if all_bars:
            scan_results = param_scan(all_bars, args.shares, cost_price)
            print(f"{'SELL_BAND':>10} {'BUY_BAND':>10} {'SELL_PCT':>9} {'BUY_PCT':>9} {'T_RATIO':>8} {'PNL':>10} {'TRADES':>7}")
            print("-" * 70)
            for sr in scan_results[:15]:
                print(f"{sr['sell_band']:>10.3f} {sr['buy_band']:>10.3f} {sr['sell_pct']:>9.1f} {sr['buy_pct']:>9.1f} "
                      f"{sr['t_ratio']:>8.2f} {sr['pnl']:>+10.2f} {sr['trades']:>7}")
            print(f"\n最优参数: SELL_BAND={scan_results[0]['sell_band']} BUY_BAND={scan_results[0]['buy_band']} "
                  f"SELL_PCT={scan_results[0]['sell_pct']} BUY_PCT={scan_results[0]['buy_pct']} "
                  f"T_RATIO={scan_results[0]['t_ratio']}")


if __name__ == "__main__":
    main()
