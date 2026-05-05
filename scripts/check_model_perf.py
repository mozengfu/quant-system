#!/usr/bin/env python3
"""
模型性能检查工具

读取预测快照和实际收益数据，计算实际 IC 并与训练时的 IC 对比。

用法:
    python3 scripts/check_model_perf.py                     # 最近 30 天概览
    python3 scripts/check_model_perf.py --days 7            # 最近 7 天
    python3 scripts/check_model_perf.py --detail            # 逐日明细
"""
import os, sys, json, glob
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

from quant_app.utils.config import get_db_config


def load_monitor_history():
    """加载训练监控历史"""
    path = os.path.join(DATA_DIR, "model_monitor_history.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def load_prediction_snapshots(days=30):
    """加载最近 days 天的预测快照"""
    snapshots = []
    for f in sorted(glob.glob(os.path.join(DATA_DIR, "predictions_*.json"))):
        basename = os.path.basename(f)
        try:
            date_str = basename.split("_")[1]  # predictions_20260505_v6.5.json
            file_date = datetime.strptime(date_str, "%Y%m%d").date()
            if (datetime.now().date() - file_date).days <= days:
                with open(f) as fh:
                    snapshots.append(json.load(fh))
        except (ValueError, IndexError):
            continue
    return snapshots


def fetch_actual_returns(conn, date):
    """获取 date 后第 1 个交易日的实际收益率（作为预测效果的验证）"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.ts_code, p.pct_chg
        FROM daily_price p
        JOIN (
            SELECT MIN(trade_date) AS next_date
            FROM daily_price
            WHERE trade_date > %s
        ) n ON p.trade_date = n.next_date
    """, (date,))
    rows = cursor.fetchall()
    cursor.close()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def compute_actual_ic(predictions, actual_returns):
    """计算预测收益率与实际收益率的秩相关系数（Spearman's rank correlation）"""
    pairs = []
    for p in predictions:
        ts_code = p.get("ts_code") or p.get("_ts_code", "")
        pred_ret = p.get("predicted_return", 0)
        actual_ret = actual_returns.get(ts_code)
        if actual_ret is not None and pred_ret != 0:
            pairs.append((pred_ret, actual_ret))
    if len(pairs) < 5:
        return None, len(pairs)
    # 手动计算 Spearman 秩相关系数
    pred_vals = [x[0] for x in pairs]
    actual_vals = [x[1] for x in pairs]
    n = len(pairs)
    # 秩变换
    pred_ranks = {v: i + 1 for i, v in enumerate(sorted(pred_vals))}
    actual_ranks = {v: i + 1 for i, v in enumerate(sorted(actual_vals))}
    rank_pred = [pred_ranks[v] for v in pred_vals]
    rank_actual = [actual_ranks[v] for v in actual_vals]
    d_sq = sum((rp - ra) ** 2 for rp, ra in zip(rank_pred, rank_actual))
    ic = 1 - (6 * d_sq) / (n * (n * n - 1))
    return round(ic, 4), n


def print_header():
    print("=" * 62)
    print(f"  ML 模型性能检查 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 62)


def print_training_summary(history):
    """打印训练记录摘要"""
    latest = history[-1]
    print(f"\n  [训练记录 - 最新]")
    print(f"    版本:        {latest.get('version', 'N/A')}")
    print(f"    训练时间:    {latest.get('trained_at', 'N/A')}")
    print(f"    Rank IC:     {latest.get('final_rank_ic', 'N/A')}")
    print(f"    日均 IC:     {latest.get('final_mean_daily_ic', 'N/A')}")
    print(f"    平均 Spread: {latest.get('avg_spread_bps', 'N/A')} bps")

    if len(history) >= 2:
        recent = history[-5:]
        avg_ic = sum(h.get("final_rank_ic", 0) or 0 for h in recent) / len(recent)
        avg_spread = sum(h.get("avg_spread_bps", 0) or 0 for h in recent) / len(recent)
        print(f"\n  [趋势 - 最近 {len(recent)} 次训练]")
        print(f"    平均 IC:     {avg_ic:.4f}")
        print(f"    平均 Spread: {avg_spread:.1f} bps")


def print_snapshot_summary(snapshots):
    """打印预测快照摘要"""
    print(f"\n  [预测记录 - 最近 {len(snapshots)} 天]")
    for s in snapshots:
        version = s.get("version", "?")
        date = s.get("date", "?")
        n = s.get("n_stocks", 0)
        bullish = sum(1 for p in s.get("predictions", []) if p.get("ml_bullish"))
        print(f"    {date} ({version}): 预测 {n} 只, 看涨 {bullish} 只")


def print_detail(snapshots, conn):
    """逐日明细（含实际 IC 计算）"""
    print(f"\n  [逐日明细]")
    print(f"  {'日期':<12} {'版本':<10} {'预测数':>6} {'看涨':>5} {'实际 IC':>10} {'样本数':>6}")
    print(f"  " + "-" * 54)
    for s in snapshots:
        date = s.get("date", "?")
        version = s.get("version", "?")
        n = s.get("n_stocks", 0)
        bullish = sum(1 for p in s.get("predictions", []) if p.get("ml_bullish"))
        # 获取预测日期后第 1 个交易日的实际收益
        try:
            pred_date = datetime.strptime(date, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            pred_date = date
        actual = fetch_actual_returns(conn, pred_date)
        ic, n_pairs = compute_actual_ic(s.get("predictions", []), actual)
        ic_str = f"{ic:.4f}" if ic is not None else "N/A"
        print(f"  {date:<12} {version:<10} {n:>6} {bullish:>5} {ic_str:>10} {n_pairs:>6}")


def main():
    days = 30
    detail = False
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg == "--detail":
            detail = True
        elif arg.startswith("--"):
            print(f"未知参数: {arg}")
            print("用法: python3 scripts/check_model_perf.py [--days=N] [--detail]")
            return

    history = load_monitor_history()
    snapshots = load_prediction_snapshots(days)
    conn = None

    print_header()

    if not history:
        print("\n  model_monitor_history.json 为空或不存在")
        print("  尚无训练记录，请先运行训练脚本")

    if not snapshots:
        print(f"\n  最近 {days} 天内无预测快照文件")
        print("  预测快照由 ml_predict.py 在策略扫描时自动保存")
        print("  需要先运行 run_three_strategies.py 触发 ml_enhanced_score()")
        return

    try:
        import pymysql
        conn = pymysql.connect(**get_db_config())
    except Exception as e:
        print(f"\n  [警告] 数据库连接失败: {e}")
        print("  将跳过实际 IC 计算，仅显示快照摘要")

    if history:
        print_training_summary(history)

    print_snapshot_summary(snapshots)

    if detail and conn:
        print_detail(snapshots, conn)

    # 版本差异告警 — 当预测使用版本与最新训练版本不一致时提示
    if history and snapshots:
        latest_train_version = history[-1].get("version", "")
        prediction_versions = set(s.get("version", "") for s in snapshots)
        if latest_train_version and prediction_versions:
            mismatched = [v for v in prediction_versions if v != latest_train_version]
            if mismatched:
                print(f"\n  [!] 预测版本与最新训练版本不一致")
                print(f"      训练版本: {latest_train_version}")
                print(f"      预测版本: {', '.join(sorted(prediction_versions))}")
                print(f"      请检查是否使用了过时的模型文件")

    if conn:
        conn.close()
    print("=" * 62)


if __name__ == "__main__":
    main()
