#!/usr/bin/env python3
"""V11.0 回测对比: 固定止损等权 vs ATR动态止损+仓位管理"""
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
FIXED_STOP = -0.07
ATR_STOP_MULT = 2.0
ATR_PERIOD = 20

# Load model
import joblib

bundle = joblib.load(get_model_path("v11.0"))
from scripts.predict_v11 import build_features_v11_inference

all_dates = sorted(pd.read_sql(
    f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}' ORDER BY trade_date", ENGINE
)['trade_date'].astype(str).tolist())
sample_dates = all_dates[::SAMPLE_INTERVAL]
sample_dates = [d for d in sample_dates if d > all_dates[5]]
logger.info(f"Dates: {len(all_dates)} total, {len(sample_dates)} samples")

# Pre-compute ATR(20)
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

conn = pymysql.connect(**DB_CONFIG)
fixed_records, atr_records = [], []

for di, buy_date in enumerate(sample_dates):
    if (di+1) % 10 == 0: logger.info(f"Progress: {di+1}/{len(sample_dates)}")
    prev_date = pd.read_sql(f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date<'{buy_date}'", ENGINE).iloc[0,0]
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
            SELECT trade_date,pct_chg,close FROM daily_price
            WHERE ts_code='{tc}' AND trade_date>'{buy_date}' ORDER BY trade_date LIMIT {HOLD_DAYS}
        """, ENGINE)
        if len(ret_df)<2: continue

        entry_close = float(ret_df['close'].iloc[0])

        # === 对照组: 固定-7%止损 ===
        exit_fixed = len(ret_df)-1
        for j in range(1,len(ret_df)):
            cur=float(ret_df['close'].iloc[j])
            if cur>0 and entry_close>0 and (cur-entry_close)/entry_close <= FIXED_STOP:
                exit_fixed=j; break
        frets=ret_df['pct_chg'].iloc[:exit_fixed+1].values/100.0; frets=frets[~np.isnan(frets)]
        if len(frets)>0:
            fixed_records.append({'date':buy_date,'ret':float((1+frets).prod()-1)*100})

        # === ATR组: 动态止损 ===
        atr_v = atr_cache.get((tc, buy_date)) or atr_cache.get((tc, prev_date))
        if atr_v and atr_v>0 and entry_close>0:
            atr_stop = -ATR_STOP_MULT * atr_v / entry_close
        else:
            atr_stop = FIXED_STOP  # fallback
        exit_atr = len(ret_df)-1
        for j in range(1,len(ret_df)):
            cur=float(ret_df['close'].iloc[j])
            if cur>0 and entry_close>0 and (cur-entry_close)/entry_close <= atr_stop:
                exit_atr=j; break
        arets=ret_df['pct_chg'].iloc[:exit_atr+1].values/100.0; arets=arets[~np.isnan(arets)]
        if len(arets)>0:
            atr_records.append({
                'date':buy_date,
                'ret':float((1+arets).prod()-1)*100,
                'atr': atr_v or 0,
                'entry': entry_close
            })

conn.close()

# ===== Results =====
def stats(records, label):
    df=pd.DataFrame(records)
    daily=df.groupby('date')['ret'].mean()
    r=daily.values; n=len(r)
    w=int((r>0).sum()); cum=float((1+r/100).prod()-1)*100
    avg=float(r.mean()); std=float(r.std())
    sp=float(avg/std*np.sqrt(252/HOLD_DAYS)) if std>0 else 0
    dd=float((r/100).min())*100
    print(f"\n  {label}:")
    print(f"    采样 {n}次, 胜率 {w/n*100:.1f}%")
    print(f"    累积 {cum:+.2f}%, 均值 {avg:+.2f}%, 夏普 {sp:.2f}, 回撤 {dd:+.2f}%")
    return {'cum':cum,'sharpe':sp,'win':w/n,'dd':dd,'n':n}

print(f"\n{'='*60}")
print("  回测对比: 固定-7%止损 vs ATR动态止损+仓位管理")
print(f"  {START_DATE}~{END_DATE}, 采样{SAMPLE_INTERVAL}天, Top{TOP_N}, 持{HOLD_DAYS}天")
print(f"{'='*60}")

s1 = stats(fixed_records, "对照组: 固定-7%止损 + 等权")

atr_df = pd.DataFrame(atr_records)
s2 = stats(atr_records, "ATR组: 2×ATR止损 + 等权")

# ATR仓位加权: weight ∝ 1/ATR
if 'atr' in atr_df.columns and (atr_df['atr']>0).any():
    atr_df['w'] = 1.0 / atr_df['atr'].clip(lower=0.01)
    atr_df['w'] = atr_df.groupby('date')['w'].transform(lambda x: x/x.sum())
    daily_w = atr_df.groupby('date').apply(lambda g: np.average(g['ret'], weights=g['w']))
    rw = daily_w.values
    cum_w = float((1+rw/100).prod()-1)*100
    sp_w = float(rw.mean()/rw.std()*np.sqrt(252/HOLD_DAYS)) if rw.std()>0 else 0
    dd_w = float((rw/100).min())*100
    ww = int((rw>0).sum())
    print("\n  ATR组+仓位加权(低波动多配):")
    print(f"    累积 {cum_w:+.2f}%, 胜率 {ww/len(rw)*100:.1f}%, 夏普 {sp_w:.2f}, 回撤 {dd_w:+.2f}%")

    # Summary comparison
    print(f"\n{'='*60}")
    print("  总结对比")
    print(f"{'='*60}")
    print(f"  {'指标':<16} {'固定止损':>10} {'ATR止损':>10} {'ATR+仓位':>10}")
    print(f"  {'累积收益':<16} {s1['cum']:>+9.1f}% {s2['cum']:>+9.1f}% {cum_w:>+9.1f}%")
    print(f"  {'夏普比率':<16} {s1['sharpe']:>10.2f} {s2['sharpe']:>10.2f} {sp_w:>10.2f}")
    print(f"  {'最大回撤':<16} {s1['dd']:>+9.1f}% {s2['dd']:>+9.1f}% {dd_w:>+9.1f}%")
