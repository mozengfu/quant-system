#!/usr/bin/env python3
"""7因子模型回测 — 5原始 + 52周低位 + 均线乖离"""
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import pymysql, numpy as np, pandas as pd, json, logging
from scipy.stats import spearmanr
from datetime import datetime
from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

FACTORS_5 = ['rps_20','turnover_rate','pb','pe_ttm','total_mv']
FACTORS_7 = ['rps_20','turnover_rate','pb','pe_ttm','total_mv','pos_52w','bias_ma20']

def main():
    conn = pymysql.connect(**get_db_config())
    df = pd.read_sql("""SELECT a.ts_code,a.trade_date,a.pct_chg,a.close,
        a.rps_20,a.turnover_rate,a.ma20,
        b.pe as pe_ttm,b.pb,b.total_mv,
        a.high_52w,a.low_52w
        FROM daily_price a JOIN daily_basic b
        ON a.ts_code=b.ts_code AND a.trade_date=b.trade_date
        WHERE a.trade_date>='2025-08-01' AND a.trade_date<'2026-05-01'
        AND a.close>0 AND b.pe>0 AND b.pe<200
        ORDER BY a.ts_code,a.trade_date""", conn)
    conn.close()

    df['ret_10d'] = df.groupby('ts_code')['pct_chg'].transform(
        lambda x: np.prod([(1+x.shift(-i-1)/100) for i in range(10)],axis=0)-1)
    df = df.dropna(subset=['ret_10d']).copy()

    # 新因子
    df['pos_52w'] = (df['close']-df['low_52w'])/(df['high_52w']-df['low_52w']+1e-9)
    df['bias_ma20'] = (df['close']-df['ma20'])/df['ma20'].replace(0,np.nan)

    dates = sorted(df['trade_date'].unique())
    logger.info(f"数据: {len(df):,}行 {len(dates)}天")

    results = {}
    for label, factors in [('5因子',FACTORS_5),('7因子',FACTORS_7)]:
        folds = []
        for vs, ve in [(0,35),(35,70),(70,105),(105,140),(140,len(dates))]:
            train = df[df['trade_date'].isin(dates[:vs])]
            val = df[df['trade_date'].isin(dates[vs:ve])].copy()
            if len(val) < 500: continue

            s = pd.DataFrame(index=val.index)
            for f in factors:
                mu, sig = train[f].median(), train[f].std()
                s[f] = -(val[f]-mu)/(sig+1e-9)
            val['score'] = s.mean(axis=1)

            ic, _ = spearmanr(val['score'], val['ret_10d'])
            di = val.groupby('trade_date').apply(
                lambda g: spearmanr(g['score'],g['ret_10d'])[0] if len(g)>10 else np.nan,
                include_groups=False).dropna()
            ir = di.mean()/di.std() if len(di)>3 and di.std()>0 else 0

            trades = []
            for d in sorted(val['trade_date'].unique()):
                picks = val[val['trade_date']==d].nlargest(3, 'score')
                trades.extend(picks['ret_10d'].tolist())
            rets = np.array(trades)
            wr = (rets>0).mean()*100 if len(rets)>0 else 0
            ar = rets.mean()*100 if len(rets)>0 else 0

            folds.append({'ic':round(ic,4),'ir':round(ir,2),'wr':round(wr,1),'ar':round(ar,2)})

        results[label] = folds

    print(f"\n{'='*60}")
    print("对比: 5因子 vs 7因子(+52周低位+均线乖离)")
    print(f"{'='*60}")
    print(f"{'折':>6s} {'5因子IC':>8s} {'7因子IC':>8s} {'5因子胜率':>10s} {'7因子胜率':>10s} {'5因子收益':>10s} {'7因子收益':>10s}")
    print("-"*62)
    for i in range(min(len(results['5因子']), len(results['7因子']))):
        r5 = results['5因子'][i]
        r7 = results['7因子'][i]
        print(f"{'折'+str(i+1):>6s} {r5['ic']:>+8.4f} {r7['ic']:>+8.4f} {r5['wr']:>8.1f}% {r7['wr']:>8.1f}% {r5['ar']:>+8.2f}% {r7['ar']:>+8.2f}%")

    avg5 = {k: np.mean([r[k] for r in results['5因子']]) for k in ['ic','wr','ar']}
    avg7 = {k: np.mean([r[k] for r in results['7因子']]) for k in ['ic','wr','ar']}
    print(f"{'平均':>6s} {avg5['ic']:>+8.4f} {avg7['ic']:>+8.4f} {avg5['wr']:>8.1f}% {avg7['wr']:>8.1f}% {avg5['ar']:>+8.2f}% {avg7['ar']:>+8.2f}%")

    # 保存
    out = Path(__file__).parent / "data" / "backtest_factor7.json"
    with open(out,'w') as f:
        json.dump({'results_5factor':results['5因子'],'results_7factor':results['7因子']}, f, indent=2)
    logger.info(f"已保存: {out}")

if __name__ == '__main__':
    main()
