#!/usr/bin/env python3
"""
V4 组合策略参数网格扫描

遍历 min_score × max_hold_days × initial_stop_pct 共 60 组参数组合，
固定 max_positions=5，输出 Top 10 排序结果。

用法:
    python3 scripts/optimize_v4_params.py
    python3 scripts/run_backtest.py optimize [--start YYYYMMDD] [--end YYYYMMDD]
"""
import os, sys, json, time, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_combo_v4 import backtest_combo_v4

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# 参数网格
PARAM_GRID = {
    "min_score": [40, 50, 60, 70, 80],
    "max_hold_days": [5, 7, 10, 14],
    "initial_stop_pct": [-0.03, -0.05, -0.07],
}

# 固定参数
FIXED_MAX_POSITIONS = 5

# 复合评分权重
W_RETURN = 0.30     # 总收益率
W_WIN_RATE = 0.15   # 胜率
W_SHARPE = 0.25     # 夏普比率
W_DD = 0.15         # 最大回撤（取倒数）
W_PL = 0.15         # 盈亏比


def composite_score(result):
    """计算复合评分，处理除零和异常值"""
    ret = result.get("总收益率", 0)
    wr = result.get("胜率", 0)
    sharpe = result.get("夏普比率", 0)
    dd = result.get("最大回撤", 0)
    pl = result.get("盈亏比", 0)

    # 最大回撤保护：越小越好 → 取倒数，回撤为 0 时给满分
    dd_score = 100.0 if dd == 0 else min(100.0, 1.0 / dd * 100)

    score = (ret * W_RETURN +
             wr * W_WIN_RATE +
             sharpe * W_SHARPE +
             dd_score * W_DD +
             pl * W_PL)
    return round(score, 2)


def run_scan(start="20251001", end="20260424"):
    """遍历参数网格，保存结果到 JSON"""
    min_scores = PARAM_GRID["min_score"]
    hold_days_list = PARAM_GRID["max_hold_days"]
    stop_pcts = PARAM_GRID["initial_stop_pct"]

    total_combos = len(min_scores) * len(hold_days_list) * len(stop_pcts)
    print(f"参数网格扫描: {total_combos} 组组合")
    print(f"  min_score: {min_scores}")
    print(f"  max_hold_days: {hold_days_list}")
    print(f"  initial_stop_pct: {stop_pcts}")
    print(f"  固定: max_positions={FIXED_MAX_POSITIONS}")
    print(f"  区间: {start} ~ {end}")
    print()

    all_results = []
    idx = 0
    errors = []

    for ms in min_scores:
        for hold in hold_days_list:
            for stop in stop_pcts:
                idx += 1
                params = {
                    "min_score": ms,
                    "max_hold_days": hold,
                    "initial_stop_pct": stop,
                    "max_positions": FIXED_MAX_POSITIONS,
                }

                print(f"[{idx}/{total_combos}] 评分>={ms} | 持仓{hold}天 | 止损{stop*100:.0f}% ... ", end="", flush=True)

                t0 = time.time()
                try:
                    result = backtest_combo_v4(
                        start_date=start,
                        end_date=end,
                        min_score=ms,
                        max_positions=FIXED_MAX_POSITIONS,
                        max_hold_days=hold,
                        initial_stop_pct=stop,
                    )
                except Exception as e:
                    err_msg = f"运行失败: {e}"
                    print(err_msg)
                    errors.append({"params": params, "error": str(e)})
                    continue

                elapsed = time.time() - t0
                if "error" in result:
                    print(f"跳过 ({result['error']})")
                    errors.append({"params": params, "error": result["error"]})
                    continue

                score = composite_score(result)
                entry = {
                    "params": params,
                    "metrics": {
                        "总收益率": result.get("总收益率"),
                        "胜率": result.get("胜率"),
                        "盈亏比": result.get("盈亏比"),
                        "夏普比率": result.get("夏普比率"),
                        "最大回撤": result.get("最大回撤"),
                        "总交易次数": result.get("总交易次数"),
                    },
                    "composite_score": score,
                    "elapsed_sec": round(elapsed, 1),
                }
                all_results.append(entry)
                print(f"收益率{result.get('总收益率','?')}% 夏普{result.get('夏普比率','?')} 评分{score} ({elapsed:.1f}s)")

    # 按复合评分降序排序
    all_results.sort(key=lambda x: x["composite_score"], reverse=True)

    # 保存结果
    output = {
        "scan_params": {
            "start_date": start,
            "end_date": end,
            "grid": PARAM_GRID,
            "fixed": {"max_positions": FIXED_MAX_POSITIONS},
            "combo_count": total_combos,
        },
        "results": all_results,
        "errors": errors,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    output_path = os.path.join(DATA_DIR, "params_scan_v4.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 打印 Top 10
    print()
    print("=" * 80)
    print("TOP 10 参数组合（按复合评分排序）")
    print("=" * 80)
    print(f"{'排名':>4} | {'评分门槛':>6} | {'持仓天数':>6} | {'止损':>6} | {'复合评分':>8} | {'收益率':>8} | {'胜率':>6} | {'夏普':>6} | {'最大回撤':>8} | {'盈亏比':>6}")
    print("-" * 80)

    for i, entry in enumerate(all_results[:10]):
        p = entry["params"]
        m = entry["metrics"]
        score = entry["composite_score"]
        print(f"{i+1:>4} | {p['min_score']:>6} | {p['max_hold_days']:>6} | {p['initial_stop_pct']*100:.0f}% | {score:>8} | {m['总收益率']:>7}% | {m['胜率']:>5}% | {m['夏普比率']:>5} | {m['最大回撤']:>7}% | {m['盈亏比']:>5}")

    # 统计信息
    if all_results:
        best = all_results[0]
        worst = all_results[-1]
        avg_score = sum(e["composite_score"] for e in all_results) / len(all_results)
        print()
        print(f"最佳组合: 评分>={best['params']['min_score']} | 持仓{best['params']['max_hold_days']}天 | 止损{best['params']['initial_stop_pct']*100:.0f}% → 评分 {best['composite_score']}")
        print(f"最差组合: 评分>={worst['params']['min_score']} | 持仓{worst['params']['max_hold_days']}天 | 止损{worst['params']['initial_stop_pct']*100:.0f}% → 评分 {worst['composite_score']}")
        print(f"平均评分: {avg_score:.2f}")
        print(f"成功组合: {len(all_results)}/{total_combos}")
        if errors:
            print(f"失败组合: {len(errors)}")

    return output_path


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "20251001"
    end = sys.argv[2] if len(sys.argv) > 2 else "20260424"
    run_scan(start=start, end=end)
