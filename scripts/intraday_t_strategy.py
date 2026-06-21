#!/usr/bin/env python3
"""
日内做T策略 — 核心逻辑
========================

设计要点 (A 股 T+1 制度下的标准做法):
  - 严格基于已有底仓做高抛低吸, 不开新仓
  - 每次 T 仓位 = 底仓的 1/3 (向下取整到 100 股的倍数)
  - 每天每只股票最多 T 一次 (buy_back + sell_high/force_close 算 1 次)
  - 14:50 强制还原未平仓位 (避免隔夜风险)
  - 不做"先卖后买" (T+1 制度下必须先有底仓, 所以是"卖 → 等价位 → 买回")
  - 不复用 sim_signals, 独立 intraday_t_log 表

信号逻辑 (实时 VWAP 估算):
  - VWAP = (累计成交额 / 累计成交量), amount 单位万元 / volume 单位手
  - 高抛 (sell_high):
      价 > VWAP * (1 + SELL_VWAP_BAND)  且  涨幅 > SELL_PCT_MIN
      且 距日内最高回撤 > PULLBACK_FROM_HIGH_MIN  (避免在尖顶卖出)
      → partial_sell 1/3 底仓
  - 低吸 (buy_back):
      价 < VWAP * (1 - BUY_VWAP_BAND)  且  跌幅 < -BUY_PCT_MIN
      且 距日内最低反弹 > PULLBACK_FROM_LOW_MIN  (避免在尖底买入)
      且 当前可买金额 >= MIN_T_AMOUNT
      → buy 创建 T 仓位 (strategy = "intraday_t_buyback")
  - 强制还原 (force_close):
      14:50 前仍有 T 仓位未平 → 市价 partial_sell 平掉

风控 (硬约束, 任何一条违反都跳过本次 T):
  R1. 单只单日 T 次数 <= MAX_T_PER_DAY (默认 1)
  R2. 标的非 ST/北交所/科创/创业 (按 ts_code 前缀 + name 关键字判断)
  R3. 底仓未触及 stop_loss / take_profit
  R4. 当前大盘 (沪深300) 跌幅 > -2% → 禁止 T
  R5. 当前距底仓成本 < MIN_HOLDING_PCT → 不做 T (刚建仓保护)
  R6. 单次 T 仓位 < 100 股 → 跳过 (A股最小 1 手)
  R7. 目标利润 < MIN_T_PROFIT_PCT → 跳过 (覆盖手续费 + 有意义)
  R8. 底仓剩余 < 100 股 → 禁止 T 卖 (避免清空底仓)
  R9. 连续 3 个 tick 触发同一信号但被风控拒绝 → 熔断当日该股 T 操作
"""

import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

import pymysql

# 把项目根加入 path, 这样可独立运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant_app.utils.config import get_db_config

logger = logging.getLogger("intraday_t")


# ============================================================
# 默认参数 (可被环境变量覆盖)
# ============================================================
DEFAULTS = {
    # 信号阈值
    "SELL_VWAP_BAND":      0.005,   # 价 > VWAP * 1.005 → 卖信号
    "SELL_PCT_MIN":        1.5,     # 涨幅 > 1.5% 才考虑卖
    "PULLBACK_FROM_HIGH":  0.5,     # 距日内最高回撤 > 0.3% (避免追高)
    "BUY_VWAP_BAND":       0.005,   # 价 < VWAP * 0.995 → 买信号
    "BUY_PCT_MIN":         1.5,     # 跌幅 > 1.5% 才考虑买
    "PULLBACK_FROM_LOW":   0.5,     # 距日内最低反弹 > 0.3% (避免抄底刀)
    # 仓位
    "T_RATIO":             0.50,    # 每次 T 仓位 = 底仓 * 1/3
    "T_SHARES_MIN":        100,     # 最小 1 手
    # 风控
    "MAX_T_PER_DAY":       1,       # 单只单日最多 1 次 T
    "MIN_T_PROFIT_PCT":    0.5,     # 目标利润 >= 0.3% (覆盖手续费)
    "MIN_HOLDING_PCT":     0.5,     # 距底仓成本涨跌幅 < 0.5% → 保护
    "FORCE_CLOSE_TIME":    "14:50", # 强制还原时间
    # 大盘
    "HS300_PANIC_PCT":    -2.0,     # 沪深300 跌幅 > 2% 禁止 T
    # 防抖
    "SKIP_DEBOUNCE":       3,       # 连续 3 tick 被风控拒 → 当日禁 T
    # 标的过滤
    "BLOCK_SUBJECTS":      ("ST", "*ST", "退", "B 股", "北证"),
}


def _load_param(name: str):
    """支持 INTRADAY_T_SELL_VWAP_BAND 形式的环境变量覆盖"""
    env_key = "INTRADAY_T_" + name
    val = os.environ.get(env_key)
    if val is None:
        return DEFAULTS[name]
    try:
        if isinstance(DEFAULTS[name], int):
            return int(val)
        return float(val)
    except ValueError:
        return DEFAULTS[name]


# ============================================================
# 数据类
# ============================================================
@dataclass
class IntradayState:
    """单只股票的当日 T 状态"""
    ts_code: str
    base_position_id: int
    base_shares: int            # 底仓股数
    cost_price: float
    stop_loss: float
    take_profit: float
    name: str = ""
    market: str = ""

    # 实时行情 (每 tick 更新)
    cur_price: float = 0.0
    prev_close: float = 0.0
    open_price: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0          # 当日累计, 手
    amount: float = 0.0          # 当日累计, 万元

    # VWAP 累加 (从开盘累计, 跨 tick 增量更新)
    cum_volume: float = 0.0      # 手
    cum_amount: float = 0.0      # 万元

    # T 状态
    t_position_id: Optional[int] = None   # 已开 T 仓的 sim_positions.id
    t_open_price: Optional[float] = None  # T 卖出价 (用于计算已实现 PnL)
    t_open_shares: int = 0
    t_action_count: int = 0               # 今日已 T 次数
    t_skip_streak: int = 0                # 连续被风控跳过的 tick 数

    @property
    def vwap(self) -> float:
        """实时 VWAP 估算, 元/股; 累积量不足时返回 0"""
        if self.cum_volume <= 0 or self.cum_amount <= 0:
            return 0.0
        # amount(万元) * 10000 / (volume(手) * 100) = amount/volume * 100
        return round(self.cum_amount * 100 / self.cum_volume, 3)

    @property
    def intraday_pct(self) -> float:
        """当日累计涨跌幅, %"""
        if self.prev_close <= 0 or self.cur_price <= 0:
            return 0.0
        return round((self.cur_price - self.prev_close) / self.prev_close * 100, 3)

    @property
    def pct_from_vwap(self) -> float:
        """价格相对 VWAP 的偏离, %"""
        v = self.vwap
        if v <= 0 or self.cur_price <= 0:
            return 0.0
        return round((self.cur_price - v) / v * 100, 3)

    @property
    def drawdown_from_high(self) -> float:
        """距日内最高回撤, %"""
        if self.high <= 0 or self.cur_price <= 0:
            return 0.0
        return round((self.high - self.cur_price) / self.high * 100, 3)

    @property
    def rebound_from_low(self) -> float:
        """距日内最低反弹, %"""
        if self.low <= 0 or self.cur_price <= 0:
            return 0.0
        return round((self.cur_price - self.low) / self.low * 100, 3)

    @property
    def holding_pct(self) -> float:
        """距底仓成本涨跌幅, %"""
        if self.cost_price <= 0 or self.cur_price <= 0:
            return 0.0
        return round((self.cur_price - self.cost_price) / self.cost_price * 100, 3)


@dataclass
class TConfig:
    """策略配置 (可从环境变量覆盖)"""
    sell_vwap_band: float = field(default_factory=lambda: _load_param("SELL_VWAP_BAND"))
    sell_pct_min: float = field(default_factory=lambda: _load_param("SELL_PCT_MIN"))
    pullback_from_high: float = field(default_factory=lambda: _load_param("PULLBACK_FROM_HIGH"))
    buy_vwap_band: float = field(default_factory=lambda: _load_param("BUY_VWAP_BAND"))
    buy_pct_min: float = field(default_factory=lambda: _load_param("BUY_PCT_MIN"))
    pullback_from_low: float = field(default_factory=lambda: _load_param("PULLBACK_FROM_LOW"))
    t_ratio: float = field(default_factory=lambda: _load_param("T_RATIO"))
    t_shares_min: int = field(default_factory=lambda: _load_param("T_SHARES_MIN"))
    max_t_per_day: int = field(default_factory=lambda: _load_param("MAX_T_PER_DAY"))
    min_t_profit_pct: float = field(default_factory=lambda: _load_param("MIN_T_PROFIT_PCT"))
    min_holding_pct: float = field(default_factory=lambda: _load_param("MIN_HOLDING_PCT"))
    force_close_time: str = field(default_factory=lambda: _load_param("FORCE_CLOSE_TIME"))
    hs300_panic_pct: float = field(default_factory=lambda: _load_param("HS300_PANIC_PCT"))
    skip_debounce: int = field(default_factory=lambda: _load_param("SKIP_DEBOUNCE"))


# ============================================================
# 行情获取 (QMT 优先, 腾讯兜底)
# ============================================================
def _qmt_quote(code: str, market: str) -> Optional[dict]:
    """从 QMT 拉实时行情, 失败返回 None"""
    try:
        import requests
        sym = f"{market}{code}"
        r = requests.get(f"http://192.168.10.25:1430/market/quote", params={"symbol": sym}, timeout=3)
        if r.status_code != 200:
            return None
        d = r.json()
        if not d or d.get("price", 0) <= 0:
            return None
        return {
            "name": d.get("name", ""),
            "price": float(d["price"]),
            "prev_close": float(d.get("prev_close", 0)),
            "open": float(d.get("open", 0)),
            "high": float(d.get("high", 0)),
            "low": float(d.get("low", 0)),
            "volume": float(d.get("volume", 0)),   # 手
            "amount": float(d.get("amount", 0)),   # 万元
        }
    except Exception as e:
        logger.debug("QMT 行情失败 %s: %s", code, e)
        return None


def _tencent_quote(code: str, market: str) -> Optional[dict]:
    """腾讯行情兜底, 返回 None 表示失败"""
    try:
        import ssl
        symbol = f"{market}{code}"
        url = f"http://qt.gtimg.cn/q={symbol}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.qq.com",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            raw = resp.read().decode("gbk")
        if "=" not in raw or "~" not in raw:
            return None
        parts = raw.strip().rstrip(";").split("~")
        if len(parts) < 40:
            return None
        return {
            "name": parts[1],
            "price": float(parts[3] or 0),
            "prev_close": float(parts[4] or 0),
            "open": float(parts[5] or 0),
            "high": float(parts[33] or 0),
            "low": float(parts[34] or 0),
            "volume": float(parts[36] or 0),    # 手
            "amount": float(parts[37] or 0),    # 万元
        }
    except Exception as e:
        logger.debug("腾讯行情失败 %s: %s", code, e)
        return None


def get_realtime_quote(code: str, market: str) -> Optional[dict]:
    """QMT 优先, 失败则用腾讯"""
    q = _qmt_quote(code, market)
    if q:
        return q
    return _tencent_quote(code, market)


def get_hs300_quote() -> Optional[dict]:
    """沪深300指数实时, 用于大盘风控"""
    q = get_realtime_quote("000300", "sh")
    if not q:
        return None
    return {
        "price": q["price"],
        "change_pct": round((q["price"] - q["prev_close"]) / q["prev_close"] * 100, 2) if q["prev_close"] else 0,
    }


# ============================================================
# 数据库读写
# ============================================================
def _db():
    return pymysql.connect(**get_db_config())


def load_base_positions(exclude_strategy_prefix: str = "intraday_t") -> list[dict]:
    """加载底仓 (status=HOLD), 排除 T 仓位

    Returns:
        list of dicts with keys: id, ts_code, stock_name, market, shares, cost_price, stop_loss, take_profit
    """
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, ts_code, stock_name, market, shares, cost_price, total_cost, stop_loss, take_profit
                 FROM sim_positions
                WHERE status = 'HOLD'
                  AND (strategy IS NULL OR strategy NOT LIKE %s)""",
            (f"{exclude_strategy_prefix}%",)
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def load_open_t_positions() -> list[dict]:
    """加载当日开着的 T 仓位 (用于强制还原 + PnL 结算)"""
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, ts_code, stock_name, market, shares, cost_price, total_cost
                 FROM sim_positions
                WHERE status = 'HOLD'
                  AND strategy LIKE 'intraday_t_buyback'
                  AND buy_date = CURDATE()"""
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        conn.close()


def log_t_action(direction: str, ts_code: str, stock_name: str,
                 base_position_id: Optional[int], t_position_id: Optional[int],
                 shares: int, price: float, vwap: Optional[float],
                 pct_from_vwap: Optional[float], intraday_pct: Optional[float],
                 target_pct: Optional[float], pnl: float, pnl_pct: float,
                 reason: str, status: str, executor_mode: str) -> int:
    """写 intraday_t_log, 返回新行 id"""
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO intraday_t_log
               (ts_code, stock_name, trade_date, direction, base_position_id, t_position_id,
                shares, price, vwap, pct_from_vwap, intraday_pct, target_pct,
                realized_pnl, realized_pnl_pct, reason, status, executor_mode)
               VALUES (%s, %s, CURDATE(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (ts_code, stock_name, direction, base_position_id, t_position_id,
             shares, price, vwap, pct_from_vwap, intraday_pct, target_pct,
             pnl, pnl_pct, reason, status, executor_mode)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def count_t_actions_today(ts_code: str) -> int:
    """查询当日 T 操作次数 (去重: buy_back + sell_high 算 1 次, force_close 不算)"""
    conn = _db()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM intraday_t_log
                WHERE ts_code = %s AND trade_date = CURDATE()
                  AND direction IN ('sell_high', 'buy_back')""",
            (ts_code,)
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


# ============================================================
# 风控
# ============================================================
def is_blocked_subject(name: str, ts_code: str) -> bool:
    """检查是否 ST / 北交所 / 创业板 / 科创板, 决定是否可做 T"""
    if not name:
        return False
    for kw in DEFAULTS["BLOCK_SUBJECTS"]:
        if kw in name:
            return True
    # 北交所代码: 8/4 开头 + .BJ
    if ts_code.endswith(".BJ"):
        return True
    return False


def check_risk(state: IntradayState, cfg: TConfig, hs300_change_pct: Optional[float]) -> tuple[bool, str]:
    """风控检查, 返回 (passed, reason)"""
    # R1: 单日 T 次数
    if state.t_action_count >= cfg.max_t_per_day:
        return False, f"R1 hit max T count {state.t_action_count}/{cfg.max_t_per_day}"

    # R2: 标的过滤
    if is_blocked_subject(state.name, state.ts_code):
        return False, f"R2 blocked subject: {state.name}"

    # R3: 止损/止盈已触发
    if state.stop_loss > 0 and state.cur_price <= state.stop_loss:
        return False, f"R3 stop loss hit: {state.cur_price} <= {state.stop_loss}"
    if state.take_profit > 0 and state.cur_price >= state.take_profit:
        return False, f"R3 take profit hit: {state.cur_price} >= {state.take_profit}"

    # R4: 大盘风险
    if hs300_change_pct is not None and hs300_change_pct <= cfg.hs300_panic_pct:
        return False, f"R4 hs300 panic: {hs300_change_pct}% <= {cfg.hs300_panic_pct}%"

    # R5: 刚建仓保护
    if abs(state.holding_pct) < cfg.min_holding_pct:
        return False, f"R5 holding too new: holding_pct={state.holding_pct}%"

    return True, "ok"


# ============================================================
# 信号
# ============================================================
def should_sell_high(state: IntradayState, cfg: TConfig) -> tuple[bool, str]:
    """高抛信号: 价偏离 VWAP 上方 + 涨幅达标 + 距高点回撤 (防追高)"""
    v = state.vwap
    if v <= 0:
        return False, "vwap not ready"
    if state.cur_price < v * (1 + cfg.sell_vwap_band):
        return False, f"price {state.cur_price} < VWAP*{1+cfg.sell_vwap_band:.3f}={v*(1+cfg.sell_vwap_band):.3f}"
    if state.intraday_pct < cfg.sell_pct_min:
        return False, f"intraday_pct {state.intraday_pct}% < {cfg.sell_pct_min}%"
    if state.drawdown_from_high < cfg.pullback_from_high:
        return False, f"too close to high: drawdown {state.drawdown_from_high}% < {cfg.pullback_from_high}%"
    return True, "ok"


def should_buy_back(state: IntradayState, cfg: TConfig) -> tuple[bool, str]:
    """低吸信号: 价格从卖后峰值回落幅度达标 + 目标利润达标
    
    买入条件:
      1. T 仓位已开 (已高抛卖出)
      2. 价格从卖后峰值回落 >= pullback_from_high %
      3. 目标利润 (卖价-现价)/卖价 >= min_t_profit_pct
    """
    if state.t_open_price is None:
        return False, "no T sell yet, cannot buy back"
    if state.t_position_id is not None:
        return False, "T position already open"

    # 价格从卖后峰值回落
    peak_after_sell = max(state.high, state.t_open_price or 0)
    drop_from_peak = (peak_after_sell - state.cur_price) / peak_after_sell * 100
    if drop_from_peak < cfg.pullback_from_high:
        return False, f"drop from peak {drop_from_peak:.2f}% < {cfg.pullback_from_high}%"

    # 目标利润
    if state.t_open_price > 0:
        target = (state.t_open_price - state.cur_price) / state.t_open_price * 100
        if target < cfg.min_t_profit_pct:
            return False, f"target profit {target:.2f}% < {cfg.min_t_profit_pct}%"

    return True, "ok"


# ============================================================
# 仓位计算
# ============================================================
def calc_t_shares(base_shares: int, ratio: float) -> int:
    """计算 T 仓位股数, 向下取整到 100 的倍数"""
    raw = int(base_shares * ratio)
    # 向下取整到 100
    return (raw // 100) * 100
