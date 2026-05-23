#!/usr/bin/env python3
"""
每日 ML 全市场预测 — 写入 MySQL ml_predictions 表 + parquet 缓存
等价于原来不存在的 scripts/daily_ml_predict.py
"""

import os, sys, logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np, pandas as pd, pymysql
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config
from ml_predict import predict_batch

DB_CONFIG = get_db_config()


def main():
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # 获取最新交易日期和全市场股票列表
    cursor.execute("SELECT MAX(trade_date) FROM daily_price")
    latest_date = cursor.fetchone()[0]
    if not latest_date:
        logger.error("无交易数据")
        sys.exit(1)

    date_str = str(latest_date)
    logger.info(f"最新交易日期: {date_str}")

    cursor.execute(
        "SELECT ts_code FROM daily_price WHERE trade_date = %s",
        (date_str,)
    )
    all_codes = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    logger.info(f"全市场股票数: {len(all_codes)}")

    # 调用 ML 预测
    logger.info("开始 ML 预测...")
    results = predict_batch(all_codes, as_of_date=latest_date)
    logger.info(f"预测完成: {len(results)} 只")

    # 排序
    scored = [
        (tc, p['probability'], p['predicted_return'], p['model_type'])
        for tc, p in results.items()
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    # 写入 MySQL ml_predictions 表
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM ml_predictions WHERE trade_date = %s", (date_str,))
    for tc, prob, ret, mt in scored:
        cursor.execute(
            "INSERT INTO ml_predictions (ts_code, trade_date, _ml_pred, predicted_return, model_type) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tc, date_str, prob, ret, mt)
        )
    conn.commit()
    logger.info(f"写入 ml_predictions 表: {len(scored)} 条")

    # 写入 parquet 缓存
    df = pd.DataFrame(scored, columns=['ts_code', '_ml_pred', 'predicted_return', 'model_type'])
    df['trade_date'] = date_str
    parquet_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', f'ml_preds_{date_str}.parquet'
    )
    df.to_parquet(parquet_path, index=False)
    logger.info(f"写入 parquet: {parquet_path}")

    # 输出 Top 15
    top15 = scored[:15]
    logger.info(f"Top 15 ({date_str}):")
    for i, (tc, prob, ret, mt) in enumerate(top15, 1):
        logger.info(f"  {i:2d}. {tc}  prob={prob:.3f}  ret={ret:.4f}  model={mt}")

    cursor.close()
    conn.close()
    logger.info("完成")


if __name__ == '__main__':
    main()
