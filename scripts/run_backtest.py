"""
统一回测入口

用法:
    python3 scripts/run_backtest.py v4 [--start YYYYMMDD] [--end YYYYMMDD] [--score N] [--hold N] [--positions N] [--output PATH]
    python3 scripts/run_backtest.py optimize [--start YYYYMMDD] [--end YYYYMMDD]
    python3 scripts/run_backtest.py analyze
    python3 scripts/run_backtest.py compare [--strategies STR] [--summary]
"""
import os, sys, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_metrics import calc_metrics, format_summary

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)


def cmd_v4(args):
    """运行 V4 组合回测"""
    from backtest_combo_v4 import backtest_combo_v4

    result = backtest_combo_v4(
        start_date=args.start,
        end_date=args.end,
        min_score=args.score,
        max_positions=args.positions,
        max_hold_days=args.hold,
    )

    if "error" in result:
        print(f"回测失败: {result['error']}")
        sys.exit(1)

    output = args.output or os.path.join(DATA_DIR, "backtest_result.json")
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    summary = format_summary(result)
    print(f"\nV4 {'| 总收益率' + str(result.get('总收益率', '')) + '%' + ' | 胜率' + str(result.get('胜率', '')) + '%' + ' | 盈亏比' + str(result.get('盈亏比', '')) + ' | 夏普' + str(result.get('夏普比率', '')) + ' | 最大回撤' + str(result.get('最大回撤', '')) + '%'}")
    print(f"结果已保存: {output}")


def cmd_optimize(args):
    """运行参数扫描"""
    sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
    from optimize_v4_params import run_scan
    result = run_scan(start=args.start, end=args.end)
    if result:
        print(f"\n参数扫描完成，结果已保存到 data/params_scan_v4.json")
        print("运行 python3 scripts/run_backtest.py analyze 查看分析")


def cmd_analyze(args):
    """分析参数扫描结果"""
    sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
    from analyze_params import run_analysis
    run_analysis()


def cmd_compare(args):
    """运行/查看多策略对比"""
    sys.path.insert(0, os.path.join(BASE_DIR, 'scripts'))
    from compare_strategies import run_comparison, print_summary

    if args.summary:
        print_summary()
    else:
        strategies = args.strategies.split(',') if args.strategies else ['v4_combo', 'v41_scan', 'v65_ml']
        run_comparison(strategies)


def main():
    parser = argparse.ArgumentParser(description="统一回测入口")
    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # v4
    p_v4 = subparsers.add_parser('v4', help='运行 V4 组合回测')
    p_v4.add_argument('--start', default='20251001')
    p_v4.add_argument('--end', default='20260424')
    p_v4.add_argument('--score', type=int, default=60)
    p_v4.add_argument('--hold', type=int, default=7)
    p_v4.add_argument('--positions', type=int, default=5)
    p_v4.add_argument('--output', default=None)

    # optimize
    p_opt = subparsers.add_parser('optimize', help='参数网格扫描')
    p_opt.add_argument('--start', default='20251001')
    p_opt.add_argument('--end', default='20260424')

    # analyze
    subparsers.add_parser('analyze', help='分析参数扫描结果')

    # compare
    p_cmp = subparsers.add_parser('compare', help='多策略对比')
    p_cmp.add_argument('--strategies', default=None)
    p_cmp.add_argument('--summary', action='store_true', help='显示已有对比结果')

    args = parser.parse_args()

    if args.command == 'v4':
        cmd_v4(args)
    elif args.command == 'optimize':
        cmd_optimize(args)
    elif args.command == 'analyze':
        cmd_analyze(args)
    elif args.command == 'compare':
        cmd_compare(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
