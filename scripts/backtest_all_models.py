#!/usr/bin/env python3
"""
全模型对比回测 — 同一参数跑所有可用模型，选出最优应用到生产
"""
import json
import logging
import os
import sys
import time
import warnings

import joblib
import numpy as np
import pandas as pd
import pymysql

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
DATA_DIR = os.path.join(BASE_DIR, "data")

START_DATE = "2025-06-01"
END_DATE = "2026-06-04"
SAMPLE_INTERVAL = 5
TOP_N = 3
HOLD_DAYS = 5
POOL_SIZE = 200

def get_trade_dates(conn):
    df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",
        conn, params=(START_DATE, END_DATE))
    return sorted(df["trade_date"].astype(str).tolist())

def get_top_vol_prev(conn, date_str, n=500):
    prev = pd.read_sql("SELECT MAX(trade_date) FROM daily_price WHERE trade_date<%s", conn, params=(date_str,))
    prev_date = str(prev.iloc[0,0])
    df = pd.read_sql(f"""SELECT ts_code FROM daily_price
        WHERE trade_date='{prev_date}' AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200
        ORDER BY amount DESC LIMIT {n}""", conn)
    return df["ts_code"].tolist()

def build_features_oos(conn, codes, as_of_date):
    """OOS 模型用的30维轻量特征"""
    if len(codes) == 0: return None
    ph = ",".join(["%s"]*len(codes))
    tds = str(as_of_date)[:10]

    df = pd.read_sql(f"""
        SELECT d.ts_code, d.trade_date, d.open, d.high, d.low, d.close, d.pre_close,
               d.vol, d.amount, d.pct_chg, d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20, d.rps_20,
               COALESCE(mf.main_net, 0) as main_net,
               COALESCE(mf.net_mf_amount, 0) as net_mf_amount,
               COALESCE(mf.buy_lg_amount, 0) as buy_lg_amount,
               COALESCE(mf.sell_lg_amount, 0) as sell_lg_amount,
               COALESCE(mf.buy_sm_amount, 0) as buy_sm_amount,
               COALESCE(mf.sell_sm_amount, 0) as sell_sm_amount,
               COALESCE(db2.pe_ttm, 0) as pe_ttm,
               COALESCE(db2.pb, 0) as pb,
               COALESCE(db2.total_mv, 0) as total_mv,
               COALESCE(db2.circ_mv, 0) as circ_mv
        FROM daily_price d
        LEFT JOIN moneyflow_daily mf ON d.ts_code=mf.ts_code AND d.trade_date=mf.trade_date
        LEFT JOIN daily_basic db2 ON d.ts_code=db2.ts_code AND d.trade_date=db2.trade_date
        WHERE d.ts_code IN ({ph}) AND d.trade_date <= '{tds}'
          AND d.trade_date >= DATE_SUB('{tds}', INTERVAL 80 DAY)
        ORDER BY d.ts_code, d.trade_date
    """, conn, params=tuple(codes))

    if df.empty: return None
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.sort_values(['ts_code','trade_date']).reset_index(drop=True)
    g = df.groupby('ts_code')

    df['chg_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['chg_10d'] = g['pct_chg'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['chg_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).sum())
    df['volatility_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).std())
    df['volatility_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).std())
    df['ret_vol_ratio'] = df['chg_5d'] / (df['volatility_5d'] + 0.01)
    df['main_cum5'] = g['main_net'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['main_cum10'] = g['main_net'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['main_cum20'] = g['main_net'].transform(lambda x: x.rolling(20, min_periods=1).sum())
    df['flow_div_5d'] = df['main_cum5'] - df['chg_5d']
    df['flow_div_10d'] = df['main_cum10'] - df['chg_10d']
    df['high_20d'] = g['high'].transform(lambda x: x.rolling(20, min_periods=1).max())
    df['low_20d'] = g['low'].transform(lambda x: x.rolling(20, min_periods=1).min())
    df['pos_20d'] = (df['close'] - df['low_20d']) / (df['high_20d'] - df['low_20d'] + 0.01)
    df['ma5_bias'] = (df['close'] - df['ma5']) / (df['ma5'] + 0.01)
    df['ma10_bias'] = (df['close'] - df['ma10']) / (df['ma10'] + 0.01)
    df['ma20_bias'] = (df['close'] - df['ma20']) / (df['ma20'] + 0.01)
    df['vol_change_5d'] = df['vol'] / (g['vol'].shift(5) + 1)
    df['turnover_change'] = df['turnover_rate'] / (g['turnover_rate'].shift(5) + 0.01)
    df['lg_net'] = df['buy_lg_amount'] - df['sell_lg_amount']
    df['sm_net'] = df['buy_sm_amount'] - df['sell_sm_amount']
    df['lg_net_ratio'] = df['lg_net'] / (df['amount'] + 1)
    df['log_mv'] = np.log(df['total_mv'].clip(lower=1e7) + 1)
    df['rev_1d'] = -df['pct_chg']
    df['amplitude'] = (df['high'] - df['low']) / (df['pre_close'] + 0.01) * 100

    result = df.groupby('ts_code').last().reset_index()
    result = result.replace([np.inf, -np.inf], 0).fillna(0)
    return result

def forward_return(conn, code, buy_date, hold=5, stop_loss=-0.07, take_profit=0.15):
    df = pd.read_sql("""SELECT pct_chg FROM daily_price
        WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s""",
        conn, params=(code, buy_date, hold))
    if len(df) < 1: return None, "data_short"
    daily_rets = df["pct_chg"].values / 100.0
    daily_rets = daily_rets[~np.isnan(daily_rets)]
    if len(daily_rets) == 0: return None, "data_short"
    cum = 1.0
    exit_reason = "hold_to_end"
    for r in daily_rets:
        cum *= (1 + r)
        if cum - 1 >= take_profit: exit_reason = "take_profit"; break
        if cum - 1 <= stop_loss: exit_reason = "stop_loss"; break
    return float((cum - 1) * 100), exit_reason

def ensemble_predict(df, bundle):
    models = bundle['models']
    subsets = bundle['feature_subsets']
    feature_cols = bundle['feature_cols']
    medians = bundle.get('global_medians', {})

    X = np.zeros((len(df), len(feature_cols)), dtype=np.float32)
    for i, c in enumerate(feature_cols):
        X[:, i] = df[c].values if c in df.columns else medians.get(c, 0)

    all_preds = []
    for mi in range(len(models)):
        subset_name = list(subsets.keys())[mi]
        avail = [c for c in subsets[subset_name] if c in feature_cols]
        idxs = [feature_cols.index(c) for c in avail]
        all_preds.append(models[mi].predict(X[:, idxs]))
    return np.mean(np.column_stack(all_preds), axis=1)

def compute_stats(rets):
    rets = np.array(rets)
    wins = int((rets > 0).sum())
    total = len(rets)
    cum = float((1 + rets/100).prod() - 1) * 100
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg/std * np.sqrt(252/HOLD_DAYS)) if std > 0 else 0

    cum_vals = [100.0]
    for r in rets: cum_vals.append(cum_vals[-1] * (1 + r/100))
    peak = 100.0; max_dd = 0.0
    for v in cum_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    return {"cum_ret": round(cum, 1), "avg_ret": round(avg, 2), "win_rate": round(wins/total*100, 1),
            "sharpe": round(sharpe, 2), "max_dd": round(max_dd, 1), "n_trades": total,
            "n_wins": wins}

def backtest_model(model_path, label):
    logger.info(f"--- {label} ---")
    t0 = time.time()
    bundle = joblib.load(model_path)
    version = bundle.get('version', '?')
    n_models = bundle.get('n_models', '?')
    n_features = bundle.get('n_features', '?')
    ic = bundle.get('final_rank_ic', bundle.get('rank_ic', '?'))
    data_range = bundle.get('data_range', '?')
    logger.info(f"  version={version} sub_models={n_models} features={n_features} ic={ic} data={data_range}")

    # 对于大特征模型，用 predict_v11 的特征构建
    use_full_features = n_features > 50
    if use_full_features:
        from scripts.predict_v11 import build_features_v11_inference

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > all_dates[0]]
    logger.info(f"  交易日:{len(all_dates)} 采样日:{len(sample_dates)}")

    rets = []

    for di, trade_date in enumerate(sample_dates):
        codes = get_top_vol_prev(conn, trade_date, POOL_SIZE)
        if len(codes) < 50: continue

        if use_full_features:
            feat = build_features_v11_inference(conn, codes, as_of_date=trade_date)
        else:
            feat = build_features_oos(conn, codes, trade_date)

        if feat is None or len(feat) < 30: continue

        try:
            preds = ensemble_predict(feat, bundle)
            feat = feat.copy()
            feat['ml_score'] = preds
        except Exception as e:
            logger.warning(f"  predict failed {trade_date}: {e}")
            continue

        top = feat.nlargest(TOP_N, 'ml_score')
        top_codes = top['ts_code'].tolist()
        batch_rets = []
        for tc in top_codes:
            fr, _ = forward_return(conn, tc, trade_date, HOLD_DAYS)
            if fr is not None: batch_rets.append(fr)

        if batch_rets:
            rets.append(float(np.mean(batch_rets)))

        if (di + 1) % 15 == 0:
            logger.info(f"  进度:{di+1}/{len(sample_dates)} 已产生{len(rets)}笔")

    conn.close()

    if len(rets) < 5:
        logger.warning(f"  {label}: 交易太少 ({len(rets)}笔), 跳过")
        return None

    stats = compute_stats(rets)
    elapsed = time.time() - t0
    logger.info(f"  {label}: 累积{stats['cum_ret']:+.1f}% 胜率{stats['win_rate']}% 夏普{stats['sharpe']} 回撤{stats['max_dd']}% ({elapsed:.0f}s)")
    return {"label": label, "path": model_path, **stats,
            "version": version, "n_models": n_models, "n_features": n_features, "ic": ic, "data_range": data_range}

def main():
    candidates = [
        # OOS 系列（轻量，30特征）
        ("data/ml_stock_model_v11_0_oos.pkl",     "OOS-v1 (训练截止2025-05)"),
        ("data/ml_stock_model_v11_0_oos_v2.pkl",  "OOS-v2 (训练截止2026-05, IC=0.086)"),
        ("data/ml_stock_model_v11_0_oos_v3.pkl",  "OOS-v3 (训练截止2025-05, IC=0.141)"),
        # 全量系列（大模型，117-128特征）
        ("data/ml_stock_model_v11_0_7lgb.pkl",    "V11.0-7LGB (当前生产, 14子/126特征)"),
        ("data/ml_stock_model_v11_0_7model.pkl",  "V11.0-7Model (7子/117特征)"),
        ("data/ml_stock_model_v11_0_11models.pkl","V11.0-11Model (11子/117特征)"),
        ("data/ml_stock_model_v11_0_bad.pkl",     "V11.2-18Model (18子/128特征, IC=0.144)"),
        ("data/ml_stock_model_v11_2.pkl",         "V11.2-Thin (19KB)"),
    ]

    print(f"\n{'='*70}")
    print(f"全模型对比回测: {START_DATE} ~ {END_DATE} | 间隔{SAMPLE_INTERVAL}d | Top{TOP_N} | 持有{HOLD_DAYS}d")
    print(f"{'='*70}")

    results = []
    for path, label in candidates:
        full = os.path.join(BASE_DIR, path)
        if not os.path.exists(full):
            logger.info(f"跳过 (文件不存在): {path}")
            continue
        try:
            r = backtest_model(full, label)
            if r: results.append(r)
        except Exception as e:
            logger.error(f"  {label} 回测失败: {e}")

    if not results:
        print("无有效结果")
        return

    # 排序：按夏普比率
    results.sort(key=lambda x: x['sharpe'], reverse=True)

    print(f"\n{'='*70}")
    print(f"{'模型':<35s} {'累积':>8s} {'胜率':>6s} {'夏普':>5s} {'回撤':>6s} {'交易':>4s}")
    print(f"{'-'*70}")
    for r in results:
        print(f"{r['label']:<35s} {r['cum_ret']:+7.1f}% {r['win_rate']:5.1f}% {r['sharpe']:5.2f} {r['max_dd']:5.1f}% {r['n_trades']:4d}")

    best = results[0]
    print(f"\n{'='*70}")
    print(f"最优: {best['label']}")
    print(f"  文件: {best['path']}  version={best['version']}  sub_models={best['n_models']}  features={best['n_features']}")
    print(f"  累积{best['cum_ret']:+.1f}%  胜率{best['win_rate']}%  夏普{best['sharpe']}  回撤{best['max_dd']}%")

    # 保存结果
    out_path = os.path.join(DATA_DIR, "backtest_all_models.json")
    json.dump({"params": {"start": START_DATE, "end": END_DATE, "interval": SAMPLE_INTERVAL,
                          "top_n": TOP_N, "hold_days": HOLD_DAYS, "pool": POOL_SIZE},
               "results": results}, open(out_path, "w"), indent=2, ensure_ascii=False, default=str)
    logger.info(f"结果已保存: {out_path}")

    return best

if __name__ == "__main__":
    main()
