"""
策略级回测引擎 — 评估的不是"信号准不准"，而是"按信号买完能不能赚到钱"

与 model_backtest 的区别:
  - 真实模拟 T+1 成交 (次日开盘价)
  - 涨跌停成交概率处理 (一字板/涨停封死常常买不进)
  - 滑点: 买入 +0.15%, 卖出 -0.15%
  - 手续费: 买入万 2.5 (含规费), 卖出万 2.5 + 印花千 1
  - 仓位管理: 按信号强度分仓 (高/中/低)
  - 止损止盈: 多档移动止盈, 时间止损
  - 输出: 资金曲线 + 交易记录 + 性能指标 (年化/夏普/回撤/胜率/盈亏比)

用法:
    from quant_app.backtest.strategy_engine import StrategyBacktest, StrategyConfig
    cfg = StrategyConfig(start='2024-01-01', end='2025-06-30', initial_capital=1_000_000)
    bt = StrategyBacktest(cfg)
    bt.run(daily_signals)  # daily_signals: {date: [Signal...], ...}
    print(bt.report())
"""
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ========== 数据结构 ==========
@dataclass
class Signal:
    """每日每个信号的最小单元"""
    date: str                  # 信号日 (T)
    ts_code: str
    score: float = 0.0         # 综合分 0-100
    confidence: str = "mid"    # high / mid / low
    expected_return: float = 0.0  # 期望收益 (模型输出)


@dataclass
class Trade:
    """单笔交易记录"""
    code: str
    name: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    qty: int
    pnl: float                 # 已实现盈亏
    pnl_pct: float             # 收益率 %
    hold_days: int
    exit_reason: str           # stop_loss / take_profit_1 / take_profit_2 / time_stop / signal_exit
    score: float
    confidence: str


@dataclass
class StrategyConfig:
    start: str
    end: str
    initial_capital: float = 1_000_000.0
    # 仓位
    high_conf_pos_pct: float = 0.20     # 高置信度单票仓位 (满仓 5 只)
    mid_conf_pos_pct: float = 0.10      # 中置信度单票仓位 (满仓 10 只)
    max_positions: int = 5
    # 成本
    commission_rate: float = 0.00025     # 手续费 (万 2.5)
    stamp_tax: float = 0.001             # 印花税 (千 1, 卖出)
    slippage_rate: float = 0.0015        # 滑点 (千 1.5)
    # 涨跌停成交概率 (近似)
    limit_up_fill_pct: float = 0.30      # 涨停板买进成功率
    limit_down_fill_pct: float = 0.30    # 跌停板卖出成功率
    # 止损止盈
    stop_loss_pct: float = -0.035        # 硬止损
    take_profit_1_pct: float = 0.05      # 第一档止盈 (卖 1/3)
    take_profit_2_pct: float = 0.10      # 第二档止盈 (再卖 1/3)
    trailing_stop_pct: float = 0.03      # 浮盈 > 5% 后, 移动止损抬到 +3%
    trailing_arm_pct: float = 0.05       # 启动移动止损的浮盈阈值
    time_stop_days: int = 3              # 持有超过 3 日未触发止盈, 减半/清仓
    time_stop_reduce_pct: float = 0.5    # 第 3 日减半仓位
    # 复权
    adjust: str = "qfq"                  # 前复权 (默认)


# ========== 回测引擎 ==========
class StrategyBacktest:
    """
    策略级回测主类

    输入: 每日信号 (date → List[Signal])
    输出: 资金曲线 + 交易记录 + 性能指标
    """

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg
        self.trades: list[Trade] = []
        self.equity_curve: list[dict] = []
        # 每日价格查找表 (懒加载)
        self._price_cache: dict[str, pd.DataFrame] = {}

    def _load_prices(self, conn, ts_codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """加载所有涉及股票的开高低收 (前复权, T+1 起的开高低)"""
        if not ts_codes:
            return {}
        # 批量查 (避免 N+1)
        placeholders = ','.join(['%s'] * len(ts_codes))
        sql = f"""
            SELECT ts_code, trade_date, open, high, low, close, pct_chg, vol, amount
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN %s AND %s
            ORDER BY ts_code, trade_date
        """
        df = pd.read_sql(sql, conn, params=(*ts_codes, start, self.cfg.end))
        df['trade_date'] = pd.to_datetime(df['trade_date'])
        return {code: g.set_index('trade_date').sort_index() for code, g in df.groupby('ts_code')}

    def _next_trading_day(self, code: str, date: pd.Timestamp, prices: dict) -> pd.Timestamp | None:
        """取下一个交易日"""
        if code not in prices or prices[code].empty:
            return None
        idx = prices[code].index
        pos = idx.searchsorted(date, side='right')
        if pos >= len(idx):
            return None
        return idx[pos]

    def _open_at(self, code: str, date: pd.Timestamp, prices: dict) -> float | None:
        """T+1 开盘价"""
        d = self._next_trading_day(code, date, prices)
        if d is None:
            return None
        row = prices[code].loc[d]
        return float(row['open']) if not pd.isna(row['open']) else None

    def _high_low_close(self, code: str, date: pd.Timestamp, prices: dict) -> tuple:
        """T+N 日开高低收, 用于盘中判断是否触发止盈止损"""
        d = self._next_trading_day(code, date, prices)
        if d is None:
            return None, None, None, None
        row = prices[code].loc[d]
        return float(row['open']), float(row['high']), float(row['low']), float(row['close'])

    def _can_buy(self, code: str, date: pd.Timestamp, prices: dict) -> bool:
        """T+1 开盘是否一字涨停 (买不进)"""
        o, h, l, c = self._high_low_close(code, date, prices)
        if o is None: return False
        prev = prices[code]
        prev_close = prev['close'].shift(1)
        prev_idx = prev.index.get_loc(date) - 1 if date in prev.index else -1
        if prev_idx < 0:
            return False
        # 近似: 开盘 >= 昨收 * 1.098 视为一字涨停
        prev_c = float(prev['close'].iloc[prev_idx])
        if prev_c <= 0: return True
        return o < prev_c * 1.098  # 买得进

    def _can_sell(self, code: str, date: pd.Timestamp, prices: dict) -> bool:
        """T+1 开盘是否一字跌停 (卖不出)"""
        o, h, l, c = self._high_low_close(code, date, prices)
        if o is None: return True  # 取不到价就当卖不出
        prev_idx = prices[code].index.get_loc(date) - 1
        if prev_idx < 0: return True
        prev_c = float(prices[code]['close'].iloc[prev_idx])
        if prev_c <= 0: return True
        return o > prev_c * 0.902  # 卖得出

    def _apply_slippage(self, price: float, side: str) -> float:
        """滑点: 买 +0.15%, 卖 -0.15%"""
        if side == 'buy':
            return price * (1 + self.cfg.slippage_rate)
        else:
            return price * (1 - self.cfg.slippage_rate)

    def _commission(self, price: float, qty: int, side: str) -> float:
        """手续费 + 印花税"""
        amount = price * qty
        comm = amount * self.cfg.commission_rate
        if side == 'sell':
            comm += amount * self.cfg.stamp_tax
        return max(comm, 5.0)  # 最低 5 元

    def _pos_pct(self, sig: Signal) -> float:
        if sig.confidence == 'high': return self.cfg.high_conf_pos_pct
        if sig.confidence == 'low':  return 0.0  # 低置信度不进
        return self.cfg.mid_conf_pos_pct

    def run(self, daily_signals: dict[str, list[Signal]], conn=None) -> dict:
        """
        核心回测循环

        Args:
            daily_signals: {date_str: [Signal, ...]}
            conn: pymysql 连接 (可选, 不传则用 ad-hoc)

        Returns:
            {
              'trades': [Trade, ...],
              'equity_curve': pd.DataFrame,
              'metrics': {年化, 夏普, 最大回撤, 胜率, 盈亏比, ...}
            }
        """
        if conn is None:
            import pymysql

            from quant_app.utils.config import get_db_config
            conn = pymysql.connect(**get_db_config())

        # 1) 收集所有涉及的 ts_code, 一次性加载价格
        all_codes = set()
        for sigs in daily_signals.values():
            for s in sigs:
                all_codes.add(s.ts_code)
        logger.info(f"Loading prices for {len(all_codes)} stocks...")
        prices = self._load_prices(conn, list(all_codes), self.cfg.start, self.cfg.end)

        # 2) 按日期排序
        sorted_dates = sorted(daily_signals.keys())
        cash = self.cfg.initial_capital
        positions: dict[str, dict] = {}  # code -> {qty, entry_price, entry_date, score, confidence, partial_taken}

        # 净值曲线
        equity_records = []

        for date_str in sorted_dates:
            d = pd.Timestamp(date_str)
            sigs = daily_signals[date_str]

            # ---- 1. 处理已有持仓的盘中事件 (止损/止盈/时间止损) ----
            to_close = []
            for code, pos in positions.items():
                if code not in prices: continue
                if d not in prices[code].index:
                    # 这一天该股停牌, 跳过
                    continue
                open_p, high_p, low_p, close_p = (
                    float(prices[code].loc[d, 'open']),
                    float(prices[code].loc[d, 'high']),
                    float(prices[code].loc[d, 'low']),
                    float(prices[code].loc[d, 'close']),
                )
                if pd.isna(open_p):
                    continue
                cost = pos['entry_price']
                cur_pct = (high_p / cost - 1)  # 用最高价判断是否触及止盈
                cur_dd = (low_p / cost - 1)    # 用最低价判断是否触及止损

                hold_days = (d - pd.Timestamp(pos['entry_date'])).days
                # 工作日计算 (近似): hold_days >= 配置的 time_stop_days
                trading_days_held = len(prices[code].index[(prices[code].index > pd.Timestamp(pos['entry_date'])) & (prices[code].index <= d)])

                # a) 硬止损 (最优先) - 按当日 low 成交
                if cur_dd <= self.cfg.stop_loss_pct:
                    # 真实成交价 = max(stop_price, 当日 low) 后的滑点
                    stop_price = cost * (1 + self.cfg.stop_loss_pct)
                    actual_exit = min(stop_price, low_p)  # 实际可能更低
                    exit_price = self._apply_slippage(actual_exit, 'sell')
                    # 涨跌停卖不出 → 改次日开盘
                    if not self._can_sell(code, d, prices):
                        next_d = self._next_trading_day(code, d, prices)
                        if next_d is not None and next_d in prices[code].index:
                            exit_price = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                            exit_d_str = next_d.strftime('%Y-%m-%d')
                        else:
                            continue
                    else:
                        exit_d_str = d.strftime('%Y-%m-%d')
                    fee = self._commission(exit_price, pos['qty'], 'sell')
                    pnl = (exit_price - cost) * pos['qty'] - fee
                    self.trades.append(Trade(
                        code=code, name=pos.get('name', code),
                        entry_date=pos['entry_date'], entry_price=cost,
                        exit_date=exit_d_str, exit_price=exit_price,
                        qty=pos['qty'], pnl=pnl,
                        pnl_pct=(exit_price/cost - 1) * 100,
                        hold_days=trading_days_held, exit_reason='stop_loss',
                        score=pos['score'], confidence=pos['confidence'],
                    ))
                    cash += pos['qty'] * exit_price - fee
                    to_close.append(code)
                    continue

                # b) 第一档止盈 (卖 1/3, 锁利, 抬止损到成本)
                if not pos['partial_taken']['tp1'] and cur_pct >= self.cfg.take_profit_1_pct:
                    sell_qty = max(pos['qty'] // 3, 100)  # 至少 100 股
                    if sell_qty >= pos['qty']:
                        sell_qty = pos['qty']
                    exit_price = self._apply_slippage(cost * (1 + self.cfg.take_profit_1_pct), 'sell')
                    if not self._can_sell(code, d, prices):
                        # 跌停卖不出, 推到次日
                        next_d = self._next_trading_day(code, d, prices)
                        if next_d is not None and next_d in prices[code].index:
                            exit_price = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                    fee = self._commission(exit_price, sell_qty, 'sell')
                    pnl = (exit_price - cost) * sell_qty - fee
                    self.trades.append(Trade(
                        code=code, name=pos.get('name', code),
                        entry_date=pos['entry_date'], entry_price=cost,
                        exit_date=d.strftime('%Y-%m-%d'), exit_price=exit_price,
                        qty=sell_qty, pnl=pnl,
                        pnl_pct=(exit_price/cost - 1) * 100,
                        hold_days=trading_days_held, exit_reason='take_profit_1',
                        score=pos['score'], confidence=pos['confidence'],
                    ))
                    cash += sell_qty * exit_price - fee
                    pos['qty'] -= sell_qty
                    pos['partial_taken']['tp1'] = True
                    pos['stop_floor'] = cost  # 止损抬到成本

                # c) 第二档止盈 (再卖 1/3, 启动移动止损)
                if pos['qty'] > 0 and not pos['partial_taken']['tp2'] and cur_pct >= self.cfg.take_profit_2_pct:
                    sell_qty = max(pos['qty'] // 2, 100)  # 剩余的一半
                    if sell_qty >= pos['qty']:
                        sell_qty = pos['qty']
                    exit_price = self._apply_slippage(cost * (1 + self.cfg.take_profit_2_pct), 'sell')
                    if not self._can_sell(code, d, prices):
                        next_d = self._next_trading_day(code, d, prices)
                        if next_d is not None and next_d in prices[code].index:
                            exit_price = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                    fee = self._commission(exit_price, sell_qty, 'sell')
                    pnl = (exit_price - cost) * sell_qty - fee
                    self.trades.append(Trade(
                        code=code, name=pos.get('name', code),
                        entry_date=pos['entry_date'], entry_price=cost,
                        exit_date=d.strftime('%Y-%m-%d'), exit_price=exit_price,
                        qty=sell_qty, pnl=pnl,
                        pnl_pct=(exit_price/cost - 1) * 100,
                        hold_days=trading_days_held, exit_reason='take_profit_2',
                        score=pos['score'], confidence=pos['confidence'],
                    ))
                    cash += sell_qty * exit_price - fee
                    pos['qty'] -= sell_qty
                    pos['partial_taken']['tp2'] = True
                    pos['trailing_active'] = True  # 启动移动止损

                # d) 移动止损
                if pos['qty'] > 0 and pos.get('trailing_active', False):
                    # 浮盈 > 5% 之后, 跟踪最高点回落 3% 卖出
                    if cur_pct < pos.get('high_pct', 0) - self.cfg.trailing_stop_pct and pos.get('high_pct', 0) >= self.cfg.trailing_arm_pct:
                        exit_price = self._apply_slippage(cost * pos.get('high_pct', 1) * (1 - self.cfg.trailing_stop_pct), 'sell')
                        if not self._can_sell(code, d, prices):
                            next_d = self._next_trading_day(code, d, prices)
                            if next_d is not None and next_d in prices[code].index:
                                exit_price = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                        fee = self._commission(exit_price, pos['qty'], 'sell')
                        pnl = (exit_price - cost) * pos['qty'] - fee
                        self.trades.append(Trade(
                            code=code, name=pos.get('name', code),
                            entry_date=pos['entry_date'], entry_price=cost,
                            exit_date=d.strftime('%Y-%m-%d'), exit_price=exit_price,
                            qty=pos['qty'], pnl=pnl,
                            pnl_pct=(exit_price/cost - 1) * 100,
                            hold_days=trading_days_held, exit_reason='trailing_stop',
                            score=pos['score'], confidence=pos['confidence'],
                        ))
                        cash += pos['qty'] * exit_price - fee
                        to_close.append(code)
                        continue

                # 更新 high_pct
                if 'high_pct' not in pos or cur_pct > pos['high_pct']:
                    pos['high_pct'] = cur_pct

                # e) 时间止损 (持有 >= 3 个交易日 且 未触发 tp1)
                if trading_days_held >= self.cfg.time_stop_days and not pos['partial_taken']['tp1']:
                    # 减半仓位
                    sell_qty = pos['qty'] // 2
                    if sell_qty >= 100:
                        exit_price = self._apply_slippage(close_p, 'sell')
                        if not self._can_sell(code, d, prices):
                            next_d = self._next_trading_day(code, d, prices)
                            if next_d is not None and next_d in prices[code].index:
                                exit_price = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                        fee = self._commission(exit_price, sell_qty, 'sell')
                        pnl = (exit_price - cost) * sell_qty - fee
                        self.trades.append(Trade(
                            code=code, name=pos.get('name', code),
                            entry_date=pos['entry_date'], entry_price=cost,
                            exit_date=d.strftime('%Y-%m-%d'), exit_price=exit_price,
                            qty=sell_qty, pnl=pnl,
                            pnl_pct=(exit_price/cost - 1) * 100,
                            hold_days=trading_days_held, exit_reason='time_stop',
                            score=pos['score'], confidence=pos['confidence'],
                        ))
                        cash += sell_qty * exit_price - fee
                        pos['qty'] -= sell_qty
                    # 剩余部分次日开盘清仓
                    elif pos['qty'] > 0:
                        # 剩余全部次日开盘清仓
                        full_qty = pos['qty']
                        next_d = self._next_trading_day(code, d, prices)
                        if next_d is not None and next_d in prices[code].index:
                            exit_p = self._apply_slippage(float(prices[code].loc[next_d, 'open']), 'sell')
                            fee = self._commission(exit_p, full_qty, 'sell')
                            pnl = (exit_p - cost) * full_qty - fee
                            self.trades.append(Trade(
                                code=code, name=pos.get('name', code),
                                entry_date=pos['entry_date'], entry_price=cost,
                                exit_date=next_d.strftime('%Y-%m-%d'), exit_price=exit_p,
                                qty=full_qty, pnl=pnl,
                                pnl_pct=(exit_p/cost - 1) * 100,
                                hold_days=trading_days_held, exit_reason='time_stop_full',
                                score=pos['score'], confidence=pos['confidence'],
                            ))
                            cash += full_qty * exit_p - fee
                        # 不 to_close (因为已经处理了), 但加一个标记确保不重复
                        pos['__cleared'] = True
                        to_close.append(code)

            for c in to_close:
                if c in positions and positions[c].get('__cleared'):
                    positions[c].pop('__cleared', None)
                positions.pop(c, None)

            # ---- 2. 当日新信号: T+1 开盘建仓 (今日不发新单, T+1 才下单) ----
            # 信号是在收盘后生成的, 实际成交是次日开盘
            # 这里我们假设 signals 已是"T 日收盘后"生成, 实际成交是 T+1
            # 简化: 跳过当日新信号, 在 _execute_next_day_open() 中处理
            # 实际: 这里已经把信号存下来, T+1 时建仓
            for sig in sigs:
                if sig.confidence == 'low':
                    continue
                if sig.ts_code in positions:
                    # 已有持仓, 跳过 (不重复建仓, 也不覆盖)
                    continue
                if len(positions) >= self.cfg.max_positions:
                    break
                pos_pct = self._pos_pct(sig)
                if pos_pct <= 0:
                    print("     -- SKIP pos_pct=0"); continue
                next_d = self._next_trading_day(sig.ts_code, d, prices)
                if next_d is None:
                    print("     -- SKIP no next"); continue
                open_p = float(prices[sig.ts_code].loc[next_d, 'open'])
                if pd.isna(open_p) or open_p <= 0:
                    print("     -- SKIP bad open"); continue
                if not self._can_buy(sig.ts_code, next_d, prices):
                    print("     -- SKIP cannot buy"); continue
                entry_price = self._apply_slippage(open_p, 'buy')
                # 仓位
                target_value = cash * pos_pct
                qty = int(target_value / entry_price / 100) * 100  # 整百
                if qty <= 0: continue
                fee = self._commission(entry_price, qty, 'buy')
                if cash < entry_price * qty + fee: continue
                cash -= entry_price * qty + fee
                positions[sig.ts_code] = {
                    'qty': qty,
                    'entry_price': entry_price,
                    'entry_date': next_d.strftime('%Y-%m-%d'),  # 真实成交日
                    'score': sig.score,
                    'confidence': sig.confidence,
                    'partial_taken': {'tp1': False, 'tp2': False},
                    'trailing_active': False,
                    'high_pct': 0.0,
                }

            # ---- 3. 当日净值 ----
            mkt_value = cash
            for code, pos in positions.items():
                if code in prices and d in prices[code].index:
                    mkt_value += pos['qty'] * float(prices[code].loc[d, 'close'])
                elif code in prices and not prices[code].empty:
                    # 停牌, 用最近收盘
                    last_close = prices[code]['close'].asof(d)
                    if not pd.isna(last_close):
                        mkt_value += pos['qty'] * float(last_close)
            equity_records.append({'date': d, 'cash': cash, 'mkt_value': mkt_value - cash, 'equity': mkt_value})

        # 4) 关闭所有未平仓位 (期末清算) — 用 cfg.end 而不是最后信号日
        if positions:
            last_date = pd.Timestamp(self.cfg.end)
            for code, pos in list(positions.items()):
                if code in prices and not prices[code].empty:
                    last_close = float(prices[code]['close'].iloc[-1])
                    exit_price = self._apply_slippage(last_close, 'sell')
                    fee = self._commission(exit_price, pos['qty'], 'sell')
                    pnl = (exit_price - pos['entry_price']) * pos['qty'] - fee
                    self.trades.append(Trade(
                        code=code, name=pos.get('name', code),
                        entry_date=pos['entry_date'], entry_price=pos['entry_price'],
                        exit_date=last_date.strftime('%Y-%m-%d'),
                        exit_price=exit_price, qty=pos['qty'], pnl=pnl,
                        pnl_pct=(exit_price/pos['entry_price'] - 1) * 100,
                        hold_days=1, exit_reason='end_of_period',
                        score=pos['score'], confidence=pos['confidence'],
                    ))

        if equity_records:
            equity_df = pd.DataFrame(equity_records).set_index('date')
        else:
            equity_df = pd.DataFrame()
        metrics = self._calc_metrics(equity_df) if not equity_df.empty else {}
        return {'trades': self.trades, 'equity_curve': equity_df, 'metrics': metrics}

    def _calc_metrics(self, equity: pd.DataFrame) -> dict:
        if equity.empty or 'equity' not in equity.columns:
            return {}
        eq = equity['equity']
        rets = eq.pct_change().dropna()
        n_days = len(eq)
        ann_factor = 252 / max(n_days, 1)
        total_ret = (eq.iloc[-1] / eq.iloc[0] - 1) if eq.iloc[0] > 0 else 0
        ann_ret = (1 + total_ret) ** ann_factor - 1
        vol = rets.std() * np.sqrt(252) if len(rets) > 1 else 0
        sharpe = (rets.mean() * 252) / (rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
        # 最大回撤
        rolling_max = eq.cummax()
        drawdown = (eq - rolling_max) / rolling_max
        max_dd = drawdown.min() if not drawdown.empty else 0
        # 交易指标
        n_trades = len(self.trades)
        if n_trades > 0:
            wins = [t for t in self.trades if t.pnl > 0]
            losses = [t for t in self.trades if t.pnl <= 0]
            win_rate = len(wins) / n_trades
            avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
            avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0
            pl_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else float('inf')
            total_pnl = sum(t.pnl for t in self.trades)
        else:
            win_rate = avg_win = avg_loss = pl_ratio = total_pnl = 0
        return {
            'n_days': n_days,
            'n_trades': n_trades,
            'total_return_pct': round(total_ret * 100, 2),
            'annual_return_pct': round(ann_ret * 100, 2),
            'annual_vol_pct': round(vol * 100, 2),
            'sharpe': round(sharpe, 2),
            'max_drawdown_pct': round(max_dd * 100, 2),
            'calmar': round(ann_ret / abs(max_dd), 2) if max_dd < 0 else 0,
            'win_rate_pct': round(win_rate * 100, 2),
            'avg_win_pct': round(avg_win, 2),
            'avg_loss_pct': round(avg_loss, 2),
            'pl_ratio': round(pl_ratio, 2),
            'total_pnl': round(total_pnl, 2),
        }

    def report(self) -> str:
        """生成可读报告"""
        if not self.trades:
            return "No trades executed."
        # 从 trades 重建 equity 序列 (避免 self.equity_curve 为空)
        if self.trades:
            eq = self.cfg.initial_capital
            eq_series = []
            for t in self.trades:
                eq += t.pnl
                eq_series.append({'date': pd.Timestamp(t.exit_date), 'equity': eq})
            metrics = self._calc_metrics(pd.DataFrame(eq_series).set_index('date')) if eq_series else {}
        else:
            metrics = {}
        lines = [
            "=" * 50,
            "  策略级回测报告",
            "=" * 50,
            f"  期间: {self.cfg.start} ~ {self.cfg.end}",
            f"  初始资金: {self.cfg.initial_capital:,.0f}",
            f"  交易笔数: {metrics.get('n_trades', 0)}",
            f"  总收益率: {metrics.get('total_return_pct', 0):.2f}%",
            f"  年化收益: {metrics.get('annual_return_pct', 0):.2f}%",
            f"  年化波动: {metrics.get('annual_vol_pct', 0):.2f}%",
            f"  夏普比率: {metrics.get('sharpe', 0):.2f}",
            f"  最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}%",
            f"  Calmar:   {metrics.get('calmar', 0):.2f}",
            f"  胜率:     {metrics.get('win_rate_pct', 0):.2f}%",
            f"  平均盈利: {metrics.get('avg_win_pct', 0):.2f}%",
            f"  平均亏损: {metrics.get('avg_loss_pct', 0):.2f}%",
            f"  盈亏比:   {metrics.get('pl_ratio', 0):.2f}",
            f"  总盈亏:   {metrics.get('total_pnl', 0):,.0f}",
            "=" * 50,
        ]
        return "\n".join(lines)
