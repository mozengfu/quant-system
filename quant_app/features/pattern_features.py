"""
技术形态特征 — 显式建模"横盘 → 突破 → 主升"

A 股主升浪启动的形态特征 (硬编码规则, 显式工程化):
  1. 横盘特征
     - range_ma10: 近 10 日振幅均值 (高/低 - 1) × 100
     - range_ma20: 近 20 日振幅均值
     - ma20_slope: MA20 斜率 (% 变化, 10 日窗口)
     - bb_width:   布林带宽度 (近 20 日)
     - bb_width_pct: 布林带宽度在过去 60 日的分位数 (收敛检测)
     - close_near_high: 收盘价距 60 日新高的距离 (%)
  2. 突破特征
     - is_breakout_20d:  收盘价 = 20 日新高
     - is_breakout_60d:  收盘价 = 60 日新高
     - gap_up_pct:       跳空高开幅度 (今日 open vs 昨日 close)
     - upper_shadow_pct: 上影线 / 实体 比例
  3. 量能特征
     - vol_ratio:        当日量 / 5 日均量
     - vol_ratio_20d:    当日量 / 20 日均量
     - amount_ma5_ratio: 当日成交额 / 5 日均额
     - turnover_rate:    换手率
  4. 均线特征
     - ma_alignment:     MA5 > MA10 > MA20 计数 (0-3)
     - ma5_above_ma20:   1/0
     - macd_hist:        MACD 柱状图
"""
import logging

import numpy as np
import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def build_pattern_features(ts_codes: list[str], as_of_date: str, conn=None, lookback_days: int = 80) -> pd.DataFrame:
    """
    计算 (ts_code, as_of_date) 截止日的形态特征
    性能: 批量拉所有股票近 lookback_days 数据, vectorized 计算
    """
    if not ts_codes:
        return pd.DataFrame()
    should_close = False
    if conn is None:
        conn = pymysql.connect(**get_db_config())
        should_close = True
    try:
        placeholders = ','.join(['%s'] * len(ts_codes))
        sql = f"""
            SELECT ts_code, trade_date, open, high, low, close, vol, amount, turnover_rate
            FROM daily_price
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN DATE_SUB(%s, INTERVAL %s DAY) AND %s
            ORDER BY ts_code, trade_date
        """
        df = pd.read_sql(sql, conn, params=(*ts_codes, as_of_date, lookback_days, as_of_date),
                         parse_dates=['trade_date'])

        results = []
        for code, g in df.groupby('ts_code', sort=False):
            g = g.sort_values('trade_date').reset_index(drop=True)
            if len(g) < 30:
                results.append({'ts_code': code, **{k: np.nan for k in _FEATURE_NAMES}})
                continue
            row = _calc_one(g)
            row['ts_code'] = code
            results.append(row)
        return pd.DataFrame(results).set_index('ts_code')
    finally:
        if should_close:
            conn.close()


_FEATURE_NAMES = [
    'pat_range_ma10', 'pat_range_ma20', 'pat_ma20_slope',
    'pat_bb_width', 'pat_bb_width_pct', 'pat_close_to_60d_high',
    'pat_is_breakout_20d', 'pat_is_breakout_60d', 'pat_gap_up_pct', 'pat_upper_shadow_pct',
    'pat_vol_ratio', 'pat_vol_ratio_20d', 'pat_amount_ma5_ratio', 'pat_turnover_rate',
    'pat_ma_alignment', 'pat_ma5_above_ma20', 'pat_macd_hist',
    'pat_is_consolidation',   # 横盘复合: range_ma20 < 8% AND ma20_slope < 3%
    'pat_is_breakout_combo',  # 突破复合: breakout_20d AND vol_ratio >= 2
]


def _calc_one(g: pd.DataFrame) -> dict:
    close = g['close']
    high = g['high']
    low = g['low']
    vol = g['vol']
    amount = g['amount']

    # MA
    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()

    # 横盘特征
    range_pct = (high - low) / close.shift(1) * 100
    range_ma10 = range_pct.rolling(10).mean().iloc[-1]
    range_ma20 = range_pct.rolling(20).mean().iloc[-1]
    ma20_slope = (ma20.iloc[-1] / ma20.iloc[-11] - 1) * 100 if len(ma20) >= 11 else np.nan
    # 布林带
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    bb_width = ((bb_upper - bb_lower) / bb_mid * 100).iloc[-1]
    bb_width_series = pd.Series(((bb_upper - bb_lower) / bb_mid * 100).rolling(60).mean().values)
    if len(bb_width_series.dropna()) > 0:
        bb_width_pct = float(bb_width_series.rank(pct=True).iloc[-1])
    else:
        bb_width_pct = np.nan
    # 距 60 日新高
    high_60 = high.rolling(60).max().iloc[-1]
    close_to_60d_high = (close.iloc[-1] / high_60 - 1) * 100 if high_60 > 0 else np.nan

    # 突破特征
    high_20 = high.rolling(20).max().iloc[-1]
    is_breakout_20d = int(close.iloc[-1] >= high_20 * 0.999)
    high_60d = high.rolling(60).max().iloc[-1]
    is_breakout_60d = int(close.iloc[-1] >= high_60d * 0.999)
    # 跳空
    prev_close = close.iloc[-2] if len(close) >= 2 else np.nan
    gap_up_pct = (g['open'].iloc[-1] / prev_close - 1) * 100 if prev_close > 0 else np.nan
    # 上影线
    body = abs(close.iloc[-1] - g['open'].iloc[-1])
    upper_shadow = high.iloc[-1] - max(close.iloc[-1], g['open'].iloc[-1])
    upper_shadow_pct = (upper_shadow / body * 100) if body > 0 else 0

    # 量能
    vol_ma5 = vol.rolling(5).mean().iloc[-1]
    vol_ma20 = vol.rolling(20).mean().iloc[-1]
    vol_ratio = vol.iloc[-1] / vol_ma5 if vol_ma5 > 0 else np.nan
    vol_ratio_20d = vol.iloc[-1] / vol_ma20 if vol_ma20 > 0 else np.nan
    amount_ma5 = amount.rolling(5).mean().iloc[-1]
    amount_ma5_ratio = amount.iloc[-1] / amount_ma5 if amount_ma5 > 0 else np.nan
    turnover_rate = g['turnover_rate'].iloc[-1]

    # 均线
    ma5_last = ma5.iloc[-1]
    ma10_last = ma10.iloc[-1]
    ma20_last = ma20.iloc[-1]
    ma_alignment = int(ma5_last > ma10_last) + int(ma10_last > ma20_last)
    ma5_above_ma20 = int(ma5_last > ma20_last)
    # MACD
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    macd_hist = ((dif - dea) * 2).iloc[-1]

    # 复合判断
    is_consolidation = int((range_ma20 <= 8.0) and (abs(ma20_slope) < 3) and (not pd.isna(range_ma20)))
    is_breakout_combo = int(is_breakout_20d and vol_ratio >= 2.0)

    return {
        'pat_range_ma10': range_ma10,
        'pat_range_ma20': range_ma20,
        'pat_ma20_slope': ma20_slope,
        'pat_bb_width': bb_width,
        'pat_bb_width_pct': bb_width_pct,
        'pat_close_to_60d_high': close_to_60d_high,
        'pat_is_breakout_20d': is_breakout_20d,
        'pat_is_breakout_60d': is_breakout_60d,
        'pat_gap_up_pct': gap_up_pct,
        'pat_upper_shadow_pct': upper_shadow_pct,
        'pat_vol_ratio': vol_ratio,
        'pat_vol_ratio_20d': vol_ratio_20d,
        'pat_amount_ma5_ratio': amount_ma5_ratio,
        'pat_turnover_rate': turnover_rate,
        'pat_ma_alignment': ma_alignment,
        'pat_ma5_above_ma20': ma5_above_ma20,
        'pat_macd_hist': macd_hist,
        'pat_is_consolidation': is_consolidation,
        'pat_is_breakout_combo': is_breakout_combo,
    }
