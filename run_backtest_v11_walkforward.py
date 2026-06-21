#!/usr/bin/env python3
"""
V11.0 Walk-Forward 回测 — 无数据泄露

核心思路：用训练脚本的 purged_walk_forward 做时序交叉验证，
每个 fold 的训练数据独立填充 NaN（防泄漏），然后对验证集做预测和模拟交易。
输出的 IC 和交易收益均为真实样本外（out-of-sample）。

用法:
  python3 run_backtest_v11_walkforward.py [--max_date 2026-05-15] [--n_folds 5] [--output result.json]

与 run_backtest_v11.py 的区别:
  - run_backtest_v11.py: 单一模型(全量数据训练)回测 → 含数据泄露风险
  - run_backtest_v11_walkforward.py: 逐 fold 训练+预测 → 无泄露，真实 OOS 表现
"""
import json
import logging
import os
import sys
from datetime import datetime

import numpy as np
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# 从训练脚本复用数据加载和特征构建
from ml_train_v11_0 import build_features, load_data, purged_walk_forward


def main():
    import argparse
    parser = argparse.ArgumentParser(description='V11.0 Walk-Forward 无泄漏回测')
    parser.add_argument('--max_date', type=str, default=None,
                        help='数据截止日期 (默认: 全部数据)')
    parser.add_argument('--n_folds', type=int, default=5,
                        help='Walk-Forward fold 数 (默认: 5)')
    parser.add_argument('--val_size', type=int, default=60,
                        help='每折验证集天数 (默认: 60)')
    parser.add_argument('--embargo', type=int, default=5,
                        help='Embargo 天数 (默认: 5)')
    parser.add_argument('--output', type=str, default='data/backtest_walkforward_v11.json',
                        help='结果输出路径')
    args = parser.parse_args()

    start = datetime.now()
    logger.info(f"{'='*60}")
    logger.info("V11.0 Walk-Forward 回测 (无泄露)")
    logger.info(f"  数据截止: {args.max_date or '全部'}")
    logger.info(f"  Folds: {args.n_folds}, Val: {args.val_size}d, Embargo: {args.embargo}d")
    logger.info(f"{'='*60}")

    # Step 1: 加载数据
    data = load_data(max_date=args.max_date)
    (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
     margin, dragon_tiger, dragon_tiger_inst, holder_change,
     zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
     block_trade, stock_forecast,
     fina_ind, sector_mf, north_mf, ml_prev,
     limit_list, top_inst_data, regime_data,
     weekly_board_rps, board_cons,  # V11.2 板 RPS (跟 ml_train_v11_0.load_data 对齐)
     min_date, max_date) = data

    logger.info(f"数据加载完成: {len(daily):,} 行, "
                f"窗口: {min_date.date()} ~ {max_date.date()}")

    # Step 2: 构建特征（不填充 NaN，留给 walk-forward 每折独立填充）
    result_tuple = build_features(data, na_fill=False)
    if result_tuple[0].empty:
        logger.error("特征构建失败")
        return
    features, global_medians, feature_cols, use_alpha_features = result_tuple
    logger.info(f"特征构建完成: {len(features):,} 样本, {len(feature_cols)} 特征")

    # Step 3: Walk-Forward 回测（含 IC 验证 + 模拟交易）
    cv_results = purged_walk_forward(
        features, feature_cols,
        n_folds=args.n_folds,
        embargo=args.embargo,
        val_size=args.val_size,
        compute_trades=True,
    )

    if cv_results is None:
        logger.error("Walk-Forward 失败")
        return

    # Step 4: 聚合交易结果
    all_trades = []
    for r in cv_results:
        all_trades.extend(r.get('trades', []))

    trade_summary = {}
    if all_trades:
        trade_rets = np.array([t['fwd_return_pct'] for t in all_trades])
        win_rate = (trade_rets > 0).mean() * 100
        avg_ret = trade_rets.mean()
        total_ret = (1 + trade_rets / 100).prod() - 1
        std_ret = trade_rets.std()
        sharpe = (avg_ret / std_ret * np.sqrt(252 / 5)) if std_ret > 0 else 0
        trade_summary = {
            'n_trades': len(all_trades),
            'win_rate_pct': round(win_rate, 1),
            'avg_return_pct': round(float(avg_ret), 2),
            'total_return_pct': round(float(total_ret * 100), 2),
            'std_return_pct': round(float(std_ret), 2),
            'sharpe': round(float(sharpe), 2),
        }

    # Step 5: 汇总
    avg_ic = np.mean([r['rank_ic'] for r in cv_results])
    avg_daily_ic = np.mean([r['mean_daily_ic'] for r in cv_results])
    avg_icir = np.mean([r['ic_ir'] for r in cv_results])
    avg_spread = np.mean([r['spread'] for r in cv_results])

    result = {
        'params': {
            'n_folds': args.n_folds,
            'val_size': args.val_size,
            'embargo': args.embargo,
            'data_range': f"{min_date.date()} ~ {max_date.date()}",
            'max_date': args.max_date,
        },
        'walk_forward_ic': {
            'avg_rank_ic': round(float(avg_ic), 4),
            'avg_daily_ic': round(float(avg_daily_ic), 4),
            'avg_icir': round(float(avg_icir), 2),
            'avg_spread_bp': round(float(avg_spread * 10000), 1),
            'per_fold': [
                {
                    'fold': r['fold'],
                    'rank_ic': round(float(r['rank_ic']), 4),
                    'daily_ic': round(float(r['mean_daily_ic']), 4),
                    'icir': round(float(r['ic_ir']), 2),
                    'spread_bp': round(float(r['spread'] * 10000), 1),
                    'train_dates': r['train_dates'],
                    'val_dates': r['val_dates'],
                    'n_val': r['n_val'],
                }
                for r in cv_results
            ],
        },
        'walk_forward_trades': trade_summary,
        'metadata': {
            'version': 'v11.0',
            'n_features': len(feature_cols),
            'n_samples': len(features),
            'n_stocks': int(features['ts_code'].nunique()),
            'generated_at': datetime.now().isoformat(),
        },
    }

    # Step 6: 打印报告
    print(f"\n{'='*55}")
    print("V11.0 Walk-Forward 回测报告（无数据泄露）")
    print(f"{'='*55}")
    print(f"  期间: {min_date.date()} ~ {max_date.date()}")
    print(f"  Folds: {args.n_folds}, Val: {args.val_size}d")
    print()
    print("  【Rank IC】")
    print(f"    平均 RankIC: {avg_ic:.4f}")
    print(f"    平均日频IC: {avg_daily_ic:.4f}")
    print(f"    平均 ICIR:   {avg_icir:.2f}")
    print(f"    Top20 Spread: {avg_spread*10000:.1f}bp")
    print()
    if trade_summary:
        print("  【模拟交易 (Top3 × 5日)】")
        print(f"    交易笔数: {trade_summary['n_trades']}")
        print(f"    胜率:     {trade_summary['win_rate_pct']:.1f}%")
        print(f"    平均收益: {trade_summary['avg_return_pct']:+.2f}%")
        print(f"    累积收益: {trade_summary['total_return_pct']:+.2f}%")
        print(f"    夏普:     {trade_summary['sharpe']:.2f}")
        print()

    # Step 7: 保存
    out_path = os.path.join(BASE_DIR, args.output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    logger.info(f"结果已保存: {out_path}")

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"耗时: {elapsed:.1f}s ({elapsed/60:.1f}min)")


if __name__ == '__main__':
    main()
