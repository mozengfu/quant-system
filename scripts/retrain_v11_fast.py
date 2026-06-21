#!/usr/bin/env python3
"""
V11.0 快速重训练 (Mac 优化版)
- 只使用成交额 Top500 股票（实际交易池）
- 3-fold Walk-Forward 验证 IC
- 节省 ~90% 时间
"""
import logging
import os
import sys
import json
from datetime import datetime
import pymysql

import numpy as np
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from ml_train_v11_0 import load_data, build_features, purged_walk_forward, train_base_models
from quant_app.utils.config import get_db_config

TOP_STOCKS = 500  # 只取成交额Top500

def get_top_stock_codes(max_date=None):
    """获取历史平均成交额最高的TOP_STOCKS只股票代码"""
    import pymysql
    from quant_app.utils.config import get_db_config

    conn = pymysql.connect(**get_db_config())
    cur = conn.cursor()

    if max_date:
        cur.execute(f"""
            SELECT ts_code, AVG(amount) as avg_amt
            FROM daily_price
            WHERE trade_date < '{max_date}' AND trade_date >= DATE_SUB('{max_date}', INTERVAL 60 DAY)
              AND LEFT(ts_code,1) NOT IN ('8','4','9')
            GROUP BY ts_code
            ORDER BY avg_amt DESC
            LIMIT {TOP_STOCKS}
        """)
    else:
        cur.execute(f"""
            SELECT ts_code, AVG(amount) as avg_amt
            FROM daily_price
            WHERE trade_date >= DATE_SUB((SELECT MAX(trade_date) FROM daily_price), INTERVAL 60 DAY)
              AND LEFT(ts_code,1) NOT IN ('8','4','9')
            GROUP BY ts_code
            ORDER BY avg_amt DESC
            LIMIT {TOP_STOCKS}
        """)

    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    logger.info(f"Top{TOP_STOCKS} 股票: {len(codes)} 只")
    return codes


def main():
    start = datetime.now()
    max_date = "2026-06-12"
    n_folds = 3

    logger.info(f"{'='*60}")
    logger.info("V11.0 快速重训练 (Mac Top500)")
    logger.info(f"  数据截止: {max_date}, Top{TOP_STOCKS} 股票, {n_folds}折")
    logger.info(f"{'='*60}")

    # Step 1: 获取Top500股票
    top_codes = get_top_stock_codes(max_date)
    top_codes_set = set(top_codes)

    # Step 2: 加载数据 (全量, 但后续过滤)
    data = load_data(max_date=max_date)
    if not data:
        logger.error("数据加载失败!")
        return

    (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
     margin, dragon_tiger, dragon_tiger_inst, holder_change,
     zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
     block_trade, stock_forecast,
     fina_ind, sector_mf, north_mf, ml_prev,
     limit_list, top_inst_data, regime_data,
     min_date, max_date) = data

    # Step 3: 过滤到 Top500 股票
    logger.info(f"过滤前行数: daily={len(daily):,}, moneyflow={len(moneyflow):,}")
    daily = daily[daily['ts_code'].isin(top_codes_set)].copy()
    moneyflow = moneyflow[moneyflow['ts_code'].isin(top_codes_set)].copy()
    stock_info = stock_info[stock_info['ts_code'].isin(top_codes_set)].copy()
    logger.info(f"过滤后行数: daily={len(daily):,}, moneyflow={len(moneyflow):,}, stocks={len(stock_info)}")

    data = (daily, idx_data, moneyflow, fundamentals, stock_info, alpha_signals,
            margin, dragon_tiger, dragon_tiger_inst, holder_change,
            zt_pool, board_ind_hist, board_ind_cons, board_concept_cons, earnings,
            block_trade, stock_forecast,
            fina_ind, sector_mf, north_mf, ml_prev,
            limit_list, top_inst_data, regime_data,
            min_date, max_date)

    # Step 4: 构建特征
    result = build_features(data, na_fill=False)
    if result[0].empty:
        logger.error("特征构建失败!")
        return

    features, global_medians, feature_cols, use_alpha_features = result
    logger.info(f"特征: {len(features):,} 样本, {len(feature_cols)} 特征, "
                f"{features['ts_code'].nunique()} 股票, "
                f"日期: {features['trade_date'].min()} ~ {features['trade_date'].max()}")

    # Step 4.5: 板RPS过滤训练样本 — 只保留在周线Top5板块成分股中的股票
    try:
        from quant_app.services.board_rps_scanner import compute_weekly_board_rps_history
        import pymysql
        wb_rps = compute_weekly_board_rps_history()
        if not wb_rps.empty:
            # 取每周Top5板块代码（RPS排名最高的5个）
            # compute_weekly_board_rps_history 输出有 rps 列，无 rank 列
            top5_weekly = wb_rps.copy()
            top5_weekly['yw'] = top5_weekly['year'].astype(str) + '-W' + top5_weekly['week'].astype(str)
            # 每周取rps前5（按板块代码去重后排序取前5）
            top5_weekly = top5_weekly.sort_values('rps', ascending=False).groupby('yw').head(5).copy()

            # 加载 stock → board 映射
            conn = pymysql.connect(**get_db_config())
            cur = conn.cursor()
            cur.execute("SELECT ts_code, board_code FROM board_concept_cons")
            stock_boards = {}
            for r in cur.fetchall():
                stock_boards.setdefault(r[0], set()).add(r[1])
            cur.close()
            conn.close()

            # 为每个样本判断是否在Top5板块中
            features['_yw'] = features['trade_date'].dt.isocalendar().year.astype(str) + '-W' + \
                              features['trade_date'].dt.isocalendar().week.astype(str)

            def _in_top5(row):
                yw = row['_yw']
                tc = row['ts_code']
                boards = stock_boards.get(tc, set())
                if not boards:
                    return False  # 无板块映射 → 过滤掉
                week_top = top5_weekly[top5_weekly['yw'] == yw]
                return week_top['board_code'].isin(boards).any()

            before = len(features)
            mask = features.apply(_in_top5, axis=1)
            features = features[mask].copy()
            features = features.drop(columns=['_yw'], errors='ignore')
            logger.info(f"板RPS过滤: {before:,} → {len(features):,} 样本（仅保留Top5板块成分股）")
        else:
            logger.warning("板RPS数据为空，跳过过滤")
    except Exception as e:
        logger.warning(f"板RPS过滤失败（跳过）: {e}")

    # Step 5: Walk-Forward 验证
    logger.info(f"\nPurged Walk-Forward ({n_folds}折)...")
    cv_results = purged_walk_forward(
        features, feature_cols,
        n_folds=n_folds, embargo=5, val_size=60,
        compute_trades=True,
    )
    if cv_results is None:
        logger.error("Walk-Forward 验证失败!")
        return

    avg_ic = float(np.mean([r['rank_ic'] for r in cv_results]))

    print(f"\n{'='*55}")
    print("V11.0 重训练 Walk-Forward 验证结果")
    print(f"{'='*55}")
    for r in cv_results:
        flag = "✅" if r['rank_ic'] > 0.03 else ("⚠️" if r['rank_ic'] > 0 else "❌")
        print(f"  {flag} 折{r['fold']}: RankIC={r['rank_ic']:.4f} 日频IC={r['mean_daily_ic']:.4f} "
              f"ICIR={r['ic_ir']:.2f} 样本={r['n_val']:,}")
    print(f"  平均 RankIC: {avg_ic:.4f}")

    # 交易结果
    all_trades = []
    for r in cv_results:
        all_trades.extend(r.get('trades', []))
    if all_trades:
        trade_rets = np.array([t['fwd_return_pct'] for t in all_trades])
        wr = float((trade_rets > 0).mean() * 100)
        ar = float(trade_rets.mean())
        total_ret = float((1 + trade_rets / 100).prod() - 1)
        std_ret = float(trade_rets.std())
        sharpe = float(ar / std_ret * np.sqrt(252 / 5)) if std_ret > 0 else 0
        print(f"  模拟交易: {len(all_trades)}笔")
        print(f"    胜率={wr:.1f}% 均收益={ar:+.2f}% 夏普={sharpe:.2f} 累积={total_ret*100:.0f}%")

    # Step 6: 训练最终模型并保存
    logger.info(f"\n训练最终模型 ({len(features):,}样本, {len(feature_cols)}特征)...")
    final_bundle = train_base_models(features, feature_cols)
    if final_bundle:
        final_bundle['feature_cols'] = feature_cols
        final_bundle['global_medians'] = global_medians
        final_bundle['version'] = 'v11.0_mac_fast_top500'
        final_bundle['n_models'] = len(final_bundle.get('models', []))
        final_bundle['n_features'] = len(feature_cols)
        final_bundle['n_samples'] = len(features)
        final_bundle['n_stocks'] = int(features['ts_code'].nunique())
        final_bundle['data_range'] = f"{min_date.date()} ~ {max_date.date()}"
        final_bundle['wf_avg_rank_ic'] = round(avg_ic, 4)
        final_bundle['generated_at'] = datetime.now().isoformat()
        final_bundle['wf_cv_results'] = [
            {'fold': r['fold'], 'rank_ic': round(float(r['rank_ic']), 4),
             'ic_ir': round(float(r['ic_ir']), 2), 'n_val': r['n_val']}
            for r in cv_results
        ]

        out_path = os.path.join(BASE_DIR, 'data', 'ml_stock_model_v11_0_mac_retrain.pkl')
        import joblib
        joblib.dump(final_bundle, out_path)
        logger.info(f"模型已保存: {out_path} ({os.path.getsize(out_path)/1024/1024:.0f}MB)")
    else:
        logger.error("模型训练失败!")

    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"总耗时: {elapsed:.1f}s ({elapsed/60:.1f}min)")


if __name__ == '__main__':
    main()
