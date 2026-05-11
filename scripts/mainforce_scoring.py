#!/usr/bin/env python3
"""
主力资金意图评分模块
计算主力意图评分（满分100分），识别主力行为阶段
"""
import os, sys, logging
from datetime import datetime, timedelta
import pymysql

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

def get_db_conn():
    return pymysql.connect(**DB_CONFIG)

def get_stock_name(conn, ts_code):
    """获取股票名称"""
    cur = conn.cursor()
    cur.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else ts_code

def calculate_mainforce_score(ts_code, trade_date=None, conn=None):
    """
    计算主力资金意图评分（满分100分）

    评分维度：
    - 资金流向 30分
    - 股东集中度 20分
    - 大宗交易 15分
    - 量价配合 15分
    - 融资融券 10分
    - 龙虎榜 10分

    返回 dict:
    {
        'ts_code': '001324.SZ',
        'name': '长青科技',
        'score': 78,
        'level': '即将拉升',  # 即将拉升/拉升在即/吸筹阶段/观望/主力在跑
        'signals': [...],
        'action': '主力吸筹完毕，准备启动',
    }
    """
    own_conn = conn is None
    if own_conn:
        conn = get_db_conn()
    signals = []
    total_score = 0
    
    try:
        # 如果没有指定日期，用最新交易日
        if trade_date is None:
            cur = conn.cursor()
            cur.execute("SELECT MAX(trade_date) FROM daily_price")
            row = cur.fetchone()
            cur.close()
            trade_date = row[0] if row else None
            if not trade_date:
                return {'ts_code': ts_code, 'name': get_stock_name(conn, ts_code), 'score': 0, 'level': '无数据', 'signals': [], 'action': '无交易数据'}
        
        name = get_stock_name(conn, ts_code)
        
        # ===== 1. 资金流向评分（30分）=====
        mf_score, mf_signal = _score_moneyflow(conn, ts_code, trade_date)
        total_score += mf_score
        if mf_signal:
            signals.append(mf_signal)
        
        # ===== 2. 股东集中度评分（20分）=====
        holder_score, holder_signal = _score_holder_concentration(conn, ts_code)
        total_score += holder_score
        if holder_signal:
            signals.append(holder_signal)
        
        # ===== 3. 大宗交易评分（15分）=====
        block_score, block_signal = _score_block_trade(conn, ts_code)
        total_score += block_score
        if block_signal:
            signals.append(block_signal)
        
        # ===== 4. 量价配合评分（15分）=====
        vp_score, vp_signal = _score_volume_price(conn, ts_code, trade_date)
        total_score += vp_score
        if vp_signal:
            signals.append(vp_signal)
        
        # ===== 5. 融资融券评分（10分）=====
        margin_score, margin_signal = _score_margin(conn, ts_code)
        total_score += margin_score
        if margin_signal:
            signals.append(margin_signal)
        
        # ===== 6. 龙虎榜评分（10分）=====
        dt_score, dt_signal = _score_dragon_tiger(conn, ts_code, trade_date)
        total_score += dt_score
        if dt_signal:
            signals.append(dt_signal)
        
        total_score = min(100, max(0, total_score))
        
        # 判断主力阶段
        level, action = _determine_level(total_score, signals)
        
        return {
            'ts_code': ts_code,
            'name': name,
            'score': total_score,
            'level': level,
            'signals': signals,
            'action': action,
        }
    except Exception as e:
        logger.warning(f"主力评分计算失败 {ts_code}: {e}")
        return {'ts_code': ts_code, 'name': ts_code, 'score': 0, 'level': '计算失败', 'signals': [], 'action': str(e)}
    finally:
        if own_conn:
            conn.close()


def _score_moneyflow(conn, ts_code, trade_date):
    """
    资金流向评分（30分）
    近5日主力净流入占比 + 股价不涨 = 高分
    """
    cur = conn.cursor()
    
    # 近5日主力净流入
    cur.execute("""
        SELECT trade_date, main_net, net_mf_amount
        FROM moneyflow_daily
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 5
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    cur.close()
    
    if not rows or len(rows) < 3:
        return 5, {'type': '资金流向', 'desc': '数据不足', 'score': 5}
    
    # 计算5日主力净流入总额
    total_main_net = sum(float(r[1]) for r in rows)
    # 获取同期股价变化
    cur = conn.cursor()
    dates = [str(r[0]) for r in rows]
    if len(dates) >= 2:
        date_strs = [d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d) for d in dates]
        start_d = min(date_strs)
        end_d = max(date_strs)
        cur.execute("""
            SELECT trade_date, close FROM daily_price
            WHERE ts_code = %s AND trade_date >= %s AND trade_date <= %s
            ORDER BY trade_date ASC
        """, (ts_code, start_d, end_d))
        price_rows = cur.fetchall()
        cur.close()
        
        price_chg_pct = 0
        if len(price_rows) >= 2:
            first_close = float(price_rows[0][1]) if price_rows[0][1] else 0
            last_close = float(price_rows[-1][1]) if price_rows[-1][1] else 0
            if first_close > 0:
                price_chg_pct = (last_close - first_close) / first_close * 100
        
        # 评分逻辑：持续净入 + 股价不涨 = 主力在吸筹 = 高分
        if total_main_net > 5000 and price_chg_pct < 3:
            score = 30
            desc = f"5日主力净流入{total_main_net:.0f}万，股价仅{'涨' if price_chg_pct > 0 else '跌'}{abs(price_chg_pct):.1f}%"
        elif total_main_net > 2000 and price_chg_pct < 5:
            score = 25
            desc = f"5日主力净流入{total_main_net:.0f}万，股价{'涨' if price_chg_pct > 0 else '跌'}{abs(price_chg_pct):.1f}%"
        elif total_main_net > 500:
            score = 15
            desc = f"5日主力净流入{total_main_net:.0f}万"
        elif total_main_net > 0:
            score = 8
            desc = f"5日主力小幅净流入{total_main_net:.0f}万"
        else:
            score = 3
            desc = f"5日主力净流出{abs(total_main_net):.0f}万"
        
        return score, {'type': '资金流向', 'desc': desc, 'score': score}
    
    cur.close()
    return 5, {'type': '资金流向', 'desc': '数据不足', 'score': 5}


def _score_holder_concentration(conn, ts_code):
    """
    股东集中度评分（20分）
    股东人数持续减少 = 筹码集中 = 高分
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT end_date, holder_num, holder_num_change, holder_change_pct
        FROM holder_change
        WHERE ts_code = %s
        ORDER BY end_date DESC LIMIT 4
    """, (ts_code,))
    rows = cur.fetchall()
    cur.close()
    
    if not rows or len(rows) < 2:
        return 10, {'type': '股东集中度', 'desc': '数据不足', 'score': 10}
    
    # 检查最近4期股东人数变化
    decreases = 0
    total_change_pct = 0
    for r in rows:
        change_pct = float(r[3]) if r[3] else 0
        if r[2] and int(r[2]) < 0:  # holder_num_change < 0 = 减少
            decreases += 1
        total_change_pct += change_pct
    
    if decreases >= 3 and total_change_pct < -5:
        score = 20
        desc = f"股东人数连续{decreases}期减少，累计降幅{abs(total_change_pct):.1f}%（筹码高度集中）"
    elif decreases >= 2:
        score = 15
        desc = f"股东人数{decreases}期减少，筹码趋于集中"
    elif decreases >= 1:
        score = 10
        desc = "股东人数偶有减少"
    else:
        score = 5
        desc = "股东人数增加，筹码分散"
    
    return score, {'type': '股东集中度', 'desc': desc, 'score': score}


def _score_block_trade(conn, ts_code):
    """
    大宗交易评分（15分）
    近60天机构买入 = 高分
    注：Tushare无专门大宗交易API，用龙虎榜替代
    """
    cur = conn.cursor()
    td_limit = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
    # 龙虎榜总数据（net_amount → net_buy）
    cur.execute("""
        SELECT trade_date, net_buy, close
        FROM dragon_tiger
        WHERE ts_code = %s AND trade_date >= %s
        ORDER BY trade_date DESC
    """, (ts_code, td_limit))
    rows = cur.fetchall()

    # 机构席位明细（exalter 含"机构专用"/"深股通专用"等）
    cur.execute("""
        SELECT trade_date, exalter, net_buy
        FROM dragon_tiger_inst
        WHERE ts_code = %s AND trade_date >= %s
          AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
        ORDER BY trade_date DESC
    """, (ts_code, td_limit))
    inst_rows = cur.fetchall()
    cur.close()

    if not rows:
        return 7, {'type': '机构动向', 'desc': '近60天无龙虎榜记录', 'score': 7}

    # 统计机构净买入（从 dragon_tiger_inst）
    inst_net_buy = 0
    inst_count = 0
    for r in inst_rows:
        nb = float(r[2]) if r[2] else 0
        inst_net_buy += nb
        inst_count += 1

    # 统计龙虎榜总净买入（从 dragon_tiger）
    total_net_buy = sum(float(r[1]) if r[1] else 0 for r in rows)

    if inst_net_buy > 5000:
        score = 15
        desc = f"近60天机构龙虎榜净买入{inst_net_buy:.0f}万（{inst_count}次）"
    elif inst_net_buy > 1000:
        score = 12
        desc = f"近60天机构龙虎榜净买入{inst_net_buy:.0f}万"
    elif total_net_buy > 5000:
        score = 8
        desc = f"近60天龙虎榜活跃，净买入{total_net_buy:.0f}万"
    else:
        score = 5
        desc = f"近60天龙虎榜净买入{total_net_buy:.0f}万"

    return score, {'type': '机构动向', 'desc': desc, 'score': score}


def _score_volume_price(conn, ts_code, trade_date):
    """
    量价配合评分（15分）
    放量涨 + 缩量跌 = 洗盘特征 = 高分
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, pct_chg, volume_ratio
        FROM daily_price
        WHERE ts_code = %s AND trade_date <= %s
        ORDER BY trade_date DESC LIMIT 10
    """, (ts_code, trade_date))
    rows = cur.fetchall()
    cur.close()
    
    if not rows or len(rows) < 5:
        return 7, {'type': '量价配合', 'desc': '数据不足', 'score': 7}
    
    # 分析量价关系
    up_days = 0
    up_high_vol = 0
    down_days = 0
    down_low_vol = 0
    
    for r in rows:
        pct = float(r[1]) if r[1] else 0
        vr = float(r[2]) if r[2] else 1.0
        if pct > 0:
            up_days += 1
            if vr > 1.2:
                up_high_vol += 1
        elif pct < 0:
            down_days += 1
            if vr < 1.0:
                down_low_vol += 1
    
    # 放量涨 + 缩量跌 = 理想洗盘
    if up_days > 0 and down_days > 0:
        up_vol_ratio = up_high_vol / up_days
        down_vol_ratio = down_low_vol / down_days
        
        if up_vol_ratio > 0.6 and down_vol_ratio > 0.6:
            score = 15
            desc = f"量价完美配合：{up_days}涨中{up_high_vol}次放量，{down_days}跌中{down_low_vol}次缩量"
        elif up_vol_ratio > 0.4 and down_vol_ratio > 0.4:
            score = 12
            desc = f"量价配合良好：{up_days}涨中{up_high_vol}次放量，{down_days}跌中{down_low_vol}次缩量"
        elif up_vol_ratio > 0.3:
            score = 8
            desc = f"涨时多放量：{up_days}涨中{up_high_vol}次放量"
        else:
            score = 5
            desc = "量价关系一般"
    else:
        score = 7
        desc = "量价数据不足"
    
    return score, {'type': '量价配合', 'desc': desc, 'score': score}


def _score_margin(conn, ts_code):
    """
    融资融券评分（10分）
    融资下降 + 股价稳定 = 主力在收集筹码
    """
    cur = conn.cursor()
    # 用 moneyflow 替代（融资融券API需高权限）
    cur.execute("""
        SELECT trade_date, net_mf_amount
        FROM moneyflow_daily
        WHERE ts_code = %s
        ORDER BY trade_date DESC LIMIT 20
    """, (ts_code,))
    rows = cur.fetchall()
    cur.close()
    
    if not rows or len(rows) < 10:
        return 5, {'type': '融资融券', 'desc': '数据不足', 'score': 5}
    
    # 近20日净流入趋势
    recent_10 = [float(r[1]) for r in rows[:10]]
    prev_10 = [float(r[1]) for r in rows[10:]]
    
    avg_recent = sum(recent_10) / len(recent_10)
    avg_prev = sum(prev_10) / len(prev_10)
    
    if avg_recent > 1000 and avg_recent > avg_prev:
        score = 10
        desc = f"近10日平均净流入{avg_recent:.0f}万，较前期增加"
    elif avg_recent > 0:
        score = 7
        desc = f"近10日平均净流入{avg_recent:.0f}万"
    else:
        score = 3
        desc = f"近10日平均净流出{abs(avg_recent):.0f}万"
    
    return score, {'type': '融资融券', 'desc': desc, 'score': score}


def _score_dragon_tiger(conn, ts_code, trade_date):
    """
    龙虎榜评分（10分）
    近30天上榜 + 机构净买入 = 高分
    """
    cur = conn.cursor()
    td_limit = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    # 上榜次数 + 总净买入
    cur.execute("""
        SELECT trade_date, net_buy
        FROM dragon_tiger
        WHERE ts_code = %s AND trade_date >= %s
    """, (ts_code, td_limit))
    rows = cur.fetchall()

    # 机构席位净买入
    cur.execute("""
        SELECT SUM(net_buy)
        FROM dragon_tiger_inst
        WHERE ts_code = %s AND trade_date >= %s
          AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
    """, (ts_code, td_limit))
    inst_result = cur.fetchone()
    cur.close()

    inst_buy = float(inst_result[0]) if inst_result and inst_result[0] else 0

    if not rows:
        return 5, {'type': '龙虎榜', 'desc': '近30天无上榜记录', 'score': 5}

    total_count = len(rows)

    if inst_buy > 3000:
        score = 10
        desc = f"近30天上榜{total_count}次，机构净买入{inst_buy:.0f}万"
    elif inst_buy > 500:
        score = 8
        desc = f"近30天上榜{total_count}次，机构净买入{inst_buy:.0f}万"
    elif total_count >= 3:
        score = 6
        desc = f"近30天上榜{total_count}次，较活跃"
    else:
        score = 3
        desc = f"近30天上榜{total_count}次"

    return score, {'type': '龙虎榜', 'desc': desc, 'score': score}


def _determine_level(score, signals):
    """根据总分和信号判断主力阶段"""
    # 提取关键信号
    has_strong_inflow = any(s['type'] == '资金流向' and s['score'] >= 25 for s in signals)
    has_holder_concentrate = any(s['type'] == '股东集中度' and s['score'] >= 15 for s in signals)
    has_inst_buy = any(s['type'] == '机构动向' and s['score'] >= 12 for s in signals)
    has_wash_pattern = any(s['type'] == '量价配合' and s['score'] >= 12 for s in signals)
    
    if score >= 75 and has_strong_inflow and has_wash_pattern:
        return '即将拉升', '主力吸筹完毕，准备启动'
    elif score >= 60 and has_strong_inflow:
        return '拉升在即', '主力资金积极建仓，可能近期启动'
    elif score >= 45 and has_holder_concentrate:
        return '吸筹阶段', '筹码逐步集中，主力在缓慢吸筹'
    elif score >= 30:
        return '观望', '主力态度暧昧，建议继续观察'
    else:
        return '主力在跑', '资金持续流出，注意风险'


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        code = sys.argv[1]
        if '.' not in code:
            if code.startswith('0') or code.startswith('3'):
                code += '.SZ'
            else:
                code += '.SH'
        result = calculate_mainforce_score(code)
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("用法: python3 mainforce_scoring.py <ts_code>")
        print("示例: python3 mainforce_scoring.py 001324.SZ")
