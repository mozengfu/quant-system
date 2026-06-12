#!/usr/bin/env python3
"""
市场状态检测模块 — Regime Detector

6 指标 → 5 状态概率 (bull/bear/panic/range/overheated)
支持历史回标（训练用）和实时检测（推理用）。

指标：
  1. 指数趋势（上证 MA5/MA20 偏离度）
  2. 市场广度（全市场涨跌家数比例）
  3. 波动率（10日截面波动率标准差）
  4. 量能趋势（5日/20日平均成交额比）
  5. 动量广度（站上MA20的个股比例）
  6. 涨跌停比例（涨停/跌停比，A股特有信号）
"""

import logging
import sys

from dotenv import load_dotenv

load_dotenv()

import numpy as np
import pandas as pd
import pymysql

from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()
REGIMES = ['bull', 'bear', 'panic', 'range', 'overheated']
REGIME_NAMES = {'bull': '牛市', 'bear': '熊市', 'panic': '恐慌', 'range': '震荡', 'overheated': '过热'}


def get_db():
    return pymysql.connect(**DB_CONFIG)


class RegimeDetector:
    """
    市场状态检测器。

    核心方法：
      - detect_daily(conn): 返回最近一个交易日的状态概率
      - backfill(conn, n_days=600): 回标 n 天的状态数据到 MySQL
      - get_regime_history(conn, n_days=200): 获取历史状态序列（用于训练标注）
    """

    def detect_daily(self, conn, trade_date=None):
        """检测指定交易日（或最近交易日）的市场状态。

        返回 dict: {regime, probs, score, indicators}
        """
        indicators = self._compute_indicators(conn, trade_date)
        if indicators is None:
            return self._default_result()

        score, probs = self._score(indicators)
        dominant = REGIMES[np.argmax(probs)]

        return {
            'trade_date': indicators.get('trade_date'),
            'regime': dominant,
            'score': round(score, 2),
            'probs': {r: round(float(p), 4) for r, p in zip(REGIMES, probs)},
            'indicators': indicators,
        }

    def backfill(self, conn, n_days=600):
        """回标历史状态数据，写入 market_regime_daily 表。"""
        logger.info(f"回标最近 {n_days} 个交易日的市场状态...")

        cur = conn.cursor()
        dates_sql = f"""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL {n_days} DAY
            ORDER BY trade_date
        """
        dates = pd.read_sql(dates_sql, conn)
        dates = sorted(pd.to_datetime(dates['trade_date']).unique())

        logger.info(f"共 {len(dates)} 个交易日需要回标")

        # 创建表
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_regime_daily (
                trade_date DATE PRIMARY KEY,
                regime VARCHAR(20),
                score DECIMAL(5,2),
                prob_bull DECIMAL(4,3),
                prob_bear DECIMAL(4,3),
                prob_panic DECIMAL(4,3),
                prob_range DECIMAL(4,3),
                prob_overheated DECIMAL(4,3),
                sh_trend DECIMAL(5,2),
                market_breadth DECIMAL(5,2),
                volatility DECIMAL(5,2),
                volume_trend DECIMAL(5,2),
                momentum_breadth DECIMAL(5,2),
                zt_ratio DECIMAL(6,2),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()

        done = 0
        for dt in dates:
            # 跳过已有数据
            cur.execute("SELECT 1 FROM market_regime_daily WHERE trade_date=%s", (dt,))
            if cur.fetchone():
                done += 1
                continue

            result = self.detect_daily(conn, dt)
            if result is None or result.get('regime') is None:
                continue

            p = result['probs']
            ind = result['indicators']
            cur.execute("""
                INSERT INTO market_regime_daily
                (trade_date, regime, score, prob_bull, prob_bear, prob_panic, prob_range, prob_overheated,
                 sh_trend, market_breadth, volatility, volume_trend, momentum_breadth, zt_ratio)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                dt, result['regime'], result['score'],
                p['bull'], p['bear'], p['panic'], p['range'], p['overheated'],
                ind.get('sh_trend', 0), ind.get('market_breadth', 0),
                ind.get('volatility', 2.0), ind.get('volume_trend', 0),
                ind.get('momentum_breadth', 0.5), ind.get('zt_ratio', 1.0),
            ))
            done += 1
            if done % 50 == 0:
                conn.commit()
                logger.info(f"  已回标 {done}/{len(dates)} 天")

        conn.commit()
        logger.info(f"回标完成: {done} 个交易日")

    def get_regime_history(self, conn, n_days=200):
        """获取历史状态序列，返回 DataFrame。用于训练数据的 regime 标注。"""
        sql = f"""
            SELECT trade_date, regime, score,
                   prob_bull, prob_bear, prob_panic, prob_range, prob_overheated,
                   sh_trend, market_breadth, volatility, volume_trend, momentum_breadth
            FROM market_regime_daily
            WHERE trade_date >= (SELECT MAX(trade_date) FROM market_regime_daily) - INTERVAL {n_days} DAY
            ORDER BY trade_date
        """
        try:
            df = pd.read_sql(sql, conn)
            if df.empty:
                logger.warning("market_regime_daily 表为空，请先运行 backfill")
                return pd.DataFrame()
            logger.info(f"获取历史状态: {len(df)} 天, 分布: {df['regime'].value_counts().to_dict()}")
            return df
        except Exception as e:
            logger.warning(f"读取 regime 历史失败: {e}")
            return pd.DataFrame()

    def _compute_indicators(self, conn, trade_date=None):
        """计算 6 个市场状态指标。"""
        cur = conn.cursor()

        if trade_date is None:
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            row = cur.fetchone()
            trade_date = pd.Timestamp(row[0])
        else:
            trade_date = pd.Timestamp(trade_date)

        dt_str = trade_date.strftime('%Y-%m-%d')

        # 1. 指数趋势 (上证 MA5/MA20)
        cur.execute("""
            SELECT trade_date, close_price FROM market_index_daily
            WHERE index_code='000001.SH'
            AND trade_date <= %s
            ORDER BY trade_date DESC LIMIT 20
        """, (dt_str,))
        rows = cur.fetchall()
        if not rows or len(rows) < 10:
            # Fallback: 用 market_index_daily 中最近的数据
            cur.execute("""
                SELECT close_price FROM market_index_daily
                WHERE index_code='000001.SH'
                ORDER BY trade_date DESC LIMIT 20
            """)
            rows = cur.fetchall()
            if not rows or len(rows) < 10:
                return None

        closes = [float(r[1]) for r in reversed(rows)]
        sh_trend = self._calc_trend_score(closes)

        # 2. 市场广度
        cur.execute("""
            SELECT
                SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END) as up_count,
                SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END) as down_count,
                COUNT(*) as total
            FROM daily_price
            WHERE trade_date = %s
            AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
        """, (dt_str,))
        row = cur.fetchone()
        if row and row[2] and row[2] > 0:
            up, down, total = int(row[0]), int(row[1]), int(row[2])
            market_breadth = ((up - down) / total) * 100  # -100 ~ +100
        else:
            market_breadth = 0

        # 3. 波动率
        cur.execute("""
            SELECT STDDEV(pct_chg) FROM daily_price
            WHERE trade_date BETWEEN %s - INTERVAL 10 DAY AND %s
            AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
        """, (dt_str, dt_str))
        row = cur.fetchone()
        volatility = float(row[0]) if row and row[0] else 2.0

        # 4. 量能趋势
        cur.execute("""
            SELECT
                (SELECT AVG(amount) FROM daily_price
                 WHERE trade_date BETWEEN %s - INTERVAL 5 DAY AND %s
                 AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')) as vol_5d,
                (SELECT AVG(amount) FROM daily_price
                 WHERE trade_date BETWEEN %s - INTERVAL 25 DAY AND %s - INTERVAL 5 DAY
                 AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')) as vol_20d
        """, (dt_str, dt_str, dt_str, dt_str))
        row = cur.fetchone()
        if row and row[0] and row[1]:
            v5d, v20d = float(row[0]), float(row[1])
            volume_trend = ((v5d - v20d) / v20d * 100) if v20d > 0 else 0
        else:
            volume_trend = 0

        # 5. 动量广度 (%个股站上MA20)
        cur.execute("""
            SELECT
                SUM(CASE WHEN close > ma20 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as pct_above_ma20
            FROM daily_price
            WHERE trade_date = %s
            AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
            AND ma20 IS NOT NULL
        """, (dt_str,))
        row = cur.fetchone()
        momentum_breadth = float(row[0]) if row and row[0] else 50.0

        # 6. 涨跌停比例
        cur.execute("""
            SELECT
                SUM(CASE WHEN pct_chg >= 9.8 THEN 1 ELSE 0 END) as zt_count,
                SUM(CASE WHEN pct_chg <= -9.8 THEN 1 ELSE 0 END) as dt_count
            FROM daily_price
            WHERE trade_date = %s
            AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
        """, (dt_str,))
        row = cur.fetchone()
        zt = int(row[0]) if row and row[0] else 0
        dt = int(row[1]) if row and row[1] else 0
        zt_ratio = min(999.0, zt / max(dt, 1))  # 涨停/跌停比, capped

        return {
            'trade_date': trade_date,
            'sh_trend': round(sh_trend, 2),
            'market_breadth': round(market_breadth, 1),
            'volatility': round(volatility, 2),
            'volume_trend': round(volume_trend, 1),
            'momentum_breadth': round(momentum_breadth, 1),
            'zt_ratio': round(zt_ratio, 2),
        }

    def _score(self, indicators):
        """将 6 指标转换为综合评分和 5 状态概率。

        评分规则与 market_state.py 兼容但更精细，用于区分 5 种状态。
        """
        sh = indicators['sh_trend']
        breadth = indicators['market_breadth']
        vol = indicators['volatility']
        vol_trend = indicators['volume_trend']
        mom = indicators['momentum_breadth']
        zt = indicators['zt_ratio']

        # 综合评分 (-100 ~ +100)
        score = (
            sh * 0.30 +
            breadth * 0.25 +
            mom * 0.20 +
            (15 if vol_trend > 20 else -10 if vol_trend < -20 else 0) * 0.15 +
            (-30 if vol > 3.5 else 10 if vol < 1.5 else 0) * 0.10
        )
        score = max(-100, min(100, score))

        # 5 状态打分（每个状态独立的「适合度」分数）
        # 降低阈值，使状态分布更均衡:
        #   bull:      score > 5   (约 20-30% 天数)
        #   bear:      score < -5  (约 15-25% 天数)
        #   panic:     vol > 3.0 且 breadth 极差
        #   overheated: score > 25 且 mom > 70
        #   range:     其余
        bull_score = max(0, score - 5) * 1.2  # 牛市: 需要 score > 5
        bear_score = max(0, -score - 5) * 1.2  # 熊市: 需要 score < -5
        panic_score = 0
        if vol > 3.0:
            panic_score = min(100, (vol - 2.5) * 30 + max(0, -breadth) * 0.5)
        if zt < 0.2:  # 跌停远多于涨停
            panic_score = min(100, panic_score + 40)
        overheated_score = 0
        if score > 25 and mom > 65:
            overheated_score = min(100, (score - 25) * 2 + (mom - 65))
        range_score = 100 - bull_score - bear_score - panic_score - overheated_score
        range_score = max(0, range_score)

        raw = np.array([bull_score, bear_score, panic_score, range_score, overheated_score], dtype=float)

        # Softmax 转为概率
        raw = raw - raw.max()  # 数值稳定
        exp_raw = np.exp(raw / 20.0)  # temperature = 20 控制区分度
        probs = exp_raw / (exp_raw.sum() + 1e-9)

        return score, probs

    def _default_result(self):
        return {
            'trade_date': None,
            'regime': 'range',
            'score': 0,
            'probs': {r: 0.2 for r in REGIMES},
            'indicators': {},
        }

    @staticmethod
    def _calc_trend_score(closes):
        """趋势评分: -100 ~ +100"""
        if len(closes) < 10:
            return 0
        ma5 = np.mean(closes[-5:])
        ma20 = np.mean(closes[-min(20, len(closes)):])
        if ma20 > 0:
            trend = (ma5 - ma20) / ma20 * 100
            return max(-100, min(100, trend * 10))
        return 0


def backfill_regime_data(n_days=600):
    """命令行入口：回标历史市场状态数据。"""
    logger.info("=" * 60)
    logger.info("市场状态回标")
    logger.info("=" * 60)

    conn = get_db()
    try:
        detector = RegimeDetector()
        detector.backfill(conn, n_days=n_days)

        # 打印统计
        hist = detector.get_regime_history(conn, n_days=n_days)
        if not hist.empty:
            print("\n状态分布:")
            for r in REGIMES:
                cnt = len(hist[hist['regime'] == r])
                pct = cnt / len(hist) * 100
                print(f"  {REGIME_NAMES.get(r, r)}: {cnt} 天 ({pct:.1f}%)")
    finally:
        conn.close()


if __name__ == '__main__':
    n = 600
    if len(sys.argv) > 1:
        n = int(sys.argv[1])
    backfill_regime_data(n_days=n)
