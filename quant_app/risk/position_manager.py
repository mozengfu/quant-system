"""
仓位管理 - "免费 alpha" 模块

目标: 通过仓位/止损/分散等执行层面的优化, 在不改变选股的前提下挤出 5-10% 年化 alpha

核心规则:
  1. 波动率倒数加权 (vol-targeting): 高波动股票少配
  2. 板块集中度上限: 单板块持仓不超过总仓位 30%
  3. 组合回撤闸门: 组合回撤 > 10% → 减半, > 15% → 全清
  4. 跟踪止损: 浮盈 > 3% 后, 跟踪止损线 = max(cost * 1.005, peak * 0.97)
  5. 分批止盈: 浮盈 > 5% 卖一半, 剩余部分继续跟踪止损
  6. 单票最大仓位: 不超过总资金 25%

使用:
    pm = PositionManager(conn)
    pm.update_account_state(cash, positions, today)
    if pm.is_drawdown_breach():
        # 阻断新买入
        return
    pos_pct = pm.compute_position_size(stock_code, today, base_pct=0.20)
    # pos_pct 是经过 vol-targeting 调整后的仓位
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


@dataclass
class PositionRules:
    """仓位规则配置"""
    base_position_pct: float = 0.20        # 基础单票仓位 20%
    max_position_pct: float = 0.25         # 单票最大 25%
    min_position_pct: float = 0.05         # 单票最小 5% (避免太散)
    max_sector_pct: float = 0.30           # 单板块上限 30%
    max_concurrent: int = 5                # 最大并发数
    target_portfolio_vol: float = 0.15     # 目标组合年化波动率 15%
    annualization: int = 252               # 年化因子 (日线)
    # 跟踪止损
    trailing_arm_pct: float = 0.03         # 浮盈达 3% 启动跟踪
    trailing_stop_pct: float = 0.02        # 跟踪距离 2%
    # 分批止盈
    partial_tp_pct: float = 0.05           # +5% 卖一半
    # 硬止损 (从 entry)
    hard_stop_pct: float = -0.05           # -5%
    # 回撤闸门
    dd_warn: float = 0.08                 # -8% 警告
    dd_breach: float = 0.12               # -12% 减半
    dd_kill: float = 0.18                  # -18% 全清


class PositionManager:
    """仓位管理器"""

    def __init__(self, conn=None, rules: PositionRules = None):
        self.conn = conn or pymysql.connect(**get_db_config())
        self.rules = rules or PositionRules()
        # 内部状态
        self.peak_equity: float | None = None
        self.current_equity: float | None = None
        self.drawdown: float = 0.0
        self.holdings: dict[str, dict] = {}  # code -> {qty, cost, peak_price, sector, ...}
        self.sector_exposure: dict[str, float] = defaultdict(float)  # sector -> value
        self._db_state_loaded = False

    def load_account_state(self):
        """从 sim_account 加载 peak_value 和 cash"""
        cur = self.conn.cursor()
        cur.execute("SELECT peak_value FROM sim_account WHERE id=1")
        row = cur.fetchone()
        self.peak_equity = float(row[0]) if row and row[0] else 100000
        self._db_state_loaded = True

    def update_equity(self, current_equity: float):
        """更新当前净值, 计算回撤 (本地 peak, 默认 100k)"""
        self.current_equity = current_equity
        if self.peak_equity is None or current_equity > self.peak_equity:
            self.peak_equity = current_equity
        self.drawdown = (current_equity - self.peak_equity) / self.peak_equity if self.peak_equity > 0 else 0

    def is_breach(self) -> bool:
        """回撤是否达到阻断阈值"""
        return self.drawdown <= -self.rules.dd_kill

    def is_warn(self) -> bool:
        """回撤警告"""
        return self.drawdown <= -self.rules.dd_breach

    def compute_stock_vol(self, ts_code: str, as_of_date: str, lookback: int = 20) -> float:
        """计算单只股票近 N 日年化波动率"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT pct_chg FROM daily_price
            WHERE ts_code=%s AND trade_date<=%s
            ORDER BY trade_date DESC LIMIT %s
        """, (ts_code, as_of_date, lookback))
        rows = cur.fetchall()
        if len(rows) < 5:
            return 0.3  # 默认 30% 年化波动率
        rets = [float(r[0]) for r in rows if r[0] is not None]
        if len(rets) < 5:
            return 0.3
        vol_daily = np.std(rets) / 100
        vol_annual = vol_daily * np.sqrt(self.rules.annualization)
        return max(vol_annual, 0.10)  # 最小 10%

    def compute_position_size(self, ts_code: str, as_of_date: str, base_pct: float = None) -> float:
        """
        vol-targeting 仓位计算:
        - 基础仓位 (base_pct)
        - 按波动率倒数调整: 高波动少配
        - 受 max/min 约束
        - 受回撤闸门影响 (回撤大时自动减半)
        """
        if base_pct is None:
            base_pct = self.rules.base_position_pct

        # 1) vol 倒数加权
        vol = self.compute_stock_vol(ts_code, as_of_date)
        vol_adj = self.rules.target_portfolio_vol / max(vol, 0.10)
        pos_pct = base_pct * vol_adj

        # 2) 上下限
        pos_pct = max(self.rules.min_position_pct, min(self.rules.max_position_pct, pos_pct))

        # 3) 回撤闸门: 回撤 > 12% 时强制减半
        if self.is_warn():
            pos_pct *= 0.5
            logger.info(f"  [仓位] 回撤警告 ({self.drawdown*100:.1f}%), 仓位减半 → {pos_pct*100:.1f}%")

        # 4) 阻断: 回撤 > 18% → 0
        if self.is_breach():
            logger.info(f"  [仓位] 回撤阻断 ({self.drawdown*100:.1f}%), 仓位归零")
            return 0.0

        return pos_pct

    def can_add_position(self, ts_code: str, sector: str, as_of_date: str) -> tuple[bool, str]:
        """
        检查是否可以建仓 (返回 bool + 原因)
        """
        if self.is_breach():
            return False, f"组合回撤 {self.drawdown*100:.1f}% 触发阻断"
        if len(self.holdings) >= self.rules.max_concurrent:
            return False, f"已达最大并发 {self.rules.max_concurrent}"
        if ts_code in self.holdings:
            return False, "已持有"
        # 板块集中度
        sector_now = self.sector_exposure.get(sector, 0)
        if sector_now > self.rules.max_sector_pct:
            return False, f"板块 {sector} 集中度 {sector_now*100:.1f}% 超限"
        return True, "OK"

    def on_buy(self, ts_code: str, sector: str, value: float, total_equity: float):
        """买入后更新内部状态"""
        self.holdings[ts_code] = {
            'sector': sector,
            'value': value,
        }
        self.sector_exposure[sector] += value / total_equity

    def on_sell(self, ts_code: str, value: float, total_equity: float):
        """卖出后更新内部状态"""
        if ts_code in self.holdings:
            sector = self.holdings[ts_code].get('sector', '')
            self.sector_exposure[sector] -= value / total_equity
            del self.holdings[ts_code]

    def check_exit(self, ts_code: str, cost: float, current_price: float, peak_price: float,
                   current_qty: int, partial_taken: bool) -> tuple[str, float]:
        """
        检查出场条件, 返回 (action, sell_ratio)
        action: 'hold' / 'partial' / 'stop' / 'trailing_stop' / 'hold'
        sell_ratio: 0-1 卖出比例
        """
        if current_qty <= 0 or current_price <= 0:
            return 'hold', 0.0

        pnl_pct = (current_price / cost - 1)
        # 1) 硬止损
        if pnl_pct <= self.rules.hard_stop_pct:
            return 'stop', 1.0
        # 2) 分批止盈
        if not partial_taken and pnl_pct >= self.rules.partial_tp_pct:
            return 'partial', 0.5
        # 3) 跟踪止损 (浮盈 > 3% 启动)
        if peak_price > cost * (1 + self.rules.trailing_arm_pct):
            trailing_stop_price = max(peak_price * (1 - self.rules.trailing_stop_pct),
                                       cost * 1.005)
            if current_price < trailing_stop_price:
                return 'trailing_stop', 1.0
        return 'hold', 0.0

    def summary(self) -> dict:
        return {
            'current_equity': round(self.current_equity, 2) if self.current_equity else None,
            'peak_equity': round(self.peak_equity, 2) if self.peak_equity else None,
            'drawdown_pct': round(self.drawdown * 100, 2),
            'n_holdings': len(self.holdings),
            'sector_exposure': {k: round(v*100, 1) for k, v in self.sector_exposure.items() if v > 0.01},
            'is_breach': self.is_breach(),
            'is_warn': self.is_warn(),
        }


if __name__ == '__main__':
    import sys
    sys.path.insert(0, '/Users/mozengfu/workspace/quant-system')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    pm = PositionManager()
    pm.update_equity(95000)  # 模拟 -5% 回撤
    print(f"  drawdown: {pm.drawdown*100:.2f}%")
    print(f"  is_warn: {pm.is_warn()}, is_breach: {pm.is_breach()}")
    # 计算单只股票仓位
    for code in ['601988.SH', '300308.SZ', '600522.SH']:
        pos = pm.compute_position_size(code, '2026-06-09', base_pct=0.20)
        vol = pm.compute_stock_vol(code, '2026-06-09')
        print(f"  {code}: vol={vol*100:.1f}% → 仓位 {pos*100:.1f}%")
    print(pm.summary())
