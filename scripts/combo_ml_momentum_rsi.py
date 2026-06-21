#!/usr/bin/env python3
"""
组合策略: 纯ML基线 + 动量惩罚 + RSI空仓择时

回测区间: 2024-11-01 ~ 2026-05-08 (和生产系统回测同期)
对比:
  ① 纯ML基线 (基准)
  ② 纯ML + 动量惩罚
  ③ 纯ML + RSI空仓择时
  ④ 纯ML + 动量惩罚 + RSI空仓择时 (组合)
"""

import json
import logging
import os
import sys
import warnings
from datetime import datetime

import numpy as np
import pandas as pd
import pymysql
from scipy import stats

warnings.filterwarnings('ignore')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()

# 回测参数（和生产系统同期）
START_DATE = "2024-11-01"
END_DATE = "2026-05-08"
INTERVAL = 5      # 每5天调仓
TOP_N = 3         # 每次选3只
TOP_POOL = 300    # 成交额Top300
HOLD_DAYS = 5     # 持有5天
INIT_CAPITAL = 100000


def get_conn():
    return pymysql.connect(**DB_CONFIG)


def load_ml_model():
    """加载ML模型"""
    model_path = os.path.join(BASE_DIR, "data", "ml_stock_model_v11_0_oos.pkl")
    if not os.path.exists(model_path):
        logger.warning(f"模型不存在: {model_path}")
        return None
    import joblib
    return joblib.load(model_path)


def get_trade_dates(conn, start, end):
    df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        conn, params=(start, end)
    )
    return sorted(df['trade_date'].astype(str).tolist())


def get_index_rsi(conn, date_str, lookback=20):
    """计算上证指数RSI(14)"""
    df = pd.read_sql("""
        SELECT close_price FROM market_index_daily
        WHERE index_code='000001.SH' AND trade_date<=%s
        ORDER BY trade_date DESC LIMIT %s
    """, conn, params=(date_str, lookback + 14))
    if len(df) < 15:
        return 50
    df = df.iloc[::-1]['close_price']
    delta = df.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-9)
    rsi = 100 - (100 / (rs + 1))
    return float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50


def get_top_vol_stocks(conn, date_str, n=TOP_POOL):
    """获取成交额TopN股票（排除ST/科创/北交所）"""
    prev = pd.read_sql(
        "SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s",
        conn, params=(date_str,)
    )
    prev_date = str(prev.iloc[0, 0])
    df = pd.read_sql(f"""
        SELECT ts_code, close, amount, turnover_rate, rps_20,
               ma5, ma10, ma20, pct_chg
        FROM daily_price
        WHERE trade_date = %s
          AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%'
          AND close <= 200 AND close > 0
        ORDER BY amount DESC LIMIT %s
    """, conn, params=(prev_date, n))
    return df


def get_recent_return(conn, ts_code, date_str, days=10):
    """计算近N日涨幅"""
    df = pd.read_sql("""
        SELECT pct_chg FROM daily_price
        WHERE ts_code=%s AND trade_date<%s
        ORDER BY trade_date DESC LIMIT %s
    """, conn, params=(ts_code, date_str, days))
    if len(df) < 2:
        return 0
    rets = df['pct_chg'].iloc[::-1].values / 100
    return float((1 + rets).prod() - 1) * 100


def forward_return(conn, code, buy_date_str, hold=HOLD_DAYS):
    """计算持有期收益率"""
    df = pd.read_sql("""
        SELECT pct_chg FROM daily_price
        WHERE ts_code=%s AND trade_date>=%s ORDER BY trade_date LIMIT %s
    """, conn, params=(code, buy_date_str, hold + 1))
    if len(df) < 2:
        return None
    rets = [float(r[0]) / 100 for r in df['pct_chg'].iloc[1:hold+1].values if r is not None]
    rets = [r for r in rets if not np.isnan(r)]
    if len(rets) == 0:
        return None
    # 成本: 0.03%双向 + 0.1%印花税(卖出)
    cost = 0.0003 * 2 + 0.001
    return float((1 + np.array(rets)).prod() - 1 - cost) * 100


def compute_metrics(returns):
    """计算回测指标"""
    if not returns:
        return {}
    rets = np.array(returns)
    cum_ret = float(np.prod(1 + rets / 100) - 1) * 100
    win_rate = float(np.mean([1 if r > 0 else 0 for r in rets])) * 100
    avg_ret = float(np.mean(rets))
    std_ret = float(np.std(rets))
    sharpe = (avg_ret / 100 * 252 / INTERVAL - 0.02) / (std_ret / 100 * np.sqrt(252 / INTERVAL)) if std_ret > 0.001 else 0

    # 最大回撤
    cumul = np.array([(1 + r / 100) for r in rets])
    cumul = np.cumprod(cumul)
    peak = np.maximum.accumulate(cumul)
    dd = (cumul - peak) / peak
    max_dd = float(np.min(dd)) * 100

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 0.001
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0

    return {
        'cum_return': cum_ret,
        'win_rate': win_rate,
        'avg_return': avg_ret,
        'sharpe': sharpe,
        'max_drawdown': max_dd,
        'profit_loss_ratio': pl_ratio,
        'n_trades': len(rets),
        'n_wins': len(wins),
    }


def run_backtest(conn, dates, use_momentum=False, use_rsi=False, rsi_threshold=40):
    """
    回测纯ML基线（可叠加动量惩罚/RSI择时）
    """
    model = load_ml_model()
    if model is None:
        logger.error("模型加载失败")
        return None

    equity = INIT_CAPITAL
    equity_curve = [equity]
    positions = {}  # {ts_code: {'shares': int, 'cost': float}}
    returns = []
    trade_log = []
    can_open = True

    # ML特征列（需和训练时一致）
    FEATURE_COLS = ['rps_20', 'turnover_rate', 'volume_ratio', 'pct_chg',
                    'close', 'ma5', 'ma10', 'ma20', 'pe_ttm', 'pb']

    for i, td in enumerate(dates):
        if i % INTERVAL != 0:
            equity_curve.append(equity)
            continue

        # === RSI空仓择时 ===
        if use_rsi:
            rsi = get_index_rsi(conn, td)
            can_open = rsi < rsi_threshold
            # logger.info(f"  {td} RSI={rsi:.1f} can_open={can_open}")

        # === 卖出：止损/止盈/调仓 ===
        to_sell = []
        for ts, pos in list(positions.items()):
            cur_df = pd.read_sql("""
                SELECT close FROM daily_price WHERE ts_code=%s AND trade_date<=%s
                ORDER BY trade_date DESC LIMIT 1
            """, conn, params=(ts, td))
            if cur_df.empty:
                continue
            cur_price = float(cur_df.iloc[0, 0])
            pnl = (cur_price - pos['cost']) / pos['cost']
            if pnl <= -0.05 or pnl >= 0.08 or i % INTERVAL == 0:
                to_sell.append((ts, cur_price, pnl))

        for ts, price, pnl in to_sell:
            shares = positions[ts]['shares']
            proceeds = price * shares
            equity += proceeds
            returns.append(pnl * 100)
            trade_log.append({'date': td, 'ts': ts, 'action': 'sell', 'pnl': pnl * 100})
            del positions[ts]

        # === 买入 ===
        if can_open and i % INTERVAL == 0:
            # 获取候选池
            pool_df = get_top_vol_stocks(conn, td, TOP_POOL)
            if pool_df.empty:
                equity_curve.append(equity)
                continue

            # 构造ML特征
            stock_codes = pool_df['ts_code'].tolist()
            feat_rows = []
            for _, row in pool_df.iterrows():
                feat = {col: row.get(col, 0) for col in FEATURE_COLS}
                feat['ts_code'] = row['ts_code']
                # 近10日动量
                feat['momentum_10d'] = get_recent_return(conn, row['ts_code'], td, 10)
                feat_rows.append(feat)

            feat_df = pd.DataFrame(feat_rows)
            feat_df = feat_df.fillna(0)

            # 动量惩罚
            if use_momentum:
                mom = feat_df['momentum_10d'].values
                # 涨幅超过20%的股票惩罚(加分变成减分)
                penalty = np.clip(mom / 100, 0, 0.3) * 0.3
                feat_df['momentum_penalty'] = penalty

            # ML预测
            X = feat_df[FEATURE_COLS].values
            X = np.nan_to_num(X, nan=0, posinf=0, neginf=0)

            try:
                probs = model.predict_proba(X)[:, 1] if model.predict_proba(X).shape[1] > 1 else model.predict(X)
                feat_df['ml_score'] = probs
            except Exception as e:
                logger.warning(f"ML预测失败: {e}")
                feat_df['ml_score'] = 0

            # 动量惩罚：分数 - 惩罚
            if use_momentum:
                feat_df['final_score'] = feat_df['ml_score'] - feat_df.get('momentum_penalty', 0)
            else:
                feat_df['final_score'] = feat_df['ml_score']

            # 排序取TopN
            feat_df = feat_df.sort_values('final_score', ascending=False)
            buy_list = feat_df.head(TOP_N)['ts_code'].tolist()

            # 等权买入
            per_stock = equity / (TOP_N + 0.01)
            for ts in buy_list:
                price_row = pd.read_sql("""
                    SELECT close FROM daily_price WHERE ts_code=%s AND trade_date<=%s
                    ORDER BY trade_date DESC LIMIT 1
                """, conn, params=(ts, td))
                if price_row.empty:
                    continue
                price = float(price_row.iloc[0, 0])
                if price <= 0:
                    continue
                shares = int(per_stock / price / 100) * 100
                if shares < 100:
                    continue
                cost = price * 1.0003
                positions[ts] = {'shares': shares, 'cost': cost}
                equity -= cost * shares
                trade_log.append({'date': td, 'ts': ts, 'action': 'buy', 'price': price})

        equity_curve.append(equity)

    # 最终清仓
    for ts, pos in list(positions.items()):
        cur_df = pd.read_sql("""
            SELECT close FROM daily_price WHERE ts_code=%s AND trade_date<=%s
            ORDER BY trade_date DESC LIMIT 1
        """, conn, params=(ts, td))
        if not cur_df.empty:
            price = float(cur_df.iloc[0, 0])
            equity += price * pos['shares']
            pnl = (price - pos['cost']) / pos['cost']
            returns.append(pnl * 100)
            trade_log.append({'date': td, 'ts': ts, 'action': 'sell', 'pnl': pnl * 100})

    metrics = compute_metrics(returns)
    metrics['equity_curve'] = equity_curve
    metrics['trade_log'] = trade_log[-20:]  # 只保留最后20条

    return metrics


def main():
    logger.info("=" * 70)
    logger.info("组合策略: 纯ML基线 + 动量惩罚 + RSI空仓择时")
    logger.info("=" * 70)

    conn = get_conn()

    # 获取交易日
    dates = get_trade_dates(conn, START_DATE, END_DATE)
    logger.info(f"回测区间: {START_DATE} ~ {END_DATE}, 共{len(dates)}个交易日")

    # 加载模型
    model = load_ml_model()
    if model is None:
        logger.error("无法加载模型，退出")
        return
    logger.info("ML模型加载成功")

    # RSI阈值测试
    rsi_thresholds = [35, 40, 45]
    strategies = [
        {'label': '①纯ML基线', 'use_momentum': False, 'use_rsi': False, 'rsi_th': 40},
        {'label': '②纯ML+动量惩罚', 'use_momentum': True, 'use_rsi': False, 'rsi_th': 40},
        {'label': '③纯ML+RSI择时40', 'use_momentum': False, 'use_rsi': True, 'rsi_th': 40},
        {'label': '④纯ML+动量+RSI40', 'use_momentum': True, 'use_rsi': True, 'rsi_th': 40},
        {'label': '⑤纯ML+动量+RSI35', 'use_momentum': True, 'use_rsi': True, 'rsi_th': 35},
        {'label': '⑥纯ML+动量+RSI45', 'use_momentum': True, 'use_rsi': True, 'rsi_th': 45},
    ]

    results = []
    for s in strategies:
        logger.info(f"\n回测: {s['label']}")
        m = run_backtest(conn, dates,
                         use_momentum=s['use_momentum'],
                         use_rsi=s['use_rsi'],
                         rsi_threshold=s['rsi_th'])
        if m:
            r = {
                'label': s['label'],
                'use_momentum': s['use_momentum'],
                'use_rsi': s['use_rsi'],
                'rsi_th': s['rsi_th'],
                'cum_return': m['cum_return'],
                'win_rate': m['win_rate'],
                'sharpe': m['sharpe'],
                'max_drawdown': m['max_drawdown'],
                'profit_loss_ratio': m['profit_loss_ratio'],
                'n_trades': m['n_trades'],
                'n_wins': m['n_wins'],
                'equity_curve': m['equity_curve'],
            }
            results.append(r)
            logger.info(f"  收益率: {m['cum_return']:+.2f}%, 胜率: {m['win_rate']:.1f}%, "
                        f"夏普: {m['sharpe']:.2f}, 回撤: {m['max_drawdown']:.2f}%")

    conn.close()

    # 结果排序
    results_df = pd.DataFrame(results).sort_values('cum_return', ascending=False)

    logger.info("\n" + "=" * 70)
    logger.info("回测结果汇总")
    logger.info("=" * 70)
    print("\n{:<25} {:>10} {:>8} {:>8} {:>8} {:>10}".format(
        '策略', '累计收益', '胜率', '夏普', '最大回撤', '交易次数'))
    print("-" * 80)
    for _, row in results_df.iterrows():
        flag = '🥇' if _ == 0 else ('🥈' if _ == 1 else ('🥉' if _ == 2 else ''))
        print("{:<25} {:>+10.2f}% {:>7.1f}% {:>8.2f} {:>+8.2f}% {:>10} {}".format(
            row['label'],
            row['cum_return'],
            row['win_rate'],
            row['sharpe'],
            row['max_drawdown'],
            int(row['n_trades']),
            flag
        ))

    # 保存结果
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/combo_ml_momentum_rsi_result.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(results_df.to_dict('records'), f, indent=2, default=str)

    # 生成报告
    md = f"""# 组合策略回测: 纯ML基线 + 动量惩罚 + RSI空仓择时

**回测区间**: {START_DATE} ~ {END_DATE}
**初始资金**: {INIT_CAPITAL}元
**候选池**: 成交额Top{TOP_POOL}
**持仓周期**: {HOLD_DAYS}天

## 回测结果

| 策略 | 累计收益 | 胜率 | 夏普 | 最大回撤 | 交易次数 |
|------|---------|------|------|---------|---------|
"""
    for _, row in results_df.iterrows():
        md += f"| {row['label']} | {row['cum_return']:+.2f}% | {row['win_rate']:.1f}% | {row['sharpe']:.2f} | {row['max_drawdown']:+.2f}% | {int(row['n_trades'])} |\n"

    best = results_df.iloc[0]
    md += f"""
## 最优策略: {best['label']}

| 指标 | 值 |
|------|-----|
| 累计收益 | {best['cum_return']:+.2f}% |
| 最终资产 | {INIT_CAPITAL * (1 + best['cum_return']/100):.2f} |
| 胜率 | {best['win_rate']:.1f}% |
| 夏普 | {best['sharpe']:.2f} |
| 最大回撤 | {best['max_drawdown']:+.2f}% |
| 盈亏比 | {best['profit_loss_ratio']:.2f} |
| 交易次数 | {int(best['n_trades'])} |

## 策略逻辑

| 策略 | 条件 |
|------|------|
| 纯ML基线 | 成交额Top300 → ML预测 → Top3 |
| +动量惩罚 | 近10日涨幅>20%的股票扣分 |
| +RSI空仓 | 大盘RSI<阈值才开仓，否则空仓 |

## 结论

**最优策略**: {best['label']}
"""
    md_path = os.path.expanduser('~/.openclaw/workspace/memory/combo_ml_momentum_rsi_report.md')
    with open(md_path, 'w') as f:
        f.write(md)

    logger.info(f"\n结果已保存: {out_path}")
    logger.info(f"报告已保存: {md_path}")

    return results_df


if __name__ == '__main__':
    main()