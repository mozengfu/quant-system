#!/usr/bin/env python3
"""
V6 vs V6.2 ML选股模型对比验证

在最近3个月数据上对比两个模型的选股效果：
  - Rank IC (Spearman)
  - Top 20% vs Bottom 20% 超额收益差
  - Top 10 平均收益
  - 日频 IC 稳定性
"""

import sys, os, json, warnings, logging
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np, pandas as pd, pymysql
from scipy.stats import spearmanr
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)
warnings.filterwarnings('ignore')

from quant_app.utils.model_loader import load_model
from quant_app.utils.config import get_db_config
from ml_predict import (
    _build_features_for_stocks_v6,
    _build_features_for_stocks_v6_2,
    _ensemble_predict,
)

DB_CONFIG = get_db_config()

TEST_DAYS = 20  # 最近 20 个交易日

def main():
    logger.info("=" * 60)
    logger.info("V6 vs V6.2 ML选股模型对比验证")
    logger.info("=" * 60)

    # 加载两套模型
    v6 = load_model("v6")
    v62 = load_model("v6.2")
    if v6 is None or v62 is None:
        logger.error("模型加载失败！")
        return

    conn = pymysql.connect(**DB_CONFIG)

    # 预加载 stock_info
    stock_info = pd.read_sql("SELECT ts_code, industry, is_st, list_date FROM stock_info", conn)
    stock_info_map = {}
    for _, r in stock_info.iterrows():
        stock_info_map[r['ts_code']] = r

    # 获取最近交易日列表
    dates = pd.read_sql(f"""
        SELECT DISTINCT trade_date FROM daily_price
        ORDER BY trade_date DESC LIMIT {TEST_DAYS}
    """, conn)['trade_date'].tolist()
    dates = sorted(dates)
    logger.info(f"测试区间: {dates[0]} ~ {dates[-1]} ({len(dates)} 个交易日)")

    # 获取流动性最好的 N 只（用最近日均成交额排序）
    all_codes = pd.read_sql("""
        SELECT ts_code, AVG(amount) as avg_amount FROM daily_price
        WHERE trade_date >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
        GROUP BY ts_code
        ORDER BY avg_amount DESC
        LIMIT 1500
    """, conn)['ts_code'].tolist()
    all_codes = [c for c in all_codes
                 if not c.startswith(('68', '83', '87', '43'))
                 and not c.startswith(('8', '4', '9'))]
    logger.info(f"候选股票: {len(all_codes)} 只（流动性Top 1500）")

    # 预测函数（批量，只取最新日期的预测值）
    def predict_single_day(ts_codes, as_of_date):
        """返回 {ts_code: pred_score}"""
        # V6
        feat6 = _build_features_for_stocks_v6(conn, ts_codes, as_of_date=as_of_date)
        if feat6.empty:
            return {}, {}
        fcols6 = v6['feature_cols']
        meds6 = v6.get('global_medians', {})
        for c in fcols6:
            if c not in feat6.columns:
                feat6[c] = meds6.get(c, 0.0)
            elif feat6[c].isna().any():
                feat6[c] = feat6[c].fillna(meds6.get(c, 0.0))
        pred6 = dict(zip(feat6['ts_code'], v6['model'].predict(feat6[fcols6].values.astype(np.float32))))

        # V6.2
        feat62 = _build_features_for_stocks_v6_2(conn, ts_codes, as_of_date=as_of_date)
        if feat62.empty:
            return pred6, {}
        pred62 = dict(zip(feat62['ts_code'], _ensemble_predict(feat62, v62)))

        return pred6, pred62

    # 逐日对比
    records = []
    for i, date in enumerate(dates):
        if (i + 1) % 5 == 0:
            logger.info(f"  进度: {i+1}/{len(dates)}")

        try:
            pred6, pred62 = predict_single_day(all_codes, as_of_date=date)
            if not pred6 or not pred62:
                continue

            common = set(pred6.keys()) & set(pred62.keys())
            if len(common) < 30:
                continue

            # 实际未来5日收益
            future_rets = {}
            for ts_code in common:
                row = pd.read_sql("""
                    SELECT close FROM daily_price
                    WHERE ts_code = %s AND trade_date >= %s
                    ORDER BY trade_date LIMIT 6
                """, conn, params=(ts_code, date))
                if len(row) >= 6:
                    fr = float(row.iloc[5]['close']) / float(row.iloc[0]['close']) - 1
                elif len(row) >= 2:
                    fr = float(row.iloc[-1]['close']) / float(row.iloc[0]['close']) - 1
                else:
                    continue
                future_rets[ts_code] = fr

            common2 = [c for c in common if c in future_rets]
            if len(common2) < 30:
                continue

            data = pd.DataFrame({
                'ts_code': common2,
                'future_ret_5d': [future_rets[c] for c in common2],
                'v6_pred': [pred6[c] for c in common2],
                'v62_pred': [pred62[c] for c in common2],
            })

            # Rank IC
            ic_v6 = spearmanr(data['future_ret_5d'], data['v6_pred'])[0]
            ic_v62 = spearmanr(data['future_ret_5d'], data['v62_pred'])[0]

            # Top/Bottom spread
            n_top = max(10, int(len(data) * 0.20))
            for label, pred_col in [('v6', 'v6_pred'), ('v62', 'v62_pred')]:
                df_s = data.sort_values(pred_col, ascending=False)
                top_avg = df_s.head(n_top)['future_ret_5d'].mean() * 100
                bot_avg = df_s.tail(n_top)['future_ret_5d'].mean() * 100
                records.append({
                    'date': str(date),
                    'model': label,
                    'rank_ic': ic_v6 if label == 'v6' else ic_v62,
                    'top20_avg': top_avg,
                    'bottom20_avg': bot_avg,
                    'spread_bps': top_avg - bot_avg,
                    'n_stocks': len(data),
                })
        except Exception as e:
            logger.warning(f"  {date} 跳过: {e}")
            continue

    conn.close()

    # === 汇总 ===
    df_results = pd.DataFrame(records)
    if df_results.empty:
        logger.error("无有效结果")
        return

    logger.info(f"\n{'='*60}")
    logger.info(f"汇总: {df_results['date'].nunique()} 天, {len(df_results)} 条记录")
    logger.info(f"{'='*60}")

    for model in ['v6', 'v62']:
        sub = df_results[df_results['model'] == model]
        logger.info(f"\n--- {model.upper()} 模型 ---")
        logger.info(f"  Rank IC:      均值={sub['rank_ic'].mean():.4f}, "
                    f"中位={sub['rank_ic'].median():.4f}, "
                    f"std={sub['rank_ic'].std():.4f}, "
                    f"IC IR={sub['rank_ic'].mean()/(sub['rank_ic'].std()+1e-9):.2f}")
        logger.info(f"  Top20 平均收益: {sub['top20_avg'].mean():.2f}%")
        logger.info(f"  Bottom20 平均收益: {sub['bottom20_avg'].mean():.2f}%")
        logger.info(f"  Top-Bottom Spread: {sub['spread_bps'].mean():.2f}bp")
        logger.info(f"  正值胜率 (IC>0): {(sub['rank_ic']>0).mean()*100:.1f}%")

    # 配对对比
    compare = df_results.pivot_table(
        index=['date'], columns='model',
        values=['rank_ic', 'spread_bps', 'top20_avg']
    )
    v6_ic = compare['rank_ic']['v6'].dropna()
    v62_ic = compare['rank_ic']['v62'].dropna()
    common_dates = v6_ic.index.intersection(v62_ic.index)
    v6_ic = v6_ic[common_dates]
    v62_ic = v62_ic[common_dates]
    delta_ic = v62_ic - v6_ic
    logger.info(f"\n--- 配对对比 (共同交易日: {len(common_dates)}) ---")
    logger.info(f"  V6   Rank IC 均值: {v6_ic.mean():.4f}")
    logger.info(f"  V6.2 Rank IC 均值: {v62_ic.mean():.4f}")
    logger.info(f"  Delta IC: {delta_ic.mean():.4f} ({delta_ic.mean()/v6_ic.mean()*100:+.1f}%)")
    logger.info(f"  V6.2 胜出天数: {(delta_ic>0).sum()}/{len(delta_ic)} ({(delta_ic>0).mean()*100:.1f}%)")

    # Spread 对比
    v6_spread = compare['spread_bps']['v6'][common_dates]
    v62_spread = compare['spread_bps']['v62'][common_dates]
    logger.info(f"  V6   Spread 均值: {v6_spread.mean():.2f}bp")
    logger.info(f"  V6.2 Spread 均值: {v62_spread.mean():.2f}bp")

    # Top10 对比
    v6_top = compare['top20_avg']['v6'][common_dates]
    v62_top = compare['top20_avg']['v62'][common_dates]
    logger.info(f"  V6   Top20 平均收益: {v6_top.mean():.2f}%")
    logger.info(f"  V6.2 Top20 平均收益: {v62_top.mean():.2f}%")

    # 保存详细结果
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'v6_vs_v6_2_comparison.json')
    summary = {
        'test_dates': f"{dates[0]} ~ {dates[-1]}",
        'n_days': len(common_dates),
        'v6': {
            'mean_rank_ic': round(float(v6_ic.mean()), 4),
            'mean_daily_ic': round(float(v6_ic.mean()), 4),
            'ic_ir': round(float(v6_ic.mean() / (v6_ic.std() + 1e-9)), 2),
            'mean_spread_bps': round(float(v6_spread.mean()), 2),
            'mean_top20_ret': round(float(v6_top.mean()), 2),
        },
        'v6_2': {
            'mean_rank_ic': round(float(v62_ic.mean()), 4),
            'mean_daily_ic': round(float(v62_ic.mean()), 4),
            'ic_ir': round(float(v62_ic.mean() / (v62_ic.std() + 1e-9)), 2),
            'mean_spread_bps': round(float(v62_spread.mean()), 2),
            'mean_top20_ret': round(float(v62_top.mean()), 2),
        },
        'improvement': {
            'ic_delta': round(float(delta_ic.mean()), 4),
            'ic_improvement_pct': round(float(delta_ic.mean() / v6_ic.mean() * 100), 1),
            'win_rate': round(float((delta_ic > 0).mean() * 100), 1),
        }
    }
    with open(out_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"\n详细结果已保存: {out_path}")

if __name__ == '__main__':
    main()
