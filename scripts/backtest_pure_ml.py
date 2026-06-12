#!/usr/bin/env python3
"""纯ML回测（含风控过滤+游资排除+业绩过滤）"""
import json
import os
import sys
import time
import warnings

warnings.filterwarnings('ignore')
import numpy as np
import pymysql
from scipy.stats import ttest_ind

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from ml_predict import _build_features_for_stocks_v6_3, _ensemble_predict, _load_best_model
from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()
START_DATE, END_DATE = "2025-10-01", "2026-05-15"
SAMPLE_INTERVAL, TOP_N, HOLD_DAYS = 5, 5, 5
MIN_VOL_STOCKS = 200
OUT_PATH = os.path.join(BASE_DIR, 'data', 'backtest_v4_pool.json')


def get_trade_dates(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date", (START_DATE, END_DATE))
    dates = sorted([r[0] for r in cur.fetchall()])
    cur.close()
    return dates


def get_top_vol_stocks(conn, date_str, n=500):
    cur = conn.cursor()
    try:
        cur.execute("SELECT ts_code FROM daily_price WHERE trade_date=%s AND LEFT(ts_code,1) NOT IN ('8','4','9') AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '43%%' AND close<=200 ORDER BY amount DESC LIMIT %s", (date_str, n))
    except:
        cur.execute("SELECT ts_code FROM daily_price WHERE trade_date=%s AND LEFT(ts_code,1) NOT IN ('8','4','9') AND ts_code NOT LIKE '83%%' AND ts_code NOT LIKE '43%%' AND close<=200 ORDER BY vol*close DESC LIMIT %s", (date_str, n))
    codes = [r[0] for r in cur.fetchall()]
    cur.close()
    return codes


def forward_return(conn, code, date_str, hold=5):
    cur = conn.cursor()
    cur.execute("SELECT pct_chg FROM daily_price WHERE ts_code=%s AND trade_date>=%s ORDER BY trade_date LIMIT %s", (code, date_str, hold+1))
    rows = cur.fetchall()
    cur.close()
    if len(rows) < 2: return None
    rets = [float(r[0])/100.0 for r in rows[1:hold+1] if r[0] is not None]
    rets = [r for r in rets if not np.isnan(r)]
    if len(rets) == 0: return None
    cost = 0.0003*2 + 0.001*2
    return float((1+np.array(rets)).prod()-1-cost)*100


def filter_stocks(conn, date_str, candidates, feat_df):
    """
    对纯ML选出的Top5候选股进行：
    1. 风控过滤（涨停追高、异常放量）
    2. 游资出货评分（≥40分排除）
    3. 业绩暴雷过滤（利润同比<-30%）
    返回过滤后的列表
    """
    if not candidates:
        return []

    ts_codes = [c['ts_code'] for c in candidates]
    name_map = {c['ts_code']: c['name'] for c in candidates}

    # 1. 建一个code→row的映射
    code_to_row = {}
    if feat_df is not None and not feat_df.empty:
        for _, row in feat_df.iterrows():
            code_to_row[row['ts_code']] = row

    # 2. 风控过滤
    filtered = []
    for c in candidates:
        tc = c['ts_code']
        row = code_to_row.get(tc, {})
        pct = float(row.get('pct_chg', 0) or 0)
        vr = float(row.get('volume_ratio', 0) or 0)
        risks = []
        if pct > 9:
            risks.append('涨停追高')
        if pct > 5 and vr > 5:
            risks.append('异常放量')
        if not risks:
            filtered.append(c)
    candidates = filtered
    if not candidates:
        return []

    # 3. 游资出货评分
    cur = conn.cursor()
    ph = ','.join(['%s'] * len(ts_codes))

    # 连板数据（60天内最高连板）
    cur.execute(f"""
        SELECT ts_code, COALESCE(MAX(last_board), 0) as max_board
        FROM zt_pool WHERE ts_code IN ({ph})
          AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY) AND last_board > 0
        GROUP BY ts_code
    """, (*ts_codes, date_str))
    board_map = {r[0]: r[1] or 0 for r in cur.fetchall()}

    # 封单萎缩
    cur.execute(f"""
        SELECT ts_code, trade_date, seal_amount
        FROM zt_pool WHERE ts_code IN ({ph})
          AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
        ORDER BY ts_code, trade_date DESC
    """, (*ts_codes, date_str))
    seal_map = {}
    for r in cur.fetchall():
        seal_map.setdefault(r[0], []).append(float(r[2] or 0))

    # 涨停+跌停
    cur.execute(f"""
        SELECT ts_code,
               SUM(CASE WHEN pct_chg >= 9.5 THEN 1 ELSE 0 END) as up_cnt,
               SUM(CASE WHEN pct_chg <= -9.5 THEN 1 ELSE 0 END) as down_cnt,
               MIN(CASE WHEN pct_chg >= 9.5 THEN trade_date END) as first_up,
               MAX(CASE WHEN pct_chg <= -9.5 THEN trade_date END) as last_down
        FROM daily_price WHERE ts_code IN ({ph})
          AND trade_date >= DATE_SUB(%s, INTERVAL 15 DAY) AND trade_date <= %s
        GROUP BY ts_code
    """, (*ts_codes, date_str, date_str))
    ud_map = {}
    for r in cur.fetchall():
        ud_map[r[0]] = {'up': r[1] or 0, 'down': r[2] or 0, 'first_up': str(r[3]) if r[3] else None, 'last_down': str(r[4]) if r[4] else None}

    # 5日均换手
    cur.execute(f"""
        SELECT ts_code, AVG(turnover_rate) as avg_tr, MAX(turnover_rate) as max_tr
        FROM daily_price WHERE ts_code IN ({ph})
          AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY) AND trade_date <= %s
        GROUP BY ts_code
    """, (*ts_codes, date_str, date_str))
    tr_map = {r[0]: {'avg': r[1] or 0, 'max': r[2] or 0} for r in cur.fetchall()}

    # 主力资金（10日累计）
    cur.execute(f"""
        SELECT ts_code, COALESCE(SUM(main_net), 0) as total
        FROM moneyflow_daily WHERE ts_code IN ({ph})
          AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
        GROUP BY ts_code
    """, (*ts_codes, date_str))
    main_map = {r[0]: r[1] or 0 for r in cur.fetchall()}

    # 评分
    excluded = set()
    for c in candidates:
        tc = c['ts_code']
        board = board_map.get(tc, 0)
        seals = seal_map.get(tc, [])
        ud = ud_map.get(tc, {})
        tr = tr_map.get(tc, {})
        mnet = main_map.get(tc, 0)

        score = 0
        reasons = []

        if board >= 4:
            score += 30; reasons.append(f'高连板{int(board)}')
        elif board == 3:
            score += 15; reasons.append('连板3次')

        if len(seals) >= 2 and seals[1] > 0 and seals[0] < seals[1] * 0.5:
            ratio = (1 - seals[0]/seals[1]) * 100
            score += 30; reasons.append(f'封单萎缩{int(ratio)}%')

        if ud.get('up', 0) > 0 and ud.get('down', 0) > 0:
            fu = ud.get('first_up')
            ld = ud.get('last_down')
            if fu and ld and fu < ld:
                score += 20; reasons.append('涨停后跌停')

        avg_tr = tr.get('avg', 0)
        max_tr = tr.get('max', 0)
        if avg_tr > 20:
            score += 15; reasons.append(f'高换手{int(avg_tr)}%')
        elif avg_tr > 15 and max_tr > 25:
            score += 10; reasons.append('换手异常')

        if mnet < -30000000:
            score += 15; reasons.append('主力流出')

        c['hm_score'] = score
        c['hm_reason'] = '; '.join(reasons)
        if score >= 40:
            excluded.add(tc)

    candidates = [c for c in candidates if c['ts_code'] not in excluded]

    # 4. 业绩暴雷过滤
    if candidates:
        remain_codes = [c['ts_code'] for c in candidates]
        cur.execute(f"""
            SELECT e.ts_code, e.net_profit_yoy
            FROM earnings_report e
            WHERE e.ts_code IN ({','.join(['%s']*len(remain_codes))})
              AND e.report_date = (
                SELECT MAX(e2.report_date) FROM earnings_report e2 WHERE e2.ts_code = e.ts_code
              )
        """, remain_codes)
        profit_map = {}
        for r in cur.fetchall():
            profit = float(r[1] or 0)
            profit_map[r[0]] = profit
        bad_codes = {tc for tc, p in profit_map.items() if p < -30}
        candidates = [c for c in candidates if c['ts_code'] not in bad_codes]

    cur.close()
    return candidates


def run_pure_ml(conn, date_str, feat_all, bundle):
    """纯ML策略：全量ML排序Top5 → 过滤"""
    top5 = []
    ml_all = _ensemble_predict(feat_all, bundle)
    ranked = sorted(zip(feat_all['ts_code'].tolist(), ml_all), key=lambda x: -x[1])
    # 取候选（Top10给过滤留空间）
    candidates = [{'ts_code': c[0], 'name': '', 'ml_score': float(c[1])} for c in ranked[:10]]

    # 获取名称
    cur = conn.cursor()
    cur.execute(f"SELECT ts_code, name FROM stock_info WHERE ts_code IN ({','.join(['%s']*len(candidates))})", [c['ts_code'] for c in candidates])
    name_map = {r[0]: r[1] for r in cur.fetchall()}
    cur.close()
    for c in candidates:
        c['name'] = name_map.get(c['ts_code'], '')

    # 过滤
    passed = filter_stocks(conn, date_str, candidates, feat_all)
    if passed:
        top5 = [c['ts_code'] for c in passed[:min(5, len(passed))]]

    return top5


def run_pure_ml_no_filter(conn, date_str, feat_all, bundle):
    """纯ML无过滤：直接Top5"""
    ml_all = _ensemble_predict(feat_all, bundle)
    ranked = sorted(zip(feat_all['ts_code'].tolist(), ml_all), key=lambda x: -x[1])
    return [c for c, _ in ranked[:TOP_N]]


def main():
    bundle, version = _load_best_model()
    if not bundle:
        print("没有可用模型！"); sys.exit(1)
    print(f"模型: {version} | {bundle.get('n_features','?')}特征 | Top{TOP_N} | 持有{HOLD_DAYS}天")

    conn = pymysql.connect(**DB_CONFIG)
    all_dates = get_trade_dates(conn)
    sample_dates = all_dates[::SAMPLE_INTERVAL]
    print(f"采样: {len(sample_dates)}天 ({sample_dates[0]} ~ {sample_dates[-1]})")

    pure_ml_filtered, pure_ml_raw = [], []
    t_start = time.time()
    skipped_no_candidates = 0

    for di, date_str in enumerate(sample_dates):
        if (di+1) % 5 == 0:
            print(f"进度: {di+1}/{len(sample_dates)} [{time.time()-t_start:.0f}s]", flush=True)

        vol_codes = get_top_vol_stocks(conn, date_str, 500)
        if len(vol_codes) < MIN_VOL_STOCKS: continue

        try:
            feat_all = _build_features_for_stocks_v6_3(conn, vol_codes, as_of_date=date_str)
        except:
            continue
        if feat_all is None or feat_all.empty or len(feat_all) < 100: continue

        # 带过滤的纯ML
        filtered_top = run_pure_ml(conn, date_str, feat_all, bundle)
        # 无过滤的纯ML（对照）
        raw_top = run_pure_ml_no_filter(conn, date_str, feat_all, bundle)

        for top_codes, store in [(filtered_top, pure_ml_filtered), (raw_top, pure_ml_raw)]:
            rets = [forward_return(conn, tc, date_str, HOLD_DAYS) for tc in top_codes]
            rets = [r for r in rets if r is not None]
            if rets:
                store.append({'date': date_str, 'n': len(rets), 'avg_ret': round(np.mean(rets), 2)})
            elif not top_codes:
                skipped_no_candidates += 1

    conn.close()
    elapsed = time.time() - t_start

    def print_stats(label, store):
        if not store:
            print(f"\n  {label}: 无有效交易"); return
        rets = np.array([r['avg_ret'] for r in store])
        wins = (rets > 0).sum()
        cum = float((1+rets/100).prod()-1)*100
        avg = float(rets.mean()); std = float(rets.std())
        sharpe = float(avg/std*np.sqrt(252/HOLD_DAYS)) if std > 0 else 0
        dd = float((rets/100).min())
        win_loss = (rets[rets>0].mean()/abs(rets[rets<0].mean())) if rets[rets<0].size > 0 and rets[rets>0].size > 0 else float('nan')
        years = len(store)*SAMPLE_INTERVAL/252
        annual = ((1+cum/100)**(1/years)-1)*100 if years > 0 else 0
        median = float(np.median(rets))
        print(f"\n  {label} ({len(store)}次/年化{annual:+.2f}%):")
        print(f"    累积: {cum:+7.2f}% | 均值: {avg:+6.2f}% | 中位: {median:+6.2f}%")
        print(f"    胜率: {wins/len(store)*100:5.1f}% | 盈亏比: {win_loss:6.2f} | 夏普: {sharpe:6.2f}")
        print(f"    回撤: {dd*100:6.2f}% | 最佳: {rets.max():+6.2f}% | 最差: {rets.min():+6.2f}%")

    print(f"\n{'='*70}")
    print(f"纯ML回测对比（2025-10-01 ~ 2026-05-15）耗时{elapsed:.0f}s")
    print(f"模型: {version} | Top{TOP_N} | 持有{HOLD_DAYS}天 | 全部过滤后无候选: {skipped_no_candidates}次")
    print(f"{'='*70}")

    print_stats("纯ML(无过滤)", pure_ml_raw)
    print_stats("纯ML(含风控+游资+业绩过滤)", pure_ml_filtered)

    # t-test
    r_raw = np.array([x['avg_ret'] for x in pure_ml_raw]) if pure_ml_raw else np.array([])
    r_fil = np.array([x['avg_ret'] for x in pure_ml_filtered]) if pure_ml_filtered else np.array([])
    if len(r_raw) > 0 and len(r_fil) > 0:
        t, p = ttest_ind(r_raw, r_fil)
        print(f"\n  有无过滤差异: t={t:.3f}, p={p:.4f}", end='')
        if p <= 0.05:
            print(f" ({'无过滤优' if r_raw.mean() > r_fil.mean() else '有过滤优'})")
        else:
            print(" (无显著差异)")

    # 保存
    summary = {}
    for key, label, store in [('pure_ml_raw','纯ML(无过滤)',pure_ml_raw), ('pure_ml_filtered','纯ML(含过滤)',pure_ml_filtered)]:
        if store:
            r = np.array([x['avg_ret'] for x in store])
            cum = float((1+r/100).prod()-1)*100
            summary[key] = {
                'trades': len(store), 'cum_return': round(cum, 2),
                'sharpe': round(float(r.mean()/r.std()*np.sqrt(252/HOLD_DAYS)) if r.std() > 0 else 0, 2),
                'win_rate': round(float((r>0).sum()/len(r)*100), 1),
                'annual_return': round(float(((1+cum/100)**(252/(len(store)*SAMPLE_INTERVAL))-1)*100), 2),
                'max_drawdown': round(float(r.min()), 2),
            }
    with open(OUT_PATH, 'w') as f:
        json.dump({'model': version, 'params': {'start': START_DATE, 'end': END_DATE, 'interval': SAMPLE_INTERVAL, 'top_n': TOP_N, 'hold_days': HOLD_DAYS},
                   'pure_ml_raw': pure_ml_raw, 'pure_ml_filtered': pure_ml_filtered, 'summary': summary}, f, indent=2, default=str)
    print(f"\n结果已保存: {OUT_PATH}")


if __name__ == '__main__':
    main()
