#!/usr/bin/env python3
"""
参数扫描结果分析器

读取 data/params_scan_v4.json，进行：
1. 单参数敏感性分析（分组平均收益/标准差）
2. Top 5 推荐组合
3. 输出报告到 data/params_optimization_report.txt

用法:
    python3 scripts/analyze_params.py
    python3 scripts/run_backtest.py analyze

说明:
    独立于扫描过程运行，只需已有扫描结果文件即可分析。
"""
import os, sys, json, time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def load_scan_results():
    """加载扫描结果"""
    path = os.path.join(DATA_DIR, "params_scan_v4.json")
    if not os.path.exists(path):
        print(f"错误: 未找到扫描结果文件 {path}")
        print("请先运行: python3 scripts/run_backtest.py optimize")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def sensitivity_analysis(results, param_key, metric_key="总收益率"):
    """计算单个参数在某个指标上的敏感性：分组均值/标准差"""
    groups = defaultdict(list)
    for entry in results:
        val = entry["params"][param_key]
        metric_val = entry["metrics"].get(metric_key, 0)
        if metric_val is not None:
            groups[val].append(metric_val)

    analysis = []
    for val, vals in sorted(groups.items()):
        n = len(vals)
        mean = round(sum(vals) / n, 2)
        if n > 1:
            variance = sum((x - mean) ** 2 for x in vals) / (n - 1)
            std = round(variance ** 0.5, 2)
        else:
            std = 0
        analysis.append((val, mean, std, n))
    return analysis


def param_display_name(key):
    """参数键 → 中文显示名"""
    names = {
        "min_score": "评分门槛",
        "max_hold_days": "持仓天数",
        "initial_stop_pct": "初始止损",
    }
    return names.get(key, key)


def format_param_value(key, val):
    """格式化参数值"""
    if key == "initial_stop_pct":
        return f"{val*100:.0f}%"
    return str(val)


def run_analysis():
    """执行分析并输出报告"""
    data = load_scan_results()
    results = data["results"]
    scan_params = data["scan_params"]

    if not results:
        print("扫描结果为空，请先运行参数扫描。")
        return

    lines = []
    def emit(text=""):
        print(text)
        lines.append(text)

    # 总览
    emit("=" * 72)
    emit("V4 组合策略参数优化分析报告")
    emit("=" * 72)
    emit(f"生成时间: {data.get('timestamp', '未知')}")
    emit(f"扫描区间: {scan_params['start_date']} ~ {scan_params['end_date']}")
    emit(f"有效组合: {len(results)}/{scan_params['combo_count']}")
    emit(f"失败组合: {len(data.get('errors', []))}")
    emit()

    # === 单参数敏感性分析 ===
    emit("-" * 72)
    emit("一、单参数敏感性分析")
    emit("-" * 72)
    emit()

    for metric in ["总收益率", "夏普比率", "胜率", "最大回撤"]:
        emit(f"--- 指标: {metric} ---")
        emit(f"{'参数值':>12} | {'平均':>8} | {'标准差':>8} | {'样本数':>6}")
        emit("-" * 42)

        for param_key in ["min_score", "max_hold_days", "initial_stop_pct"]:
            analysis = sensitivity_analysis(results, param_key, metric)
            if not analysis:
                continue
            emit(f"  [{param_display_name(param_key)}]")
            for val, mean, std, n in analysis:
                pval = format_param_value(param_key, val)
                emit(f"{pval:>12} | {mean:>7.2f} | {std:>7.2f} | {n:>6}")
            emit()

    # === Top 5 推荐 ===
    emit("-" * 72)
    emit("二、Top 5 参数组合推荐")
    emit("-" * 72)
    emit()
    emit(f"{'排名':>4} | {'评分门槛':>6} | {'持仓天数':>6} | {'止损':>6} | {'复合评分':>8} | {'收益率':>8} | {'胜率':>6} | {'夏普':>6} | {'最大回撤':>8} | {'盈亏比':>6}")
    emit("-" * 80)

    sorted_results = sorted(results, key=lambda x: x["composite_score"], reverse=True)
    for i, entry in enumerate(sorted_results[:5]):
        p = entry["params"]
        m = entry["metrics"]
        score = entry["composite_score"]
        emit(f"{i+1:>4} | {p['min_score']:>6} | {p['max_hold_days']:>6} | {p['initial_stop_pct']*100:.0f}% | {score:>8} | {m['总收益率']:>7}% | {m['胜率']:>5}% | {m['夏普比率']:>5} | {m['最大回撤']:>7}% | {m['盈亏比']:>5}")

    # 与当前默认参数的对比
    emit()
    emit("-" * 72)
    emit("三、与默认参数对比")
    emit("-" * 72)
    emit()

    default_params = {"min_score": 60, "max_hold_days": 7, "initial_stop_pct": -0.05}
    default_entry = None
    for entry in results:
        p = entry["params"]
        if (p["min_score"] == default_params["min_score"] and
            p["max_hold_days"] == default_params["max_hold_days"] and
            p["initial_stop_pct"] == default_params["initial_stop_pct"]):
            default_entry = entry
            break

    if default_entry:
        dm = default_entry["metrics"]
        emit(f"  默认参数 (评分>=60 | 持仓7天 | 止损-5%):")
        emit(f"    收益率: {dm['总收益率']}%  胜率: {dm['胜率']}%  夏普: {dm['夏普比率']}  最大回撤: {dm['最大回撤']}%  盈亏比: {dm['盈亏比']}")
        emit()

        best = sorted_results[0]
        best_p = best["params"]
        best_m = best["metrics"]
        emit(f"  最优参数 (评分>={best_p['min_score']} | 持仓{best_p['max_hold_days']}天 | 止损{best_p['initial_stop_pct']*100:.0f}%):")
        emit(f"    收益率: {best_m['总收益率']}%  胜率: {best_m['胜率']}%  夏普: {best_m['夏普比率']}  最大回撤: {best_m['最大回撤']}%  盈亏比: {best_m['盈亏比']}")
    else:
        emit(f"  默认参数组合不在扫描结果中（可能是扫描区间不同）")
    emit()

    # === 最优参数推荐结论 ===
    emit("=" * 72)
    emit("四、优化建议")
    emit("=" * 72)
    emit()

    if sorted_results:
        best = sorted_results[0]
        best_p = best["params"]
        emit(f"  推荐参数: min_score={best_p['min_score']}, max_hold_days={best_p['max_hold_days']}, initial_stop_pct={best_p['initial_stop_pct']}")
        emit(f"  预期提升: 复合评分 {best['composite_score']}")

    # 保存报告
    report_path = os.path.join(DATA_DIR, "params_optimization_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n分析报告已保存: {report_path}")


if __name__ == "__main__":
    run_analysis()
