#!/usr/bin/env python3
"""
模拟交易系统 V6 - V11.0 纯ML模型驱动
- 独立模拟账户，初始资金 10 万元
- 使用V11.0 纯ML选股管线（成交额Top300 → ML排序 → TopN）
- 根据大盘状态决定是否建仓
- 自动记录信号到MySQL
- 实时计算收益率、胜率、最大回撤

MySQL 表结构：
  quant_db.sim_account - 账户状态
  quant_db.sim_trades  - 交易记录
  quant_db.sim_positions - 模拟持仓
  quant_db.sim_signals - 信号记录
"""
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timedelta

import pymysql

# sys.path.insert(REMOVED)  # noqa)))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)

# ========== 配置 ==========
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

INITIAL_CAPITAL = 100000  # 初始资金 10 万
MAX_POSITIONS = 3          # 最大持仓数（纯ML策略）
STOP_LOSS_PCT = -0.03   # 固定止损 -3%（兜底值，动态值由 get_market_params 提供）
TAKE_PROFIT_PCT = 0.06  # 固定止盈 +6%（兜底值，动态值由 get_market_params 提供）

# 仓位管理
POSITION_SIZING_MODE = 'equal'   # 'equal' | 'weighted'
PER_POSITION_PCT = 0.30          # 单仓最大占现金比例 30%

# 回撤断路器：总亏损超过此比例时暂停新买入
DRAWDOWN_CIRCUIT_BREAKER = -0.15  # -15%
# 熊市不建仓（大盘跌幅>0.5%或涨跌比<35%）
BEAR_NO_BUY = True


# ========== 交易日工具 ==========
def _count_trading_days_since(buy_date):
    """统计买入后经过了多少个交易日，fallback 到日历日"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_price "
            "WHERE trade_date > %s AND trade_date <= %s",
            (buy_date.strftime("%Y%m%d") if hasattr(buy_date, 'strftime') else str(buy_date),
             datetime.now().strftime("%Y%m%d"))
        )
        count = cursor.fetchone()[0] or 0
        cursor.close()
        conn.close()
        return count
    except Exception:
        return (datetime.now().date() - buy_date).days if buy_date else 0


# ========== 市场状态参数 ==========
def get_market_params():
    """获取当前生效的全部风控参数
    优先级：市场状态参数 > 代码默认值
    供 daily_scan（动态止损/仓位）和 position_monitor status 命令使用"""
    try:
        from market_state import get_market_state
        ms = get_market_state() or {}
        p = ms.get('params', {})
        # 取大盘涨跌幅用于状态名称
        market_info = get_market_state_for_sim()
        return {
            'state': ms.get('state', 'range'),
            'state_name': market_info.get('state_name', '常态'),
            'stop_loss_pct': p.get('stop_loss_pct', -3) / 100,
            'take_profit_pct': p.get('take_profit_pct', 6) / 100,
            'max_positions': p.get('max_positions', 3),
            'ml_threshold': p.get('ml_threshold', 0.55),
            'position_sizing_mode': POSITION_SIZING_MODE,
            'per_position_pct': PER_POSITION_PCT,
            'drawdown_circuit_breaker': DRAWDOWN_CIRCUIT_BREAKER,
        }
    except Exception:
        return {
            'state': 'range',
            'state_name': '常态',
            'stop_loss_pct': -0.03,
            'take_profit_pct': 0.06,
            'max_positions': 3,
            'ml_threshold': 0.55,
            'position_sizing_mode': POSITION_SIZING_MODE,
            'per_position_pct': PER_POSITION_PCT,
            'drawdown_circuit_breaker': DRAWDOWN_CIRCUIT_BREAKER,
        }


# ========== 腾讯财经行情（盘后也可用）==========
def get_stock_realtime(code: str, market: str = "sz"):
    """用腾讯财经获取实时行情（盘后也能拿到收盘价）"""
    symbol = "%s%s" % (market, code)
    url = "http://qt.gtimg.cn/q=" + symbol
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
        if "~" not in data:
            return None
        parts = data.strip().rstrip(";").split("~")
        if len(parts) < 50:
            return None
        return {
            "名称": parts[1],
            "代码": parts[2],
            "现价": float(parts[3]),
            "昨收": float(parts[4]),
            "今开": float(parts[5]) if len(parts) > 5 and parts[5] else 0,
            "最高": float(parts[33]) if len(parts) > 33 and parts[33] else 0,
            "最低": float(parts[34]) if len(parts) > 34 and parts[34] else 0,
            "涨跌幅": float(parts[32]),
            "成交量": float(parts[6]),
            "成交额": float(parts[37]),
            "换手率": float(parts[38]) if len(parts) > 38 else 0,
            "量比": float(parts[39]) if len(parts) > 39 else 0,
        }
    except Exception:
        return None


# ========== 大盘状态 ==========
_mkt_cache = {"index": None, "breadth": None, "ts": 0}

def _get_market_data():
    """缓存10分钟：上证历史(Tushare) + 涨跌比(MySQL)"""
    import time as _t
    now = _t.time()
    if _mkt_cache["ts"] and now - _mkt_cache["ts"] < 600:
        return _mkt_cache["index"], _mkt_cache["breadth"], _mkt_cache.get("north_flow")
    try:
        import tushare as ts
        pro = ts.pro_api()
        idx_df = pro.index_daily(ts_code='000001.SH', limit=5)
        _mkt_cache["index"] = idx_df
        # 北向资金
        try:
            nf = pro.moneyflow_hsgt(start_date='', end_date='', limit=2)
            if len(nf) > 0:
                latest = nf.iloc[0]
                _mkt_cache["north_flow"] = {
                    "north_net": float(latest.get("north_money", 0)),
                    "date": str(latest.get("trade_date", "")),
                }
        except Exception:
            _mkt_cache["north_flow"] = None
        # 涨跌比从MySQL daily_price读
        try:
            import pymysql
            conn = pymysql.connect(**DB_CONFIG)
            cur = conn.cursor()
            cur.execute("""SELECT COUNT(CASE WHEN pct_chg>0 THEN 1 END),
                                  COUNT(CASE WHEN pct_chg<0 THEN 1 END)
                           FROM daily_price WHERE trade_date=%s""",
                       (str(idx_df.iloc[0]["trade_date"]),))
            up, down = cur.fetchone()
            cur.close(); conn.close()
            up, down = int(up or 0), int(down or 0)
            _mkt_cache["breadth"] = {"up": up, "down": down, "ratio": round(up/max(down,1), 2)}
        except Exception:
            _mkt_cache["breadth"] = None
        _mkt_cache["ts"] = now
        return _mkt_cache["index"], _mkt_cache["breadth"], _mkt_cache.get("north_flow")
    except Exception:
        return _mkt_cache["index"], _mkt_cache["breadth"], _mkt_cache.get("north_flow")

def get_market_state_for_sim():
    """获取大盘状态 — 统一从 market_monitor 的 market_state.json 读取
    market_monitor 已改为 QMT 主数据源 + 腾讯降级，是唯一权威判断。
    """
    import json
    import os
    try:
        state_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  'data', 'market_state.json')
        if os.path.exists(state_file):
            with open(state_file) as f:
                st = json.load(f)
            is_bear = st.get("is_bear", False)
            sh_pct = st.get("sh_pct", 0)
            state_name = st.get("state_name", "常态")

            # 涨跌比仍从MySQL读取（用于threshold调整）
            _, breadth, _ = _get_market_data()

            # threshold: 基于涨跌比微调，逆市时提高门槛
            effective_threshold = 1.5
            if is_bear:
                effective_threshold = 2.5
            elif breadth and breadth.get("ratio", 1.0) < 0.8:
                effective_threshold = 2.0

            return {
                "is_bear": is_bear,
                "state_name": state_name,
                "mkt_chg": sh_pct,
                "threshold": effective_threshold,
                "breadth": breadth,
                "threshold_raw": effective_threshold,
            }
    except Exception as e:
        logger.warning("读取market_state.json失败: %s", e)
    # 默认常态
    return {"is_bear": False, "state_name": "常态", "mkt_chg": 0, "threshold": 1.5}


# ========== 旧策略选股（保留作CLI参考）==========
def ml_select_from_strategy(strategy_name, latest_date):
    """
    从指定策略选出一只最佳股票（与推荐系统一致）
    strategy_name: 'bottom' | 'strong' | 'combo'
    """
    import pymysql
    from mainforce_scoring import calculate_mainforce_score

    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    if strategy_name == 'bottom':
        # 底部起步：从stock_pool_bottom.json读取
        pool_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'stock_pool_bottom.json')
        if not os.path.exists(pool_file):
            cursor.close()
            return None
        with open(pool_file) as f:
            data = json.load(f)
        stocks = data.get("stocks", [])
        if not stocks:
            cursor.close()
            return None
        # 标准化代码
        for s in stocks:
            raw = s.get('代码', '')
            if '.' in raw and len(raw.split('.')[0]) == 6:
                s['_ts_code'] = raw
            elif len(raw) == 6:
                market = 'SH' if raw.startswith('6') else 'SZ'
                s['_ts_code'] = "%s.%s" % (raw, market)
            elif len(raw) >= 8:
                s['_ts_code'] = "%s.%s" % (raw[2:], 'SH' if raw[:2]=='SH' else 'SZ')
            else:
                s['_ts_code'] = raw
    elif strategy_name == 'strong':
        # 强势活跃：从stock_pool_snap读取
        cursor.execute("SELECT MAX(snap_date) FROM quant_db.stock_pool_snap")
        snap_date = cursor.fetchone()[0]
        if not snap_date:
            cursor.close()
            return None
        cursor.execute("""
            SELECT ts_code, name, industry, price, change_pct,
                   turnover_rate, vol_ratio, quick_score, entry_reason
            FROM quant_db.stock_pool_snap
            WHERE snap_date = %s
            ORDER BY quick_score DESC
            LIMIT 50
        """, (snap_date,))
        rows = cursor.fetchall()
        if not rows:
            cursor.close()
            return None
        stocks = []
        for r in rows:
            code = r[0].split(".")[0]
            mkt = "sz" if r[0].endswith(".SZ") else "sh"
            code_full = "%s%s" % (mkt.upper(), code)
            stocks.append({
                "代码": code_full,
                "名称": r[1] or "",
                "行业": r[2] or "",
                "现价": float(r[3]) if r[3] else 0,
                "涨跌幅": "%.2f%%" % float(r[4]),
                "换手率": "%.2f%%" % float(r[5] or 0),
                "量比": "%.2f" % float(r[6] or 0),
                "综合评分": round(float(r[7]) if r[7] else 0, 0),
                "入选理由": r[8] or "",
            })
    else:
        # combo：SQL筛选
        sql = """
            SELECT d.ts_code, s.name, s.industry,
                   d.close, d.pct_chg,
                   d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20
            FROM quant_db.daily_price d
            JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE d.trade_date = %s
              AND d.close > 5
              AND d.pct_chg > 1
              AND d.pct_chg < 9.5
              AND d.turnover_rate > 1.5
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
              AND d.ts_code NOT LIKE '92%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
              AND (
                  ((d.ma5 > d.ma10) AND (d.ma10 > d.ma20) AND d.close > d.ma5 AND d.volume_ratio > 1.5)
                  OR (d.pct_chg > 3.0 AND d.volume_ratio > 1.5)
              )
            ORDER BY d.pct_chg DESC
            LIMIT 100
        """
        cursor.execute(sql, (str(latest_date),))
        candidates = cursor.fetchall()
        if not candidates:
            cursor.close()
            return None
        stocks = []
        for r in candidates:
            ts_code = r[0]
            try:
                mf = calculate_mainforce_score(ts_code, latest_date, conn=conn)
            except Exception:
                mf = {'score': 0}
            mainforce_score = mf.get('score', 0)
            if mainforce_score < 60:
                continue
            code_raw = ts_code.split(".")[0]
            mkt = "sz" if ts_code.endswith(".SZ") else "sh"
            code_full = "%s%s" % (mkt.upper(), code_raw)
            reasons = []
            if r[7] and r[8] and r[9] and float(r[7]) > float(r[8]) > float(r[9]):
                reasons.append("均线多头")
            if float(r[6] or 0) > 1.5:
                reasons.append("量比充足")
            if mf.get('net_flow', 0) > 0:
                reasons.append("主力净流入")
            stocks.append({
                "代码": code_full,
                "名称": r[1] or "",
                "行业": r[2] or "",
                "现价": float(r[3]) if r[3] else 0,
                "涨跌幅": "%.2f%%" % float(r[4]),
                "换手率": "%.2f%%" % float(r[5] or 0),
                "量比": "%.2f" % float(r[6] or 0),
                "综合评分": mainforce_score,
                "主力评分": mainforce_score,
                "入选理由": " + ".join(reasons) if reasons else "技术面达标",
            })
        if not stocks:
            cursor.close()
            conn.close()
            return None

    if not stocks:
        cursor.close()
        conn.close()
        return None

    # ML增强评分
    try:
        from ml_predict import ml_enhanced_score
        stocks = ml_enhanced_score(stocks, db_conn=conn)
    except Exception as e:
        logger.warning("ML增强失败: %s", e)
        for s in stocks:
            s['ml概率'] = 0.5
            s['增强评分'] = s.get('综合评分', 0)
            s['热点板块'] = ''
            s['资金趋势'] = ''

    # 获取大盘状态决定阈值
    mkt_info = get_market_state_for_sim()
    primary_thresh = mkt_info['threshold']
    fallback1 = primary_thresh - 0.10 if mkt_info['is_bear'] else primary_thresh - 0.05

    # 漏斗筛选
    qualified = [s for s in stocks if s.get('ml概率', 0) >= primary_thresh]
    if not qualified:
        qualified = [s for s in stocks if s.get('ml概率', 0) >= fallback1]
    if not qualified:
        qualified = stocks

    # 排序
    def rank_key(s):
        ml = s.get('ml概率', 0.5)
        accel = 1 if s.get('资金趋势') == 'accelerating' else 0
        hot = 1 if s.get('热点板块') else 0
        return ml + accel * 0.001 + hot * 0.0005

    qualified.sort(key=rank_key, reverse=True)
    best = qualified[0]

    ml_prob = best.get('ml概率', 0)
    if ml_prob >= primary_thresh:
        signal = '强'
    elif ml_prob >= fallback1:
        signal = '中'
    else:
        signal = '弱'

    cursor.close()
    conn.close()

    return {
        "代码": best.get('代码', ''),
        "名称": best.get('名称', ''),
        "行业": best.get('行业', ''),
        "现价": best.get('现价', 0),
        "涨跌幅": best.get('涨跌幅', ''),
        "换手率": best.get('换手率', ''),
        "量比": best.get('量比', ''),
        "综合评分": best.get('综合评分', 0),
        "增强评分": best.get('增强评分', best.get('综合评分', 0)),
        "ml概率": best.get('ml概率', 0.5),
        "热点板块": best.get('热点板块', ''),
        "资金趋势": best.get('资金趋势', ''),
        "信号强度": signal,
        "入选理由": best.get('入选理由', ''),
        "止损价": round(best.get('现价', 0) * (1 + get_market_params()['stop_loss_pct']), 2),
        "策略来源": {"bottom": "底部起步", "strong": "强势活跃", "combo": "组合策略"}[strategy_name],
        "market_state": mkt_info['state_name'],
    }


# ========== 建表 ==========
def create_tables():
    """创建模拟交易所需的表"""
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # 账户状态表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sim_account (
            id INT AUTO_INCREMENT PRIMARY KEY,
            initial_capital DECIMAL(12,2) NOT NULL DEFAULT 100000.00,
            cash DECIMAL(12,2) NOT NULL DEFAULT 100000.00,
            total_value DECIMAL(12,2) NOT NULL DEFAULT 100000.00,
            profit_loss DECIMAL(12,2) NOT NULL DEFAULT 0.00,
            profit_pct DECIMAL(8,4) NOT NULL DEFAULT 0.0000,
            max_drawdown DECIMAL(8,4) NOT NULL DEFAULT 0.0000,
            peak_value DECIMAL(12,2) NOT NULL DEFAULT 100000.00,
            trade_count INT NOT NULL DEFAULT 0,
            win_count INT NOT NULL DEFAULT 0,
            win_rate DECIMAL(6,4) NOT NULL DEFAULT 0.0000,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY idx_date (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 交易记录表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(50) NOT NULL,
            market ENUM('sz','sh') NOT NULL,
            action ENUM('BUY','SELL') NOT NULL,
            price DECIMAL(8,3) NOT NULL,
            shares INT NOT NULL,
            amount DECIMAL(12,2) NOT NULL,
            commission DECIMAL(8,2) NOT NULL DEFAULT 5.00,
            stamp_tax DECIMAL(8,2) NOT NULL DEFAULT 0.00,
            trade_date DATE NOT NULL,
            trade_time DATETIME NOT NULL,
            profit_loss DECIMAL(10,2) DEFAULT NULL COMMENT '卖出时盈亏',
            profit_pct DECIMAL(8,4) DEFAULT NULL COMMENT '卖出时盈亏率',
            reason VARCHAR(100) DEFAULT NULL COMMENT '交易原因',
            created_at DATETIME NOT NULL,
            INDEX idx_ts_code (ts_code),
            INDEX idx_trade_date (trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 模拟持仓表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sim_positions (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(50) NOT NULL,
            market ENUM('sz','sh') NOT NULL,
            shares INT NOT NULL,
            cost_price DECIMAL(8,3) NOT NULL,
            total_cost DECIMAL(12,2) NOT NULL,
            current_price DECIMAL(8,3) DEFAULT NULL,
            market_value DECIMAL(12,2) DEFAULT NULL,
            profit_loss DECIMAL(10,2) DEFAULT 0.00,
            profit_pct DECIMAL(8,4) DEFAULT 0.0000,
            stop_loss DECIMAL(8,3) NOT NULL,
            take_profit DECIMAL(8,3) NOT NULL,
            buy_date DATE NOT NULL,
            buy_time DATETIME NOT NULL,
            status ENUM('HOLD','SOLD') NOT NULL DEFAULT 'HOLD',
            sell_date DATE DEFAULT NULL,
            sell_price DECIMAL(8,3) DEFAULT NULL,
            final_pnl DECIMAL(10,2) DEFAULT NULL,
            final_pnl_pct DECIMAL(8,4) DEFAULT NULL,
            updated_at DATETIME NOT NULL,
            INDEX idx_ts_code (ts_code),
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 信号记录表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sim_signals (
            id INT AUTO_INCREMENT PRIMARY KEY,
            signal_type VARCHAR(20) NOT NULL COMMENT '信号类型: 买入/止损/止盈/超时',
            ts_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(50) NOT NULL,
            price DECIMAL(8,3) NOT NULL,
            shares INT NOT NULL DEFAULT 0,
            strategy VARCHAR(50) DEFAULT NULL COMMENT '策略来源',
            ml_prob DECIMAL(6,4) DEFAULT NULL,
            enhanced_score DECIMAL(8,2) DEFAULT NULL,
            market_state VARCHAR(20) DEFAULT NULL,
            reason VARCHAR(200) DEFAULT NULL,
            signal_date DATE NOT NULL,
            signal_time DATETIME NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT '已执行' COMMENT '已执行/持仓中/已平仓',
            close_price DECIMAL(8,3) DEFAULT NULL,
            close_date DATE DEFAULT NULL,
            pnl DECIMAL(10,2) DEFAULT NULL,
            pnl_pct DECIMAL(8,4) DEFAULT NULL,
            created_at DATETIME NOT NULL,
            INDEX idx_ts_code (ts_code),
            INDEX idx_signal_date (signal_date),
            INDEX idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # 初始化账户（如果没有记录）
    cursor.execute("SELECT COUNT(*) FROM sim_account")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO sim_account 
            (initial_capital, cash, total_value, profit_loss, profit_pct, 
             max_drawdown, peak_value, trade_count, win_count, win_rate, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            INITIAL_CAPITAL, INITIAL_CAPITAL, INITIAL_CAPITAL, 0, 0,
            0, INITIAL_CAPITAL, 0, 0, 0, datetime.now()
        ))
        logger.info(f"模拟账户已初始化，初始资金: {INITIAL_CAPITAL}")

    conn.commit()
    cursor.close()
    conn.close()
    logger.info("模拟交易表创建完成")


# ========== 辅助函数 ==========
def get_db_conn():
    return pymysql.connect(**DB_CONFIG)


def get_account():
    """获取当前账户状态"""
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sim_account ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return None
    cols = [d[0] for d in cursor.description]
    account = dict(zip(cols, row))
    cursor.close()
    conn.close()
    return account


def get_holding_positions():
    """获取当前模拟持仓"""
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, ts_code, stock_name, market, shares, cost_price, total_cost,
               current_price, market_value, profit_loss, profit_pct,
               stop_loss, take_profit, buy_date, status
        FROM sim_positions
        WHERE status = 'HOLD'
    """)
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    positions = [dict(zip(cols, r)) for r in rows]
    cursor.close()
    conn.close()
    return positions


# ========== 纯ML选股（生产主模式）==========
def pure_ml_scan(top_n=3):
    """
    纯ML选股管线 — 与生产管线一致（PURE_ML=1）
    成交额Top300(前日) → V11.0预测 → ML百分位过滤 → TopN
    """
    conn = get_db_conn()
    cur = conn.cursor()

    # 最新交易日
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest_date = cur.fetchone()[0]
    if not latest_date:
        cur.close(); conn.close()
        return []
    today_str = str(latest_date)

    # 前一日成交额Top300
    cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (today_str,))
    prev_date = cur.fetchone()[0]
    if not prev_date:
        cur.close(); conn.close()
        return []
    prev_str = str(prev_date)

    cur.execute("""
        SELECT d.ts_code, s.name, s.industry, d.close, d.pct_chg
        FROM daily_price d
        JOIN stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date = %s
          AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '8%%'
          AND d.ts_code NOT LIKE '4%%' AND d.ts_code NOT LIKE '9%%'
          AND s.name NOT LIKE '%%ST%%' AND s.name NOT LIKE '%%退%%'
          AND d.close <= 200 AND d.close >= 3
        ORDER BY d.amount DESC
        LIMIT 300
    """, (prev_str,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    if not rows:
        logger.info("纯ML选股: 无候选")
        return []

    ts_codes = [r[0] for r in rows]
    name_map = {r[0]: r[1] or "" for r in rows}
    price_map = {r[0]: float(r[3]) if r[3] else 0 for r in rows}
    pct_map = {r[0]: float(r[4]) if r[4] else 0 for r in rows}

    # V11.0 批量预测
    try:
        from ml_predict import predict_batch
        preds = predict_batch(ts_codes)
    except Exception as e:
        logger.warning("ML预测失败: %s", e)
        return []

    if not preds:
        return []

    # 百分位过滤（保留ML排名前50%）+ 按z_score排序
    qualified = []
    for tc in ts_codes:
        ml = preds.get(tc, {})
        rank_pct = ml.get('rank_pct', 0.5)
        z_score = ml.get('predicted_return', 0)
        if rank_pct > 0.50:  # 只保留横截面前50%
            continue

        # 5日累计涨幅超30%过滤（避免追高短期暴涨股）
        try:
            conn5 = get_db_conn()
            cur5 = conn5.cursor()
            # 查询5个交易日前的收盘价
            cur5.execute(
                "SELECT close FROM daily_price WHERE ts_code=%s AND trade_date < %s ORDER BY trade_date DESC LIMIT 5",
                (tc, today_str),
            )
            rows5 = cur5.fetchall()
            cur5.close()
            conn5.close()
            if len(rows5) >= 5:
                price_5d_ago = float(rows5[-1][0]) if rows5[-1] and rows5[-1][0] else 0
                cur_price = price_map.get(tc, 0)
                if price_5d_ago > 0 and cur_price > 0:
                    gain_5d = (cur_price - price_5d_ago) / price_5d_ago * 100
                    if gain_5d > 30:
                        logger.debug("跳过 %s: 5日涨幅%.1f%% > 30%%", tc, gain_5d)
                        continue
        except Exception:
            pass

        market = "sz" if tc.endswith(".SZ") else "sh"
        qualified.append({
            "ts_code": tc,
            "name": name_map.get(tc, ""),
            "market": market,
            "price": price_map.get(tc, 0),
            "pct_chg": pct_map.get(tc, 0),
            "ml_prob": ml.get('probability', 0.5),
            "ml_score": z_score,
            "rank_pct": rank_pct,
        })

    qualified.sort(key=lambda x: x["ml_score"], reverse=True)

    if qualified:
        logger.info("纯ML选股: %d只通过百分位过滤 → 取Top%d", len(qualified), top_n)

    return qualified[:top_n]


# ========== V4 策略选股 ==========
def v4_scan(top_n=3):
    """
    V4.1→V6.5 级联策略扫描：技术筛选 → 综合评分(龙虎榜/股东加分) → V6.5 ML排序
    与 /api/combo_scan 级联策略一致
    """
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
    latest_date = cur.fetchone()[0]
    if not latest_date:
        cur.close(); conn.close()
        return []
    today_str = str(latest_date)

    sql = """
        SELECT d.ts_code, s.name, s.industry,
               d.close, d.pct_chg, d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20
        FROM quant_db.daily_price d
        JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date = %s
          AND d.close > 5 AND d.pct_chg > 1 AND d.pct_chg < 9.5 AND d.turnover_rate > 1.5
          AND s.is_st = 0 AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '92%%'
          AND d.ts_code NOT LIKE '8%%' AND d.ts_code NOT LIKE '4%%'
          AND (
              (d.ma5 > d.ma10 AND d.ma10 > d.ma20 AND d.ma5 IS NOT NULL AND d.ma20 IS NOT NULL AND d.close > d.ma5 AND d.volume_ratio > 1.5)
              OR (d.pct_chg > 4.0 AND d.volume_ratio > 2.0 AND d.close > d.ma5)
          )
        ORDER BY d.pct_chg DESC LIMIT 200
    """
    cur.execute(sql, (today_str,))
    candidates = cur.fetchall()

    if not candidates:
        cur.close(); conn.close()
        return []

    # 加载龙虎榜数据（近30天）
    dt_30 = (datetime.strptime(today_str, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    cur.execute("SELECT ts_code, trade_date, net_buy FROM dragon_tiger WHERE trade_date >= %s AND net_buy != 0", (dt_30,))
    dt_map = {}
    for r in cur.fetchall():
        dt_map.setdefault(r[0], []).append((str(r[1]), float(r[2] or 0)))

    cur.execute("SELECT ts_code, trade_date, net_buy, exalter FROM dragon_tiger_inst WHERE trade_date >= %s AND net_buy != 0", (dt_30,))
    dti_map = {}
    for r in cur.fetchall():
        dti_map.setdefault(r[0], []).append((str(r[1]), float(r[2] or 0), r[3] or ''))

    # 股东人数变化
    hc_from = (datetime.strptime(today_str, '%Y-%m-%d') - timedelta(days=730)).strftime('%Y-%m-%d')
    cur.execute("SELECT ts_code, end_date, holder_num_change FROM holder_change WHERE end_date >= %s AND end_date <= %s ORDER BY ts_code, end_date DESC",
                (hc_from, today_str))
    hc_map = {}
    for r in cur.fetchall():
        hc_map.setdefault(r[0], []).append((str(r[1]), int(r[2] or 0)))
    cur.close()

    # 龙虎榜加分
    def _dragon_bonus(ts_code):
        inst_net = sum(nb for _, nb, _ in dti_map.get(ts_code, []) if _ >= dt_30)
        if inst_net > 30000000: return 15
        if inst_net > 5000000: return 12
        return 8 if any(True for _ in dt_map.get(ts_code, []) if _[0] >= dt_30) else 0

    # 股东集中度加分
    def _holder_bonus(ts_code):
        rows = sorted([(td, chg) for td, chg in hc_map.get(ts_code, []) if td <= today_str], key=lambda x: x[0], reverse=True)
        if len(rows) < 2: return 0
        dec = sum(1 for _, chg in rows[:4] if chg < 0)
        return (10 if dec >= 3 else 7 if dec >= 2 else 4 if dec >= 1 else 0)

    from mainforce_scoring import calculate_mainforce_score
    stocks = []
    for r in candidates:
        ts_code = r[0]
        name = r[1] or ""
        industry = r[2] or ""
        price = float(r[3]) if r[3] else 0
        pct_chg = float(r[4]) if r[4] else 0
        turnover = float(r[5]) if r[5] else 0
        vol_ratio = float(r[6]) if r[6] else 0
        ma5 = float(r[7]) if r[7] else 0
        ma10 = float(r[8]) if r[8] else 0
        ma20 = float(r[9]) if r[9] else 0
        if price <= 0:
            continue

        try:
            mf = calculate_mainforce_score(ts_code, latest_date, conn=conn)
        except Exception:
            mf = {'score': 0, 'level': '未知'}
        mainforce_score = mf.get('score', 0)

        if mainforce_score < 50:
            continue

        qs = 0
        if ma5 > ma10 > ma20 and ma20 > 0: qs += 40
        if price > ma5: qs += 20
        if vol_ratio > 2.0: qs += 20
        if pct_chg > 3: qs += 10
        if turnover > 3: qs += 10

        dt_bonus = _dragon_bonus(ts_code)
        hc_bonus = _holder_bonus(ts_code)

        code_raw = ts_code.split(".")[0]
        mkt = "sz" if ts_code.endswith(".SZ") else "sh"

        stocks.append({
            "代码": f"{mkt.upper()}{code_raw}",
            "交易所": mkt, "名称": name, "行业": industry,
            "现价": price, "涨跌幅": f"{pct_chg:+.2f}%",
            "换手率": f"{turnover:.2f}%", "量比": f"{vol_ratio:.2f}",
            "主力评分": int(mainforce_score), "阶段判断": mf.get('level', '未知'),
            "综合评分": qs + dt_bonus + hc_bonus,
            "龙虎榜加分": dt_bonus, "股东加分": hc_bonus,
            "ts_code": ts_code, "name": name, "market": mkt,
            "price": price, "pct_chg": pct_chg, "vol_ratio": vol_ratio,
        })
    conn.close()

    if not stocks:
        return []

    # ML增强评分（级联核心：V4.1筛选 → V6.5 ML排序）
    try:
        from ml_predict import ml_enhanced_score
        conn2 = get_db_conn()
        stocks = ml_enhanced_score(stocks, db_conn=conn2)
        conn2.close()
    except Exception as e:
        logger.warning("ML增强失败，跳过: %s", e)
        for s in stocks:
            s['ml概率'] = 0.5
            s['预测收益'] = 0.0
            s['增强评分'] = s.get('综合评分', 0)

    # 按预测收益降序
    stocks.sort(key=lambda x: x.get('预测收益', 0), reverse=True)

    result = [{
        "ts_code": s["ts_code"], "name": s["name"], "market": s["market"],
        "price": s["price"], "pct_chg": s["pct_chg"], "vol_ratio": s["vol_ratio"],
        "mainforce_score": s.get("主力评分", 0),
        "ml_prob": s.get("ml概率", 0.5),
        "enhanced_score": s.get("增强评分", 0),
        "predicted_return": s.get("预测收益", 0),
    } for s in stocks]

    return result[:top_n]


# ========== 信号记录 ==========
def record_signal(signal_type, ts_code, stock_name, price, shares, strategy, ml_prob, enhanced_score, market_state, reason, status="已执行"):
    """记录信号到MySQL sim_signals表

    重新抛 IntegrityError(1062) 给调用方,让 scanner / morning_execute 能识别
    uk_sim_signals_executed 触发的同 ts_code+date 重复。其他异常仍吞掉避免影响主流程。
    """
    import pymysql
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sim_signals
            (signal_type, ts_code, stock_name, price, shares, strategy, ml_prob, enhanced_score,
             market_state, reason, signal_date, signal_time, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (signal_type, ts_code, stock_name, price, shares, strategy,
              ml_prob, enhanced_score, market_state, reason,
              datetime.now().strftime("%Y-%m-%d"), datetime.now(), status, datetime.now()))
        conn.commit()
        cursor.close()
        conn.close()
    except pymysql.err.IntegrityError:
        # uk_sim_signals_executed 触发:让调用方决定怎么处理
        raise
    except Exception as e:
        logger.warning("记录信号失败: %s", e)


# ========== 交易执行 ==========
def execute_buy(ts_code, name, market, price, shares, trade_date=None, reason="ML策略买入",
                strategy=None, ml_prob=None, enhanced_score=None, market_state=None):
    """执行模拟买入"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    amount = round(price * shares, 2)
    commission = max(5.0, amount * 0.00025)

    account = get_account()
    if not account:
        logger.error("无法获取账户状态")
        return False

    if float(account["cash"]) < amount + commission:
        logger.warning("资金不足: 需要 %.2f, 可用 %.2f", amount + commission, float(account["cash"]))
        return False

    conn = get_db_conn()
    cursor = conn.cursor()

    # 扣减资金
    new_cash = float(account["cash"]) - amount - commission
    cursor.execute("""
        UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s
    """, (new_cash, datetime.now(), account["id"]))

    # 记录交易
    cursor.execute("""
        INSERT INTO sim_trades
        (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
         trade_date, trade_time, reason, created_at)
        VALUES (%s, %s, %s, 'BUY', %s, %s, %s, %s, 0, %s, %s, %s, %s)
    """, (ts_code, name, market, price, shares, amount, commission,
          trade_date, datetime.now(), reason, datetime.now()))

    # 记录持仓（含ML信息），使用市场状态参数计算止盈止损
    _mp_exec = get_market_params()
    stop_loss = round(price * (1 + _mp_exec['stop_loss_pct']), 3)
    take_profit = round(price * (1 + _mp_exec['take_profit_pct']), 3)
    total_cost = amount + commission

    cursor.execute("""
        INSERT INTO sim_positions
        (ts_code, stock_name, market, shares, cost_price, total_cost,
         stop_loss, take_profit, buy_date, buy_time, status, updated_at,
         ml_prob, strategy, market_state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HOLD', %s, %s, %s, %s)
    """, (ts_code, name, market, shares, price, total_cost,
          stop_loss, take_profit, trade_date, datetime.now(), datetime.now(),
          ml_prob, strategy, market_state))

    # 更新账户交易计数
    cursor.execute("""
        UPDATE sim_account SET trade_count = trade_count + 1, updated_at = %s WHERE id = %s
    """, (datetime.now(), account["id"]))

    conn.commit()
    cursor.close()
    conn.close()

    # 记录信号
    record_signal("买入", ts_code, name, price, shares, strategy or "ML策略",
                  ml_prob, enhanced_score, market_state, reason, "持仓中")

    logger.info("✅ 模拟买入: %s %d股 @ %.2f (金额: %.2f) [ML=%.2f, 策略=%s]",
                name, shares, price, amount, ml_prob or 0, strategy or "ML")
    return True


def execute_sell(position_id, price, trade_date=None, reason="止盈/止损"):
    """执行模拟卖出"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    conn = get_db_conn()
    cursor = conn.cursor()

    # 获取持仓
    cursor.execute("""
        SELECT p.*, a.id as account_id, a.cash
        FROM sim_positions p
        JOIN sim_account a ON a.id = (SELECT MAX(id) FROM sim_account)
        WHERE p.id = %s AND p.status = 'HOLD'
    """, (position_id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        logger.warning("持仓 %d 不存在或已卖出", position_id)
        return False

    cols = [d[0] for d in cursor.description]
    pos = dict(zip(cols, row))

    shares = int(pos["shares"])
    cost_price = float(pos["cost_price"])
    amount = round(price * shares, 2)
    commission = max(5.0, amount * 0.00025)
    stamp_tax = amount * 0.001
    total_fees = commission + stamp_tax

    sell_amount = amount - total_fees
    pnl = sell_amount - float(pos["total_cost"])
    pnl_pct = pnl / float(pos["total_cost"])

    # 更新账户资金
    new_cash = float(pos["cash"]) + sell_amount
    cursor.execute("""
        UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s
    """, (new_cash, datetime.now(), pos["account_id"]))

    # 记录交易
    cursor.execute("""
        INSERT INTO sim_trades
        (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
         trade_date, trade_time, profit_loss, profit_pct, reason, created_at)
        VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (pos["ts_code"], pos["stock_name"], pos["market"], price, shares, amount,
          commission, stamp_tax, trade_date, datetime.now(), pnl, pnl_pct, reason, datetime.now()))

    # 更新持仓状态
    cursor.execute("""
        UPDATE sim_positions
        SET status = 'SOLD', sell_date = %s, sell_price = %s,
            final_pnl = %s, final_pnl_pct = %s, updated_at = %s
        WHERE id = %s
    """, (trade_date, price, pnl, pnl_pct, datetime.now(), position_id))

    # 更新账户统计
    cursor.execute("""
        UPDATE sim_account
        SET trade_count = trade_count + 1,
            win_count = win_count + CASE WHEN %s > 0 THEN 1 ELSE 0 END,
            win_rate = CASE WHEN trade_count + 1 > 0
                      THEN (win_count + CASE WHEN %s > 0 THEN 1 ELSE 0 END) / (trade_count + 1)
                      ELSE 0 END,
            updated_at = %s
        WHERE id = %s
    """, (pnl, pnl, datetime.now(), pos["account_id"]))

    conn.commit()

    # 更新信号记录
    cursor.execute("""
        UPDATE sim_signals SET status='已平仓', close_price=%s, close_date=%s, pnl=%s, pnl_pct=%s
        WHERE ts_code=%s AND status='持仓中' ORDER BY id DESC LIMIT 1
    """, (price, trade_date, pnl, pnl_pct, pos["ts_code"]))
    conn.commit()

    cursor.close()
    conn.close()

    logger.info("💰 模拟卖出: %s %d股 @ %.2f 盈亏: %.2f (%.2f%%)",
                pos["stock_name"], shares, price, pnl, pnl_pct * 100)
    return True


def execute_partial_sell(position_id, shares_to_sell, price, trade_date=None, reason="止盈减仓"):
    """执行模拟部分卖出（卖出指定股数，不清仓）"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    conn = get_db_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT p.*, a.id as account_id, a.cash
        FROM sim_positions p
        JOIN sim_account a ON a.id = (SELECT MAX(id) FROM sim_account)
        WHERE p.id = %s AND p.status = 'HOLD'
    """, (position_id,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        logger.warning("持仓 %d 不存在或已卖出", position_id)
        return False

    cols = [d[0] for d in cursor.description]
    pos = dict(zip(cols, row))

    total_shares = int(pos["shares"])
    shares_remaining = total_shares - shares_to_sell
    if shares_remaining <= 0:
        # 如果要卖的数量≥持仓数，转全仓卖出
        cursor.close()
        conn.close()
        return execute_sell(position_id, price, trade_date, reason)

    cost_price = float(pos["cost_price"])
    amount = round(price * shares_to_sell, 2)
    commission = max(5.0, amount * 0.00025)
    stamp_tax = amount * 0.001
    total_fees = commission + stamp_tax

    sell_amount = amount - total_fees
    cost_of_sold = float(pos["total_cost"]) * (shares_to_sell / total_shares)
    pnl = sell_amount - cost_of_sold

    # 更新账户资金
    new_cash = float(pos["cash"]) + sell_amount
    cursor.execute("""
        UPDATE sim_account SET cash = %s, updated_at = %s WHERE id = %s
    """, (new_cash, datetime.now(), pos["account_id"]))

    # 更新持仓（减少股数和成本）
    new_total_cost = float(pos["total_cost"]) * (shares_remaining / total_shares)
    cursor.execute("""
        UPDATE sim_positions
        SET shares = %s, total_cost = %s, updated_at = %s
        WHERE id = %s
    """, (shares_remaining, new_total_cost, datetime.now(), position_id))

    # 记录交易
    cursor.execute("""
        INSERT INTO sim_trades
        (ts_code, stock_name, market, action, price, shares, amount, commission, stamp_tax,
         trade_date, trade_time, profit_loss, profit_pct, reason, created_at)
        VALUES (%s, %s, %s, 'SELL', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (pos["ts_code"], pos["stock_name"], pos["market"], price, shares_to_sell, amount,
          commission, stamp_tax, trade_date, datetime.now(), pnl, pnl / cost_of_sold if cost_of_sold > 0 else 0,
          reason, datetime.now()))

    conn.commit()
    cursor.close()
    conn.close()

    logger.info("💰 模拟减仓: %s %d→%d股 @ %.2f 盈亏: %.2f (%s)",
                pos["stock_name"], total_shares, shares_remaining, price, pnl, reason)
    return True



# ========== 更新账户净值 ==========
def update_account_value():
    """更新账户总价值和最大回撤"""
    account = get_account()
    if not account:
        return

    positions = get_holding_positions()
    holding_value = 0

    conn = get_db_conn()
    cursor = conn.cursor()

    for pos in positions:
        # 获取实时价格更新当前价
        code = pos["ts_code"].split(".")[0]
        market = pos["market"]
        quote = get_stock_realtime(code, market)

        if quote:
            current_price = quote["现价"]
            market_value = round(current_price * int(pos["shares"]), 2)
            pnl = round(market_value - float(pos["total_cost"]), 2)
            pnl_pct = round(pnl / float(pos["total_cost"]), 4) if float(pos["total_cost"]) > 0 else 0

            cursor.execute("""
                UPDATE sim_positions
                SET current_price = %s, market_value = %s,
                    profit_loss = %s, profit_pct = %s, updated_at = %s
                WHERE id = %s
            """, (current_price, market_value, pnl, pnl_pct, datetime.now(), pos["id"]))
            holding_value += market_value

    total_value = float(account["cash"]) + holding_value
    profit_loss = total_value - float(account["initial_capital"])
    profit_pct = profit_loss / float(account["initial_capital"]) if float(account["initial_capital"]) > 0 else 0

    # 更新峰值和最大回撤
    peak = max(float(account["peak_value"]), total_value)
    drawdown = (peak - total_value) / peak if peak > 0 else 0

    cursor.execute("""
        UPDATE sim_account
        SET total_value = %s, profit_loss = %s, profit_pct = %s,
            peak_value = %s, max_drawdown = %s, updated_at = %s
        WHERE id = %s
    """, (total_value, profit_loss, profit_pct, peak, drawdown, datetime.now(), account["id"]))

    conn.commit()
    cursor.close()
    conn.close()

    logger.info("📊 账户净值: %.2f 盈亏: %.2f (%.2f%%) 最大回撤: %.2f%%",
                total_value, profit_loss, profit_pct * 100, drawdown * 100)


# ========== 每日扫描（ML驱动）==========
def refresh_positions_prices():
    """刷新所有持仓的现价、市值、盈亏（不触发交易）"""
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, ts_code, market, shares, total_cost 
        FROM sim_positions 
        WHERE status = 'HOLD'
    """)
    rows = cursor.fetchall()
    if not rows:
        cursor.close()
        conn.close()
        return
    cols = [d[0] for d in cursor.description]

    for row in rows:
        pos = dict(zip(cols, row))
        code = pos["ts_code"].split(".")[0]
        market = pos["market"]
        quote = get_stock_realtime(code, market)
        if not quote:
            continue

        price = quote["现价"]
        market_value = round(price * int(pos["shares"]), 2)
        pnl = round(market_value - float(pos["total_cost"]), 2)
        pnl_pct = pnl / float(pos["total_cost"])

        cursor.execute("""
            UPDATE sim_positions
            SET current_price = %s, market_value = %s, 
                profit_loss = %s, profit_pct = %s, updated_at = %s
            WHERE id = %s
        """, (price, market_value, pnl, pnl_pct, datetime.now(), pos["id"]))

    conn.commit()
    cursor.close()
    conn.close()
    logger.info("✅ 已刷新 %d 只持仓的现价与盈亏数据", len(rows))


def daily_scan():
    """
    每日盘后执行：
    1. 检查大盘状态（熊市不建仓）
    2. 检查已有模拟持仓的止盈止损
    3. 用纯ML管线选股买入
    4. 刷新持仓现价（确保数据库数据是最新的）
    5. 更新模拟账户状态
    """
    logger.info("=== 模拟交易每日扫描开始（V11.0纯ML）===")
    logger.info("Top300→V11.0预测→ML百分位过滤 | 分级止盈+6%/+10%/+18% | 持有≤5天")

    # 1. 获取大盘状态 + 市场参数
    market_params = get_market_params()
    logger.info("市场状态: %s 止损%.0f%% 止盈%.0f%% 最大持仓%d",
                market_params['state'],
                market_params['stop_loss_pct'] * -100,
                market_params['take_profit_pct'] * 100,
                market_params['max_positions'])
    mkt_info = get_market_state_for_sim()
    logger.info("大盘状态: %s (涨跌幅: %.2f%%, 阈值: %.2f)",
                mkt_info['state_name'], mkt_info['mkt_chg'], mkt_info['threshold'])

    # 2. 检查现有持仓（半自动：止损自动执行，止盈/超时给出建议）
    positions = get_holding_positions()
    account = get_account()
    if not account:
        logger.error("无法获取账户状态")
        return

    for pos in positions:
        code = pos["ts_code"].split(".")[0]
        market = pos["market"]
        quote = get_stock_realtime(code, market)

        if not quote:
            continue

        price = quote["现价"]
        shares = int(pos["shares"])
        cost_price = float(pos["cost_price"])
        pct_chg = (price - cost_price) / cost_price * 100

        # 计算关键价位（使用动态市场状态参数）
        _mp = get_market_params()
        _sl = _mp['stop_loss_pct']
        stop_price = round(cost_price * (1 + _sl), 2)
        tp1_price = round(cost_price * 1.06, 2)    # +6% 建议卖1/3
        tp2_price = round(cost_price * 1.10, 2)    # +10% 建议再卖1/3
        tp3_price = round(cost_price * 1.18, 2)    # +18% 建议清仓

        # 持有天数（按交易日计算）
        buy_date = pos.get("buy_date")
        days_held = _count_trading_days_since(buy_date) if buy_date else 0

        # 止损（自动执行）
        if price <= stop_price:
            execute_sell(pos["id"], price, reason="模拟止损")
            logger.info("🔴 自动止损: %s 买入%.2f→现价%.2f (%.1f%%)", pos["stock_name"], cost_price, price, pct_chg)
            record_signal("止损", pos["ts_code"], pos["stock_name"], price,
                          shares, "持仓管理", pos.get('ml_prob'),
                          pos.get('enhanced_score'), pos.get('market_state', ''),
                          "触发止损线", "已平仓")
            continue

        # === 自动止盈/超时卖出 ===
        sold_or_pending = False

        # 分级止盈（从高到低判断，触发后不再检查后续）
        if price >= tp3_price:
            execute_sell(pos["id"], price, reason="止盈清仓(+18%)")
            logger.info("🟢 自动止盈清仓: %s 买入%.2f→现价%.2f (%.1f%%)", pos["stock_name"], cost_price, price, pct_chg)
            record_signal("止盈", pos["ts_code"], pos["stock_name"], price,
                          shares, "持仓管理", pos.get('ml_prob'),
                          pos.get('enhanced_score'), pos.get('market_state', ''),
                          "触发止盈清仓(+18%)", "已平仓")
            sold_or_pending = True
        elif price >= tp2_price:
            sell_shares = max(100, shares // 3)
            execute_partial_sell(pos["id"], sell_shares, price, reason="止盈减仓(+10%)")
            logger.info("🟡 自动止盈减仓: %s 买入%.2f→现价%.2f (%.1f%%) 卖出%d股", pos["stock_name"], cost_price, price, pct_chg, sell_shares)
            record_signal("止盈", pos["ts_code"], pos["stock_name"], price,
                          sell_shares, "持仓管理", pos.get('ml_prob'),
                          pos.get('enhanced_score'), pos.get('market_state', ''),
                          "触发止盈减仓(+10%)", "持仓中")
            sold_or_pending = True
        elif price >= tp1_price:
            sell_shares = max(100, shares // 3)
            execute_partial_sell(pos["id"], sell_shares, price, reason="止盈减仓(+6%)")
            logger.info("🟡 自动止盈减仓: %s 买入%.2f→现价%.2f (%.1f%%) 卖出%d股", pos["stock_name"], cost_price, price, pct_chg, sell_shares)
            record_signal("止盈", pos["ts_code"], pos["stock_name"], price,
                          sell_shares, "持仓管理", pos.get('ml_prob'),
                          pos.get('enhanced_score'), pos.get('market_state', ''),
                          "触发止盈减仓(+6%)", "持仓中")
            sold_or_pending = True

        # 超时卖出（持有超过5天且未触发任何止盈）
        if not sold_or_pending and days_held > 5:
            execute_sell(pos["id"], price, reason="超时卖出(>5天)")
            logger.info("⚪ 自动超时卖出: %s 买入%.2f→现价%.2f (%.1f%%) 持有%d天",
                        pos["stock_name"], cost_price, price, pct_chg, days_held)
            record_signal("超时", pos["ts_code"], pos["stock_name"], price,
                          shares, "持仓管理", pos.get('ml_prob'),
                          pos.get('enhanced_score'), pos.get('market_state', ''),
                          "超时卖出(>5天)", "已平仓")
            sold_or_pending = True

        if not sold_or_pending:
            logger.info("  持仓正常: %s 成本%.2f 现价%.2f (%.1f%%) 持有%d天",
                        pos["stock_name"], cost_price, price, pct_chg, days_held)

    # 3. ML策略选股买入（检查大盘状态）
    current_holds = get_holding_positions()
    _mp_buy = get_market_params()
    available_slots = _mp_buy['max_positions'] - len(current_holds)

    # 回撤断路器：总亏损超过阈值时暂停新买入
    account_info = get_account()
    if account_info and float(account_info.get("profit_pct", 0)) < DRAWDOWN_CIRCUIT_BREAKER:
        logger.warning("⚠️ 回撤断路器触发: 总亏损 %.1f%% < %.0f%%，暂停新买入",
                       float(account_info["profit_pct"]) * 100, abs(DRAWDOWN_CIRCUIT_BREAKER) * 100)

    elif available_slots > 0 and not mkt_info['is_bear']:
        # 获取最新交易日
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]
        cursor.close()
        conn.close()

        if latest_date:
            # 纯ML管线选股，取最多 available_slots 只
            candidates = pure_ml_scan(top_n=3)

            # 按可用仓位截断
            if len(candidates) > available_slots:
                candidates = candidates[:available_slots]

            if not candidates:
                logger.info("纯ML扫描无符合条件的股票")
            else:
                logger.info("纯ML选出 %d 只候选: %s",
                            len(candidates), ", ".join([f"{c['name']}(ML={c.get('ml_score',0):.3f},pct={c.get('rank_pct',0):.2f})" for c in candidates]))
                to_buy = candidates

                if POSITION_SIZING_MODE == 'equal':
                    per_position = min(
                        float(account["cash"]) / available_slots,
                        float(account["cash"]) * PER_POSITION_PCT
                    )
                else:
                    per_position = float(account["cash"]) / available_slots

                for pick in to_buy:
                    price = pick["price"]
                    if price <= 0:
                        continue

                    ts_code = pick["ts_code"]
                    name = pick["name"]
                    market_full = pick["market"]

                    # 计算可买股数
                    shares = int(per_position / price / 100) * 100
                    if shares < 100:
                        logger.warning("%s 可买股数不足100股，跳过", name)
                        continue

                    # 检查是否已持有
                    already_held = any(p["ts_code"] == ts_code for p in current_holds)
                    if already_held:
                        logger.info("%s 已持有，跳过", name)
                        continue

                    # 检查涨停（开盘涨停买不进，跳过）
                    try:
                        conn_buy = get_db_conn()
                        cur_buy = conn_buy.cursor()
                        cur_buy.execute("""
                            SELECT pct_chg FROM daily_price
                            WHERE ts_code = %s AND trade_date = (
                                SELECT MAX(trade_date) FROM daily_price
                            )
                        """, (ts_code,))
                        row_zt = cur_buy.fetchone()
                        cur_buy.close()
                        conn_buy.close()
                        if row_zt and float(row_zt[0] or 0) >= 9.5:
                            logger.info("%s(%s) 今日涨幅%.1f%%已近涨停，买不进，跳过",
                                        name, ts_code, float(row_zt[0]))
                            continue
                    except Exception as e:
                        logger.warning("涨停检查失败: %s", e)

                    success = execute_buy(
                        ts_code, name, market_full,
                        price, shares,
                        reason="纯ML: V11.0排序(%.3f)" % pick.get("ml_score", 0),
                        strategy="纯ML(V11.0)",
                        ml_prob=pick.get("ml_prob", 0),
                        enhanced_score=pick.get("ml_score", 0),
                        market_state=_mp_buy.get('state_name', '常态')
                    )
                    if not success:
                        logger.warning("%s 买入失败，跳过", name)
        else:
            logger.info("无最新交易日数据，跳过买入")
    elif available_slots > 0 and mkt_info['is_bear']:
        logger.info("🐻 大盘为逆市状态，暂停建仓")
    else:
        logger.info("已满仓(%d/%d)，无需买入", len(current_holds), _mp_buy['max_positions'])

    # 4. 刷新持仓现价（确保数据库数据是最新的）
    refresh_positions_prices()

    # 5. 更新账户净值
    update_account_value()

    # 6. 同步持仓到 JSON（供 position_monitor / feishu_alerts 读取）
    sync_positions_to_json()

    # 7. 记录净值快照
    _record_nav_snapshot()

    logger.info("=== 模拟交易每日扫描完成 ===")


# ========== JSON 同步（供 position_monitor / feishu_alerts 使用）==========
def sync_positions_to_json():
    """将 MySQL 持仓同步到 data/positions.json（单向：MySQL → JSON）"""
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, ts_code, stock_name, market, shares, cost_price, total_cost,
               current_price, market_value, profit_loss, profit_pct,
               stop_loss, take_profit, buy_date, status, updated_at
        FROM sim_positions
        WHERE status = 'HOLD'
    """)
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    cursor.close()
    conn.close()

    positions = []
    for row in rows:
        p = dict(zip(cols, row))
        code = str(p["ts_code"]).split(".")[0] if "." in str(p["ts_code"]) else str(p["ts_code"])
        profit_pct_val = float(p["profit_pct"]) * 100 if p["profit_pct"] else 0
        # 防御：DB 中 stop_loss/take_profit 可能为 0（QMT 实盘同步路径未带止损价）。
        # 此时用市场状态参数或默认 -3% 兜底，避免下游 position_monitor/feishu_alerts
        # 因 stop_loss <= 0 永远不触发。
        cost = float(p["cost_price"])
        raw_sl = float(p["stop_loss"])
        raw_tp = float(p["take_profit"])
        if raw_sl <= 0:
            try:
                fallback_sl_pct = get_market_params().get("stop_loss_pct", -0.03)
            except Exception:
                fallback_sl_pct = -0.03
            raw_sl = round(cost * (1 + fallback_sl_pct), 3)
        if raw_tp <= 0:
            try:
                fallback_tp_pct = get_market_params().get("take_profit_pct", 0.06)
            except Exception:
                fallback_tp_pct = 0.06
            raw_tp = round(cost * (1 + fallback_tp_pct), 3)

        positions.append({
            "position_id": int(p["id"]),
            "code": code,
            "market": p["market"],
            "name": p["stock_name"],
            "cost": cost,
            "shares": int(p["shares"]),
            "stop_loss": raw_sl,
            "take_profit": raw_tp,
            "buy_date": str(p["buy_date"]) if p["buy_date"] else "",
            "current_price": float(p["current_price"]) if p["current_price"] else cost,
            "float_pnl": float(p["profit_loss"]) if p["profit_loss"] else 0,
            "float_pnl_pct": round(profit_pct_val, 2),
        })

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(base_dir, "data", "positions.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({"positions": positions}, f, ensure_ascii=False, indent=2)
    logger.info("✅ 持仓已同步到 positions.json（%d 只）", len(positions))


# ========== 净值历史 ==========
def _record_nav_snapshot():
    """记录每日净值快照到 data/nav_history.json"""
    account = get_account()
    if not account:
        return
    today = str(datetime.now().date())

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    nav_path = os.path.join(base_dir, "data", "nav_history.json")

    history = []
    if os.path.exists(nav_path):
        try:
            with open(nav_path) as f:
                history = json.load(f)
        except Exception:
            history = []

    # 去重：同一天不重复记录
    if history and history[-1].get("date") == today:
        logger.debug("今日净值已记录，跳过")
        return

    positions = get_holding_positions()
    holdings_value = sum(
        (float(p.get("current_price") or 0)) * int(p.get("shares") or 0)
        for p in positions
    ) if positions else 0

    snapshot = {
        "date": today,
        "total_value": float(account["total_value"]),
        "cash": float(account["cash"]),
        "holdings_value": round(holdings_value, 2),
        "profit_pct": round(float(account["profit_pct"]) * 100, 2) if account.get("profit_pct") else 0,
        "max_drawdown": round(float(account["max_drawdown"]) * 100, 2) if account.get("max_drawdown") else 0,
        "trade_count": account.get("trade_count", 0),
    }
    history.append(snapshot)

    with open(nav_path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    logger.info("📊 净值快照已记录: %.2f (%.2f%%)", snapshot["total_value"], snapshot["profit_pct"])


# ========== API 响应 ==========
def get_sim_account_info():
    """获取模拟账户完整信息（供 API 调用）"""
    account = get_account()
    positions = get_holding_positions()

    if not account:
        return {"error": "无账户数据"}

    # 获取最近 10 笔交易
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ts_code, stock_name, market, action, price, shares, amount,
               trade_date, profit_loss, profit_pct, reason
        FROM sim_trades
        ORDER BY trade_time DESC
        LIMIT 10
    """)
    trades = []
    for row in cursor.fetchall():
        trades.append({
            "ts_code": row[0], "name": row[1], "market": row[2],
            "action": row[3], "price": float(row[4]), "shares": row[5],
            "amount": float(row[6]), "trade_date": str(row[7]),
            "profit_loss": float(row[8]) if row[8] else None,
            "profit_pct": float(row[9]) * 100 if row[9] else None,
            "reason": row[10],
        })

    # 获取信号记录（新增）
    cursor.execute("""
        SELECT id, signal_type, ts_code, stock_name, price, shares, strategy, ml_prob,
               market_state, reason, signal_date, status, close_price, pnl, pnl_pct
        FROM sim_signals
        ORDER BY id DESC
        LIMIT 20
    """)
    signals = []
    for row in cursor.fetchall():
        signals.append({
            "id": row[0], "type": row[1], "ts_code": row[2], "name": row[3],
            "price": float(row[4]) if row[4] else 0, "shares": row[5],
            "strategy": row[6] or "", "ml_prob": float(row[7]) if row[7] else 0,
            "market_state": row[8] or "", "reason": row[9] or "",
            "signal_date": str(row[10]) if row[10] else "",
            "status": row[11] or "",
            "close_price": float(row[12]) if row[12] else 0,
            "pnl": float(row[13]) if row[13] else 0,
            "pnl_pct": float(row[14]) * 100 if row[14] else 0,
        })
    cursor.close()
    conn.close()

    # 获取持仓（转换 Decimal，profit_pct 转百分比）
    for p in positions:
        for k in ['cost_price', 'total_cost', 'current_price', 'market_value', 'profit_loss', 'stop_loss', 'take_profit']:
            if k in p and p[k] is not None:
                p[k] = float(p[k])
        if 'profit_pct' in p and p['profit_pct'] is not None:
            p['profit_pct'] = round(float(p['profit_pct']) * 100, 2)
        if 'shares' in p:
            p['shares'] = int(p['shares'])
        # 添加ML信息
        if 'ml_prob' in p:
            p['ml_prob'] = float(p['ml_prob']) if p['ml_prob'] else 0

    return {
        "account": {
            "initial_capital": float(account["initial_capital"]),
            "cash": float(account["cash"]),
            "total_value": float(account["total_value"]),
            "profit_loss": float(account["profit_loss"]),
            "profit_pct": round(float(account["profit_pct"]) * 100, 2),
            "max_drawdown": round(float(account["max_drawdown"]) * 100, 2),
            "trade_count": account["trade_count"],
            "win_count": account["win_count"],
            "win_rate": round(float(account["win_rate"]) * 100, 2),
            "updated_at": str(account["updated_at"]),
        },
        "positions": positions,
        "recent_trades": trades,
        "signals": signals,
    }


# ========== 入口 ==========
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="模拟交易系统")
    parser.add_argument("action", choices=["init", "scan", "v4_scan", "status"],
                        help="init=建表初始化, scan=每日扫描, v4_scan=V4候选扫描, status=账户状态")
    args = parser.parse_args()

    if args.action == "init":
        create_tables()
    elif args.action == "scan":
        create_tables()  # 确保表存在
        daily_scan()
    elif args.action == "v4_scan":
        candidates = v4_scan(top_n=5)
        for c in candidates:
            logger.info("V4候选: %s(%s) 主力评分=%.0f ML=%.2f",
                        c["name"], c["ts_code"], c["mainforce_score"], c.get("ml_prob", 0))
        print(json.dumps(candidates, ensure_ascii=False, indent=2, default=str))
    elif args.action == "status":
        info = get_sim_account_info()
        print(json.dumps(info, ensure_ascii=False, indent=2))
