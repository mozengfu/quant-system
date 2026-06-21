#!/usr/bin/env python3
"""
多因子模型回测 - 使用本地因子数据集
因子: RPS动量反转 + 主力意图 + 趋势反转 + 换手率反转 + RSI反转
标签: T+5日前向收益 (target_ret_5d)
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ============ 参数配置 ============
DATA_PATH = Path(__file__).parent.parent / "data" / "factor_dataset" / "factor_2024-01-01_2026-06-09.parquet"
START_DATE = "2025-01-01"
END_DATE = "2026-06-12"
N_FOLDS = 5
REBALANCE_INTERVAL = 5
MAX_POSITIONS = 5
INITIAL_CAPITAL = 100_000.0
STOP_LOSS = -0.05
TAKE_PROFIT = 0.08


def build_composite_score(df):
    """构建5因子合成评分 (反转因子)
    
    数据分析结论:
    - ret_20d → target_ret_5d: IC=-0.061 (强反转)
    - vol_breakout → target_ret_5d: IC=-0.028
    - up_down_ratio_5d → target_ret_5d: IC=-0.026
    - ma5_ma20_diff → target_ret_5d: IC=-0.029
    - turnover_5d → target_ret_5d: IC=-0.072
    - rsi_14 → target_ret_5d: IC=-0.017
    全部为负 → 市场短期反转效应明显
    策略: 选近期弱势股 (低RPS/低换手/低RSI)，期望均值回归
    """
    df = df.copy()
    
    def neg_zscore_per_date(df, col, date_col='trade_date'):
        """按日期计算Z值并取负 (反转: 低因子值→高评分)"""
        def _neg_zscore(g):
            mn, sd = g.mean(), g.std()
            if sd < 1e-9:
                return pd.Series(0.0, index=g.index)
            return -(g - mn) / sd
        return df.groupby(date_col)[col].transform(_neg_zscore)
    
    # 因子1: RPS反转 (近期跌得多的未来跑赢)
    df['f_rps'] = neg_zscore_per_date(df, 'ret_20d')
    
    # 因子2: 成交量萎缩反转
    df['f_main'] = (neg_zscore_per_date(df, 'vol_breakout') * 0.5 +
                    neg_zscore_per_date(df, 'up_down_ratio_5d') * 0.5)
    
    # 因子3: 趋势反转 (下跌趋势的未来跑赢)
    df['f_trend'] = neg_zscore_per_date(df, 'ma5_ma20_diff')
    
    # 因子4: 换手率反转 (低换手率的未来跑赢)
    df['f_turnover'] = neg_zscore_per_date(df, 'turnover_5d')
    
    # 因子5: RSI反转 (低RSI超卖股未来跑赢)
    df['f_rsi'] = neg_zscore_per_date(df, 'rsi_14')
    
    # 合成评分 (等权)
    df['composite_score'] = (
        df['f_rps'] + df['f_main'] + df['f_trend'] +
        df['f_turnover'] + df['f_rsi']
    ) / 5.0
    
    return df


def calculate_ic(df, score_col='composite_score', ret_col='target_ret_5d'):
    """计算每日RankIC"""
    ic_list = []
    for date, group in df.groupby('trade_date'):
        if len(group) >= 10:
            valid = group[[score_col, ret_col]].dropna()
            if len(valid) >= 10:
                corr, _ = spearmanr(valid[score_col], valid[ret_col])
                if not np.isnan(corr):
                    ic_list.append({'date': date, 'ic': corr})
    return pd.DataFrame(ic_list)


def simulate_trading(df, buy_dates, top_n=MAX_POSITIONS):
    """模拟交易 (基于target_ret_5d作为持仓期收益)"""
    equity_curve = []
    trades = []
    
    for buy_date in buy_dates:
        day_data = df[df['trade_date'] == buy_date].copy()
        if len(day_data) == 0:
            continue
        
        # 选股: 综合评分top (注意: 已经是反转因子, 低→好)
        day_data = day_data.dropna(subset=['composite_score'])
        top_picks = day_data.nlargest(top_n, 'composite_score')
        
        for _, row in top_picks.iterrows():
            ts_code = row['ts_code']
            forward_ret = row.get('target_ret_5d', 0) / 100.0  # 转小数
            if np.isnan(forward_ret):
                continue
            
            # 止损止盈
            ret = forward_ret
            if ret <= STOP_LOSS:
                action = 'stop_loss'
                ret_pct = STOP_LOSS * 100
            elif ret >= TAKE_PROFIT:
                action = 'take_profit'
                ret_pct = TAKE_PROFIT * 100
            else:
                action = 'sell'
                ret_pct = ret * 100
            
            trades.append({
                'date': buy_date, 'ts_code': ts_code,
                'action': action, 'ret_pct': ret_pct,
                'forward_ret': forward_ret
            })
        
        # 当日估值 (简化: 假设等权持有, 用平均收益)
        if trades:
            recent = [t for t in trades if t['date'] == buy_date]
            avg_ret = np.mean([t['forward_ret'] for t in recent]) if recent else 0
            equity_curve.append({
                'date': buy_date,
                'n_picks': len(recent),
                'avg_ret': avg_ret
            })
    
    return equity_curve, trades


def main():
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("多因子模型回测 (因子数据集版)")
    logger.info("=" * 60)
    
    # 加载数据
    logger.info("加载因子数据集...")
    df = pd.read_parquet(DATA_PATH)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df[df['trade_date'] >= pd.Timestamp(START_DATE)]
    df = df[df['trade_date'] <= pd.Timestamp(END_DATE)]
    logger.info(f"  {len(df):,} 行, {df['trade_date'].nunique()} 天, {df['ts_code'].nunique()} 股")
    
    # 构建合成评分
    logger.info("构建5因子合成评分 (反转因子)...")
    df = build_composite_score(df)
    logger.info(f"  composite_score: min={df['composite_score'].min():.3f}, max={df['composite_score'].max():.3f}")
    
    # Walk-Forward 回测
    dates = sorted(df['trade_date'].unique())
    val_size = len(dates) // (N_FOLDS + 1)
    logger.info(f"Walk-Forward: {N_FOLDS}折, val_size={val_size}天/折")
    
    all_results = []
    all_ic = []
    
    for fold in range(N_FOLDS):
        vs = fold * val_size
        ve = vs + val_size if fold < N_FOLDS - 1 else len(dates)
        
        train_dates = dates[:vs]
        val_dates = dates[vs:ve]
        sample_dates = val_dates[::REBALANCE_INTERVAL]
        
        val_df = df[df['trade_date'].isin(val_dates)]
        
        if len(val_df) < 500:
            continue
        
        # IC 计算
        ic_df = calculate_ic(val_df)
        if len(ic_df) > 3:
            rank_ic = ic_df['ic'].mean()
            ic_std = ic_df['ic'].std()
            icir = rank_ic / ic_std * np.sqrt(252 / 5) if ic_std > 0.0 else 0.0
        else:
            rank_ic = 0.0
            ic_std = 0.0
            icir = 0.0
        
        all_ic.extend(ic_df['ic'].tolist())
        
        # 模拟交易
        equity_curve, trades = simulate_trading(val_df, sample_dates)
        
        if trades:
            rets = np.array([t['forward_ret'] for t in trades])
            wins = int((rets > 0).sum())
            total = len(rets)
            win_rate = wins / total * 100.0 if total > 0 else 0.0
            
            # 用平均收益估算净值
            avg_daily_ret = np.mean([e['avg_ret'] for e in equity_curve]) if equity_curve else 0.0
            n_periods = len(equity_curve)
            total_ret = ((1 + avg_daily_ret) ** min(n_periods, 20) - 1) * 100.0 if n_periods > 0 else 0.0
            
            # 最大回撤估算
            cum_rets = np.cumprod([1 + t['forward_ret'] for t in trades])
            peak = np.maximum.accumulate(cum_rets)
            dd = ((cum_rets - peak) / peak).min()
        else:
            total_ret = 0.0
            win_rate = 0.0
            dd = 0.0
        
        result = {
            'fold': fold + 1,
            'val_period': f"{pd.Timestamp(val_dates[0]).strftime('%Y%m%d')}~{pd.Timestamp(val_dates[-1]).strftime('%Y%m%d')}",
            'n_val': len(val_df),
            'rank_ic': round(float(rank_ic), 4),
            'ic_std': round(float(ic_std), 4),
            'icir': round(float(icir), 4),
            'total_ret': round(float(total_ret), 2),
            'win_rate': round(float(win_rate), 1),
            'max_drawdown': round(float(dd * 100), 2),
            'n_rebalances': len(sample_dates),
            'n_trades': len(trades),
        }
        all_results.append(result)
        
        ic_flag = '✅' if abs(rank_ic) > 0.03 else ('⚠️' if abs(rank_ic) > 0 else '❌')
        logger.info(f"  折{fold+1}: IC={rank_ic:.4f} ICIR={icir:.3f} "
                    f"收益={total_ret:.2f}% 胜率={win_rate:.1f}% 回撤={dd*100:.2f}%")
    
    # ============ 汇总 ============
    avg_ic = float(np.mean([r['rank_ic'] for r in all_results]))
    avg_icir = float(np.mean([r['icir'] for r in all_results]))
    avg_ret = float(np.mean([r['total_ret'] for r in all_results]))
    avg_wr = float(np.mean([r['win_rate'] for r in all_results]))
    max_dd = float(np.max([r['max_drawdown'] for r in all_results]))
    
    # 整体IC (所有验证集IC)
    overall_ic = float(np.mean(all_ic)) if all_ic else 0.0
    overall_ic_std = float(np.std(all_ic)) if all_ic else 1.0
    overall_icir = overall_ic / overall_ic_std * np.sqrt(252 / 5) if overall_ic_std > 0.0 else 0.0
    
    print(f"\n{'='*60}")
    print("多因子模型回测结果")
    print(f"{'='*60}")
    print(f"因子: RPS反转 + 成交量萎缩反转 + 趋势反转 + 换手率反转 + RSI反转")
    print(f"方法: 5因子等权, Walk-Forward {N_FOLDS}折验证")
    print("-" * 60)
    
    for r in all_results:
        print(f"  折{r['fold']} ({r['val_period']}): "
              f"IC={r['rank_ic']:.4f} ICIR={r['icir']:.3f} "
              f"收益={r['total_ret']:+.2f}% 胜率={r['win_rate']:.1f}% 回撤={r['max_drawdown']:.2f}%")
    
    print("-" * 60)
    print(f"  ★ 整体 RankIC: {overall_ic:.4f}")
    print(f"  ★ 整体 ICIR:   {overall_icir:.4f}")
    print(f"  平均 RankIC:   {avg_ic:.4f}")
    print(f"  平均 ICIR:     {avg_icir:.4f}")
    print(f"  平均收益:      {avg_ret:+.2f}%")
    print(f"  平均胜率:      {avg_wr:.1f}%")
    print(f"  最大回撤:      {max_dd:.2f}%")
    print(f"{'='*60}")
    print(f"  耗时: {(datetime.now()-start).seconds}s")
    
    # 保存
    output = {
        'params': {
            'start_date': START_DATE, 'end_date': END_DATE,
            'n_folds': N_FOLDS, 'rebalance_interval': REBALANCE_INTERVAL,
            'max_positions': MAX_POSITIONS, 'stop_loss': STOP_LOSS, 'take_profit': TAKE_PROFIT,
            'factors': ['RPS反转(ret_20d)', '成交量萎缩反转(vol_breakout+up_down_ratio)',
                        '趋势反转(ma5_ma20_diff)', '换手率反转(turnover_5d)', 'RSI反转(rsi_14)'],
            'weights': '等权', 'direction': '反转 (做多弱势股)'
        },
        'fold_results': all_results,
        'summary': {
            'overall_rank_ic': round(overall_ic, 4),
            'overall_icir': round(overall_icir, 4),
            'avg_rank_ic': round(avg_ic, 4),
            'avg_icir': round(avg_icir, 4),
            'avg_return': round(avg_ret, 2),
            'avg_win_rate': round(avg_wr, 1),
            'max_drawdown': round(max_dd, 2),
        }
    }
    
    out_path = Path(__file__).parent.parent / 'data' / 'multi_factor_backtest.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"已保存: {out_path}")
    
    return output


if __name__ == '__main__':
    main()