#!/usr/bin/env python3
"""V11.0 回测 — 止盈止损+持仓时间 综合优化对比"""
import logging
import os
import sys
import warnings

warnings.filterwarnings('ignore')
import numpy as np
import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from sqlalchemy import create_engine

from ml_predict import _ensemble_predict
from quant_app.utils.config import get_db_config
from quant_app.utils.model_loader import get_model_path

DB_CONFIG = get_db_config(); DB_CONFIG.pop('unix_socket', None)
DB_CONFIG['host']='127.0.0.1'; DB_CONFIG['port']=3306
ENGINE = create_engine(
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset=utf8mb4",
    pool_pre_ping=True, pool_size=5)

START_DATE, END_DATE = "2024-11-01", "2026-05-15"
SAMPLE_INTERVAL, TOP_N, HOLD_DAYS = 5, 3, 5
ATR_PERIOD, ATR_STOP_MULT = 20, 2.0

import joblib

bundle = joblib.load(get_model_path("v11.0"))
from scripts.predict_v11 import build_features_v11_inference

# Dates
all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}' ORDER BY trade_date", ENGINE
)['trade_date'].astype(str).tolist())
sample_dates = all_dates[::SAMPLE_INTERVAL]
sample_dates = [d for d in sample_dates if d > all_dates[5]]
logger.info(f"Dates: {len(all_dates)} total, {len(sample_dates)} samples")

# Pre-compute ATR
logger.info("Pre-computing ATR(20)...")
atr_cache = {}
conn_a = pymysql.connect(**DB_CONFIG); cur_a = conn_a.cursor()
cur_a.execute(f"SELECT DISTINCT ts_code FROM daily_price WHERE trade_date>=DATE_SUB('{START_DATE}',INTERVAL 60 DAY)")
all_codes = [r[0] for r in cur_a.fetchall()]
for ci, code in enumerate(all_codes):
    if ci % 500 == 0: logger.info(f"  ATR {ci}/{len(all_codes)}")
    df = pd.read_sql(f"SELECT trade_date,high,low,close FROM daily_price WHERE ts_code='{code}' ORDER BY trade_date", ENGINE)
    if len(df) < ATR_PERIOD+3: continue
    df['tr'] = np.maximum(df['high']-df['low'],
        np.maximum(abs(df['high']-df['close'].shift(1)), abs(df['low']-df['close'].shift(1))))
    df['atr'] = df['tr'].rolling(ATR_PERIOD).mean()
    for _, row in df.dropna().iterrows():
        atr_cache[(code, str(row['trade_date']))] = float(row['atr'])
cur_a.close(); conn_a.close()
logger.info(f"ATR cache: {len(atr_cache)} entries")

# Maps: buy_date → previous trading date
prev_date_map = {}
for i, d in enumerate(all_dates):
    if i > 0: prev_date_map[d] = all_dates[i-1]

conn = pymysql.connect(**DB_CONFIG)

# ===== Strategy variants =====
def baseline_exit(entry_price, closes, atr_val, day_idx):
    """固定-7%止损 + 3.5%移动止盈 + 8天强平"""
    peak = entry_price
    for j, c in enumerate(closes):
        peak = max(peak, c)
        # 固定止损
        if (c - entry_price) / entry_price <= -0.07:
            return j, "固定止损"
        # 移动止盈
        if (peak - entry_price) / entry_price > 0.05 and c <= peak * 0.965:
            return j, "移动止盈"
        # 超时（弱股>5天, 盈利<3%）
        if j >= 4 and (c - entry_price) / entry_price < 0.03:
            return j, "超时卖出"
        # 强平>8天
        if j >= 7:
            return j, "强制平仓"
    return len(closes)-1, "持有到期"

def atr_exit_v1(entry_price, closes, atr_val, day_idx):
    """ATR止损 + 3.5%移动止盈(原版) + 8天强平"""
    if atr_val is None or atr_val <= 0: atr_val = entry_price * 0.035
    atr_stop = entry_price - ATR_STOP_MULT * atr_val
    fixed_stop = entry_price * 0.93
    stop = max(atr_stop, fixed_stop)
    peak = entry_price
    for j, c in enumerate(closes):
        peak = max(peak, c)
        if c <= stop: return j, "ATR止损"
        if (peak - entry_price) / entry_price > 0.05 and c <= peak * 0.965:
            return j, "移动止盈"
        if j >= 4 and (c - entry_price) / entry_price < 0.03:
            return j, "超时卖出"
        if j >= 7: return j, "强制平仓"
    return len(closes)-1, "持有到期"

def atr_exit_v2(entry_price, closes, atr_val, day_idx):
    """ATR止损 + ATR移动止盈 + 8天强平"""
    if atr_val is None or atr_val <= 0: atr_val = entry_price * 0.035
    atr_stop = entry_price - ATR_STOP_MULT * atr_val
    stop = max(atr_stop, entry_price * 0.93)
    trail_dist = max(2 * atr_val / entry_price, 0.03)
    peak = entry_price
    for j, c in enumerate(closes):
        peak = max(peak, c)
        if c <= stop: return j, "ATR止损"
        peak_pct = (peak - entry_price) / entry_price
        if peak_pct > 0.05 and (peak - c) / peak > trail_dist:
            return j, "ATR移动止盈"
        if j >= 4 and (c - entry_price) / entry_price < 0.03:
            return j, "超时卖出"
        if j >= 7: return j, "强制平仓"
    return len(closes)-1, "持有到期"

def atr_exit_v3(entry_price, closes, atr_val, day_idx):
    """ATR止损 + 分级ATR移动止盈 + 8天强平"""
    if atr_val is None or atr_val <= 0: atr_val = entry_price * 0.035
    atr_stop = entry_price - ATR_STOP_MULT * atr_val
    stop = max(atr_stop, entry_price * 0.93)
    peak = entry_price
    for j, c in enumerate(closes):
        peak = max(peak, c)
        if c <= stop: return j, "ATR止损"
        peak_pct = (peak - entry_price) / entry_price
        # 分级移动止盈
        if peak_pct > 0.20: dist = 4 * atr_val / entry_price   # 大牛股给4xATR空间
        elif peak_pct > 0.10: dist = 3 * atr_val / entry_price
        elif peak_pct > 0.05: dist = 2 * atr_val / entry_price
        else: dist = 999
        if peak_pct > 0.05 and (peak - c) / peak > dist:
            return j, "分级ATR止盈"
        if j >= 4 and (c - entry_price) / entry_price < 0.03:
            return j, "超时卖出"
        if j >= 7: return j, "强制平仓"
    return len(closes)-1, "持有到期"

def atr_exit_v4(entry_price, closes, atr_val, day_idx):
    """ATR止损 + 分级ATR止盈 + 智能持仓时间"""
    if atr_val is None or atr_val <= 0: atr_val = entry_price * 0.035
    atr_stop = entry_price - ATR_STOP_MULT * atr_val
    stop = max(atr_stop, entry_price * 0.93)
    peak = entry_price
    for j, c in enumerate(closes):
        peak = max(peak, c)
        pct = (c - entry_price) / entry_price
        peak_pct = (peak - entry_price) / entry_price
        # ATR止损
        if c <= stop: return j, "ATR止损"
        # 快速止盈：1~2天涨>10%
        if j <= 1 and pct > 0.10:
            return j, "快速止盈"
        # 分级移动止盈
        if peak_pct > 0.20: dist = 4 * atr_val / entry_price
        elif peak_pct > 0.10: dist = 3 * atr_val / entry_price
        elif peak_pct > 0.05: dist = 2 * atr_val / entry_price
        else: dist = 999
        if peak_pct > 0.05 and (peak - c) / peak > dist:
            return j, "分级ATR止盈"
        # 时间管理（按盈利分级）
        if pct > 0.20:
            continue  # 大牛股不限制时间
        elif pct > 0.10:
            if j >= 9: return j, "超时(10天盈利)"  # 盈利>10%给10天
        elif pct > 0.03:
            if j >= 7: return j, "超时(7天平盈)"   # 微赚给7天
        else:
            if j >= 4: return j, "超时(5天不赚)"   # 不赚5天清
    return len(closes)-1, "持有到期"

strategies = [
    ("基线: 固定止损+3.5%移动止盈+8天强平", baseline_exit, []),
    ("ATR止损+原版止盈", atr_exit_v1, []),
    ("ATR止损+ATR移动止盈", atr_exit_v2, []),
    ("ATR止损+分级ATR止盈", atr_exit_v3, []),
    ("ATR止损+分级止盈+智能时间", atr_exit_v4, []),
]

results = {s[0]: [] for s in strategies}

for di, buy_date in enumerate(sample_dates):
    if (di+1) % 15 == 0: logger.info(f"Progress: {di+1}/{len(sample_dates)}")
    prev_date = prev_date_map.get(buy_date)
    if not prev_date: continue

    top_codes = pd.read_sql(f"""
        SELECT ts_code FROM daily_price WHERE trade_date='{prev_date}'
        AND LEFT(ts_code,1) NOT IN ('8','4','9') AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
        AND close<=200 ORDER BY amount DESC LIMIT 500
    """, ENGINE)['ts_code'].tolist()
    if len(top_codes)<100: continue

    try: feat = build_features_v11_inference(conn, top_codes, as_of_date=buy_date)
    except: continue
    if feat is None or feat.empty or len(feat)<50: continue

    v80f = bundle['feature_cols']; medians = bundle.get('global_medians',{})
    for col in v80f:
        if col not in feat.columns: feat[col]=medians.get(col,0.0)
    feat=feat.fillna(0)
    ml_preds=_ensemble_predict(feat,bundle)
    codes=feat['ts_code'].tolist()
    ranked=sorted(zip(codes,ml_preds),key=lambda x:-x[1])
    pure_ml_top=[c for c,_ in ranked[:TOP_N]]

    for tc in pure_ml_top:
        ret_df = pd.read_sql(f"""
            SELECT trade_date, close FROM daily_price
            WHERE ts_code='{tc}' AND trade_date>'{buy_date}' ORDER BY trade_date LIMIT {HOLD_DAYS+12}
        """, ENGINE)
        if len(ret_df) < 2: continue

        entry = float(ret_df['close'].iloc[0])
        closes = ret_df['close'].values[1:].astype(float)
        atr_v = atr_cache.get((tc, buy_date)) or atr_cache.get((tc, prev_date))

        for name, exit_fn, _ in strategies:
            exit_day, reason = exit_fn(entry, closes, atr_v, di)
            ret = (closes[exit_day] - entry) / entry if exit_day < len(closes) else (closes[-1]-entry)/entry
            results[name].append({'date': buy_date, 'ret': float(ret)*100, 'reason': reason})

conn.close()

# ===== Results =====
def calc_stats(records):
    if not records: return {}
    df = pd.DataFrame(records)
    daily = df.groupby('date')['ret'].mean()
    r = daily.values; n=len(r)
    w=int((r>0).sum()); cum=float((1+r/100).prod()-1)*100
    avg=float(r.mean()); std=float(r.std())
    sp=float(avg/std*np.sqrt(252/HOLD_DAYS)) if std>0 else 0
    dd=float((r/100).min())*100
    # Exit reason distribution
    exit_counts = df['reason'].value_counts().to_dict() if 'reason' in df.columns else {}
    return {'cum':cum,'sharpe':sp,'win':w/n,'dd':dd,'n':n,'avg':avg,'exits':exit_counts}

print(f"\n{'='*65}")
print("  止盈止损+持仓时间 综合优化回测")
print(f"  {START_DATE}~{END_DATE}, 采样{SAMPLE_INTERVAL}天, Top{TOP_N}")
print(f"{'='*65}")

all_stats = {}
for name, _, _ in strategies:
    s = calc_stats(results[name])
    all_stats[name] = s
    print(f"\n  {name}:")
    print(f"    累积 {s['cum']:+.1f}%  夏普 {s['sharpe']:.2f}  胜率 {s['win']*100:.1f}%  回撤 {s['dd']:+.1f}%")
    if s.get('exits'):
        exits_str = ' | '.join(f'{k}:{v}' for k,v in sorted(s['exits'].items(), key=lambda x:-x[1])[:4])
        print(f"    退出分布: {exits_str}")

# Summary table
print(f"\n{'='*65}")
print(f"  {'策略':<30} {'累积':>8} {'夏普':>6} {'胜率':>6} {'回撤':>7}")
print(f"  {'-'*57}")
for name, _, _ in strategies:
    s = all_stats[name]
    print(f"  {name:<30} {s['cum']:>+7.1f}% {s['sharpe']:>5.2f} {s['win']*100:>5.1f}% {s['dd']:>+6.1f}%")

# Best
best = max(all_stats.items(), key=lambda x: x[1]['cum'])
print(f"\n  ★ 最佳: {best[0]} (累积 {best[1]['cum']:+.1f}%)")
