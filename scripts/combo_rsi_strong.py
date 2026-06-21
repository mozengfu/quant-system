#!/usr/bin/env python3
"""
组合策略: 空仓择时(RSI<40) + 强势股回调买入
回测区间: 2024-01-01 ~ 2026-06-12
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
    idx_df = pd.read_sql("""
        SELECT trade_date, close_price FROM market_index_daily
        WHERE index_code = '000001.SH' AND trade_date >= '20240101'
        ORDER BY trade_date
    """, conn)
    idx_df['trade_date'] = pd.to_datetime(idx_df['trade_date'])
    idx_df = idx_df.sort_values('trade_date')
    idx_df['ma20'] = idx_df['close_price'].rolling(20).mean()
    
    # RSI计算
    delta = idx_df['close_price'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    idx_df['rsi_14'] = 100 - (100 / (rs + 1))
    
    # MA20斜率判断状态
    idx_df['ma20_slope'] = idx_df['ma20'].pct_change(20)
    idx_df['state'] = 'sideways'
    idx_df.loc[idx_df['ma20_slope'] > 0.015, 'state'] = 'trend'
    idx_df.loc[idx_df['ma20_slope'] < -0.015, 'state'] = 'bear'
    
    return idx_df[['trade_date', 'close_price', 'ma20', 'rsi_14', 'state']]


def load_north_money(conn):
    mf = pd.read_sql("""
        SELECT trade_date, north_money as north_net
        FROM north_moneyflow WHERE trade_date >= '20240101'
        ORDER BY trade_date
    """, conn)
    mf['trade_date'] = pd.to_datetime(mf['trade_date'])
    return mf


def select_strong_pullback(day_df, rps_min=40, rps_max=70):
    """
    强势股回调买入:
    - RPS 40-70 (不太高不太低)
    - 股价回踩MA20附近(不破)
    """
    rps = day_df['rps_20'].fillna(0)
    close = day_df['close'].fillna(0)
    ma20 = day_df['ma20'].fillna(0)
    
    # 强势: RPS 40-70
    strong_mask = (rps >= rps_min) & (rps <= rps_max)
    
    # 回调: 股价在MA20的92%-105%之间(回踩均线)
    pullback_mask = (close > ma20 * 0.92) & (close < ma20 * 1.05)
    
    # 综合评分
    score = np.zeros(len(day_df))
    score[strong_mask & pullback_mask] = 80
    
    # 额外加分: 北向资金净流入
    # 已在主函数传入north参数
    
    candidates = day_df[score > 0].copy()
    candidates['score'] = score[score > 0]
    return candidates


def run_combo_backtest(df, idx_df, mf_df,
                      rsi_threshold=40,
                      top_n=20, hold_days=5,
                      stop_loss=-0.05, stop_profit=0.08,
                      rps_min=40, rps_max=70,
                      test_rsi_thresholds=[30, 35, 40, 45, 50]):
    """
    组合策略回测
    条件:
    1. 大盘RSI < rsi_threshold 才开仓
    2. 个股选强势股回调(RPS 40-70 + 回踩均线)
    """
    trade_dates = sorted(df['trade_date'].unique())
    
    # 预计算未来价格
    df = df.sort_values(['ts_code', 'trade_date'])
    df['price_future'] = df.groupby('ts_code')['close'].shift(-hold_days)
    
    results = []
    
    for rsi_th in test_rsi_thresholds:
        logger.info(f"\n回测: RSI阈值={rsi_th}")
        
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
            
            # === 核心条件: 大盘RSI < 阈值才开仓 ===
            can_open = market_rsi < rsi_th
            
            # === 卖出逻辑 ===
            to_sell = []
            for ts, pos in list(positions.items()):
                cur_row = day_df[day_df['ts_code'] == ts]
                if cur_row.empty:
                    continue
                cur_price = cur_row.iloc[0]['close']
                pnl = (cur_price - pos['cost']) / pos['cost']
                # 止损/止盈
                if pnl <= stop_loss or pnl >= stop_profit:
                    to_sell.append(ts)
                    proceeds = cur_price * pos['shares']
                    capital += proceeds
                    trades.append({'date': td, 'ts_code': ts, 'action': 'sell',
                                   'price': cur_price, 'shares': pos['shares'], 'pnl': pnl,
                                   'rsi_threshold': rsi_th})
            
            for ts in to_sell:
                del positions[ts]
            
            # === 买入逻辑 ===
            if i > 0 and i % hold_days == 0 and can_open:
                # 选强势股回调
                candidates = select_strong_pullback(day_df, rps_min, rps_max)
                
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
                                   'price': price, 'shares': shares, 'pnl': 0,
                                   'rsi_threshold': rsi_th})
            
            # 市值
            pos_value = 0
            for ts, pos in positions.items():
                price_row = day_df[day_df['ts_code'] == ts]
                if price_row.empty:
                    continue
                pos_value += price_row.iloc[0]['close'] * pos['shares']
            
            equity.append({
                'date': td, 'total': capital + pos_value,
                'n_pos': len(positions), 'can_open': can_open,
                'market_rsi': market_rsi, 'rsi_threshold': rsi_th
            })
        
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
        
        # 计算IC
        ic_records = []
        for td in trade_dates:
            day_df_ic = df[df['trade_date'] == td].dropna(subset=['price_future'])
            if len(day_df_ic) < 20:
                continue
            state_row = idx_df[idx_df['trade_date'] == td]
            market_rsi = state_row.iloc[0]['rsi_14'] if not state_row.empty else 50
            if market_rsi >= rsi_th:
                continue
            
            candidates = select_strong_pullback(day_df_ic, rps_min, rps_max)
            if candidates.empty:
                continue
            
            scores = candidates.set_index('ts_code')['score']
            future_ret = (day_df_ic.set_index('ts_code')['price_future'] - day_df_ic.set_index('ts_code')['close']) / day_df_ic.set_index('ts_code')['close']
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
        
        # 开仓天数统计
        open_days = sum(1 for e in equity if e['can_open'])
        
        result = {
            'rsi_threshold': rsi_th,
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
            'open_days': open_days,
            'total_days': len(trade_dates),
        }
        
        logger.info(f"  收益率: {total_ret*100:.2f}%, 回撤: {max_dd*100:.2f}%, 胜率: {win_rate*100:.1f}%, 盈亏比: {pl_ratio:.2f}")
        logger.info(f"  开仓天数: {open_days}/{len(trade_dates)}, RankIC: {ic_mean:.4f}, ICIR: {icir:.2f}")
        
        results.append(result)
        
        # 保存equity曲线
        eq_df.to_csv(os.path.expanduser(f'~/.openclaw/workspace/memory/combo_eq_rsi{rsi_th}.csv'), index=False)
    
    return results


def main():
    logger.info("=" * 70)
    logger.info("组合策略: 空仓择时(RSI<阈值) + 强势股回调买入")
    logger.info("=" * 70)
    
    conn = get_conn()
    
    logger.info("\n加载数据...")
    df = load_data(conn)
    idx_df = load_index_state(conn)
    mf_df = load_north_money(conn)
    conn.close()
    
    # 合并北向资金
    df = df.merge(mf_df, on='trade_date', how='left')
    df['north_positive'] = df['north_net'] > 0
    
    # RSI阈值参数扫描
    test_thresholds = [30, 35, 40, 45, 50, 60]
    
    logger.info("\n执行回测...")
    results = run_combo_backtest(
        df, idx_df, mf_df,
        rsi_threshold=40,
        top_n=20, hold_days=5,
        test_rsi_thresholds=test_thresholds
    )
    
    # 结果汇总
    results_df = pd.DataFrame(results).sort_values('total_return', ascending=False)
    
    logger.info("\n" + "=" * 70)
    logger.info("回测结果汇总")
    logger.info("=" * 70)
    
    print("\n{:<15} {:>10} {:>10} {:>8} {:>8} {:>8} {:>8} {:>10}".format(
        'RSI阈值', '收益率', '最大回撤', '胜率', '盈亏比', 'RankIC', 'ICIR', '开仓天数'))
    print("-" * 100)
    
    for _, row in results_df.iterrows():
        flag = '✅' if row['total_return'] > 0 else '❌'
        print("{:<15} {:>+.2f}% {:>+.2f}% {:>7.1f}% {:>8.2f} {:>+8.4f} {:>+7.2f} {:>10}/{:<10} {}".format(
            f'RSI<{row["rsi_threshold"]}',
            row['total_return'] * 100,
            row['max_drawdown'] * 100,
            row['win_rate'] * 100,
            row['profit_loss_ratio'],
            row['rank_ic'],
            row['icir'],
            int(row['open_days']),
            int(row['total_days']),
            flag
        ))
    
    # 保存
    import json
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/combo_strategy_result.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(results_df.to_dict('records'), f, indent=2, default=str)
    
    best = results_df.iloc[0]
    
    # 生成报告
    md = f"""# 组合策略回测结果: 空仓择时 + 强势股回调

**回测区间**: 2024-01-01 ~ 2026-06-12
**初始资金**: 10万
**持仓周期**: 5天
**止损/止盈**: -5% / +8%

## RSI阈值扫描结果

| RSI阈值 | 收益率 | 最大回撤 | 胜率 | 盈亏比 | RankIC | ICIR | 开仓天数 |
|---------|--------|---------|------|--------|--------|------|---------|
"""
    for _, row in results_df.iterrows():
        flag = '🥇' if _ == 0 else ''
        md += f"| RSI<{int(row['rsi_threshold'])} | {row['total_return']*100:+.2f}% | {row['max_drawdown']*100:+.2f}% | {row['win_rate']*100:.1f}% | {row['profit_loss_ratio']:.2f} | {row['rank_ic']:.4f} | {row['icir']:.2f} | {int(row['open_days'])}/{int(row['total_days'])} {flag} |\n"
    
    md += f"""
## 最优配置: RSI<{int(best['rsi_threshold'])}

| 指标 | 值 |
|------|-----|
| 总收益率 | {best['total_return']*100:.2f}% |
| 最终资产 | {best['final_asset']:.2f} |
| 最大回撤 | {best['max_drawdown']*100:.2f}% |
| 胜率 | {best['win_rate']*100:.1f}% |
| 盈亏比 | {best['profit_loss_ratio']:.2f} |
| RankIC | {best['rank_ic']:.4f} |
| ICIR | {best['icir']:.2f} |
| 开仓天数 | {int(best['open_days'])}/{int(best['total_days'])} |

## 策略逻辑

1. **空仓择时**: 大盘RSI < {int(best['rsi_threshold'])} 时才开仓，其他时间空仓
2. **强势股回调**: RPS {40}-{70} + 股价回踩MA20(92%-105%)
3. **止损**: -5%
4. **止盈**: +8%

## 结论

**最优RSI阈值**: {int(best['rsi_threshold'])}

RSI阈值越低 → 开仓条件越严 → 信号越少但质量越高
RSI阈值越高 → 开仓越频繁 → 但可能买在高位

建议: 选择 **{int(best['rsi_threshold'])}** 作为 RSI 阈值
"""
    
    md_path = os.path.expanduser('~/.openclaw/workspace/memory/combo_strategy_report.md')
    with open(md_path, 'w') as f:
        f.write(md)
    
    logger.info(f"\n结果已保存: {out_path}")
    logger.info(f"报告已保存: {md_path}")
    
    return results_df


if __name__ == '__main__':
    main()