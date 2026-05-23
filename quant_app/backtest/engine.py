"""统一回测引擎。

用法:
    from quant_app.backtest.engine import BacktestEngine

    engine = BacktestEngine(top_candidates=300, top_n=3, hold_days=5)
    result = engine.run("2024-11-01", "2026-05-08", signal_fn=my_signal_fn)
    print(f"收益: {result.total_return:.1f}%, 胜率: {result.win_rate:.1f}%")

signal_fn 签名:
    def my_signal_fn(trade_date: str) -> list[str]:
        # 返回当日推荐股票代码列表
        return ["000001.SZ", "000002.SZ", ...]
"""
import logging
from dataclasses import dataclass, field

import numpy as np
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()


@dataclass
class TradeRecord:
    entry_date: str
    exit_date: str
    ts_code: str
    entry_price: float
    exit_price: float
    pct_return: float


@dataclass
class BacktestResult:
    trades: list[TradeRecord] = field(default_factory=list)
    total_return: float = 0.0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    n_trades: int = 0
    n_wins: int = 0
    nav_values: list[float] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"回测结果: {self.n_trades} 笔交易, "
            f"累积收益 {self.total_return:.2f}%, "
            f"胜率 {self.win_rate:.1f}%, "
            f"盈亏比 {self.profit_loss_ratio:.2f}, "
            f"夏普 {self.sharpe:.2f}, "
            f"最大回撤 {self.max_drawdown:.2f}%"
        )


class BacktestEngine:
    """统一回测引擎。

    核心逻辑:
    1. 获取交易日期列表
    2. 按采样间隔生成信号（由调用方提供 signal_fn）
    3. 按持有期计算每笔交易的实际收益
    4. 计算统计指标

    与 quant-system 现有回测脚本的对比:
    - scripts/backtest_pure_ml_clean.py: 防数据泄漏版本，用前一日成交额
    - scripts/backtest_current_pipeline.py: 完整管线回测（含风控/游资/业绩）
    - scripts/backtest_v4_ml_v65_vs_v80.py: 多模型对比回测
    本引擎通过 signal_fn 回调统一以上所有场景。
    """

    def __init__(
        self,
        top_candidates: int = 300,
        top_n: int = 3,
        hold_days: int = 5,
        sample_interval: int = 5,
        use_prev_amount: bool = True,
    ):
        """
        Args:
            top_candidates: 候选池大小（成交额 Top N）
            top_n: 每次买入数量
            hold_days: 持有天数
            sample_interval: 采样间隔（每 N 个交易日生成一次信号）
            use_prev_amount: 用前一日成交额排序选股（防数据泄漏）
        """
        self.top_candidates = top_candidates
        self.top_n = top_n
        self.hold_days = hold_days
        self.sample_interval = sample_interval
        self.use_prev_amount = use_prev_amount

    def run(
        self,
        start_date: str,
        end_date: str,
        signal_fn,
        conn=None,
    ) -> BacktestResult:
        """执行回测。

        Args:
            start_date: 开始日期 (YYYY-MM-DD 或 YYYYMMDD)
            end_date: 结束日期
            signal_fn: 信号生成函数 trade_date(str) -> list[str]
            conn: 可选的已有数据库连接

        Returns:
            BacktestResult
        """
        own_conn = conn is None
        if own_conn:
            conn = pymysql.connect(**DB_CONFIG)

        try:
            trade_dates = self._get_trade_dates(conn, start_date, end_date)
            if len(trade_dates) < self.hold_days + 1:
                logger.warning("交易日期不足，无法回测")
                return BacktestResult()

            logger.info("回测 %s ~ %s (%d 个交易日, 持有%d天, 每%d天采样)",
                        start_date, end_date, len(trade_dates),
                        self.hold_days, self.sample_interval)

            result = BacktestResult()
            price_cache: dict[str, dict[str, float]] = {}
            self._preload_prices(conn, price_cache, trade_dates)

            signal_dates = trade_dates[:-(self.hold_days - 1)] if len(trade_dates) >= self.hold_days else []

            for i, trade_date in enumerate(signal_dates):
                if i % self.sample_interval != 0:
                    continue

                signals = signal_fn(trade_date)
                if not signals:
                    continue

                exit_idx = i + self.hold_days
                if exit_idx >= len(trade_dates):
                    continue

                exit_date = trade_dates[exit_idx]
                for ts_code in signals[:self.top_n]:
                    entry_price = price_cache.get(ts_code, {}).get(trade_date)
                    exit_price = price_cache.get(ts_code, {}).get(exit_date)
                    if entry_price and exit_price and entry_price > 0:
                        ret = (exit_price - entry_price) / entry_price * 100
                        result.trades.append(TradeRecord(
                            entry_date=trade_date,
                            exit_date=exit_date,
                            ts_code=ts_code,
                            entry_price=entry_price,
                            exit_price=exit_price,
                            pct_return=ret,
                        ))

            self._compute_metrics(result)
            return result

        finally:
            if own_conn:
                conn.close()

    def get_top_pool(self, conn, trade_date: str) -> list[str]:
        """按成交额获取候选股票池。

        使用 trade_date 的前一交易日成交额排序，防止数据泄漏。
        """
        if self.use_prev_amount:
            row = conn.cursor()
            row.execute(
                "SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s",
                (trade_date,)
            )
            prev_date = row.fetchone()[0]
            if prev_date is None:
                return []
            query_date = prev_date
        else:
            query_date = trade_date

        query = """
            SELECT ts_code FROM daily_price
            WHERE trade_date = %s
              AND LEFT(ts_code, 1) NOT IN ('8', '4', '9')
              AND ts_code NOT LIKE '83%%'
              AND ts_code NOT LIKE '87%%'
              AND ts_code NOT LIKE '43%%'
              AND close <= 200
            ORDER BY amount DESC LIMIT %s
        """
        row = conn.cursor()
        row.execute(query, (query_date, self.top_candidates))
        return [r[0] for r in row.fetchall()]

    # ── 内部方法 ──

    def _get_trade_dates(self, conn, start: str, end: str) -> list[str]:
        """获取交易日列表。"""
        import pandas as pd
        df = pd.read_sql(
            "SELECT DISTINCT trade_date FROM daily_price "
            "WHERE trade_date >= %s AND trade_date <= %s ORDER BY trade_date",
            conn,
            params={"start": start, "end": end},
        )
        return sorted(df["trade_date"].astype(str).tolist())

    def _preload_prices(self, conn, cache: dict, trade_dates: list[str]):
        """预加载所有交易日的收盘价。"""
        import pandas as pd
        df = pd.read_sql(
            "SELECT ts_code, trade_date, close FROM daily_price "
            "WHERE trade_date >= %s AND trade_date <= %s",
            conn,
            params={"start": trade_dates[0], "end": trade_dates[-1]},
        )
        for _, row in df.iterrows():
            cache.setdefault(str(row["ts_code"]), {})[str(row["trade_date"])] = float(row["close"]) if row["close"] else 0

    def _compute_metrics(self, result: BacktestResult):
        """计算回测指标。"""
        result.n_trades = len(result.trades)
        result.n_wins = sum(1 for t in result.trades if t.pct_return > 0)
        if result.n_trades == 0:
            return

        trade_returns = np.array([t.pct_return / 100 for t in result.trades])

        # 累计收益（复利）
        cumulative = float(np.prod(1 + trade_returns) - 1)
        result.total_return = cumulative * 100
        result.win_rate = result.n_wins / result.n_trades * 100

        # 盈亏比
        wins_ret = [t.pct_return for t in result.trades if t.pct_return > 0]
        losses_ret = [t.pct_return for t in result.trades if t.pct_return <= 0]
        avg_win = np.mean(wins_ret) if wins_ret else 0
        avg_loss = abs(np.mean(losses_ret)) if losses_ret else 1
        result.profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        # 夏普（按持有期年化）
        if len(trade_returns) > 5 and trade_returns.std() > 0:
            result.sharpe = float(
                trade_returns.mean() / trade_returns.std() * np.sqrt(252 / self.hold_days)
            )

        # 最大回撤
        nav = np.cumprod(1 + trade_returns)
        peak = np.maximum.accumulate(nav)
        dd = (nav - peak) / peak
        result.max_drawdown = float(abs(min(dd)) * 100 if len(dd) > 0 else 0)

        # NAV 曲线
        nav_with_start = np.concatenate([[1.0], nav])
        if len(nav_with_start) > 100:
            indices = np.linspace(0, len(nav_with_start) - 1, 100, dtype=int)
            result.nav_values = [float(nav_with_start[i]) for i in indices]
        else:
            result.nav_values = [float(x) for x in nav_with_start]
