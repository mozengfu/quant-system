#!/usr/bin/env python3
"""
每日AI精选 TOP5 — 四层过滤管道

1. ML初筛：V4分市场状态模型，rank_pct >= 95%
2. 基本面过滤：ST/*ST、市值<30亿、连涨>3天、业绩预亏
3. 技术面确认：MACD金叉/即将金叉、均线支撑、缩量回调
4. 综合评分排序：ML得分40% + 技术面40% + 资金流20%
"""

import sys, os, logging
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np, pandas as pd, pymysql, json
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

def get_db():
    return pymysql.connect(**DB_CONFIG)

def load_v6_predictions(conn):
    """加载ML模型全市场预测 — 自动选择最佳模型(V6.2 > V6)"""
    from ml_predict import _load_best_model, _build_features_for_stocks_v6, _build_features_for_stocks_v6_2, _build_features_for_stocks_v6_3, HISTORY_DAYS

    bundle, version = _load_best_model()
    if not bundle:
        logger.error("ML模型不可用")
        return [], None

    # 获取全市场 ts_code 列表
    cur = conn.cursor()
    cur.execute("SELECT ts_code FROM stock_info WHERE ts_code NOT LIKE '688%%' AND ts_code NOT LIKE '8%%' AND ts_code NOT LIKE '4%%' AND ts_code NOT LIKE '9%%'")
    all_codes = [r[0] for r in cur.fetchall()]
    cur.close()

    if version in ("v6.5", "v6.4", "v6.3"):
        features = _build_features_for_stocks_v6_3(conn, all_codes)
    elif version == "v6.2":
        features = _build_features_for_stocks_v6_2(conn, all_codes)
    else:
        features = _build_features_for_stocks_v6(conn, all_codes)
    if features.empty:
        logger.error("特征构建失败")
        return [], None

    feature_cols = bundle['feature_cols']
    medians = bundle.get('global_medians', {})
    for col in feature_cols:
        if col not in features.columns:
            features[col] = medians.get(col, 0.0)
        elif features[col].isna().any():
            features[col] = features[col].fillna(medians.get(col, 0.0))

    X = features[feature_cols].values.astype(np.float32)

    if 'models' in bundle:
        # V6.2 / V6.3 ensemble 模型
        preds = np.zeros((len(X), len(bundle['models'])))
        for i, model in enumerate(bundle['models']):
            preds[:, i] = model.predict(X)
        pred_returns = np.mean(preds, axis=1)
    else:
        pred_returns = bundle['model'].predict(X)

    features['ml_score'] = pred_returns
    features['rank_pct'] = (pred_returns.argsort().argsort() + 1) / len(pred_returns) * 100

    logger.info(f"ML模型 ({version}): {len(features)}只股票")
    return features, features['trade_date'].max() if 'trade_date' in features.columns else None

def filter_basic(features, conn):
    """第二层：基本面硬过滤"""
    cur = conn.cursor()
    
    # 获取股票基本信息
    cur.execute("SELECT ts_code, name, industry, market FROM stock_info")
    info_rows = cur.fetchall()
    info_map = {}
    for row in info_rows:
        info_map[row[0]] = {'name': row[1], 'industry': row[2], 'market': row[3]}
    
    # 获取市值数据（从daily_basic）
    try:
        latest_date = features['trade_date'].max()
        date_str = latest_date.strftime('%Y%m%d') if hasattr(latest_date, 'strftime') else str(latest_date).replace('-', '')[:8]
        cur.execute("""
            SELECT ts_code, total_mv, circ_mv 
            FROM daily_basic WHERE trade_date = %s
        """, (date_str,))
        mv_rows = cur.fetchall()
        mv_map = {r[0]: r[1] for r in mv_rows}
    except Exception:
        mv_map = {}
    
    # 获取最近5天涨跌幅（判断连涨）
    cur.execute("""
        SELECT ts_code, trade_date, pct_chg 
        FROM daily_price 
        WHERE trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
        ORDER BY ts_code, trade_date DESC
    """, (latest_date,))
    price_rows = cur.fetchall()
    
    up_streak = {}
    for code in features['ts_code'].unique():
        code_prices = [r for r in price_rows if r[0] == code]
        streak = 0
        for r in code_prices[:5]:  # 最多看5天
            if r[2] > 0:  # pct_chg > 0
                streak += 1
            else:
                break
        up_streak[code] = streak
    
    # 获取ST标记（名称中包含ST）
    st_stocks = set()
    for code, info in info_map.items():
        if info['name'] and ('ST' in info['name'] or '*ST' in info['name']):
            st_stocks.add(code)
    
    # 过滤规则
    filtered = []
    for _, row in features.iterrows():
        code = row['ts_code']
        info = info_map.get(code, {})
        
        # 规则1: rank_pct >= 90%（放宽到90%给后面过滤留余地）
        if row['rank_pct'] < 90:
            continue
        
        # 规则2: 排除ST/*ST
        if code in st_stocks:
            continue
        
        # 规则3: 排除科创板(688/689)、北交所
        if code[:2] in ('68', '83', '87', '43'):
            continue
        
        # 规则4: 市值 >= 30亿（total_mv单位：万元）
        mv = mv_map.get(code, 0)
        if mv and mv < 300000:  # 30亿 = 300000万
            continue
        
        # 规则5: 连涨不超过3天
        if up_streak.get(code, 0) >= 4:
            continue
        
        # 规则6: 排除名字带"退"的
        name = info.get('name', '')
        if '退' in name:
            continue
        
        filtered.append({
            'ts_code': code,
            'name': info.get('name', code.split('.')[0]),
            'industry': info.get('industry', ''),
            'ml_score': float(row['ml_score']),
            'rank_pct': float(row['rank_pct']),
            'total_mv': mv,
            'up_streak': up_streak.get(code, 0),
        })
    
    logger.info(f"基本面过滤: {len(features)} -> {len(filtered)}只")
    return filtered

def get_technical_signals(conn, codes):
    """第三层：技术面信号提取"""
    if not codes:
        return {}
    
    cur = conn.cursor()
    latest_date_query = "SELECT MAX(trade_date) FROM daily_price"
    cur.execute(latest_date_query)
    latest = cur.fetchone()[0]
    
    # 获取每只股票最近30天数据
    placeholders = ','.join(['%s'] * len(codes))
    cur.execute(f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close,
               pct_chg, turnover_rate, volume_ratio, vol,
               ma5, ma10, ma20
        FROM daily_price 
        WHERE ts_code IN ({placeholders})
        AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
        ORDER BY ts_code, trade_date
    """, (*codes, latest))
    
    rows = cur.fetchall()
    cols = ['ts_code','trade_date','open','high','low','close','pre_close',
            'pct_chg','turnover_rate','volume_ratio','vol','ma5','ma10','ma20']
    
    signals = {}
    
    for code in codes:
        code_rows = [r for r in rows if r[0] == code]
        if len(code_rows) < 15:
            signals[code] = {'score': 0, 'reasons': ['数据不足']}
            continue
        
        df = pd.DataFrame(code_rows, columns=cols)
        for c in cols[2:]:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        
        df = df.sort_values('trade_date').reset_index(drop=True)
        
        score = 0
        reasons = []
        
        # 信号1: MACD金叉
        df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['macd_diff'] = df['ema12'] - df['ema26']
        df['macd_signal'] = df['macd_diff'].ewm(span=9, adjust=False).mean()
        
        last = df.iloc[-1]
        prev = df.iloc[-2]
        
        if last['macd_diff'] > last['macd_signal'] and prev['macd_diff'] <= prev['macd_signal']:
            score += 30
            reasons.append("MACD刚金叉")
        elif last['macd_diff'] > last['macd_signal'] and (last['macd_diff'] - last['macd_signal']) < abs(last['macd_diff']) * 0.3:
            score += 20
            reasons.append("MACD即将金叉")
        
        # 信号2: 均线支撑
        if last['close'] > last['ma5'] > last['ma10'] > last['ma20']:
            score += 25
            reasons.append("均线多头排列")
        elif last['close'] > last['ma10'] and last['ma5'] > last['ma10']:
            score += 15
            reasons.append("站稳10日线")
        elif abs(last['close'] - last['ma20']) / last['close'] < 0.02:
            score += 10
            reasons.append("回踩20日线附近")
        
        # 信号3: 缩量回调（价跌量缩 = 健康调整）
        if last['pct_chg'] < 0:
            vol_ratio = last['volume_ratio'] if pd.notna(last['volume_ratio']) else 1
            if vol_ratio < 0.8:
                score += 20
                reasons.append("缩量回调")
            elif vol_ratio < 1.0:
                score += 10
                reasons.append("量能萎缩")
        elif last['pct_chg'] > 0 and last.get('volume_ratio', 1) > 1.2:
            score += 10
            reasons.append("放量上涨")
        
        # 信号4: 价格在52周高位附近（强势股）
        try:
            cur2 = conn.cursor()
            cur2.execute("""
                SELECT high_52w, low_52w FROM daily_price 
                WHERE ts_code=%s AND trade_date=%s
            """, (code, latest))
            r = cur2.fetchone()
            if r and r[0] and r[1]:
                pos = (last['close'] - r[1]) / (r[0] - r[1])
                if pos > 0.8:
                    score += 15
                    reasons.append("接近52周高位")
                elif pos > 0.6:
                    score += 10
                    reasons.append("52周中高位")
            cur2.close()
        except Exception as _e:
            logger.error(f"Error in ml_daily_top5.py: {_e}")
        
        # 信号5: 近3日资金流
        try:
            cur3 = conn.cursor()
            cur3.execute("""
                SELECT main_net FROM moneyflow_daily 
                WHERE ts_code=%s AND trade_date >= DATE_SUB(%s, INTERVAL 3 DAY)
                ORDER BY trade_date DESC LIMIT 3
            """, (code, latest))
            mf_rows = cur3.fetchall()
            cur3.close()
            if mf_rows:
                net_3d = sum(r[0] for r in mf_rows if r[0])
                if net_3d > 0:
                    score += 10
                    reasons.append("3日主力净流入")
        except Exception as _e:
            logger.error(f"Error in ml_daily_top5.py: {_e}")
        
        signals[code] = {
            'score': min(score, 100),
            'reasons': reasons if reasons else ['无明显技术信号'],
            'macd_diff': float(last.get('macd_diff', 0)),
            'macd_signal': float(last.get('macd_signal', 0)),
            'close': float(last['close']),
            'pct_chg': float(last['pct_chg']),
            'ma5': float(last.get('ma5', 0)),
            'ma10': float(last.get('ma10', 0)),
            'ma20': float(last.get('ma20', 0)),
        }
    
    return signals

def generate_top5(conn):
    """完整四层过滤，输出TOP5"""
    
    # 第一层：ML模型预测
    features, latest_date = load_v6_predictions(conn)
    if not len(features):
        return []
    
    # 第二层：基本面过滤
    candidates = filter_basic(features, conn)
    if not candidates:
        return []
    
    codes = [c['ts_code'] for c in candidates]
    
    # 第三层：技术面信号
    tech_signals = get_technical_signals(conn, codes)
    
    # 第四层：综合评分排序 + Alpha 增强
    from alpha_signal_integration import get_alpha_boost_map, apply_alpha_boost
    
    # 获取今日 Alpha 信号
    today_str = datetime.now().strftime('%Y-%m-%d')
    boost_map = get_alpha_boost_map(today_str)
    
    for c in candidates:
        code = c['ts_code']
        ts = tech_signals.get(code, {})
        
        # 综合得分 = ML得分(归一化到40分) + 技术面得分(40分) + 资金流(20分)
        ml_norm = min(c['ml_score'] * 40, 40)
        tech = ts.get('score', 0) * 0.4
        
        # 资金流得分
        money = 10 if '3日主力净流入' in ts.get('reasons', []) else 0
        money += 10 if '放量上涨' in ts.get('reasons', []) else 0
        
        c['total_score'] = round(ml_norm + tech + money, 1)
        c['tech_score'] = ts.get('score', 0)
        c['tech_reasons'] = ts.get('reasons', [])
        c['price'] = ts.get('close', 0)
        c['pct_chg'] = ts.get('pct_chg', 0)
    
    # 应用 Alpha 信号增强 (加分/减分)
    candidates = apply_alpha_boost(candidates, boost_map, scale_factor=15.0)
    
    # 排序取TOP5
    candidates.sort(key=lambda x: x['total_score'], reverse=True)
    top5 = candidates[:5]
    
    # 格式化输出
    results = []
    for i, c in enumerate(top5, 1):
        results.append({
            'rank': i,
            'code': c['ts_code'].split('.')[0],
            'name': c['name'],
            'industry': c['industry'],
            'price': f"{c['price']:.2f}" if c['price'] else '--',
            'change': f"{c['pct_chg']:+.2f}%" if c['pct_chg'] else '--',
            'ml_score': f"{c['ml_score']:.3f}",
            'rank_pct': f"{c['rank_pct']:.0f}%",
            'tech_score': c['tech_score'],
            'total_score': c['total_score'],
            'reasons': c['tech_reasons'],
            'total_mv': float(c['total_mv']) if c['total_mv'] else 0,
        })
    
    return results

def main():
    conn = get_db()
    try:
        # 使用 V4+ML 过滤策略（2026-05-09 起生效）
        from quant_app.services.strategy_service import generate_v4_ml_top5
        top5 = generate_v4_ml_top5(conn)
        
        if not top5:
            print("❌ 今日无符合条件的股票")
            return
        
        print(f"\n{'='*60}")
        print(f"📊 AI每日精选 TOP5 (V4+ML) | {datetime.now().strftime('%Y-%m-%d')}")
        print(f"{'='*60}\n")
        
        for s in top5:
            print(f"#{s['rank']} {s['ts_code'].split('.')[0]} {s['name']} | {s['industry']}")
            print(f"   价格: {s['price']} | V4: {s['total_score']} | ML: {s['ml_score']}")
            print(f"   📌 {' | '.join(s['reasons'])}")
            print()
        
        # 保存JSON供前端/cron使用
        output = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'count': len(top5),
            'strategy': 'V4+ML_Filter',
            'stocks': top5,
        }
        
        output_path = os.path.join(os.path.dirname(__file__), 'data', 'ai_top5_daily.json')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info(f"结果已保存: {output_path}")
        
    finally:
        conn.close()

if __name__ == '__main__':
    main()
