#!/usr/bin/env python3
"""
ICIR可信度验证 - 高效版 v2
"""
import logging
import sys
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pymysql
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()

def get_conn():
    return pymysql.connect(**DB_CONFIG)


def main():
    logger.info("=" * 60)
    logger.info("ICIR可信度验证 - 2024~2026分年份")
    logger.info("=" * 60)
    
    conn = get_conn()
    hold_days = 5
    
    years = ['2024', '2025', '2026']
    all_results = []
    
    for year in years:
        if year == '2026':
            end_date = '20260612'
        else:
            end_date = f'{year}1231'
        
        logger.info(f"\n处理 {year} 年...")
        
        # 随机抽样500只股票
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT ts_code FROM daily_price 
            WHERE trade_date >= '20240101' AND close > 0
            AND ts_code NOT LIKE '688%%' AND ts_code NOT LIKE '8%%'
            LIMIT 800
        """)
        sample_stocks = [r[0] for r in cur.fetchall()]
        cur.close()
        
        if not sample_stocks:
            logger.warning(f"{year}年无抽样股票")
            continue
        
        placeholders = ','.join(['%s'] * len(sample_stocks))
        
        # 取该年每日数据和未来5日收益
        sql = f"""
            SELECT a.trade_date, a.ts_code, a.rps_20, a.turnover_rate,
                   (a.close - a.ma20)/a.ma20 as ma_diff, b.pe_ttm, 
                   a.close as price_now
            FROM daily_price a
            LEFT JOIN daily_basic b ON a.ts_code = b.ts_code AND a.trade_date = b.trade_date
            WHERE a.trade_date BETWEEN '{year}0101' AND '{end_date}'
              AND a.close > 0 AND a.ma20 > 0
              AND a.ts_code IN ({placeholders})
            ORDER BY a.trade_date, a.ts_code
        """
        df = pd.read_sql(sql, conn, params=sample_stocks)
        
        if df.empty:
            logger.warning(f"{year}年无数据")
            continue
        
        logger.info(f"  数据: {len(df)} 行, {df['trade_date'].nunique()} 天")
        
        # 构建 future_price 映射: (ts_code, trade_date) -> future_price
        # 用shift模拟LEAD
        df = df.sort_values(['ts_code', 'trade_date'])
        df['price_future'] = df.groupby('ts_code')['price_now'].shift(-hold_days)
        df = df.dropna(subset=['price_future'])
        
        if len(df) < 100:
            logger.warning(f"{year}年有效数据不足")
            continue
        
        # 计算因子评分
        df['rps_s'] = (df['rps_20'].fillna(50) - 50) / (df['rps_20'].std() + 1e-9)
        df['tr_s'] = (df['turnover_rate'].fillna(0) - df['turnover_rate'].median()) / (df['turnover_rate'].std() + 1e-9)
        df['ma_s'] = -df['ma_diff']  # 均线下方=弱势,取负后高分
        pe_val = df['pe_ttm'].fillna(0).clip(-200, 200)
        df['pe_s'] = -pe_val  # 高PE负分
        
        # 综合评分
        df['factor_score'] = df[['rps_s', 'tr_s', 'ma_s', 'pe_s']].mean(axis=1)
        df['factor_rank'] = df.groupby('trade_date')['factor_score'].rank()
        
        # 未来收益
        df['future_ret'] = (df['price_future'] - df['price_now']) / df['price_now']
        
        # 每日IC
        ic_records = []
        for td, grp in df.groupby('trade_date'):
            if len(grp) < 20:
                continue
            ic, _ = stats.spearmanr(grp['factor_rank'], grp['future_ret'])
            if not np.isnan(ic):
                ic_records.append({'date': td, 'ic': ic})
        
        ic_df = pd.DataFrame(ic_records)
        
        if len(ic_df) > 5:
            ic_mean = ic_df['ic'].mean()
            ic_std = ic_df['ic'].std()
            icir = ic_mean / ic_std if ic_std > 0.001 else 0
            ic_pos_pct = (ic_df['ic'] > 0).mean()
            cum_ic = ic_df['ic'].sum()
            
            logger.info(f"  RankIC均值: {ic_mean:.4f}")
            logger.info(f"  RankIC标准差: {ic_std:.4f}")
            logger.info(f"  ICIR: {icir:.2f}")
            logger.info(f"  IC>0比例: {ic_pos_pct*100:.1f}%")
            logger.info(f"  累计RankIC: {cum_ic:.2f}")
            
            all_results.append({
                'year': year,
                'n_days': len(ic_df),
                'ic_mean': ic_mean,
                'ic_std': ic_std,
                'icir': icir,
                'ic_pos_pct': ic_pos_pct,
                'cum_ic': cum_ic
            })
    
    conn.close()
    
    # 汇总
    logger.info("\n" + "=" * 60)
    logger.info("汇总结果")
    logger.info("=" * 60)
    
    res_df = pd.DataFrame(all_results)
    if not res_df.empty:
        for _, row in res_df.iterrows():
            flag = '✅' if row['icir'] > 0.5 else '⚠️' if row['icir'] > 0 else '❌'
            logger.info(f"  {row['year']}年: IC={row['ic_mean']:.4f}, ICIR={flag}{row['icir']:.2f}, IC>0={row['ic_pos_pct']*100:.0f}%, 天数={row['n_days']}")
        
        total_ic = res_df['ic_mean'].mean()
        total_std = np.sqrt((res_df['ic_std']**2).sum()) / len(res_df)
        total_icir = total_ic / total_std if total_std > 0.001 else 0
        
        logger.info(f"\n  整体加权ICIR: {total_icir:.2f}")
        
        # 可视化ASCII
        logger.info("\n累计RankIC曲线:")
        logger.info("-" * 50)
        for i, row in res_df.iterrows():
            bar_len = int(max(0, row['cum_ic']) * 5)
            bar = '█' * bar_len if bar_len > 0 else ''
            neg_len = int(max(0, -row['cum_ic']) * 5)
            neg_bar = '▓' * neg_len if neg_len > 0 else ''
            flag = '✅' if row['icir'] > 0.5 else '⚠️'
            logger.info(f"  {row['year']} | {flag}ICIR={row['icir']:5.2f} | 累计IC={row['cum_ic']:7.2f} | {bar}{neg_bar}")
        logger.info("-" * 50)
    
    # 保存结果
    import json
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/icir_validation_result.json')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(res_df.to_dict('records'), f, indent=2, default=str)
    logger.info(f"\n结果已保存: {out_path}")
    
    return res_df


if __name__ == '__main__':
    main()