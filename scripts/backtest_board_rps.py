#!/usr/bin/env python3
"""
板 RPS90 纯动量回测 — 零泄露
只测试"买Top5 RPS板块成分股等权持有"这个策略本身
"""
import sys, os, json, logging
import numpy as np
import pymysql
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config
from quant_app.services.board_rps_scanner import get_board_rps

DB_CONFIG = get_db_config()
START_DATE = "2026-02-01"  # board数据从1月起, 2月才有足够历史
END_DATE = "2026-06-09"
SAMPLE_INTERVAL = 5  # 每5个交易日采样
HOLD_DAYS = 5


def get_trade_dates():
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""SELECT DISTINCT trade_date FROM daily_price
        WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date""", (START_DATE, END_DATE))
    dates = sorted([r[0] for r in cur.fetchall()])
    conn.close()
    return dates


def get_top_stocks(as_of_date, top_n_boards=5):
    """获取Top板块成分股"""
    from quant_app.services.board_rps_scanner import get_top_board_stocks
    try:
        c = get_top_board_stocks(top_n_boards=top_n_boards, as_of_date=as_of_date)
        return c['ts_codes'], c['board_names'], c['board_rps']
    except:
        return [], [], []


def forward_rets(conn, codes, buy_date, hold=HOLD_DAYS):
    """含T+1的未来持有期收益"""
    rets = []
    for tc in codes:
        cur = conn.cursor()
        cur.execute("""SELECT pct_chg FROM daily_price
            WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s""",
            (tc, buy_date, hold))
        vals = [r[0] for r in cur.fetchall() if r[0] is not None]
        cur.close()
        if len(vals) >= 1:
            cum = float(np.prod(1 + np.array(vals) / 100) - 1) * 100
            rets.append(cum)
    return rets


def main():
    dates = get_trade_dates()
    sample_dates = dates[::SAMPLE_INTERVAL]
    sample_dates = [d for d in sample_dates if d > dates[5]]
    logger.info(f"交易日:{len(dates)} 采样点:{len(sample_dates)} ({sample_dates[0]}~{sample_dates[-1]})")

    conn = pymysql.connect(**DB_CONFIG)
    results_board = []
    results_top3 = []
    board_history = []

    for di, buy_date in enumerate(sample_dates):
        future = [d for d in dates if d > buy_date]
        if len(future) < HOLD_DAYS:
            continue

        codes, names, rps_vals = get_top_stocks(buy_date)
        if len(codes) < 3:
            continue

        # A) 等权所有成分股
        rets_all = forward_rets(conn, codes, buy_date)
        if rets_all:
            avg = round(float(np.mean(rets_all)), 2)
            results_board.append({'date': str(buy_date)[:10], 'n': len(rets_all), 'avg_ret': avg,
                                  'boards': names[:3]})

        # B) 直接取板块成分股中成交额最高的3只(不做ML排序, 作为基线)
        cur = conn.cursor()
        ph = ','.join(['%s'] * len(codes))
        cur.execute(f"""SELECT ts_code FROM daily_price
            WHERE ts_code IN ({ph}) AND trade_date=%s
            ORDER BY amount DESC LIMIT 3""", (*codes, buy_date))
        top3_vol = [r[0] for r in cur.fetchall()]
        cur.close()
        if top3_vol:
            rets_top3 = forward_rets(conn, top3_vol, buy_date)
            if rets_top3:
                results_top3.append({'date': str(buy_date)[:10], 'n': len(rets_top3),
                                     'avg_ret': round(float(np.mean(rets_top3)), 2),
                                     'codes': top3_vol, 'boards': names[:3]})

        # 记录板块信息
        board_history.append({'date': str(buy_date)[:10], 'boards': names, 'rps': rps_vals})

        if (di + 1) % 3 == 0:
            logger.info(f"进度:{di+1}/{len(sample_dates)} 板={len(results_board)} Top3={len(results_top3)}")

    conn.close()

    # ====== 结果 ======
    print(f"\n{'='*65}")
    print(f"板RPS90 纯动量回测 ({START_DATE} ~ {END_DATE})")
    print(f"  每{SAMPLE_INTERVAL}天采样, 持有{HOLD_DAYS}天, 不含滑点/手续费")
    print(f"{'='*65}")

    def m(label, store):
        if not store: return
        r = np.array([x['avg_ret'] for x in store])
        wins = int((r > 0).sum()); n = len(r)
        cum = float((1+r/100).prod()-1)*100
        avg = float(r.mean()); std = float(r.std())
        shp = float(avg/std*np.sqrt(252/HOLD_DAYS)) if std > 0 else 0
        dd = float(min(0, (r/100).min()))
        pl = -(r[r<0].mean()/r[r>0].mean()) if (r[r<0].size>0 and r[r>0].size>0) else 0
        print(f"\n  {label} ({n}笔):")
        print(f"    累积:+{cum:>7.2f}%  均值:{avg:>+6.2f}%  胜率:{wins/n*100:.0f}%  夏普:{shp:.2f}")
        print(f"    盈亏比:{pl:>.2f}  回撤:{dd*100:.1f}%  最佳:{r.max():>+6.2f}%  最差:{r.min():>+6.2f}%")

    m("A) 板RPS等权(全部成分股)", results_board)
    m("B) 板RPS+成交额Top3", results_top3)

    # 输出每个采样点的板块变化
    print(f"\n\n  各采样点Top板块:")
    for h in board_history:
        b = [f"{n}(RPS{r:.0f})" for n, r in zip(h['boards'], h['rps'])]
        print(f"    {h['date']}: {', '.join(b[:3])}")

    # 保存
    json.dump({'params':{'interval':SAMPLE_INTERVAL,'hold_days':HOLD_DAYS},
               'board': results_board, 'top3': results_top3, 'board_history': board_history},
              open(os.path.join(os.path.dirname(__file__),'..','data','backtest_board_rps.json'),'w'),
              indent=2, default=str)
    logger.info("已保存: data/backtest_board_rps.json")


if __name__ == '__main__':
    main()
