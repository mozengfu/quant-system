#!/usr/bin/env python3
"""
方向二：市场状态自适应 — 识别市场当前处于什么状态，动态调整策略参数

市场状态：
- trend_up: 趋势上涨（适合持仓，提高止盈目标）
- trend_down: 趋势下跌（适合减仓，严格止损）
- range: 震荡（适合短线，降低止盈目标）
- panic: 恐慌（空仓观望）
- overheated: 过热（减仓止盈）
"""

import os
import time
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import pymysql
import numpy as np

logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

# 市场状态缓存（30秒TTL，避免交易时段多个调用方重复爬取）
_state_cache = {}
_STATE_CACHE_TTL = 30


def _get_cached_state():
    c = _state_cache.get('state')
    if c and time.time() - c['ts'] < _STATE_CACHE_TTL:
        return c['data']
    return None


def _set_cached_state(data):
    _state_cache['state'] = {'data': data, 'ts': time.time()}


def get_market_state(db_conn=None):
    """综合判断市场当前状态，缓存30秒"""
    cached = _get_cached_state()
    if cached:
        return cached

    should_close = False
    if db_conn is None:
        db_conn = pymysql.connect(**DB_CONFIG)
        should_close = True
    
    try:
        cur = db_conn.cursor()
        
        # ========== 1. 指数趋势（上证+创业板） ==========
        # 先查market_index_daily是否有足够数据
        cur.execute("SELECT COUNT(*) FROM market_index_daily WHERE index_code='000001.SH'")
        idx_count = cur.fetchone()[0]
        
        sh_trend_score = 0
        cyb_trend_score = 0
        sh_ma5 = None
        sh_ma20 = None
        
        if idx_count >= 20:
            # 有足够指数数据
            cur.execute("""
                SELECT trade_date, close_price, change_pct 
                FROM market_index_daily 
                WHERE index_code='000001.SH' 
                ORDER BY trade_date DESC LIMIT 20
            """)
            sh_rows = cur.fetchall()
            if sh_rows:
                closes = [float(r[1]) for r in reversed(sh_rows)]
                sh_trend_score = _calc_trend_score(closes)
                sh_ma5 = np.mean(closes[-5:])
                sh_ma20 = np.mean(closes[-20:])
            
            cur.execute("""
                SELECT close_price FROM market_index_daily 
                WHERE index_code='399006.SZ' 
                ORDER BY trade_date DESC LIMIT 20
            """)
            cyb_rows = cur.fetchall()
            if cyb_rows:
                cyb_closes = [float(r[0]) for r in reversed(cyb_rows)]
                cyb_trend_score = _calc_trend_score(cyb_closes)
        
        if sh_ma5 is None or sh_ma20 is None:
            # fallback: 用MySQL里的daily_price计算上证指数近似
            # 直接用daily_price的MA20平均值来代表大盘趋势
            cur.execute("""
                SELECT AVG(ma20), AVG(close) FROM daily_price 
                WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price WHERE ma20 IS NOT NULL)
                AND SUBSTRING(ts_code,1,2) IN ('60','00','30')
            """)
            row = cur.fetchone()
            if row and row[0] and row[1]:
                avg_ma20 = float(row[0])
                avg_close = float(row[1])
                sh_trend_score = 30 if avg_close > avg_ma20 else -30
        
        # ========== 2. 市场广度（涨跌家数比） - 优先使用实时数据 ==========
        from quant_app.services.realtime_service import get_market_overview as get_market_info, _is_trading_time
        
        rt_data = None
        if _is_trading_time():
            try:
                rt_data = get_market_info()
            except Exception as _e:
                logger.error(f"Error in market_state.py: {_e}")
                
        if rt_data and rt_data.get('source') == 'realtime':
            # 使用实时涨跌比
            ratio_val = rt_data['breadth_ratio']
            up = rt_data['up_cnt']
            down = rt_data['total_cnt'] - up
            total = rt_data['total_cnt']
            avg_chg = rt_data['mkt_chg']  # 用上证指数近似
            market_breadth = (ratio_val - 50) * 2  # 0-100 -> -100 ~ +100
        else:
            # 回退 MySQL 数据库
            cur.execute("""
                SELECT 
                    SUM(CASE WHEN pct_chg > 0 THEN 1 ELSE 0 END) as up_count,
                    SUM(CASE WHEN pct_chg < 0 THEN 1 ELSE 0 END) as down_count,
                    SUM(CASE WHEN pct_chg >= 5 THEN 1 ELSE 0 END) as limit_up,
                    SUM(CASE WHEN pct_chg <= -5 THEN 1 ELSE 0 END) as limit_down,
                    AVG(pct_chg) as avg_chg,
                    COUNT(*) as total
                FROM daily_price
                WHERE trade_date = (SELECT MAX(trade_date) FROM daily_price)
                AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
            """)
            breadth_row = cur.fetchone()
            
            market_breadth = 0
            if breadth_row and breadth_row[5] > 0:
                up = int(breadth_row[0])
                down = int(breadth_row[1])
                total = int(breadth_row[5])
                avg_chg = float(breadth_row[4]) if breadth_row[4] else 0
                
                # 涨跌比
                if total > 0:
                    ratio = (up - down) / total
                    market_breadth = ratio * 100  # -100 ~ +100
                
                # 极端情况加分/减分
                limit_up_count = int(breadth_row[2])
                limit_down_count = int(breadth_row[3])
                if limit_down_count > 200:
                    market_breadth -= 20
                if limit_up_count > 100:
                    market_breadth += 10
        
        # ========== 3. 波动率 ==========
        cur.execute("""
            SELECT STDDEV(pct_chg) FROM daily_price
            WHERE trade_date >= (
                SELECT MAX(trade_date) FROM daily_price
            ) - INTERVAL 10 DAY
            AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')
        """)
        vol_row = cur.fetchone()
        volatility = float(vol_row[0]) if vol_row and vol_row[0] else 2.0
        
        # 高波动 = 恐慌信号
        volatility_score = 0
        if volatility > 4.0:
            volatility_score = -40  # 高波动恐慌
        elif volatility > 3.0:
            volatility_score = -15
        elif volatility < 1.5:
            volatility_score = 10  # 低波动稳定
        
        # ========== 4. 量能趋势（对比5日均量和20日均量） ==========
        cur.execute("""
            SELECT 
                (SELECT AVG(amount) FROM daily_price 
                 WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 10 DAY
                 AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')) as vol_5d,
                (SELECT AVG(amount) FROM daily_price 
                 WHERE trade_date >= (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 25 DAY
                 AND trade_date < (SELECT MAX(trade_date) FROM daily_price) - INTERVAL 5 DAY
                 AND SUBSTRING(ts_code,1,2) IN ('60','00','01','30')) as vol_20d
        """)
        vol_trend_row = cur.fetchone()
        volume_trend = 0
        if vol_trend_row and vol_trend_row[0] and vol_trend_row[1]:
            vol_5d = float(vol_trend_row[0])
            vol_20d = float(vol_trend_row[1])
            if vol_20d > 0:
                volume_trend = ((vol_5d - vol_20d) / vol_20d) * 100
        
        volume_score = 0
        if volume_trend > 30:
            volume_score = 15  # 放量上涨信号
        elif volume_trend > 10:
            volume_score = 5
        elif volume_trend < -20:
            volume_score = -10  # 缩量
        
        # ========== 5. 综合评分 ==========
        # 各指标加权
        total_score = (
            sh_trend_score * 0.35 +
            cyb_trend_score * 0.15 +
            market_breadth * 0.25 +
            volatility_score * 0.15 +
            volume_score * 0.10
        )
        total_score = max(-100, min(100, total_score))
        
        # ========== 6. 状态判定 ==========
        if total_score >= 40:
            state = 'overheated'
            state_name = '过热'
            advice = '市场过热，建议减仓止盈，新仓≤15%'
        elif total_score >= 15:
            state = 'trend_up'
            state_name = '趋势上涨'
            advice = '市场趋势向好，可适当加仓，持有强势股'
        elif total_score >= -15:
            state = 'range'
            state_name = '震荡'
            advice = '市场震荡，短线操作，快进快出'
        elif total_score >= -40:
            state = 'trend_down'
            state_name = '趋势下跌'
            advice = '市场偏弱，控制仓位，严格止损'
        else:
            state = 'panic'
            state_name = '恐慌'
            advice = '市场恐慌，建议空仓观望，不宜抄底'
        
        # ========== 7. 策略参数推荐 ==========
        params = _get_strategy_params(state)

        result = {
            'state': state,
            'state_name': state_name,
            'score': round(total_score, 1),
            'advice': advice,
            'params': params,
            'indicators': {
                'sh_trend': round(sh_trend_score, 1),
                'cyb_trend': round(cyb_trend_score, 1),
                'market_breadth': round(market_breadth, 1),
                'volatility': round(volatility, 2),
                'volume_trend': round(volume_trend, 1),
            },
            'scan_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }
        _set_cached_state(result)
        return result

    except Exception as e:
        logger.error(f"市场状态判断失败: {e}")
        err_result = {
            'state': 'range',
            'state_name': '震荡（默认）',
            'score': 0,
            'advice': '无法获取市场数据，按震荡市操作',
            'params': _get_strategy_params('range'),
            'indicators': {},
            'error': str(e),
            'scan_date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }
        _set_cached_state(err_result)
        return err_result
    finally:
        if should_close and db_conn:
            db_conn.close()


def _calc_trend_score(closes):
    """计算趋势评分：-100 ~ +100"""
    if len(closes) < 10:
        return 0
    
    # MA5 vs MA20
    ma5 = np.mean(closes[-5:])
    ma20 = np.mean(closes[-min(20, len(closes)):])
    
    if ma20 > 0:
        trend = (ma5 - ma20) / ma20 * 100
        return max(-100, min(100, trend * 10))  # 放大10倍
    return 0


def _get_strategy_params(state):
    """根据市场状态返回推荐策略参数"""
    params = {
        'trend_up': {
            'stop_loss_pct': -5,
            'take_profit_pct': 12,
            'max_positions': 5,
            'position_pct': 20,
            'hold_days': 7,
            'ml_threshold': 0.50,  # 牛市可以降低ML门槛
        },
        'trend_down': {
            'stop_loss_pct': -3,
            'take_profit_pct': 5,
            'max_positions': 3,
            'position_pct': 10,
            'hold_days': 3,
            'ml_threshold': 0.60,  # 熊市提高ML门槛
        },
        'range': {
            'stop_loss_pct': -4,
            'take_profit_pct': 6,
            'max_positions': 4,
            'position_pct': 15,
            'hold_days': 5,
            'ml_threshold': 0.55,
        },
        'panic': {
            'stop_loss_pct': -2,
            'take_profit_pct': 3,
            'max_positions': 1,
            'position_pct': 5,
            'hold_days': 2,
            'ml_threshold': 0.65,  # 恐慌时只有高概率才买
        },
        'overheated': {
            'stop_loss_pct': -5,
            'take_profit_pct': 10,
            'max_positions': 3,
            'position_pct': 15,
            'hold_days': 4,
            'ml_threshold': 0.55,
        },
    }
    return params.get(state, params['range'])


if __name__ == '__main__':
    result = get_market_state()
    print(f"\n市场状态: {result['state_name']} (得分: {result['score']})")
    print(f"操作建议: {result['advice']}")
    print(f"\n指标:")
    for k, v in result.get('indicators', {}).items():
        print(f"  {k}: {v}")
    print(f"\n推荐策略参数:")
    for k, v in result['params'].items():
        print(f"  {k}: {v}")
