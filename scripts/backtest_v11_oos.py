#!/usr/bin/env python3
"""
V11.0 OOS回测 — 严格隔离：训练截止2025-05-31，回测2025-06-01起
"""
import json
import logging
import os
import sys
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
MODEL_PATH = os.path.join(BASE_DIR, "data", "ml_stock_model_v11_0_oos.pkl")

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

def build_features_for_backtest(conn, codes, as_of_date):
    """为回测构建特征（与训练时相同的特征集）"""
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

    # 动量
    df['chg_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['chg_10d'] = g['pct_chg'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['chg_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).sum())

    # 波动率
    df['volatility_5d'] = g['pct_chg'].transform(lambda x: x.rolling(5, min_periods=1).std())
    df['volatility_20d'] = g['pct_chg'].transform(lambda x: x.rolling(20, min_periods=1).std())
    df['ret_vol_ratio'] = df['chg_5d'] / (df['volatility_5d'] + 0.01)

    # 资金流
    df['main_cum5'] = g['main_net'].transform(lambda x: x.rolling(5, min_periods=1).sum())
    df['main_cum10'] = g['main_net'].transform(lambda x: x.rolling(10, min_periods=1).sum())
    df['main_cum20'] = g['main_net'].transform(lambda x: x.rolling(20, min_periods=1).sum())
    df['flow_div_5d'] = df['main_cum5'] - df['chg_5d']
    df['flow_div_10d'] = df['main_cum10'] - df['chg_10d']

    # 价格位置
    df['high_20d'] = g['high'].transform(lambda x: x.rolling(20, min_periods=1).max())
    df['low_20d'] = g['low'].transform(lambda x: x.rolling(20, min_periods=1).min())
    df['pos_20d'] = (df['close'] - df['low_20d']) / (df['high_20d'] - df['low_20d'] + 0.01)

    # 均线偏离
    df['ma5_bias'] = (df['close'] - df['ma5']) / (df['ma5'] + 0.01)
    df['ma10_bias'] = (df['close'] - df['ma10']) / (df['ma10'] + 0.01)
    df['ma20_bias'] = (df['close'] - df['ma20']) / (df['ma20'] + 0.01)

    # 量/换手
    df['vol_change_5d'] = df['vol'] / (g['vol'].shift(5) + 1)
    df['turnover_change'] = df['turnover_rate'] / (g['turnover_rate'].shift(5) + 0.01)

    # 大单
    df['lg_net'] = df['buy_lg_amount'] - df['sell_lg_amount']
    df['sm_net'] = df['buy_sm_amount'] - df['sell_sm_amount']
    df['lg_net_ratio'] = df['lg_net'] / (df['amount'] + 1)

    # 其他
    df['log_mv'] = np.log(df['total_mv'].clip(lower=1e7) + 1)
    df['rev_1d'] = -df['pct_chg']
    df['amplitude'] = (df['high'] - df['low']) / (df['pre_close'] + 0.01) * 100

    # 只取每个股票的最新一行
    result = df.groupby('ts_code').last().reset_index()
    result = result.replace([np.inf, -np.inf], 0).fillna(0)

    return result

def forward_return(conn, code, buy_date, hold=5, stop_loss=-0.07, take_profit=0.15):
    """含止损止盈的持仓收益: 触及止损/止盈即提前退出"""
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
        if cum - 1 >= take_profit:
            exit_reason = "take_profit"
            break
        if cum - 1 <= stop_loss:
            exit_reason = "stop_loss"
            break

    return float((cum - 1) * 100), exit_reason

def ensemble_predict(df, bundle):
    """集成预测"""
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

def main():
    logger.info("加载OOS模型...")
    bundle = joblib.load(MODEL_PATH)
    logger.info(f"模型: {bundle.get('version')}, {bundle.get('n_models')}子模型, "
                f"{bundle.get('n_features')}特征, RankIC={bundle.get('final_rank_ic')}, "
                f"数据: {bundle.get('data_range')}")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > all_dates[0]]
    logger.info(f"交易日:{len(all_dates)} 采样日:{len(sample_dates)}")

    results = []

    for di, trade_date in enumerate(sample_dates):
        codes = get_top_vol_prev(conn, trade_date, POOL_SIZE)
        if len(codes) < 50: continue

        feat = build_features_for_backtest(conn, codes, trade_date)
        if feat is None or len(feat) < 30: continue

        try:
            preds = ensemble_predict(feat, bundle)
            feat = feat.copy()
            feat['ml_score'] = preds
        except Exception:
            continue

        top = feat.nlargest(TOP_N, 'ml_score')
        top_codes = top['ts_code'].tolist()

        rets = []
        exit_reasons = []
        for tc in top_codes:
            fr, reason = forward_return(conn, tc, trade_date, HOLD_DAYS)
            if fr is not None:
                rets.append(fr)
                exit_reasons.append(reason)

        if rets:
            results.append({
                "date": trade_date,
                "codes": top_codes,
                "avg_ret": round(float(np.mean(rets)), 2),
                "n": len(rets),
                "rets": [round(r, 2) for r in rets],
                "exits": exit_reasons,
            })

        if (di + 1) % 10 == 0:
            logger.info(f"进度:{di+1}/{len(sample_dates)} 交易:{len(results)}")

    conn.close()

    # 汇总
    print(f"\n{'='*55}")
    print("V11.0 OOS回测 (严格隔离 + 止盈+15% / 止损-7%)")
    print(f"训练: {bundle.get('data_range')}  →  回测: {START_DATE} ~ {END_DATE}")
    print(f"间隔{SAMPLE_INTERVAL}d | 持仓{HOLD_DAYS}d | Top{TOP_N} | 池{POOL_SIZE}")
    print(f"{'='*55}")

    if not results:
        print("  无交易记录")
        return

    rets = np.array([r["avg_ret"] for r in results])
    wins = int((rets > 0).sum())
    total = len(rets)
    cum = float((1 + rets/100).prod() - 1) * 100
    avg = float(rets.mean())
    std = float(rets.std())
    sharpe = float(avg/std * np.sqrt(252/HOLD_DAYS)) if std > 0 else 0

    # 最大回撤
    cum_vals = [100.0]
    for r in rets:
        cum_vals.append(cum_vals[-1] * (1 + r/100))
    peak = 100.0
    max_dd = 0.0
    for v in cum_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd

    print(f"  纯ML(OOS): 采样{total}次  累积{cum:+.1f}%  均值{avg:+.2f}%  "
          f"胜率{wins/total*100:.0f}%  夏普{sharpe:.2f}  最大回撤{max_dd:.1f}%")

    # 止盈止损统计
    all_exits = [e for r in results for e in r.get("exits", [])]
    total_exits = len(all_exits)
    if total_exits > 0:
        tp_count = all_exits.count("take_profit")
        sl_count = all_exits.count("stop_loss")
        hold_count = all_exits.count("hold_to_end")
        print(f"  止盈:{tp_count}({tp_count/total_exits*100:.0f}%)  "
              f"止损:{sl_count}({sl_count/total_exits*100:.0f}%)  "
              f"持有到期:{hold_count}({hold_count/total_exits*100:.0f}%)")

    # 与污染模型对比
    print("\n  对比:")
    print("  被污染的模型: 累积+12.24%  夏普8.26  胜率87%")
    print(f"  严格OOS模型:  累积{cum:+.1f}%  夏普{sharpe:.2f}  胜率{wins/total*100:.0f}%")
    print(f"  差距:         {cum-12.24:+.1f}%       {sharpe-8.26:+.2f}       {wins/total*100-87:+.0f}%")

    out_path = os.path.join(BASE_DIR, "data", "backtest_v11_oos.json")
    json.dump({"results": results, "summary": {
        "cum_ret": round(cum, 2), "avg_ret": round(avg, 2),
        "win_rate": round(wins/total*100, 1), "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd, 1), "n_trades": total,
    }}, open(out_path, "w"), indent=2, default=str)
    logger.info(f"结果: {out_path}")

if __name__ == "__main__":
    main()
