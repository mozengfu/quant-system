#!/usr/bin/env python3
"""
市场状态自适应策略回测
- 趋势市(MA20向上) → 动量策略(追强势股)
- 震荡市(MA20走平) → 反转策略(买弱势股)
- 下跌市(MA20向下) → 空仓/轻仓

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
    """加载每日行情数据"""
    sql = f"""
        SELECT a.trade_date, a.ts_code, a.close, a.amount, a.turnover_rate,
               a.rps_20,
               a.ma5, a.ma10, a.ma20,
               b.pe_ttm, b.pb, b.volume_ratio
        FROM daily_price a
        LEFT JOIN daily_basic b ON a.ts_code = b.ts_code AND a.trade_date = b.trade_date
        WHERE a.trade_date BETWEEN '{start}' AND '{end}'
          AND a.close > 0
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


def compute_market_state(conn, index_code='000001.SH'):
    """
    计算每日大盘状态
    返回: trend(向上)/sideways(走平)/bear(向下)
    """
    # 从market_index_daily获取指数数据
    idx_df = pd.read_sql("""
        SELECT trade_date, close_price FROM market_index_daily
        WHERE index_code = %s AND trade_date >= '20240101'
        ORDER BY trade_date
    """, conn, params=(index_code,))
    
    idx_df['trade_date'] = pd.to_datetime(idx_df['trade_date'])
    idx_df = idx_df.sort_values('trade_date')
    
    # 计算MA20
    idx_df['ma20'] = idx_df['close_price'].rolling(20).mean()
    idx_df['ma5'] = idx_df['close_price'].rolling(5).mean()
    
    # MA20斜率 (20日变化率)
    idx_df['ma20_slope'] = idx_df['ma20'].pct_change(20)
    
    # 状态判断
    def get_state(row):
        slope = row['ma20_slope']
        if pd.isna(slope):
            return 'sideways'
        elif slope > 0.015:  # 月均>1.5%上涨 = 趋势向上
            return 'trend'
        elif slope < -0.015:  # 月均>1.5%下跌 = 趋势向下
            return 'bear'
        else:
            return 'sideways'
    
    idx_df['state'] = idx_df.apply(get_state, axis=1)
    
    return idx_df[['trade_date', 'close_price', 'ma20', 'state']]


def compute_stock_factors(day_df, state):
    """
    根据市场状态计算股票因子评分
    - trend市: 动量因子(RPS高+均线多头)
    - sideways市: 反转因子(RPS低+超卖)
    - bear市: 空仓
    """
    scores = pd.DataFrame(index=day_df.index)
    
    if state == 'trend':
        # 动量因子: RPS越高越好,均线多头排列
        rps = day_df['rps_20'].fillna(50)
        scores['rps'] = (rps - rps.median()) / (rps.std() + 1e-9)
        
        # 均线多头
        ma_score = np.zeros(len(day_df))
        mask1 = (day_df['ma5'] > day_df['ma10']) & (day_df['ma10'] > day_df['ma20'])
        mask2 = day_df['close'] > day_df['ma20']
        ma_score[mask1 & mask2] = 1.0
        ma_score[mask2 & ~mask1] = 0.5
        scores['trend'] = ma_score
        
        # 综合: RPS 60% + 趋势 40%
        day_df['factor_score'] = scores['rps'] * 0.6 + scores['trend'] * 0.4
        
    elif state == 'sideways':
        # 反转因子: RPS低+换手低+价格低位
        rps = day_df['rps_20'].fillna(50)
        scores['rps_rev'] = -(rps - rps.median()) / (rps.std() + 1e-9)
        
        # 换手率低
        tr = day_df['turnover_rate'].fillna(0) * 100
        tr_med = tr.median()
        scores['tr_rev'] = -(tr - tr_med) / (tr.std() + 1e-9)
        
        # 价格位置 (相对20日均线)
        ma20 = day_df['ma20'].fillna(0)
        price_pos = (day_df['close'] - ma20) / (ma20 + 1e-9)
        scores['price_rev'] = -price_pos  # 低于均线好
        
        day_df['factor_score'] = (scores['rps_rev'] + scores['tr_rev'] + scores['price_rev']) / 3
        
    else:  # bear
        # 空仓,所有股票评分=0
        day_df['factor_score'] = 0
    
    day_df['factor_rank'] = day_df.groupby('trade_date')['factor_score'].rank(ascending=False)
    
    return day_df


def run_backtest(df, market_states, initial_capital=100000, top_n=20, hold_days=5):
    """执行回测"""
    trade_dates = sorted(df['trade_date'].unique())
    
    # 预计算每只股票的5日后价格
    df = df.sort_values(['ts_code', 'trade_date'])
    df['price_future'] = df.groupby('ts_code')['close'].shift(-hold_days)
    
    positions = {}
    trades = []
    equity = []
    capital = initial_capital
    
    # 当前状态缓存
    current_state = 'sideways'
    
    for i, td in enumerate(trade_dates):
        # 获取当日大盘状态
        state_row = market_states[market_states['trade_date'] == td]
        if not state_row.empty:
            current_state = state_row.iloc[0]['state']
        
        day_df = df[df['trade_date'] == td].copy()
        
        # 计算当日因子评分
        day_df = compute_stock_factors(day_df, current_state)
        
        # === 卖出逻辑 ===
        to_sell = []
        for ts, pos in list(positions.items()):
            cur_row = day_df[day_df['ts_code'] == ts]
            if cur_row.empty:
                continue
            cur_price = cur_row.iloc[0]['close']
            pnl = (cur_price - pos['cost']) / pos['cost']
            
            if pnl <= -0.05 or pnl >= 0.08 or current_state == 'bear':
                to_sell.append(ts)
                proceeds = cur_price * pos['shares']
                capital += proceeds
                trades.append({
                    'date': td, 'ts_code': ts, 'action': 'sell',
                    'price': cur_price, 'shares': pos['shares'], 'pnl': pnl
                })
        
        for ts in to_sell:
            del positions[ts]
        
        # === 买入逻辑 ===
        if i > 0 and i % hold_days == 0 and current_state != 'bear':
            candidates = day_df.sort_values('factor_rank')
            
            # trend市:选动量股(评分高)  sideways市:选反转股(评分低但>0)
            if current_state == 'trend':
                buy_df = candidates[candidates['factor_score'] > 0].head(top_n)
            else:
                buy_df = candidates[candidates['factor_score'] > 0].head(top_n)
            
            available = [c for c in buy_df['ts_code'].tolist() if c not in positions]
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
                trades.append({
                    'date': td, 'ts_code': ts, 'action': 'buy',
                    'price': price, 'shares': shares, 'pnl': 0
                })
        
        # === 计算市值 ===
        pos_value = 0
        for ts, pos in positions.items():
            price_row = day_df[day_df['ts_code'] == ts]
            if price_row.empty:
                continue
            cur_price = price_row.iloc[0]['close']
            pos_value += cur_price * pos['shares']
        
        total = capital + pos_value
        equity.append({'date': td, 'state': current_state, 'total': total, 'positions': len(positions)})
    
    # === 统计 ===
    eq_df = pd.DataFrame(equity)
    total_return = (eq_df['total'].iloc[-1] - initial_capital) / initial_capital
    
    peak = eq_df['total'].cummax()
    max_dd = ((eq_df['total'] - peak) / peak).min()
    
    sell_trades = [t for t in trades if t['action'] == 'sell' and t['pnl'] != 0]
    win_rate = len([t for t in sell_trades if t['pnl'] > 0]) / max(len(sell_trades), 1)
    
    wins = [t['pnl'] for t in sell_trades if t['pnl'] > 0]
    losses = [t['pnl'] for t in sell_trades if t['pnl'] < 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 0.001
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    # 各状态天数
    state_days = eq_df.groupby('state')['date'].count().to_dict()
    
    # 每日IC计算
    ic_records = []
    for td in trade_dates:
        state_row = market_states[market_states['trade_date'] == td]
        if state_row.empty:
            continue
        state = state_row.iloc[0]['state']
        if state == 'bear':
            continue  # 空仓不计算IC
        
        day_df = df[df['trade_date'] == td].dropna(subset=['price_future'])
        if len(day_df) < 20:
            continue
        
        day_df = compute_stock_factors(day_df, state)
        factor_rank = day_df.set_index('ts_code')['factor_rank']
        future_ret = (day_df.set_index('ts_code')['price_future'] - day_df.set_index('ts_code')['close']) / day_df.set_index('ts_code')['close']
        
        common = factor_rank.index.intersection(future_ret.index)
        if len(common) < 20:
            continue
        
        ic, _ = stats.spearmanr(factor_rank.loc[common], future_ret.loc[common])
        if not np.isnan(ic):
            ic_records.append({'date': td, 'ic': ic, 'state': state})
    
    ic_df = pd.DataFrame(ic_records)
    
    result = {
        '总收益率': f"{total_return*100:.2f}%",
        '最终资产': f"{eq_df['total'].iloc[-1]:.2f}",
        '最大回撤': f"{max_dd*100:.2f}%",
        '胜率': f"{win_rate*100:.1f}%",
        '盈亏比': f"{pl_ratio:.2f}",
        '买入次数': len([t for t in trades if t['action'] == 'buy']),
        '卖出次数': len(sell_trades),
        '趋势市天数': state_days.get('trend', 0),
        '震荡市天数': state_days.get('sideways', 0),
        '下跌市天数': state_days.get('bear', 0),
    }
    
    if not ic_df.empty:
        ic_mean = ic_df['ic'].mean()
        ic_std = ic_df['ic'].std()
        icir = ic_mean / ic_std if ic_std > 0.001 else 0
        ic_pos = (ic_df['ic'] > 0).mean()
        result['RankIC均值'] = f"{ic_mean:.4f}"
        result['ICIR'] = f"{icir:.2f}"
        result['IC>0比例'] = f"{ic_pos*100:.1f}%"
    else:
        result['RankIC均值'] = 'N/A'
        result['ICIR'] = 'N/A'
        result['IC>0比例'] = 'N/A'
    
    return result, trades, eq_df, ic_df


def main():
    logger.info("=" * 60)
    logger.info("市场状态自适应策略回测")
    logger.info("=" * 60)
    
    conn = get_conn()
    
    logger.info("\n计算大盘状态...")
    market_states = compute_market_state(conn)
    state_counts = market_states['state'].value_counts()
    logger.info(f"  趋势市: {state_counts.get('trend', 0)}天")
    logger.info(f"  震荡市: {state_counts.get('sideways', 0)}天")
    logger.info(f"  下跌市: {state_counts.get('bear', 0)}天")
    
    logger.info("\n加载数据...")
    df = load_data(conn)
    conn.close()
    
    logger.info("\n执行回测...")
    result, trades, eq_df, ic_df = run_backtest(df, market_states)
    
    logger.info("\n" + "=" * 60)
    logger.info("回测结果")
    logger.info("=" * 60)
    for k, v in result.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 60)
    
    # 保存结果
    import json
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/adaptive_strategy_result.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    
    result_save = {k: v for k, v in result.items()}
    with open(out_path, 'w') as f:
        json.dump(result_save, f, indent=2)
    
    # Equity曲线
    eq_path = os.path.expanduser('~/.openclaw/workspace/memory/adaptive_strategy_equity.csv')
    eq_df.to_csv(eq_path, index=False)
    
    logger.info(f"\n结果已保存: {out_path}")
    logger.info(f" equity已保存: {eq_path}")
    
    return result


if __name__ == '__main__':
    main()