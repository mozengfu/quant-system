#!/usr/bin/env python3
"""
预计算 V4.1→V6 级联策略每日候选集（使用 _load_best_model 自动选择最佳模型）
输出: data/v65_candidates.json

用法: python3 scripts/precompute_v65_candidates.py
"""
import os, sys, json, logging
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
import pymysql
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')

START_DATE, END_DATE = "2025-10-01", "2026-04-30"
V41_CANDIDATE_LIMIT = 30
TOP_N = 5


def load_df(sql):
    conn = pymysql.connect(**get_db_config())
    df = pd.read_sql(sql, conn)
    conn.close()
    return df


logger.info("加载数据...")
daily = load_df(
    f"SELECT ts_code, trade_date, close, pct_chg, turnover_rate, volume_ratio, "
    f"ma5, ma10, ma20, rps_20, high_52w, low_52w, vol, amount "
    f"FROM daily_price WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}'"
)
for c in ['vol', 'amount', 'close']:
    daily[c] = daily[c].fillna(0)
mf = load_df(
    f"SELECT ts_code, trade_date, main_net FROM moneyflow_daily "
    f"WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}'"
)
mf['main_net'] = mf['main_net'].fillna(0)
daily = daily.merge(mf, on=['ts_code', 'trade_date'], how='left')
daily['main_net'] = daily['main_net'].fillna(0)

dt_df = load_df(
    f"SELECT ts_code, trade_date, net_buy FROM dragon_tiger "
    f"WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}' AND net_buy!=0"
)
dti_df = load_df(
    f"SELECT ts_code, trade_date, net_buy FROM dragon_tiger_inst "
    f"WHERE trade_date>='{START_DATE}' AND trade_date<='{END_DATE}' AND net_buy!=0"
)
hc_df = load_df(
    f"SELECT ts_code, end_date trade_date, holder_num_change FROM holder_change "
    f"WHERE end_date>='{START_DATE}' AND end_date<='{END_DATE}'"
)

dt_d, dti_d, hc_d = defaultdict(list), defaultdict(list), defaultdict(list)
for _, r in dt_df.iterrows():
    dt_d[r['ts_code']].append((str(r['trade_date'])[:10], float(r['net_buy'] or 0)))
for _, r in dti_df.iterrows():
    dti_d[r['ts_code']].append((str(r['trade_date'])[:10], float(r['net_buy'] or 0)))
for _, r in hc_df.iterrows():
    hc_d[r['ts_code']].append((str(r['trade_date'])[:10], int(r['holder_num_change'] or 0)))


def v4_score(row):
    pct, vr, tr = float(row.get('pct_chg', 0)), float(row.get('volume_ratio', 0)), float(row.get('turnover_rate', 0))
    ma5, ma10, ma20 = float(row.get('ma5', 0)), float(row.get('ma10', 0)), float(row.get('ma20', 0))
    rps, close = float(row.get('rps_20', 0)), float(row.get('close', 0))
    h52w, l52w = float(row.get('high_52w', 0) or 0), float(row.get('low_52w', 0) or 0)
    mn = float(row.get('main_net', 0) or 0)
    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return -1
    cond1 = (1.0 < vr < 10 and tr > 1.5 and ma5 > ma10 > ma20 and close > ma5)
    cond2 = (pct > 4.0 and vr > 2.0 and close > ma5)
    if not cond1 and not cond2:
        return -1
    sc = 0
    if -3 <= pct < 0:
        sc += 30
    elif 0 <= pct <= 3:
        sc += 25
    elif 3 < pct <= 5:
        sc += 30
    elif 5 < pct <= 10:
        sc += 20
    else:
        return -1
    if vr > 3:
        sc += 30
    elif vr > 1.5:
        sc += 25
    elif vr > 1.0:
        sc += 10
    if 5 <= tr <= 10:
        sc += 20
    elif 3 <= tr < 5:
        sc += 15
    elif 2 <= tr < 3:
        sc += 8
    elif tr > 20:
        sc += 5
    sc += 30 if ma5 > ma10 > ma20 else 16
    if rps >= 80:
        sc += 20
    elif rps >= 60:
        sc += 15
    elif rps >= 40:
        sc += 10
    if h52w and l52w and h52w > l52w > 0:
        pos = (close - l52w) / (h52w - l52w) * 100
        if pos < 60:
            sc += 15
        elif pos >= 85:
            return -1
    if mn > 5000:
        sc += 15
    elif mn > 1000:
        sc += 10
    elif mn > 0:
        sc += 5
    return sc


def dragon_bonus(tc, date):
    td_30 = (datetime.strptime(date, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
    inst = sum(nb for td, nb in dti_d.get(tc, []) if td >= td_30)
    if inst > 30000000:
        return 15
    elif inst > 5000000:
        return 12
    if sum(1 for td, _ in dt_d.get(tc, []) if td >= td_30) > 0:
        return 8
    return 0


def holder_bonus(tc, date):
    rows = sorted([(td, c) for td, c in hc_d.get(tc, []) if td <= date], reverse=True)
    if len(rows) < 2:
        return 0
    dec = sum(1 for _, c in rows[:4] if c < 0)
    return (10 if dec >= 3 else 7 if dec >= 2 else 4 if dec >= 1 else 0)


logger.info("V4.1 评分...")
dly = daily.copy()
dly['trade_date'] = pd.to_datetime(dly['trade_date'])
dly['date_str'] = dly['trade_date'].dt.strftime('%Y-%m-%d')
trade_dates = sorted(dly['date_str'].unique())
trade_dates = [d for d in trade_dates if START_DATE <= d <= END_DATE]

daily_v41_candidates = {}
for date in trade_dates:
    day = dly[dly['date_str'] == date]
    if day.empty:
        continue
    cands = []
    for _, row in day.iterrows():
        tc = row['ts_code']
        sc = v4_score(row)
        if sc < 0:
            continue
        sc += dragon_bonus(tc, date) + holder_bonus(tc, date)
        cands.append((tc, sc))
    cands.sort(key=lambda x: x[1], reverse=True)
    daily_v41_candidates[date] = [tc for tc, _ in cands[:V41_CANDIDATE_LIMIT]]

logger.info("加载模型...")
# 策略：尝试加载与 V6.3 特征兼容的最佳集成模型
# 如果都不行，回退到 V6 + V6 原生特征
from ml_predict import _load_model, _load_best_model
from ml_predict import _build_features_for_stocks_v6_3, _build_features_for_stocks_v6, _ensemble_predict

bundle = None
version = None
build_fn = _build_features_for_stocks_v6_3

# 优先尝试集成模型
for v in ['v6.5', 'v6.4', 'v6.3', 'v6.2']:
    b = _load_model(v)
    if b and 'models' in b:
        bundle = b
        version = v
        build_fn = _build_features_for_stocks_v6_3
        break

# 如果集成模型都不行，回退 V6 + V6 原生特征
if bundle is None:
    b, ver = _load_best_model()
    if b:
        bundle = b
        version = ver
        build_fn = _build_features_for_stocks_v6
        # 兼容单模型格式
        if 'models' not in bundle and 'model' in bundle:
            bundle['models'] = [bundle['model']]
            bundle['ensemble_n_models'] = 1

if bundle is None:
    logger.error("没有可用的模型！")
    sys.exit(1)

ic = bundle.get('final_rank_ic', 'N/A')
n_models = len(bundle.get('models', []))
build_name = build_fn.__name__
logger.info(f"模型: {version} (IC={ic}, {n_models}子模型), 特征构建: {build_name}")

conn = pymysql.connect(**get_db_config())
ml_cache = {}
for di, date in enumerate(trade_dates):
    if (di + 1) % 30 == 0:
        logger.info(f"  ML: {di+1}/{len(trade_dates)} ({datetime.now().strftime('%H:%M')})")
    cands = daily_v41_candidates.get(date, [])
    if not cands:
        continue
    try:
        feat_df = build_fn(conn, cands, as_of_date=date)
        if feat_df is not None and not feat_df.empty:
            preds = _ensemble_predict(feat_df, bundle)
            for i, (_, row) in enumerate(feat_df.iterrows()):
                ml_cache[(row['ts_code'], date)] = float(preds[i])
    except Exception as e:
        logger.warning(f"  ML失败 {date}: {e}")
        import traceback
        traceback.print_exc()
    for tc in cands:
        if (tc, date) not in ml_cache:
            ml_cache[(tc, date)] = 0.0
conn.close()

# 输出：每日候选及ML分数
output = {}
ndays = 0
for date in trade_dates:
    cands = daily_v41_candidates.get(date, [])
    if not cands:
        continue
    scored = [(tc, round(ml_cache.get((tc, date), 0.0), 4)) for tc in cands]
    scored.sort(key=lambda x: x[1], reverse=True)
    output[date] = [{'c': tc, 'ml': sc} for tc, sc in scored[:TOP_N]]
    ndays += sum(1 for item in output[date] if item['ml'] != 0)

out_path = os.path.join(OUT_DIR, 'v65_candidates.json')
with open(out_path, 'w') as f:
    json.dump(output, f)
logger.info(f"候选已保存: {out_path} ({len(output)}天, {ndays}条非零ML)")
