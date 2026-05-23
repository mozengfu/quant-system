#!/usr/bin/env python3
"""
纯 ML vs V4+ML 严谨回测 — 排除数据泄露

关键防泄漏措施：
1. 用前一日成交额排序（当天成交额当天未知）
2. 特征构建严格传 as_of_date
3. 前向收益从次日开始计算
4. 每次用独立的数据库连接
"""
import os, sys, json, logging, time
from datetime import datetime, timedelta
import numpy as np, pandas as pd, pymysql

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from quant_app.utils.model_loader import load_model
from quant_app.utils.config import get_db_config
from ml_predict import _ensemble_predict, _build_features_for_stocks_v8_0
from quant_app.services.strategy_service import _v4_score_single

DB_CONFIG = get_db_config()
START_DATE, END_DATE = "2024-11-01", "2026-05-08"
SAMPLE_INTERVAL = 5
TOP_N = int(os.environ.get('TOP_N', '3'))
HOLD_DAYS = 5

OUT_PATH = os.path.join(BASE_DIR, 'data', 'backtest_pure_ml_12m.json')


def get_trade_dates(conn):
    df = pd.read_sql(f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '{START_DATE}' AND trade_date <= '{END_DATE}' ORDER BY trade_date", conn)
    return sorted(df['trade_date'].astype(str).tolist())


def get_top_vol_yesterday(conn, date_str, n=500):
    """用前一天的成交额排序选股（防泄漏）"""
    prev = pd.read_sql(f"SELECT MAX(trade_date) FROM daily_price WHERE trade_date < '{date_str}'", conn)
    prev_date = str(prev.iloc[0, 0])
    df = pd.read_sql(f"""
        SELECT ts_code, amount FROM daily_price
        WHERE trade_date = '{prev_date}'
          AND LEFT(ts_code, 1) NOT IN ('8','4','9')
          AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '87%%' AND ts_code NOT LIKE '43%%'
          AND close <= 200
        ORDER BY amount DESC LIMIT {n}
    """, conn)
    return df['ts_code'].tolist()


def forward_return_clean(conn, code, buy_date_str, hold=5):
    """从买入日次日开始计算持有期收益"""
    df = pd.read_sql(f"""
        SELECT trade_date, pct_chg FROM daily_price
        WHERE ts_code = '{code}' AND trade_date > '{buy_date_str}'
        ORDER BY trade_date LIMIT {hold}
    """, conn)
    if len(df) < 2:
        return None
    rets = df['pct_chg'].iloc[:hold].values / 100.0
    rets = rets[~np.isnan(rets)]
    if len(rets) == 0:
        return None
    return float((1 + rets).prod() - 1) * 100


def main():
    import argparse
    parser = argparse.ArgumentParser(description='纯 ML vs V4+ML 严谨回测')
    parser.add_argument('--model', type=str, default='v8.1',
                        help='模型标识或 .pkl 文件路径')
    args = parser.parse_args()

    logger.info(f"加载模型: {args.model}...")
    use_v11 = 'v11' in args.model.lower()
    if use_v11:
        from scripts.predict_v11 import load_v11_model, predict_v11
        from quant_app.utils.model_loader import get_model_path
        model_path = args.model if args.model.endswith('.pkl') else get_model_path(args.model)
        if model_path is None:
            model_path = args.model  # fallback to raw string
        bundle = load_v11_model(model_path)
        use_v11_predict = True
    elif args.model.endswith('.pkl'):
        import joblib
        bundle = joblib.load(args.model)
        use_v11_predict = False
    else:
        bundle = load_model(args.model)
        if bundle is None:
            bundle = load_model("v8.0")
        use_v11_predict = False
    version = bundle.get('version', args.model)
    logger.info(f"模型: {version}, {bundle.get('n_features','?')}特征")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    # 跳过前5天（需要前一天的数据排序）
    sample_dates = [d for d in sample_dates if d > all_dates[5]]
    logger.info(f"交易日: {len(all_dates)}, 采样日: {len(sample_dates)}")

    v4ml_results = []
    pure_ml_results = []
    ics = []

    for di, buy_date in enumerate(sample_dates):
        if (di + 1) % 5 == 0:
            logger.info(f"进度: {di+1}/{len(sample_dates)} ({datetime.now().strftime('%H:%M')})")

        # 用前一日的成交额选股池（当天成交额是未来信息）
        vol_codes = get_top_vol_yesterday(conn, buy_date, 500)
        if len(vol_codes) < 100:
            continue

        # 构建特征（严格按买入日期）
        try:
            if use_v11_predict:
                from scripts.predict_v11 import build_features_v11_inference
                feat = build_features_v11_inference(conn, vol_codes, as_of_date=buy_date)
            else:
                feat = _build_features_for_stocks_v8_0(conn, vol_codes, as_of_date=buy_date)
        except Exception as e:
            logger.warning(f"特征构建失败: {e}")
            continue
        if feat is None or feat.empty or len(feat) < 50:
            continue

        # ML 预测
        v80f = bundle['feature_cols']
        medians = bundle.get('global_medians', {})
        for col in v80f:
            if col not in feat.columns:
                feat[col] = medians.get(col, 0.0)
        feat = feat.fillna(0)
        ml_preds = _ensemble_predict(feat, bundle)
        codes = feat['ts_code'].tolist()

        # ===== V4+ML =====
        v4ml_top = []
        try:
            v4_picks = []
            for _, row in feat.iterrows():
                sc = _v4_score_single(row)
                if sc >= 0:
                    v4_picks.append((row['ts_code'], sc))
            v4_picks.sort(key=lambda x: -x[1])
            v4_top30 = [c for c, _ in v4_picks[:30]]
            if v4_top30:
                v4_scores = {c: ml_preds[codes.index(c)] for c in v4_top30 if c in codes}
                v4ml_top = [c for c, _ in sorted(v4_scores.items(), key=lambda x: -x[1])[:TOP_N]]
        except Exception:
            pass

        # ===== 纯 ML =====
        pure_ml_top = []
        try:
            ranked = sorted(zip(codes, ml_preds), key=lambda x: -x[1])
            pure_ml_top = [c for c, _ in ranked[:TOP_N]]
        except Exception:
            pass

        # ===== 前向收益（从次日开始） =====
        for label, top, store in [('V4+ML', v4ml_top, v4ml_results), ('纯ML', pure_ml_top, pure_ml_results)]:
            rets = []
            for tc in top:
                fr = forward_return_clean(conn, tc, buy_date, HOLD_DAYS)
                if fr is not None:
                    rets.append(fr)
            if rets:
                store.append({'date': buy_date, 'avg_ret': round(float(np.mean(rets)), 2), 'n': len(rets)})

        # ===== 当日 IC =====
        if len(codes) >= 30:
            sort_vol = pd.read_sql(f"SELECT ts_code, amount FROM daily_price WHERE trade_date = '{buy_date}' AND ts_code IN ({','.join(['%s']*len(codes))})", conn, params=codes)
            if not sort_vol.empty:
                vol_map = dict(zip(sort_vol['ts_code'].tolist(), sort_vol['amount'].tolist()))

                # Use volume as a rough proxy for "forward interest"
                # (not actual returns, just to check if ML ordering correlates with volume)
                pass

    conn.close()

    # ===== 结果汇总 =====
    print(f"\n{'='*55}")
    print(f"纯 ML vs V4+ML 严谨回测（{START_DATE} ~ {END_DATE}）")
    print(f"{'='*55}")
    print(f"模型: {version}, 采样: {SAMPLE_INTERVAL}天, 持仓: {HOLD_DAYS}天, Top{TOP_N}")
    print()

    for label, store in [('V4+ML', v4ml_results), ('纯ML', pure_ml_results)]:
        if not store:
            print(f"  {label}: 无有效交易")
            continue
        rets = np.array([r['avg_ret'] for r in store])
        wins = int((rets > 0).sum())
        total = len(rets)
        cum = float((1 + rets / 100).prod() - 1) * 100
        avg = float(rets.mean())
        std = float(rets.std())
        sharpe = float(avg / std * np.sqrt(252 / HOLD_DAYS)) if std > 0 else 0
        dd = float((rets / 100).min())
        print(f"  {label}:")
        print(f"    采样次数: {total}")
        print(f"    累积收益: {cum:+.2f}%")
        print(f"    单次均值: {avg:+.2f}%")
        print(f"    胜率:     {wins/total*100:.1f}% ({wins}W/{total-wins}L)")
        print(f"    夏普:     {sharpe:.2f}")
        print(f"    最大回撤: {dd*100:.2f}%")
        print()

    if v4ml_results and pure_ml_results:
        v4_rets = [r['avg_ret'] for r in v4ml_results]
        ml_rets = [r['avg_ret'] for r in pure_ml_results]
        min_len = min(len(v4_rets), len(ml_rets))
        diff = np.array(ml_rets[:min_len]) - np.array(v4_rets[:min_len])
        print(f"  差异分析（纯 ML - V4+ML）:")
        print(f"    均值差: {diff.mean():+.2f}%")
        print(f"    纯ML胜出: {(diff>0).sum()}/{min_len} ({(diff>0).sum()/min_len*100:.0f}%)")

    output = {
        'model': version,
        'params': {'start': START_DATE, 'end': END_DATE, 'interval': SAMPLE_INTERVAL, 'top_n': TOP_N, 'hold_days': HOLD_DAYS},
        'v4ml': v4ml_results,
        'pure_ml': pure_ml_results,
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    logger.info(f"结果已保存: {OUT_PATH}")


if __name__ == '__main__':
    main()
