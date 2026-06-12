#!/usr/bin/env python3
"""
5因子等权模型回测 — 零泄露
因子: PB低估 + 换手率反转 + PE低估 + 小市值 + RPS反转
权重: 等权
标签: T+10日前向收益
"""
import sys, os, json, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import numpy as np, pandas as pd, pymysql
from scipy.stats import spearmanr
from datetime import datetime
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

FACTORS = ['rps_20','turnover_rate','pb','pe_ttm','total_mv']  # 全部负向
N_FOLDS = 5

def main():
    start = datetime.now()
    conn = pymysql.connect(**get_db_config())
    df = pd.read_sql("""SELECT a.ts_code,a.trade_date,a.pct_chg,a.rps_20,a.turnover_rate,b.pb,b.pe as pe_ttm,b.total_mv FROM daily_price a JOIN daily_basic b ON a.ts_code=b.ts_code AND a.trade_date=b.trade_date WHERE a.trade_date>='2025-08-01' AND a.trade_date<'2026-05-01' AND a.close>0 AND b.pe>0 AND b.pe<200 ORDER BY a.ts_code,a.trade_date""", conn)
    conn.close()
    logger.info(f"数据: {len(df):,} 行")

    df['ret_10d'] = df.groupby('ts_code')['pct_chg'].transform(
        lambda x: np.prod([(1+x.shift(-i-1)/100) for i in range(10)], axis=0) - 1)
    df = df.dropna(subset=['ret_10d'])
    logger.info(f"有T+10收益: {len(df):,}")

    dates = sorted(df['trade_date'].unique())
    val_size = len(dates) // (N_FOLDS + 1)
    results = []

    for fold in range(N_FOLDS):
        vs = (fold + 1) * val_size
        ve = min(vs + val_size, len(dates))
        if fold == N_FOLDS - 1: ve = len(dates)

        train = df[df['trade_date'].isin(dates[:vs])]
        val = df[df['trade_date'].isin(dates[vs:ve])]
        if len(val) < 1000: continue

        # 5因子等权打分
        s = pd.DataFrame(index=val.index)
        for f in FACTORS:
            mu, sigma = train[f].median(), train[f].std()
            if sigma < 0.0001: continue
            s[f] = -(val[f] - mu) / sigma  # 负向因子取负Z值
        val['score'] = s.mean(axis=1)

        # IC
        ic, _ = spearmanr(val['score'], val['ret_10d'])
        di = val.groupby('trade_date').apply(
            lambda g: spearmanr(g['score'],g['ret_10d'])[0] if len(g)>10 else np.nan,
            include_groups=False).dropna()
        ir = di.mean()/di.std() if len(di)>3 and di.std()>0 else 0

        # 模拟交易
        trades = []
        for d in sorted(val['trade_date'].unique()):
            day = val[val['trade_date']==d].nlargest(3, 'score')
            trades.extend(day['ret_10d'].tolist())
        rets = np.array(trades)
        wr = (rets>0).mean()*100 if len(rets)>0 else 0
        ar = rets.mean()*100 if len(rets)>0 else 0

        results.append({'fold':fold+1,'val':f"{dates[vs].strftime('%Y%m%d')}~{dates[ve-1].strftime('%Y%m%d')}",
                        'n':len(val),'ic':round(ic,4),'ir':round(ir,2),'wr':round(wr,1),'ar':round(ar,2)})
        logger.info(f"  折{fold+1}: IC={ic:.4f} IR={ir:.2f} 胜率={wr:.1f}% 均收益={ar:+.2f}%")

    # 汇总
    print(f"\n{'='*55}")
    print("5因子等权模型 — Walk-Forward 回测结果")
    print(f"{'='*55}")
    print(f"  因子: {', '.join(FACTORS)} (等权)")
    print(f"  持仓: 10个交易日")
    for r in results:
        s = '✅' if abs(r['ic'])>0.05 else ('⚠️' if abs(r['ic'])>0 else '❌')
        print(f"  {s} 折{r['fold']}: IC={r['ic']:.4f} IR={r['ir']:.2f} 胜率={r['wr']:.1f}% 均收益={r['ar']:+.2f}%")
    print(f"  平均: IC={np.mean([r['ic'] for r in results]):.4f} IR={np.mean([r['ir'] for r in results]):.2f} 胜率={np.mean([r['wr'] for r in results]):.1f}% 均收益={np.mean([r['ar'] for r in results]):+.2f}%")
    print(f"  耗时: {(datetime.now()-start).seconds}s")

    # 保存
    out = Path(__file__).parent / "data" / "backtest_factor5.json"
    with open(out, 'w') as f:
        json.dump({'params':{'factors':FACTORS,'horizon':'10日','weight':'等权','n_folds':N_FOLDS},'results':results}, f, indent=2)
    logger.info(f"已保存: {out}")

if __name__ == '__main__':
    main()
