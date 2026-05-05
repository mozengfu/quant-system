"""
标准化回测指标计算模块

从各类回测脚本中提取指标计算逻辑，统一口径。
"""
import math
import json

def calc_win_rate(closed_trades):
    """计算胜率"""
    if not closed_trades:
        return 0, 0, 0
    win = [t for t in closed_trades if t.get('pnl', 0) > 0]
    loss = [t for t in closed_trades if t.get('pnl', 0) <= 0]
    win_rate = len(win) / len(closed_trades) * 100
    return win_rate, len(win), len(loss)


def calc_sharpe(daily_values):
    """年化夏普比率（无风险利率=0）"""
    if len(daily_values) < 10:
        return 0
    daily_returns = [(daily_values[j] - daily_values[j-1]) / daily_values[j-1]
                     for j in range(1, len(daily_values))]
    avg_ret = sum(daily_returns) / len(daily_returns)
    var = sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)
    std = math.sqrt(var)
    return (avg_ret / std) * math.sqrt(252) if std > 0 else 0


def calc_max_drawdown(daily_values):
    """最大回撤（百分比）"""
    if not daily_values:
        return 0
    peak = daily_values[0]
    max_dd = 0
    for v in daily_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_profit_loss_ratio(closed_trades):
    """盈亏比 = 平均盈利 / 平均亏损"""
    if not closed_trades:
        return 0, 0, 0
    win = [t for t in closed_trades if t.get('pnl', 0) > 0]
    loss = [t for t in closed_trades if t.get('pnl', 0) <= 0]
    total_win = sum(t['pnl'] for t in win)
    total_loss = abs(sum(t['pnl'] for t in loss))
    avg_win = total_win / len(win) if win else 0
    avg_loss = total_loss / len(loss) if loss else 0
    pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    return pl_ratio, avg_win, avg_loss


def calc_total_return(final_value, initial_cash):
    """总收益率百分比"""
    return (final_value - initial_cash) / initial_cash * 100


def calc_metrics(closed_trades, daily_values, initial_cash):
    """一次性计算所有指标，返回指标字典"""
    total_return = calc_total_return(daily_values[-1], initial_cash) if daily_values else 0
    win_rate, wins, losses = calc_win_rate(closed_trades)
    sharpe = calc_sharpe(daily_values)
    max_dd = calc_max_drawdown(daily_values)
    pl_ratio, avg_win, avg_loss = calc_profit_loss_ratio(closed_trades)

    return {
        "总收益率": round(total_return, 2),
        "年化收益率": None,
        "胜率": round(win_rate, 2),
        "盈利次数": wins,
        "亏损次数": losses,
        "总交易次数": len(closed_trades),
        "盈亏比": round(pl_ratio, 2),
        "平均盈利": round(avg_win, 2),
        "平均亏损": round(avg_loss, 2),
        "夏普比率": round(sharpe, 2),
        "最大回撤": round(max_dd, 2),
    }


def format_summary(result):
    """格式化一行指标摘要"""
    parts = [
        f"总收益率{result.get('总收益率', 'N/A')}%",
        f"胜率{result.get('胜率', 'N/A')}%",
        f"盈亏比{result.get('盈亏比', 'N/A')}",
        f"夏普{result.get('夏普比率', 'N/A')}",
        f"最大回撤{result.get('最大回撤', 'N/A')}%",
    ]
    return " | ".join(parts)
