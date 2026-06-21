#!/usr/bin/env python3
"""
累计RankIC曲线 + 分年份验证
目标: 验证ICIR 3.87的真实可信度
"""
import logging
import sys
import os
from datetime import datetime

import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

DB_CONFIG = get_db_config()

def get_conn():
    return pymysql.connect(**DB_CONFIG)


def load_data(conn, start_date, end_date):
    """加载每日行情数据"""
    sql = """
        SELECT d.ts_code, d.trade_date, d.close, d.amount, d.turnover_rate,
               d.rps_20, d.ma5, d.ma10, d.ma20,
               b.pe_ttm, b.volume_ratio
        FROM daily_price d
        LEFT JOIN daily_basic b ON d.ts_code = b.ts_code AND d.trade_date = b.trade_date
        WHERE d.trade_date BETWEEN %s AND %s
          AND d.close > 0
          AND d.ts_code NOT LIKE '688%%'
          AND d.ts_code NOT LIKE '8%%'
          AND d.ts_code NOT LIKE '4%%'
          AND d.ts_code NOT LIKE '9%%'
        ORDER BY d.trade_date, d.ts_code
    """
    df = pd.read_sql(sql, conn, params=(start_date, end_date))
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    logger.info(f"数据: {len(df)} 行, {df['trade_date'].nunique()} 天")
    return df


def compute_factor_scores(df):
    """
    计算每日因子评分 (5因子等权,反转方向)
    - RPS反转: 近期弱势股
    - 换手率反转: 低换手股
    - 趋势反转: 回踩均线股
    """
    results = []
    dates = sorted(df['trade_date'].unique())
    
    for i, td in enumerate(dates):
        day_df = df[df['trade_date'] == td].copy()
        if len(day_df) < 50:
            continue
        
        # 因子计算
        scores = pd.DataFrame(index=day_df.index)
        
        # 1. RPS反转因子 (越低越好)
        rps = day_df['rps_20'].fillna(50)
        scores['rps_rev'] = -(rps - rps.mean()) / (rps.std() + 1e-9)
        
        # 2. 换手率反转 (越低越好)
        tr = day_df['turnover_rate'].fillna(0) * 100
        scores['tr_rev'] = -(tr - tr.median()) / (tr.std() + 1e-9)
        
        # 3. 趋势反转 (ma5 < ma20 差值为负 = 弱势)
        ma5 = day_df['ma5'].fillna(0)
        ma20 = day_df['ma20'].fillna(0)
        close = day_df['close'].fillna(0)
        diff = (ma5 - ma20) / (ma20 + 1e-9)
        scores['trend_rev'] = -diff
        
        # 4. 价格位置 (近期跌多的好)
        close_20d = []
        for idx, row in day_df.iterrows():
            td_str = td.strftime('%Y%m%d')
            prev_rows = df[(df['ts_code'] == row['ts_code']) & (df['trade_date'] < td)]
            if len(prev_rows) >= 20:
                prev_20 = prev_rows.sort_values('trade_date').tail(20)
                close_20d.append(prev_20['close'].iloc[0])
            else:
                close_20d.append(row['close'])
        day_df['close_20d'] = close_20d
        ret_20d = (day_df['close'] - day_df['close_20d']) / (day_df['close_20d'] + 1e-9)
        scores['ret_rev'] = -(ret_20d - ret_20d.mean()) / (ret_20d.std() + 1e-9)
        
        # 5. PE反转 (高PE不好, 但 PE<0 当成中性)
        pe = day_df['pe_ttm'].fillna(0)
        valid_pe = pe > 0
        pe_score = np.zeros(len(day_df))
        pe_score[valid_pe] = -(pe[valid_pe] - pe[valid_pe].median()) / (pe[valid_pe].std() + 1e-9)
        scores['pe_rev'] = pe_score
        
        # 综合评分
        day_df['factor_score'] = scores.mean(axis=1).values
        day_df['factor_rank'] = day_df['factor_score'].rank()  # 越低越弱
        
        for _, row in day_df.iterrows():
            results.append({
                'ts_code': row['ts_code'],
                'trade_date': td,
                'factor_score': row['factor_score'],
                'factor_rank': row['factor_rank']
            })
        
        if i % 50 == 0:
            logger.info(f"  [{i}/{len(dates)}] {td.date()}")
    
    return pd.DataFrame(results)


def compute_rolling_ic(factor_df, df, hold_days=5):
    """计算每日RankIC和累计RankIC"""
    dates = sorted(factor_df['trade_date'].unique())
    ic_records = []
    
    for i, td in enumerate(dates):
        idx = dates.index(td)
        if idx + hold_days >= len(dates):
            break
        
        after_td = dates[idx + hold_days]
        
        # T日因子排名
        before = factor_df[factor_df['trade_date'] == td].set_index('ts_code')['factor_rank']
        
        # T+5收益
        before_prices = df[df['trade_date'] == td].set_index('ts_code')['close']
        after_prices = df[df['trade_date'] == after_td].set_index('ts_code')['close']
        
        common = before.index.intersection(after_prices.index).intersection(before_prices.index)
        if len(common) < 10:
            continue
        
        # 计算收益率
        returns = (after_prices.loc[common] - before_prices.loc[common]) / before_prices.loc[common]
        rank_returns = returns.rank()
        rank_factor = before.loc[common].rank()
        
        # Spearman相关
        ic = rank_factor.corr(rank_returns)
        
        ic_records.append({
            'date': td,
            'ic': ic,
            'ic_abs': abs(ic)
        })
    
    ic_df = pd.DataFrame(ic_records)
    ic_df['cum_ic'] = ic_df['ic'].cumsum()
    ic_df['cum_ic_abs'] = ic_df['ic_abs'].cumsum()
    
    return ic_df


def yearly_validation(factor_df, df, hold_days=5):
    """分年份验证"""
    years = [2024, 2025, 2026]
    results = []
    
    for year in years:
        if year == 2026:
            end = '20260612'
        else:
            end = f'{year}1231'
        
        year_factor = factor_df[
            (factor_df['trade_date'] >= f'{year}0101') & 
            (factor_df['trade_date'] <= end)
        ]
        year_df = df[
            (df['trade_date'] >= f'{year}0101') & 
            (df['trade_date'] <= end)
        ]
        
        if len(year_factor) < 100:
            continue
        
        # 计算该年份IC
        dates = sorted(year_factor['trade_date'].unique())
        ics = []
        
        for i, td in enumerate(dates):
            idx = dates.index(td)
            if idx + hold_days >= len(dates):
                break
            
            after_td = dates[idx + hold_days]
            before = year_factor[year_factor['trade_date'] == td].set_index('ts_code')['factor_rank']
            before_prices = year_df[year_df['trade_date'] == td].set_index('ts_code')['close']
            after_prices = year_df[year_df['trade_date'] == after_td].set_index('ts_code')['close']
            
            common = before.index.intersection(after_prices.index).intersection(before_prices.index)
            if len(common) < 10:
                continue
            
            returns = (after_prices.loc[common] - before_prices.loc[common]) / before_prices.loc[common]
            ic = before.loc[common].rank().corr(returns.rank())
            ics.append(ic)
        
        if ics:
            ic_mean = np.mean(ics)
            ic_std = np.std(ics)
            icir = ic_mean / ic_std if ic_std > 0 else 0
            results.append({
                'year': year,
                'n_days': len(ics),
                'ic_mean': ic_mean,
                'ic_std': ic_std,
                'icir': icir,
                'ic>0_pct': sum(1 for x in ics if x > 0) / len(ics)
            })
    
    return pd.DataFrame(results)


def plot_cum_ic(ic_df, out_path):
    """生成ASCII累计IC图"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    
    fig, axes = plt.subplots(2, 1, figsize=(14, 8))
    
    # 上图: 累计RankIC
    ax1 = axes[0]
    dates = ic_df['date'].astype(str)
    ax1.plot(range(len(ic_df)), ic_df['cum_ic'].values, 'b-', linewidth=1.5, label='累计RankIC')
    ax1.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax1.fill_between(range(len(ic_df)), 0, ic_df['cum_ic'].values, alpha=0.3)
    ax1.set_title('累计 RankIC 曲线 (2024-2026)', fontsize=14)
    ax1.set_xlabel('交易日')
    ax1.set_ylabel('累计 RankIC')
    ax1.grid(True, alpha=0.3)
    
    # 标记关键点
    max_idx = ic_df['cum_ic'].idxmax()
    min_idx = ic_df['cum_ic'].idxmin()
    ax1.scatter([max_idx], [ic_df.loc[max_idx, 'cum_ic']], color='green', s=50, zorder=5)
    ax1.scatter([min_idx], [ic_df.loc[min_idx, 'cum_ic']], color='red', s=50, zorder=5)
    
    # 下图: 滚动IC (20日均值)
    ax2 = axes[1]
    ic_df['ic_rolling'] = ic_df['ic'].rolling(20).mean()
    ax2.plot(range(len(ic_df)), ic_df['ic_rolling'].values, 'orange', linewidth=1.5, label='20日滚动IC均值')
    ax2.axhline(y=0, color='gray', linestyle='--', alpha=0.5)
    ax2.scatter(range(len(ic_df)), ic_df['ic'].values, alpha=0.3, s=5, color='blue', label='每日IC')
    ax2.set_title('每日IC vs 20日滚动均值', fontsize=14)
    ax2.set_xlabel('交易日')
    ax2.set_ylabel('RankIC')
    ax2.grid(True, alpha=0.3)
    ax2.legend()
    
    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    logger.info(f"图片已保存: {out_path}")
    plt.close()


def main():
    logger.info("=" * 60)
    logger.info("ICIR可信度验证: 累计RankIC曲线 + 分年份验证")
    logger.info("=" * 60)
    
    conn = get_conn()
    
    # 加载2024-2026数据
    logger.info("\n加载数据 2024-01-01 ~ 2026-06-12...")
    df = load_data(conn, '20240101', '20260612')
    conn.close()
    
    # 计算因子评分
    logger.info("\n计算因子评分...")
    factor_df = compute_factor_scores(df)
    logger.info(f"因子评分: {len(factor_df)} 条")
    
    # 计算滚动IC和累计IC
    logger.info("\n计算RankIC...")
    hold_days = 5
    ic_df = compute_rolling_ic(factor_df, df, hold_days=hold_days)
    
    # 整体ICIR
    ic_mean = ic_df['ic'].mean()
    ic_std = ic_df['ic'].std()
    icir = ic_mean / ic_std if ic_std > 0 else 0
    
    logger.info(f"\n整体 RankIC 统计:")
    logger.info(f"  总交易日数: {len(ic_df)}")
    logger.info(f"  RankIC 均值: {ic_mean:.4f}")
    logger.info(f"  RankIC 标准差: {ic_std:.4f}")
    logger.info(f"  ICIR: {icir:.4f}")
    logger.info(f"  IC>0 比例: {(ic_df['ic'] > 0).mean()*100:.1f}%")
    logger.info(f"  累计RankIC (终点): {ic_df['cum_ic'].iloc[-1]:.2f}")
    
    # 分年份验证
    logger.info("\n分年份验证:")
    year_results = yearly_validation(factor_df, df, hold_days=hold_days)
    for _, row in year_results.iterrows():
        logger.info(f"  {int(row['year'])}年: IC均值={row['ic_mean']:.4f}, ICIR={row['icir']:.2f}, IC>0比例={row['ic>0_pct']*100:.0f}%, 天数={int(row['n_days'])}")
    
    # 生成图片
    out_path = os.path.expanduser('~/.openclaw/workspace/memory/icir_validation_cum_ic.png')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    plot_cum_ic(ic_df, out_path)
    
    # 保存结果到JSON
    import json
    result_path = os.path.expanduser('~/.openclaw/workspace/memory/icir_validation_result.json')
    result = {
        'overall': {
            'n_days': len(ic_df),
            'ic_mean': float(ic_mean),
            'ic_std': float(ic_std),
            'icir': float(icir),
            'ic_positive_pct': float((ic_df['ic'] > 0).mean()),
            'cum_ic_final': float(ic_df['cum_ic'].iloc[-1])
        },
        'yearly': year_results.to_dict('records')
    }
    with open(result_path, 'w') as f:
        json.dump(result, f, indent=2, default=str)
    
    logger.info(f"\n结果已保存: {result_path}")
    logger.info(f"图片已保存: {out_path}")
    
    # 生成Markdown报告
    md = f"""# ICIR 可信度验证结果 - {datetime.now().strftime('%Y-%m-%d')}

## 整体统计 (2024-01-01 ~ 2026-06-12)
| 指标 | 值 | 说明 |
|------|-----|------|
| 总交易日数 | {len(ic_df)} | 足够样本 |
| RankIC 均值 | {ic_mean:.4f} | >0.03有效 |
| RankIC 标准差 | {ic_std:.4f} | |
| **ICIR** | **{icir:.2f}** | >0.5及格, >1.0优秀 |
| IC>0 比例 | {(ic_df['ic'] > 0).mean()*100:.1f}% | 越高越好 |
| 累计RankIC终点 | {ic_df['cum_ic'].iloc[-1]:.2f} | >0表示因子整体有效 |

## 分年份验证

| 年份 | RankIC均值 | ICIR | IC>0比例 | 天数 |
|------|-----------|------|---------|------|
"""
    for _, row in year_results.iterrows():
        icir_val = row['icir']
        flag = '✅' if icir_val > 0.5 else '⚠️'
        md += f"| {int(row['year'])} | {row['ic_mean']:.4f} | {flag} {icir_val:.2f} | {row['ic>0_pct']*100:.0f}% | {int(row['n_days'])} |\n"
    
    md += f"""
## 图片
![累计RankIC曲线](icir_validation_cum_ic.png)

## 结论

**ICIR {icir:.2f} 可信度评估**:

"""
    if icir > 1.0:
        md += "- ICIR > 1.0: **信号稳定，可信度高** ✅\n"
    elif icir > 0.5:
        md += "- ICIR > 0.5: **信号中等稳定，参考可用** ⚠️\n"
    else:
        md += "- ICIR < 0.5: **信号不稳定，需谨慎** ❌\n"
    
    if ic_df['cum_ic'].iloc[-1] > 0:
        md += "- 累计RankIC终点 > 0: **因子整体有效** ✅\n"
    else:
        md += "- 累计RankIC终点 < 0: **因子整体无效** ❌\n"
    
    yearly_icirs = year_results['icir'].tolist()
    if len(yearly_icirs) >= 2:
        variance = np.std(yearly_icirs)
        if variance < 1.0:
            md += "- 各年份ICIR方差 < 1.0: **跨年稳定性好** ✅\n"
        else:
            md += "- 各年份ICIR方差较大: **跨年稳定性差** ⚠️\n"
    
    md_path = os.path.expanduser('~/.openclaw/workspace/memory/icir_validation_report.md')
    with open(md_path, 'w') as f:
        f.write(md)
    
    logger.info(f"\n报告已保存: {md_path}")
    logger.info("=" * 60)
    
    return result


if __name__ == '__main__':
    main()