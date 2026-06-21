#!/usr/bin/env python3
"""板RPS ATR2x+固定止盈8% 详细回测"""
import sys, os, json, logging
import numpy as np
import pandas as pd
import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

from quant_app.utils.config import get_db_config
from quant_app.services.board_rps_scanner import get_top_board_stocks

DB_CONFIG = get_db_config()
START = "2026-02-01"
END = "2026-06-09"
MAX_HOLD = 20

def atr_arr(h, l, c, p=14):
    tr = np.maximum(h[1:]-l[1:], np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1]))
    atr = np.zeros(len(c))
    if len(tr) < p: return atr
    atr[p] = tr[:p].mean()
    for i in range(p+1, len(c)):
        atr[i] = (atr[i-1]*(p-1)+tr[i-1])/p
    return atr

conn = pymysql.connect(**DB_CONFIG)
dates = sorted(pd.read_sql(f"SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>='{START}' AND trade_date<='{END}' ORDER BY trade_date", conn)['trade_date'].tolist())
samples = [d for d in dates[5:] if d > dates[5]]

trades_detail = []

for bd in samples:
    if len([d for d in dates if d > bd]) < 3: continue
    try:
        c = get_top_board_stocks(as_of_date=bd)
    except: continue
    if not c['ts_codes'] or len(c['ts_codes']) < 3: continue
    cur = conn.cursor()
    ph = ','.join(['%s']*len(c['ts_codes']))
    cur.execute(f"SELECT ts_code FROM daily_price WHERE ts_code IN ({ph}) AND trade_date=%s ORDER BY amount DESC LIMIT 3", (*c['ts_codes'], bd))
    t3 = [r[0] for r in cur.fetchall()]; cur.close()
    for tc in t3:
        cur = conn.cursor()
        cur.execute("SELECT open,high,low,close,pct_chg FROM daily_price WHERE ts_code=%s AND trade_date>%s ORDER BY trade_date LIMIT %s", (tc, str(bd)[:10], MAX_HOLD))
        rows = cur.fetchall(); cur.close()
        if len(rows) < 2: continue
        ohlc = [{'open':float(r[0]),'high':float(r[1]),'low':float(r[2]),'close':float(r[3]),'pct':float(r[4])} for r in rows if r[3] is not None]
        if len(ohlc) < 2: continue

        entry = ohlc[0]['close']
        closes = [d['close'] for d in ohlc]
        highs = [d['high'] for d in ohlc]
        lows = [d['low'] for d in ohlc]
        atr = atr_arr(np.array(highs), np.array(lows), np.array(closes))

        cum = 1.0
        peak_pct = 0.0
        reason = "持有到期"
        hi = len(closes)

        for i in range(len(closes)):
            day_ret = closes[i]/entry - 1
            cum *= (1+ohlc[i]['pct']/100)
            if i >= 14 and atr[i]>0 and entry>0:
                atr_stop_pct = -2.0*atr[i]/entry
                if day_ret < atr_stop_pct:
                    hi = i+1
                    reason = "ATR止损"
                    break
            if day_ret >= 0.08:
                hi = i+1
                reason = "固定止盈+8%"
                break
            if day_ret < -0.07:
                hi = i+1
                reason = "固定止损-7%"
                break
            if day_ret > peak_pct: peak_pct = day_ret
            if peak_pct >= 0.05 and i >= 14 and atr[i]>0 and entry>0:
                dd = peak_pct - day_ret
                if dd >= 2.0*atr[i]/entry:
                    hi = i+1
                    reason = "ATR移动止盈"
                    break

        total_ret = (cum-1)*100
        trades_detail.append({'ret':total_ret, 'hold':hi, 'reason':reason})

conn.close()

rets = np.array([t['ret'] for t in trades_detail])
n = len(rets)
wins = rets>0
losses = ~wins

print(f"\n{'='*60}")
print("ATR2x止损 + 固定止盈8% 详细回测")
print(f"{'='*60}")
print(f"总交易: {n} 笔")
print(f"胜率:   {wins.sum()}/{n} = {wins.mean()*100:.1f}%")
print(f"累积:   {(np.prod(1+rets/100)-1)*100:+.2f}%")
print(f"均收益: {rets.mean():+.2f}%")
print(f"中位收益: {np.median(rets):+.2f}%")

if wins.any():
    print(f"\n盈利交易 ({wins.sum()}笔):")
    print(f"  均值: {rets[wins].mean():+.2f}%")
    print(f"  中位: {np.median(rets[wins]):+.2f}%")
    print(f"  范围: {rets[wins].min():+.2f}% ~ {rets[wins].max():+.2f}%")
if losses.any():
    print(f"\n亏损交易 ({losses.sum()}笔):")
    print(f"  均值: {rets[losses].mean():+.2f}%")
    print(f"  中位: {np.median(rets[losses]):+.2f}%")
    print(f"  范围: {rets[losses].min():+.2f}% ~ {rets[losses].max():+.2f}%")

print(f"\n盈亏比: {abs(rets[wins].mean()/rets[losses].mean()):.2f}" if wins.any() and losses.any() else "")
pl = abs(rets[wins].mean()/rets[losses].mean()) if wins.any() and losses.any() else 0
sharpe = rets.mean()/rets.std()*np.sqrt(252/5) if rets.std()>0 else 0
print(f"夏普: {sharpe:.2f}")
print(f"标准差: {rets.std():.2f}%")

print(f"\n退出原因:")
for reason in sorted(set(t['reason'] for t in trades_detail)):
    grp = [t for t in trades_detail if t['reason']==reason]
    rets_r = np.array([t['ret'] for t in grp])
    print(f"  {reason}: {len(grp)}笔 胜率={(rets_r>0).mean()*100:.0f}% 均收益={rets_r.mean():+.2f}%")

print(f"\n持仓天数分布:")
for d in sorted(set(t['hold'] for t in trades_detail)):
    grp = [t for t in trades_detail if t['hold']==d]
    print(f"  {d}天: {len(grp)}笔")
