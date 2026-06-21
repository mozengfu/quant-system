#!/usr/bin/env python3
"""
日内做T策略 — 自动分析日报
============================
每天盘后 (建议 17:00 crontab) 运行一次, 分析当日/近 N 日 T 信号质量。
从 intraday_t_log 表读取数据, 输出:
  1. 当日 T 操作统计 (高抛/低吸/强制还原次数, 盈亏)
  2. 信号质量评分 (误报率、有效触发价 vs VWAP 偏离分布)
  3. 参数调优建议 (当前 VWAP band / 目标利润率是否合适)
  4. 建议写入飞书

用法:
  python3 scripts/intraday_t_analysis.py                     # 当日分析
  python3 scripts/intraday_t_analysis.py --days 7            # 近 7 天汇总
  python3 scripts/intraday_t_analysis.py --days 7 --tune     # 输出调优建议
"""

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date

import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
from quant_app.services.notification_service import send_feishu

logger = logging.getLogger(__name__)

# ---------- 参考参数 (如果 intraday_t_log 为空则用这些去重放信号) ----------
DEFAULT_SELL_BAND = 0.005
DEFAULT_BUY_BAND = 0.005
DEFAULT_TARGET_PCT = 0.3


def fetch_logs(days: int = 1) -> list[dict]:
    """从 intraday_t_log 拉取近 N 天数据"""
    config = get_db_config()
    conn = pymysql.connect(**config)
    cur = conn.cursor()
    cur.execute(
        """SELECT id, ts_code, stock_name, trade_date, direction,
                  shares, price, vwap, pct_from_vwap, intraday_pct,
                  target_pct, realized_pnl, realized_pnl_pct, reason, status,
                  executor_mode, created_at
             FROM intraday_t_log
            WHERE trade_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            ORDER BY trade_date DESC, created_at ASC""",
        (days,)
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def analyze(rows: list[dict], do_tune: bool = False) -> dict:
    """分析数据, 返回统计和调优建议"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 基础统计
    total = len(rows)
    by_direction = defaultdict(list)
    for r in rows:
        by_direction[r["direction"]].append(r)

    # 当日
    today_rows = [r for r in rows if str(r["trade_date"]) == today_str]

    # 盈利能力
    sell_high_logs = by_direction.get("sell_high", [])
    buy_back_logs  = by_direction.get("buy_back", [])
    force_close_logs = by_direction.get("force_close", [])

    total_pnl = sum(float(r["realized_pnl"] or 0) for r in rows)
    total_pnl_pct = sum(float(r["realized_pnl_pct"] or 0) for r in rows)
    avg_pnl_pct = total_pnl_pct / len(rows) if rows else 0

    # 高抛信号统计
    sell_vwap_deviations = [float(r["pct_from_vwap"] or 0) for r in sell_high_logs]
    buy_vwap_deviations = [float(r["pct_from_vwap"] or 0) for r in buy_back_logs]

    # 有效 T 次数 (sell_high + buy_back 配对)
    completed_trades = min(len(sell_high_logs), len(buy_back_logs))

    # ==================== 调优分析 ====================
    tune_suggestions = []

    if do_tune and len(sell_high_logs) >= 3:
        # 高抛偏离度分布 → 建议 VWAP band
        avg_sell_dev = sum(abs(d) for d in sell_vwap_deviations) / len(sell_vwap_deviations) if sell_vwap_deviations else DEFAULT_SELL_BAND
        suggested_sell_band = round(max(0.002, min(0.02, avg_sell_dev * 0.8)), 3)
        tune_suggestions.append({
            "param": "SELL_VWAP_BAND",
            "current": DEFAULT_SELL_BAND,
            "suggested": suggested_sell_band,
            "logic": f"历史 {len(sell_high_logs)} 次高抛平均偏离 {avg_sell_dev*100:.2f}%, 建议 {suggested_sell_band*100:.2f}%"
        })

    if do_tune and len(buy_back_logs) >= 3:
        avg_buy_dev = sum(abs(d) for d in buy_vwap_deviations) / len(buy_vwap_deviations) if buy_vwap_deviations else DEFAULT_BUY_BAND
        suggested_buy_band = round(max(0.002, min(0.02, avg_buy_dev * 0.8)), 3)
        tune_suggestions.append({
            "param": "BUY_VWAP_BAND",
            "current": DEFAULT_BUY_BAND,
            "suggested": suggested_buy_band,
            "logic": f"历史 {len(buy_back_logs)} 次低吸平均偏离 {avg_buy_dev*100:.2f}%, 建议 {suggested_buy_band*100:.2f}%"
        })

    if do_tune and len(buy_back_logs) >= 3:
        # 已实现盈亏分布 → 判断目标利润率
        realized_pcts = [float(r["realized_pnl_pct"] or 0) for r in buy_back_logs if float(r.get("realized_pnl_pct", 0) or 0) != 0]
        if realized_pcts:
            avg_realized_pct = sum(realized_pcts) / len(realized_pcts)
            min_realized = min(realized_pcts)
            max_realized = max(realized_pcts)
            suggested_target = round(max(0.1, avg_realized_pct * 0.7), 2)
            tune_suggestions.append({
                "param": "MIN_T_PROFIT_PCT",
                "current": DEFAULT_TARGET_PCT,
                "suggested": suggested_target,
                "logic": f" {len(realized_pcts)} 次实际盈亏: min={min_realized:.2f}% avg={avg_realized_pct:.2f}% max={max_realized:.2f}%, 建议 {suggested_target}%"
            })

    # 强制还原率 → 说明 pullback 参数可能需要调
    force_close_rate = len(force_close_logs) / max(total, 1)

    return {
        "today": today_str,
        "days_covered": max(1, (max(r["trade_date"] for r in rows) if rows else date.today()).day
                            - (min(r["trade_date"] for r in rows) if rows else date.today()).day + 1),
        "total_logs": total,
        "today_logs": len(today_rows),
        "sell_high_count": len(sell_high_logs),
        "buy_back_count": len(buy_back_logs),
        "force_close_count": len(force_close_logs),
        "completed_trades": completed_trades,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_pct": round(avg_pnl_pct, 3),
        "sell_vwap_dev_avg": round(sum(abs(d) for d in sell_vwap_deviations) / len(sell_vwap_deviations), 3) if sell_vwap_deviations else 0,
        "buy_vwap_dev_avg": round(sum(abs(d) for d in buy_vwap_deviations) / len(buy_vwap_deviations), 3) if buy_vwap_deviations else 0,
        "force_close_rate": round(force_close_rate, 3),
        "tune_suggestions": tune_suggestions,
    }


def build_message(stats: dict) -> str:
    """构建飞书报告"""
    lines = []
    lines.append(f"【日内做T日报】{stats['today']}")
    lines.append(f"覆盖 {stats['days_covered']} 天, 共 {stats['total_logs']} 条记录")
    lines.append("")

    lines.append("=== 当日概览 ===")
    lines.append(f"  今日记录: {stats['today_logs']} 条")
    lines.append(f"  高抛: {stats['sell_high_count']} 次")
    lines.append(f"  低吸: {stats['buy_back_count']} 次")
    lines.append(f"  强制还原: {stats['force_close_count']} 次")
    lines.append(f"  有效配对 T: {stats['completed_trades']} 次")
    lines.append("")

    lines.append("=== 盈亏情况 ===")
    sign = "+" if stats['total_pnl'] >= 0 else ""
    lines.append(f"  累计盈亏: {sign}{stats['total_pnl']:.2f} 元")
    lines.append(f"  平均单笔盈亏: {sign}{stats['avg_pnl_pct']:.2f}%")
    lines.append("")

    lines.append("=== 信号质量 ===")
    lines.append(f"  高抛 VWAP 偏离: {stats['sell_vwap_dev_avg']*100:.2f}%")
    lines.append(f"  低吸 VWAP 偏离: {stats['buy_vwap_dev_avg']*100:.2f}%")
    lines.append(f"  强制还原率: {stats['force_close_rate']*100:.1f}%")

    if stats['force_close_rate'] > 0.3:
        lines.append("  ⚠️ 强制还原率 > 30%, PULLBACK 参数过严或大盘不配合")
    elif stats['force_close_rate'] < 0.05:
        lines.append("  ✅ 强制还原率 < 5%, T 策略执行力良好")

    if stats.get('tune_suggestions'):
        lines.append("")
        lines.append("=== 调优建议 ===")
        for s in stats['tune_suggestions']:
            lines.append(f"  {s['param']}: {s['current']} → 建议 {s['suggested']}")
            lines.append(f"    理由: {s['logic']}")

    lines.append("")
    lines.append("---")
    lines.append(f"下次分析: python3 scripts/intraday_t_analysis.py --days 7 --tune")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="日内做T日志分析")
    parser.add_argument("--days", type=int, default=1, help="分析近 N 天数据 (默认 1)")
    parser.add_argument("--tune", action="store_true", help="输出参数调优建议")
    parser.add_argument("--send", action="store_true", help="发送飞书报告")
    args = parser.parse_args()

    rows = fetch_logs(days=args.days)
    if not rows:
        print(f"intraday_t_log 近 {args.days} 天无数据, 无需分析")
        return

    stats = analyze(rows, do_tune=args.tune)
    msg = build_message(stats)

    print(msg)

    if args.send:
        send_feishu(msg)
        print("飞书报告已发送")


if __name__ == "__main__":
    main()
