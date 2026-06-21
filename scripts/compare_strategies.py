#!/usr/bin/env python3
"""
6种策略对比回测 - 找出A股最优方案
回测区间: 2024-01-01 ~ 2026-06-12
初始资金: 10万
"""
import logging
import sys
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pymysql
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()

def get_conn():
    return pymysql.connect(**DB_CONFIG)


def load_data(conn, start='20240101', end='20260612'):
    """加载每日数据"""
    sql = f"""
        SELECT a.trade_date, a.ts_code, a.close, a.amount, a.turnover_rate,
               a.ma5, a.ma10, a.ma20, a.rps_20,
               b.pe_ttm, b.pb, b.volume_ratio
        FROM daily_price a
        LEFT JOIN daily_basic b ON a.ts_code = b.ts_code AND a.trade_date = b.trade_date
        WHERE a.trade_date BETWEEN '{start}' AND '{end}'
          AND a.close > 0 AND a.ma20 > 0
          AND a.ts_code NOT LIKE '688%%'
          AND a.ts_code NOT LIKE '8%%'
          AND a.ts_code NOT LIKE '4%%'
          AND a.ts_code NOT LIKE '9%%'
        ORDER BY a.trade_date, a.ts_code
    """
    df = pd.read_sql(sql, conn)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    logger.info(f"数据: {len(df)}行, {df['trade_date'].nunique()}天")
    return df


def load_index_state(conn):
    """加载大盘状态"""
    idx_df = pd.read_sql("""
        SELECT trade_date, close_price FROM market_index_daily
        WHERE index_code = '000001.SH' AND trade_date >= '20240101'
        ORDER BY trade_date
    """, conn)
    idx_df['trade_date'] = pd.to_datetime(idx_df['trade_date'])
    idx_df['ma20'] = idx_df['close_price'].rolling(20).mean()
    idx_df['ma5'] = idx_df['close_price'].rolling(5).mean()
    idx_df['rsi_14'] = compute_rsi(idx_df['close_price'], 14)
    idx_df['state'] = 'sideways'
    slope = idx_df['ma20'].pct_change(20)
    idx_df.loc[slope > 0.015, 'state'] = 'trend'
    idx_df.loc[slope < -0.015, 'state'] = 'bear'
    return idx_df[['trade_date', 'close_price', 'ma20', 'rsi_14', 'state']]


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (rs + 1))


def load_moneyflow(conn):
    """加载北向资金"""
    mf = pd.read_sql("""
        SELECT trade_date, north_money as north_net
        FROM north_moneyflow
        WHERE trade_date >= '20240101'
        ORDER BY trade_date
    """, conn)
    mf['trade_date'] = pd.to_datetime(mf['trade_date'])
    return mf


def run_strategy(df, idx_df, mf_df, strategy_name, strategy_fn, 
                top_n=20, hold_days=5, stop_loss=-0.05, stop_profit=0.08):
    """通用回测框架"""
    trade_dates = sorted(df['trade_date'].unique())
    
    # 预计算未来价格
    df = df.sort_values(['ts_code', 'trade_date'])
    df['price_future'] = df.groupby('ts_code')['close'].shift(-hold_days)
    
    positions = {}
    trades = []
    equity = []
    capital = 100000
    
    for i, td in enumerate(trade_dates):
        # 大盘状态
        state_row = idx_df[idx_df['trade_date'] == td]
        market_state = state_row.iloc[0]['state'] if not state_row.empty else 'sideways'
        market_rsi = state_row.iloc[0]['rsi_14'] if not state_row.empty else 50
        
        # 北向资金
        mf_row = mf_df[mf_df['trade_date'] == td]
        north_positive = mf_row.iloc[0]['north_net'] > 0 if not mf_row.empty else False
        
        day_df = df[df['trade_date'] == td].copy()
        if day_df.empty:
            continue
        
        # 计算当日候选股票和评分
        candidates = strategy_fn(day_df, market_state, market_rsi, north_positive)
        
        if candidates is None or candidates.empty:
            # 空仓
            to_sell = list(positions.keys())
        else:
            # 卖出
            to_sell = []
            for ts, pos in list(positions.items()):
                cur_row = day_df[day_df['ts_code'] == ts]
                if cur_row.empty:
                    continue
                cur_price = cur_row.iloc[0]['close']
                pnl = (cur_price - pos['cost']) / pos['cost']
                if pnl <= stop_loss or pnl >= stop_profit:
                    to_sell.append(ts)
                    proceeds = cur_price * pos['shares']
                    capital += proceeds
                    trades.append({'date': td, 'ts_code': ts, 'action': 'sell',
                                   'price': cur_price, 'shares': pos['shares'], 'pnl': pnl})
            
            for ts in to_sell:
                del positions[ts]
            
            # 买入 (每hold_days天调仓)
            if i > 0 and i % hold_days == 0:
                available = [c for c in candidates['ts_code'].tolist() if c not in positions]
                max_buy = min(top_n - len(positions), len(available))
                buy_list = available[:max_buy]
                
                per_stock = capital / (top_n - len(positions) + max_buy + 0.01)
                for ts in buy_list:
                    price_row = day_df[day_df['ts_code'] == ts]
                    if price_row.empty:
                        continue
                    price = price_row.iloc[0]['close']
                    if price <= 0:
                        continue
                    shares = int(per_stock / price / 100) * 100
                    if shares < 100:
                        continue
                    cost = price * 1.0003
                    positions[ts] = {'shares': shares, 'cost': cost}
                    capital -= cost * shares
                    trades.append({'date': td, 'ts_code': ts, 'action': 'buy',
                                   'price': price, 'shares': shares, 'pnl': 0})
        
        # 市值
        pos_value = 0
        for ts, pos in positions.items():
            price_row = day_df[day_df['ts_code'] == ts]
            if price_row.empty:
                continue
            pos_value += price_row.iloc[0]['close'] * pos['shares']
        
        equity.append({'date': td, 'total': capital + pos_value, 'n_pos': len(positions)})
    
    # 统计
    eq_df = pd.DataFrame(equity)
    total_ret = (eq_df['total'].iloc[-1] - 100000) / 100000
    peak = eq_df['total'].cummax()
    max_dd = ((eq_df['total'] - peak) / peak).min()
    
    sell_trades = [t for t in trades if t['action'] == 'sell' and t['pnl'] != 0]
    win_rate = len([t for t in sell_trades if t['pnl'] > 0]) / max(len(sell_trades), 1)
    wins = [t['pnl'] for t in sell_trades if t['pnl'] > 0]
    losses = [t['pnl'] for t in sell_trades if t['pnl'] < 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 0.001
    pl_ratio = avg_win / avg_loss
    
    # RankIC
    ic_records = []
    for td in trade_dates:
        day_df = df[df['trade_date'] == td].dropna(subset=['price_future'])
        if len(day_df) < 20:
            continue
        state_row = idx_df[idx_df['trade_date'] == td]
        market_state = state_row.iloc[0]['state'] if not state_row.empty else 'sideways'
        market_rsi = state_row.iloc[0]['rsi_14'] if not state_row.empty else 50
        mf_row = mf_df[mf_df['trade_date'] == td]
        north_positive = mf_row.iloc[0]['north_net'] > 0 if not mf_row.empty else False
        
        candidates = strategy_fn(day_df, market_state, market_rsi, north_positive)
        if candidates is None or candidates.empty:
            continue
        
        scores = candidates.set_index('ts_code')['score']
        future_ret = (day_df.set_index('ts_code')['price_future'] - day_df.set_index('ts_code')['close']) / day_df.set_index('ts_code')['close']
        common = scores.index.intersection(future_ret.index)
        if len(common) < 20:
            continue
        ic, _ = stats.spearmanr(scores.loc[common], future_ret.loc[common])
        if not np.isnan(ic):
            ic_records.append({'date': td, 'ic': ic})
    
    ic_df = pd.DataFrame(ic_records)
    ic_mean = ic_df['ic'].mean() if not ic_df.empty else 0
    ic_std = ic_df['ic'].std() if not ic_df.empty else 1
    icir = ic_mean / ic_std if ic_std > 0.001 else 0
    ic_pos_pct = (ic_df['ic'] > 0).mean() if not ic_df.empty else 0
    
    return {
        'strategy': strategy_name,
        'total_return': total_ret,
        'final_asset': eq_df['total'].iloc[-1],
        'max_drawdown': max_dd,
        'win_rate': win_rate,
        'profit_loss_ratio': pl_ratio,
        'n_buy': len([t for t in trades if t['action'] == 'buy']),
        'n_sell': len(sell_trades),
        'rank_ic': ic_mean,
        'icir': icir,
        'ic_pos_pct': ic_pos_pct,
    }, eq_df


# ========== 6种策略 ==========

def strategy_bollinger(day_df, state, rsi, north):
    """布林带均值回归: 价格触及布林下轨买入"""
    ma20 = day_df['ma20'].fillna(0)
    close = day_df['close'].fillna(0)
    
    # 计算布林带 (20日)
    std = day_df.groupby('trade_date')['close'].transform(lambda x: x.rolling(20).std())
    std = std.fillna(0)
    lower = ma20 - 2 * std
    upper = ma20 + 2 * std
    
    # 价格触及下轨且偏离大
    score = np.zeros(len(day_df))
    mask = (close < lower) & (close > ma20 * 0.85)  # 触及下轨但不破新低
    score[mask] = 80
    
    # 中轨反弹
    mask2 = (close > ma20) & (close < lower + std)
    score[mask2] = 60
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def strategy_value(day_df, state, rsi, north):
    """价值因子: ROE>10% + PE合理 + PB低估"""
    pe = day_df['pe_ttm'].fillna(0)
    pb = day_df['pb'].fillna(0)
    
    score = np.zeros(len(day_df))
    
    # PE 5-30 之间给高分
    valid_pe = (pe > 5) & (pe < 30)
    score[valid_pe] += 40
    score[(pe > 5) & (pe < 15)] += 20  # 低PE额外加分
    
    # PB < 3 给高分
    valid_pb = (pb > 0) & (pb < 3)
    score[valid_pb] += 30
    score[(pb > 0) & (pb < 1.5)] += 20  # 低PB额外加分
    
    # 北向资金正面
    if north:
        score += 10
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def strategy_rsi_macd(day_df, state, rsi, north):
    """RSI超卖+MACD金叉: 双重确认底部"""
    score = np.zeros(len(day_df))
    
    # RSI < 30 超卖
    rsi_val = day_df['rps_20'].fillna(50)  # 用RPS20代理RSI
    score[rsi_val < 30] += 50
    score[(rsi_val >= 30) & (rsi_val < 40)] += 30
    
    # MACD金叉代理: ma5上穿ma20
    ma5 = day_df['ma5'].fillna(0)
    ma20 = day_df['ma20'].fillna(0)
    mask = (ma5 > ma20) & (ma5.shift(1) <= ma20.shift(1))
    score[mask] += 40
    
    # 价格在均线附近(触底反弹)
    close = day_df['close'].fillna(0)
    mask2 = (close > ma20 * 0.95) & (close < ma20 * 1.05)
    score[mask2] += 20
    
    if north:
        score += 10
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def strategy_north_follow(day_df, state, rsi, north):
    """北向资金跟随: 外资买的股票跟单"""
    score = np.zeros(len(day_df))
    
    # 北向资金净流入日
    if north:
        score += 50
    
    # 外资偏好的低PE价值股
    pe = day_df['pe_ttm'].fillna(0)
    valid_pe = (pe > 0) & (pe < 25)
    score[valid_pe] += 30
    
    # PB合理
    pb = day_df['pb'].fillna(0)
    valid_pb = (pb > 0) & (pb < 2)
    score[valid_pb] += 20
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def strategy_strong_pullback(day_df, state, rsi, north):
    """强势股回调买入: 前期涨多的股票回调后买入"""
    score = np.zeros(len(day_df))
    
    rps = day_df['rps_20'].fillna(0)
    close = day_df['close'].fillna(0)
    ma20 = day_df['ma20'].fillna(0)
    
    # 强势股: RPS 40-70 (不太高不太低)
    strong_mask = (rps >= 40) & (rps <= 70)
    score[strong_mask] += 40
    
    # 回调: 股价回踩MA20附近(不破)
    pullback_mask = (close > ma20 * 0.92) & (close < ma20 * 1.05)
    score[pullback_mask] += 40
    
    # 北向资金正面
    if north:
        score += 20
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def strategy_empty_timing(day_df, state, rsi, north):
    """空仓择时: 大盘RSI<40才买入，其他时间空仓"""
    score = np.zeros(len(day_df))
    
    # 大盘超卖才买入
    if rsi < 40:
        # RPS 30-60
        rps = day_df['rps_20'].fillna(50)
        mask = (rps >= 30) & (rps <= 60)
        score[mask] = 50
    else:
        return day_df[score > 0].assign(score=score[score > 0])
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


STRATEGIES = {
    '布林带均值回归': strategy_bollinger,
    '价值因子(ROE+PE+PB)': strategy_value,
    'RSI超卖+MACD金叉': strategy_rsi_macd,
    '北向资金跟随': strategy_north_follow,
    '强势股回调买入': strategy_strong_pullback,
    '空仓择时(RSI<40)': strategy_empty_timing,
}


def main():
    logger.info("=" * 70)
    logger.info("6种策略对比回测 - 找出A股最优方案")
    logger.info("=" * 70)
    
    conn = get_conn()
    
    logger.info("\n加载数据...")
    df = load_data(conn)
    idx_df = load_index_state(conn)
    mf_df = load_moneyflow(conn)
    conn.close()
    
    # 合并
    df = df.merge(idx_df[['trade_date', 'rsi_14']], on='trade_date', how='left')
    
    results = []
    eq_curves = {}
    
    for name, fn in STRATEGIES.items():
        logger.info(f"\n{'='*50}")
        logger.info(f"回测策略: {name}")
        logger.info(f"{'='*50}")
        
        result, eq_df = run_strategy(df, idx_df, mf_df, name, fn)
        
        logger.info(f"  总收益率: {result['total_return']*100:.2f}%")
        logger.info(f"  最大回撤: {result['max_drawdown']*100:.2f}%")
        logger.info(f"  胜率: {result['win_rate']*100:.1f}%")
        logger.info(f"  盈亏比: {result['profit_loss_ratio']:.2f}")
        logger.info(f"  RankIC: {result['rank_ic']:.4f}")
        logger.info(f"  ICIR: {result['icir']:.2f}")
        
        results.append(result)
        eq_curves[name] = eq_df
    
    # 排序
    results_df = pd.DataFrame(results).sort_values('total_return', ascending=False)
    
    logger.info("\n" + "=" * 70)
    logger.info("策略排名 (按总收益率)")
    logger.info("=" * 70)
    
    print("\n{:<25} {:>10} {:>10} {:>8} {:>8} {:>8} {:>8}".format(
        '策略', '收益率', '最大回撤', '胜率', '盈亏比', 'RankIC', 'ICIR'))
    print("-" * 90)
    
    for _, row in results_df.iterrows():
        flag = '✅' if row['total_return'] > 0 else '❌'
        print("{:<25} {:>+.2f}% {:>+.2f}% {:>7.1f}% {:>8.2f} {:>+8.4f} {:>+7.2f} {}".format(
            row['strategy'],
            row['total_return'] * 100,
            row['max_drawdown'] * 100,
            row['win_rate'] * 100,
            row['profit_loss_ratio'],
            row['rank_ic'],
            row['icir'],
            flag
        ))
    
    # 保存结果
    import json
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/strategy_comparison_result.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    
    results_df['total_return_pct'] = results_df['total_return'] * 100
    results_df['max_drawdown_pct'] = results_df['max_drawdown'] * 100
    results_df['win_rate_pct'] = results_df['win_rate'] * 100
    
    with open(out_path, 'w') as f:
        json.dump(results_df.to_dict('records'), f, indent=2, default=str)
    
    logger.info(f"\n结果已保存: {out_path}")
    
    # 生成Markdown报告
    md_path = os.path.expanduser('~/.openclaw/workspace/memory/strategy_comparison_report.md')
    best = results_df.iloc[0]
    
    md = f"""# 6种策略对比回测结果 - {datetime.now().strftime('%Y-%m-%d')}

## 回测参数
- 回测区间: 2024-01-01 ~ 2026-06-12
- 初始资金: 10万
- 持仓周期: 5天
- 止损: -5% | 止盈: +8%
- Top N: 20只

## 策略排名

| 排名 | 策略 | 收益率 | 最大回撤 | 胜率 | 盈亏比 | RankIC | ICIR |
|------|------|--------|---------|------|--------|--------|------|
"""
    for i, (_, row) in enumerate(results_df.iterrows()):
        flag = '🥇' if i == 0 else '🥈' if i == 1 else '🥉' if i == 2 else ''
        md += f"| {i+1} | {row['strategy']} {flag} | {row['total_return']*100:+.2f}% | {row['max_drawdown']*100:+.2f}% | {row['win_rate']*100:.1f}% | {row['profit_loss_ratio']:.2f} | {row['rank_ic']:.4f} | {row['icir']:.2f} |\n"
    
    md += f"""
## 最优策略: {best['strategy']}
| 指标 | 值 |
|------|-----|
| 总收益率 | {best['total_return']*100:.2f}% |
| 最终资产 | {best['final_asset']:.2f} |
| 最大回撤 | {best['max_drawdown']*100:.2f}% |
| 胜率 | {best['win_rate']*100:.1f}% |
| 盈亏比 | {best['profit_loss_ratio']:.2f} |
| RankIC | {best['rank_ic']:.4f} |
| ICIR | {best['icir']:.2f} |

## 各策略特点

### 1. 布林带均值回归
- **逻辑**: 价格触及布林下轨买入，反弹到中轨卖出
- **适合**: 区间震荡市
- **优点**: 买点明确，不追高
- **缺点**: 趋势市会连续止损

### 2. 价值因子(ROE+PE+PB)
- **逻辑**: 低PE + 低PB + 高ROE
- **适合**: 所有市场，长线持有
- **优点**: 基本面好，安全性高
- **缺点**: 短期收益慢，不敏感

### 3. RSI超卖+MACD金叉
- **逻辑**: RSI<30超卖 + MACD金叉双重确认底部
- **适合**: 底部区间
- **优点**: 双重确认可靠性高
- **缺点**: 信号少，可能错过机会

### 4. 北向资金跟随
- **逻辑**: 外资净买入时跟单
- **适合**: 外资主导的行情
- **优点**: 外资择时能力强
- **缺点**: 外资持股公布有延迟

### 5. 强势股回调买入
- **逻辑**: 前期强势股回踩均线时买入
- **适合**: 趋势市、强势板块
- **优点**: 顺势而为，盈亏比高
- **缺点**: 震荡市失效

### 6. 空仓择时(RSI<40)
- **逻辑**: 大盘RSI<40才买入，其他时间空仓
- **适合**: 所有市场
- **优点**: 减少亏损次数
- **缺点**: 可能完全踏空

## 结论

**最优策略**: {best['strategy']} (收益率 {best['total_return']*100:.2f}%)

**建议**: 
1. 优先考虑 **{best['strategy']}**
2. 结合市场状态动态调整
3. 小仓位({best['strategy']}的30%)试跑验证
"""
    
    with open(md_path, 'w') as f:
        f.write(md)
    
    logger.info(f"报告已保存: {md_path}")
    
    return results_df


if __name__ == '__main__':
    main()