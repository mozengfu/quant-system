#!/usr/bin/env python3
"""V8.6 专属参数扫描 — 25 组 pct/bw 组合"""
import os, sys, json, subprocess, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKTEST = os.path.join(PROJECT_DIR, 'scripts', 'backtest_v4_ml_v65_vs_v80.py')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

# 25 组加密扫描
PCT_CANDIDATES = [0.10, 0.15, 0.20, 0.25, 0.30]
BW_CANDIDATES = [0.10, 0.15, 0.20, 0.25, 0.30]

results = []
for pct in PCT_CANDIDATES:
    for bw in BW_CANDIDATES:
        logger.info(f"测试: pct={pct:.2f}, bw={bw:.2f}")
        env = os.environ.copy()
        env['ML_BACKTEST_PCT'] = str(pct)
        env['ML_BACKTEST_BW'] = str(bw)

        proc = subprocess.run(
            [sys.executable, BACKTEST, '--model', 'v8.6'],
            capture_output=True, text=True, cwd=PROJECT_DIR,
            timeout=600, env=env,
        )

        for line in proc.stdout.split('\n'):
            if line.startswith('__RESULT__'):
                json_str = line.replace('__RESULT__', '').replace('__RESULT_END__', '')
                r = json.loads(json_str)
                r['pct_threshold'] = pct
                r['blend_weight'] = bw
                results.append(r)
                total = r['ml_passed'] + r['ml_filtered']
                pass_rate = r['ml_passed'] / total * 100 if total > 0 else 0
                print(f"  → 收益: {r['total_return_pct']:.2f}%, 夏普: {r['sharpe_ratio']:.2f}, "
                      f"胜率: {r['win_rate_pct']:.1f}%, 回撤: {r['max_drawdown_pct']:.2f}%, "
                      f"ML通过率: {pass_rate:.0f}%")
                break

if not results:
    logger.error("调优未获取到任何结果")
    sys.exit(1)

results.sort(key=lambda x: x['total_return_pct'], reverse=True)

print(f"\n{'=' * 90}")
print(f"  V8.6 参数扫描结果 (按收益率降序)")
print(f"{'=' * 90}")
h = f"  {'pct_th':>6s} {'bw':>4s} {'收益率':>8s} {'夏普':>6s} {'胜率':>6s} {'交易':>4s} {'盈亏比':>6s} {'回撤':>8s} {'ML通过率':>8s}"
print(h)
print(f"  {'-' * 88}")

for r in results:
    total = r['ml_passed'] + r['ml_filtered']
    pass_rate = r['ml_passed'] / total * 100 if total > 0 else 0
    print(
        f"  {r['pct_threshold']:>5.2f}  {r['blend_weight']:>3.2f}  "
        f"{r['total_return_pct']:>6.2f}%  {r['sharpe_ratio']:>5.2f}  "
        f"{r['win_rate_pct']:>5.1f}%  {r['total_trades']:>3d}  {r['profit_factor']:>5.2f}  "
        f"{r['max_drawdown_pct']:>6.2f}%  {pass_rate:>6.0f}%"
    )

best = results[0]
print(f"\n  ★ V8.6 最优: pct={best['pct_threshold']}, bw={best['blend_weight']}")
print(f"    收益: {best['total_return_pct']:.2f}%, 夏普: {best['sharpe_ratio']:.2f}, 回撤: {best['max_drawdown_pct']:.2f}%")

# 对比 V8.0 同参数
print(f"\n  对比 V8.0 (pct=0.10, bw=0.10): 收益 +40.11%, 夏普 1.82, 回撤 22.11%")
diff_ret = best['total_return_pct'] - 40.11
print(f"  V8.6 最优 vs V8.0 收益差: {diff_ret:+.2f}%")

out_path = os.path.join(PROJECT_DIR, 'data', 'backtest_v8_6_tune.json')
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({'tune_results': results, 'best': {
        'pct_threshold': best['pct_threshold'],
        'blend_weight': best['blend_weight'],
        'total_return_pct': best['total_return_pct'],
        'sharpe_ratio': best['sharpe_ratio'],
        'max_drawdown_pct': best['max_drawdown_pct'],
    }}, f, ensure_ascii=False, indent=2)
print(f"\n  调优结果已保存: {out_path}")
