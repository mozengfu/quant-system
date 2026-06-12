#!/usr/bin/env python3
"""
V11.0 OOS 模型预测 — 30特征轻量版，Rank IC=0.1405 (当前最优)
用法: python3 scripts/predict_v11_oos.py
"""
import logging
import os
import sys
import warnings

import joblib
import numpy as np
import pandas as pd
import pymysql

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
MODEL_PATH = os.path.join(BASE_DIR, "data", "ml_stock_model_v11_0_oos_v2.pkl")

_bundle = None
_feature_cols = None


def load_model(path=None):
    global _bundle, _feature_cols
    p = path or MODEL_PATH
    _bundle = joblib.load(p)
    _feature_cols = _bundle['feature_cols']
    logger.info(f"OOS模型加载: IC={_bundle.get('final_rank_ic'):.4f}, "
                f"{_bundle.get('n_models')}子模型, {len(_feature_cols)}特征, "
                f"样本={_bundle.get('n_samples')}")
    return _bundle


def build_features(conn, ts_codes, as_of_date=None):
    """为指定股票构建30个OOS特征"""
    if _bundle is None:
        load_model()
    if not ts_codes:
        return None

    tds = str(as_of_date)[:10] if as_of_date else pd.Timestamp.now().strftime('%Y-%m-%d')
    ph = ",".join(["%s"] * len(ts_codes))

    sql = f"""
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
               COALESCE(db2.total_mv, 0) as total_mv
        FROM daily_price d
        LEFT JOIN moneyflow_daily mf ON d.ts_code=mf.ts_code AND d.trade_date=mf.trade_date
        LEFT JOIN daily_basic db2 ON d.ts_code=db2.ts_code AND d.trade_date=db2.trade_date
        WHERE d.ts_code IN ({ph}) AND d.trade_date <= '{tds}'
          AND d.trade_date >= DATE_SUB('{tds}', INTERVAL 80 DAY)
        ORDER BY d.ts_code, d.trade_date
    """
    df = pd.read_sql(sql, conn, params=tuple(ts_codes))
    if df.empty:
        return None

    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
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

    # 大单/小单
    df['lg_net'] = df['buy_lg_amount'] - df['sell_lg_amount']
    df['sm_net'] = df['buy_sm_amount'] - df['sell_sm_amount']
    df['lg_net_ratio'] = df['lg_net'] / (df['amount'] + 1)

    # 其他
    df['log_mv'] = np.log(df['total_mv'].clip(lower=1e7) + 1)
    df['rev_1d'] = -df['pct_chg']
    df['amplitude'] = (df['high'] - df['low']) / (df['pre_close'] + 0.01) * 100

    # 取每个股票最新一行
    result = df.groupby('ts_code').last().reset_index()
    result = result.replace([np.inf, -np.inf], 0).fillna(0)
    return result


def ensemble_predict(df, bundle=None):
    """集成预测 - 多子模型平均"""
    if bundle is None:
        bundle = _bundle
    if bundle is None:
        load_model()
        bundle = _bundle

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


def predict(conn, ts_codes, as_of_date=None):
    """主预测接口：构建特征 → 集成预测"""
    feat = build_features(conn, ts_codes, as_of_date)
    if feat is None or feat.empty:
        return None
    preds = ensemble_predict(feat)
    result = feat[['ts_code']].copy()
    result['ml_score'] = preds
    return result.sort_values('ml_score', ascending=False)


def predict_daily_top(top_n=30):
    """日度预测: 成交额Top500中选出TopN"""
    conn = pymysql.connect(**DB_CONFIG)
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = cur.fetchone()[0]
        logger.info(f"最新交易日: {latest}")

        cur.execute("""
            SELECT ts_code FROM daily_price 
            WHERE trade_date=%s AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200
            ORDER BY amount DESC LIMIT 500
        """, (latest,))
        codes = [r[0] for r in cur.fetchall()]
        cur.close()

        result = predict(conn, codes, latest)
        if result is not None:
            top = result.head(top_n)
            print(f"\n{'Rank':<5} {'Code':<12} {'Score':>10}")
            print("-" * 30)
            for i, row in top.iterrows():
                print(f"{i+1:<5} {row['ts_code']:<12} {row['ml_score']:>10.4f}")
            return result
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V11.0 OOS 日度ML预测")
    parser.add_argument("--top", type=int, default=30, help="输出Top N (默认30)")
    parser.add_argument("--save", action="store_true", help="保存完整结果到JSON")
    args = parser.parse_args()

    load_model()
    result = predict_daily_top(args.top)
    if result is not None and args.save:
        out_path = os.path.join(BASE_DIR, "data", "ml_rank_v11_oos.json")
        result.to_json(out_path, orient='records', force_ascii=False)
        logger.info(f"结果已保存: {out_path}")


def predict_for_scheduler(conn, codes, as_of_date=None, top_n=30):
    """供调度器调用的推荐接口，返回统一格式"""
    load_model()
    if conn is None:
        import pymysql

        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        auto_close = True
    else:
        auto_close = False
    try:
        feat = build_features(conn, codes, as_of_date)
        if feat is None or feat.empty:
            return None
        preds = ensemble_predict(feat)
        ca = feat['ts_code'].tolist()
        ranked = sorted(zip(ca, preds), key=lambda x: -x[1])
        return [(tc, float(sc)) for tc, sc in ranked[:top_n]]
    finally:
        if auto_close:
            conn.close()
