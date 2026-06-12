"""
实时扫描策略回测引擎 v8 - 配置驱动
config/scanner_config.yaml 改权重即时生效
"""
import logging
import math
import os
from collections import defaultdict
from pathlib import Path

import pymysql
import yaml

logger = logging.getLogger(__name__)

def _get_db_config():
    """获取数据库配置，优先环境变量"""
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", 3306)),
        "user": os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_DATABASE", "quant_db"),
        "charset": "utf8mb4",
    }
COMMISSION = 0.0003

class _Cfg: pass
cfg = _Cfg()

def _load():
    p = Path(__file__).parent.parent.parent / "config" / "scanner_config.yaml"
    with open(p, encoding="utf-8") as f: return yaml.safe_load(f)

def _apply(raw):
    cfg.CAPITAL = raw["capital"].get("total", 50000)
    cfg.MAX_POS = raw["position"].get("max_positions", 3)
    cfg.POS_SIZE = cfg.CAPITAL / cfg.MAX_POS
    r = raw["risk"]
    cfg.STOP = r.get("stop_loss", -0.08)
    cfg.TP = r.get("take_profit", 0.15)
    cfg.TRA = r.get("trailing_activate", 0.06)
    cfg.TRD = r.get("trailing_distance", 0.04)
    cfg.MH = r.get("max_hold_days", 10)
    e = raw["entry"]
    cfg.MIN_S = e.get("min_score", 65)
    cfg.MIN_V = e.get("min_vol_ratio", 1.3)
    cfg.AM20 = e.get("require_above_ma20", True)
    cfg.MIN_A = e.get("min_daily_amount", 100000)
    cfg.SC = raw.get("scoring", {})
    cfg.PL = raw.get("stock_pool", {})
    cfg.MF = raw.get("market_filter", {})
    cfg.BT = raw.get("backtest", {})

def _q(sql, params=None):
    try:
        c = pymysql.connect(**_get_db_config(), connect_timeout=10)
        cur = c.cursor(); cur.execute(sql, params or ()); r = cur.fetchall()
        cur.close(); c.close(); return r
    except: return []

def _ma(a, n):
    if len(a) < n: return sum(a)/len(a) if a else 0
    return sum(a[-n:])/n

def _rsi(cs):
    if len(cs) < 15: return 50
    g = [max(cs[i]-cs[i-1],0) for i in range(1,len(cs))]
    l = [max(cs[i-1]-cs[i],0) for i in range(1,len(cs))]
    ag,al=sum(g[-14:])/14,sum(l[-14:])/14
    return 100-(100/(1+ag/al)) if al else 100

# ── 因子 ──
def f_v(td, hist):
    s=cfg.SC.get("volume_breakout",{}); score,vol=0,td.get("vol",0)
    if len(hist)<5: return 0,1
    vs=[d["vol"] for d in hist[-20:]]; av=sum(vs)/len(vs) if vs else 1; vr=vol/av if av>0 else 1
    if vr>2.0: score+=s.get("vol_ratio_2x",15)
    elif vr>1.5: score+=s.get("vol_ratio_1_5x",10)
    elif vr>1.0: score+=s.get("vol_ratio_1x",5)
    cs=[d["close"] for d in hist[-20:]]; hi,lo=max(cs),min(cs); p=td.get("close",0)
    if hi>lo:
        pos=(p-lo)/(hi-lo)*100
        if pos>80: score+=s.get("price_high_80pct",8)
        elif pos>50: score+=s.get("price_mid_50pct",4)
        elif pos<20: score+=s.get("price_low_20pct",8)
    if vr>1.2 and td.get("pct_chg",0)>0: score+=s.get("vol_price_up",7)
    return min(s.get("max_score",30),score), vr

def f_m(td, hist, ixp=0):
    s=cfg.SC.get("momentum",{}); score,pc=0,td.get("pct_chg",0)
    if len(hist)<5: return 0
    if pc>5: score+=s.get("pct_gt_5",8)
    elif pc>3: score+=s.get("pct_gt_3",6)
    elif pc>1: score+=s.get("pct_gt_1",3)
    elif pc>-1: score+=s.get("pct_gt_minus1",1)
    elif pc<-5: score+=s.get("pct_lt_minus5",-5)
    if len(hist)>=5:
        r5=(hist[-1]["close"]/hist[-5]["close"]-1)*100
        if r5>8: score+=s.get("ret5_gt_8",6)
        elif r5>4: score+=s.get("ret5_gt_4",4)
        elif r5>1: score+=s.get("ret5_gt_1",2)
    rel=pc-ixp
    if rel>3: score+=s.get("rel_gt_3",7)
    elif rel>1.5: score+=s.get("rel_gt_1_5",4)
    elif rel>0.5: score+=s.get("rel_gt_0_5",2)
    elif rel<-2: score+=s.get("rel_lt_minus2",-4)
    if len(hist)>=3:
        up=sum(1 for d in hist[-3:] if d["pct_chg"]>0); score+=up*s.get("up_day_bonus",1.5)
    return max(0,min(s.get("max_score",25),score))

def f_t(td, hist):
    s=cfg.SC.get("trend",{})
    if len(hist)<20: return s.get("neutral_score",12), False
    cs=[d["close"] for d in hist]; p=td.get("close",0)
    m5,m10,m20=_ma(cs,5),_ma(cs,10),_ma(cs,20); score=0
    if p>m5: score+=s.get("above_ma5",3)
    if p>m10: score+=s.get("above_ma10",4)
    if p>m20: score+=s.get("above_ma20",5)
    if m5>m10>m20: score+=s.get("bull_alignment",8)
    elif m5>m10: score+=s.get("semi_bull",3)
    if len(cs)>=10:
        sl=(m5-_ma(cs[-10:],5))/_ma(cs[-10:],5)*100
        if sl>2: score+=s.get("slope_gt_2",5)
        elif sl>0.5: score+=s.get("slope_gt_0_5",2)
        elif sl<-2: score+=s.get("slope_lt_minus2",-3)
    return max(0,min(s.get("max_score",25),score)), p>m20

def f_l(td, hist):
    s=cfg.SC.get("liquidity",{}); score=s.get("base",10); amt=td.get("amount",0)
    if amt>500000: score+=s.get("amount_gt_5e8",6)
    elif amt>100000: score+=s.get("amount_gt_1e8",3)
    elif amt>50000: score+=s.get("amount_gt_5e7",1)
    if len(hist)>=10:
        pts=[abs(d["pct_chg"]) for d in hist[-10:]]
        if sum(pts)/len(pts)>5: score+=s.get("vol_penalty",-3)
    return max(0,min(s.get("max_score",20),score))

def f_r(td, hist):
    s=cfg.SC.get("rsi_bonus",{}); cs=[d["close"] for d in hist]
    if len(cs)<15: return 0
    r=_rsi(cs)
    if r<30: return s.get("oversold_30",5)
    elif r<40: return s.get("oversold_40",3)
    elif r>80: return s.get("overbought_80",-5)
    elif r>70: return s.get("overbought_70",-2)
    return 0

def f_b(td, hist):
    s=cfg.SC.get("bollinger_bonus",{}); cs=[d["close"] for d in hist]
    if len(cs)<21: return s.get("default",2)
    mid=sum(cs[-20:])/20; sd=math.sqrt(sum((x-mid)**2 for x in cs[-20:])/20)
    p=td.get("close",0); pb=(p-(mid-2*sd))/(4*sd) if sd>0 else 0.5
    if pb<0.2: return s.get("lower_band",5)
    elif pb<0.4: return s.get("mid_low",3)
    elif pb>0.9: return s.get("upper_band",0)
    elif pb>0.7: return s.get("mid_high",1)
    return s.get("mid_range", 2)


# ====== v9 新增因子（日线回测版） ======

def f_brk(td, hist):
    """日内突破（日线近似）：突破前日高点/低点 (max 15)"""
    s = cfg.SC.get("intraday_breakout", {})
    if not s.get("enabled", True):
        return 0
    if len(hist) < 2:
        return 3
    prev = hist[-1]
    prev_h, prev_l, prev_c = prev.get("high", 0), prev.get("low", 0), prev.get("close", 0)
    p = td.get("close", 0)
    score = 0
    if prev_h > 0:
        if p > prev_h:         score += 8
        elif p > prev_h * 0.98: score += 4
        elif p < prev_l:       score -= 5
        elif p > prev_c:       score += 2
    if prev_h > prev_l:
        ipos = (p - td.get("open", prev_c)) / (td.get("high", prev_h) - td.get("low", prev_l)) \
            if td.get("high", 0) > td.get("low", 0) else 0.5
        if ipos > 0.7:         score += 5
        elif ipos > 0.3:       score += 3
    return max(0, min(s.get("max_score", 15), score))


def f_mf_daily(td, hist):
    """资金博弈（日线近似）：成交额绝对量 + 放量程度 (max 10)"""
    s = cfg.SC.get("money_flow", {})
    if not s.get("enabled", True):
        return 0
    amt = td.get("amount", 0)
    score = 0
    if amt > 1e9:       score += 5
    elif amt > 5e8:     score += 3
    elif amt > 1e8:     score += 1
    if len(hist) >= 5:
        avg_amt = sum(d["amount"] for d in hist[-5:]) / 5
        if avg_amt > 0 and amt > avg_amt * 1.5:
            score += 4
        elif amt > avg_amt:
            score += 2
    return max(0, min(s.get("max_score", 10), score))


def f_idx_daily(td, ixp, idx_data):
    """多指数强度（日线版）：相对上证+创业板+科创50 (max 10)"""
    s = cfg.SC.get("multi_index", {})
    if not s.get("enabled", True):
        return 0
    pc = td.get("pct_chg", 0)
    if not idx_data:
        # 降级：只用沪深300
        rel = pc - ixp
        if rel > 2:     return 6
        elif rel > 1:   return 4
        elif rel > 0:   return 2
        elif rel < -2:  return -2
        return 0
    score, count = 0, 0
    for bc in ["000001.SH", "399006.SZ", "000688.SH"]:
        ip = idx_data.get(bc)
        if ip is None:
            continue
        count += 1
        rel = pc - ip
        if rel > 2:       score += 4
        elif rel > 1:     score += 2
        elif rel > 0:     score += 1
        elif rel < -2:    score -= 2
    if count > 0:
        score = round(score / count * 3)
    return max(0, min(s.get("max_score", 10), score))


def run_backtest(start_date=None, end_date=None):
    raw = _load(); _apply(raw)
    sd = start_date or cfg.BT.get("start_date","2025-01-01")
    ed = end_date or cfg.BT.get("end_date","2026-06-05")

    tds = [str(r[0]) for r in _q("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date",(sd,ed))]
    if len(tds)<60: return {"error":"数据不足"}

    ma = cfg.PL.get("min_avg_amount",100000000)//1000
    md = cfg.PL.get("min_trade_days",100); mn=cfg.PL.get("max_stocks",500)
    pool = [r[0] for r in _q(f"SELECT ts_code FROM (SELECT ts_code, AVG(amount) a FROM daily_price WHERE trade_date>=%s AND trade_date<=%s GROUP BY ts_code HAVING AVG(amount)>{ma} AND COUNT(*)>={md} ORDER BY a DESC LIMIT {mn}) t",(sd,ed))]
    if not pool: return {"error":"无股"}

    logger.info(f"回测: {len(tds)}天 {len(pool)}只 门槛{cfg.MIN_S}")

    alld = defaultdict(list)
    for i in range(0,len(pool),100):
        b=pool[i:i+100]; ph=",".join(["%s"]*len(b))
        for r in _q(f"SELECT ts_code,trade_date,open,high,low,close,vol,pct_chg,amount FROM daily_price WHERE ts_code IN ({ph}) AND trade_date>=%s AND trade_date<=%s ORDER BY ts_code,trade_date",[*b,sd,ed]):
            alld[r[0]].append({"date":str(r[1]),"open":float(r[2]or 0),"high":float(r[3]or 0),"low":float(r[4]or 0),"close":float(r[5]or 0),"vol":float(r[6]or 0),"pct_chg":float(r[7]or 0),"amount":float(r[8]or 0)})

    # 多指数数据
    idx = {str(r[0]):{"close":float(r[1]or 0),"pct_chg":float(r[2]or 0)} for r in _q("SELECT trade_date,close,pct_chg FROM daily_price WHERE ts_code='000300.SH' AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",(sd,ed))}
    idx_multi = {}  # 多指数数据: {code: {date: pct_chg}}
    for bc in ["000001.SH", "399006.SZ", "000688.SH"]:
        idx_multi[bc] = {str(r[0]): float(r[1] or 0) for r in _q("SELECT trade_date,pct_chg FROM daily_price WHERE ts_code=%s AND trade_date>=%s AND trade_date<=%s ORDER BY trade_date",(bc,sd,ed))}

    cash=cfg.CAPITAL; pos={}; trades=[]; eq=[]; wu=cfg.BT.get("warmup_days",30)
    check_cnt=0; pass_cnt=0  # 调试计数器

    for di,td in enumerate(tds):
        if di<wu: eq.append({"date":td,"equity":cfg.CAPITAL,"cash":cfg.CAPITAL,"positions":0,"position_value":0}); continue
        pv=tds[di-1]; ixp=idx.get(td,{}).get("pct_chg",0); ixc=idx.get(td,{}).get("close",0)
        # 当日多指数数据
        idx_today = {bc: idx_multi[bc].get(td, 0) for bc in idx_multi}
        ixm=sum(idx.get(tds[j],{}).get("close",0) for j in range(max(0,di-20),di))/20 if di>=20 else ixc
        bear=ixc<ixm and ixp<cfg.MF.get("bearish_threshold",-0.015)

        # 平仓
        sell=[]
        for c,ps in list(pos.items()):
            dl=alld.get(c,[]); dd=next((d for d in dl if d["date"]==td),None)
            if not dd: continue
            cp=dd["close"]; pnl=cp/ps["bp"]-1
            if cp>ps.get("pk",ps["bp"]): ps["pk"]=cp
            pk=ps.get("pk",ps["bp"]); reason=None
            if pnl>=cfg.TRA and cp<=pk*(1-cfg.TRD): reason="移动止损"
            elif pnl<=cfg.STOP: reason="止损"
            elif pnl>=cfg.TP: reason="止盈"
            elif (di-ps["bdi"])>cfg.MH: reason="超时"
            if reason:
                cash+=ps["sh"]*cp*(1-COMMISSION)
                trades.append({"type":"sell","code":c,"date":td,"price":round(cp,2),"shares":ps["sh"],"amount":round(ps["sh"]*cp*(1-COMMISSION),2),"pnl_pct":round(pnl*100,2),"reason":reason,"hold_days":di-ps["bdi"]})
                sell.append(c)
        for c in sell: del pos[c]

        # 买入
        if len(pos)<cfg.MAX_POS and cash>=cfg.POS_SIZE*1.1 and not bear:
            cand=[]
            for c in pool:
                if c in pos: continue
                dl=alld.get(c,[])
                if not dl: continue
                si=next((j for j,d in enumerate(dl) if d["date"]==pv),-1)
                if si<30: continue
                d=dl[si]
                if d["amount"]<cfg.MIN_A: continue
                if cfg.PL.get("exclude_st",True) and ("ST" in c or "*ST" in c): continue
                hist=dl[max(0,si-30):si+1]
                td_={"close":d["close"],"vol":d["vol"],"pct_chg":d["pct_chg"],"amount":d["amount"]}
                vs,vr=f_v(td_,hist); ts,am=f_t(td_,hist)
                if vr<cfg.MIN_V: continue
                if cfg.AM20 and not am: continue
                check_cnt+=1
                # v9 评分：6基础 + 3新增(突破/资金/多指数)
                sc = vs + f_m(td_, hist, ixp) + ts + f_l(td_, hist) + f_r(td_, hist) + f_b(td_, hist) \
                     + f_brk(td_, hist) + f_mf_daily(td_, hist) + f_idx_daily(td_, ixp, idx_today)
                if sc>=cfg.MIN_S:
                    pass_cnt+=1
                    cand.append((c,sc,d["close"]))
            cand.sort(key=lambda x:x[1],reverse=True)
            for i in range(min(cfg.MAX_POS-len(pos),len(cand))):
                c,sc,pr=cand[i]; sh=int(cfg.POS_SIZE/pr/100)*100
                if sh<100: continue
                cost=sh*pr*(1+COMMISSION)
                if cost>cash: continue
                cash-=cost
                pos[c]={"sh":sh,"cost":pr,"bd":td,"bp":pr,"bdi":di,"es":sc,"pk":pr}
                trades.append({"type":"buy","code":c,"date":td,"price":round(pr,2),"shares":sh,"amount":round(cost,2),"score":sc,"reason":f"{int(sc)}分"})

        pv_=sum(ps["sh"]*next((d["close"] for d in alld.get(c,[]) if d["date"]==td),ps["bp"]) for c,ps in pos.items())
        eq.append({"date":td,"equity":round(cash+pv_,2),"cash":round(cash,2),"positions":len(pos),"position_value":round(pv_,2)})

    ld=tds[-1]
    for c,ps in list(pos.items()):
        dl=alld.get(c,[]); p=next((d["close"] for d in dl if d["date"]==ld),ps["bp"])
        cash+=ps["sh"]*p*(1-COMMISSION)
        trades.append({"type":"sell","code":c,"date":ld,"price":round(p,2),"shares":ps["sh"],"amount":round(ps["sh"]*p*(1-COMMISSION),2),"pnl_pct":round((p/ps["bp"]-1)*100,2),"reason":"回测结束","hold_days":len(tds)-ps["bdi"]})

    logger.info(f"FILTER: 检查{check_cnt}次 通过{pass_cnt}次 阈值{cfg.MIN_S}")

    fe=cash; tr=(fe/cfg.CAPITAL-1)*100
    bt_=[t for t in trades if t["type"]=="buy"]; st=[t for t in trades if t["type"]=="sell"]
    wt=[t for t in st if t.get("pnl_pct",0)>0]
    dr=[eq[i]["equity"]/eq[i-1]["equity"]-1 for i in range(1,len(eq)) if eq[i-1]["equity"]>0]
    ad=sum(dr)/len(dr) if dr else 0; sdd=math.sqrt(sum((r-ad)**2 for r in dr)/len(dr)) if dr else 0
    sp=(ad/sdd*math.sqrt(252)) if sdd>0 else 0
    pk_v=cfg.CAPITAL; mdd=0
    for e in eq:
        if e["equity"]>pk_v: pk_v=e["equity"]
        dd=(pk_v-e["equity"])/pk_v*100
        if dd>mdd: mdd=dd
    wp=len(wt)/len(st)*100 if st else 0
    aw=sum(t.get("pnl_pct",0) for t in wt)/len(wt) if wt else 0
    lt=[t for t in st if t.get("pnl_pct",0)<=0]
    al=sum(t.get("pnl_pct",0) for t in lt)/len(lt) if lt else 0
    rs=defaultdict(lambda:{"count":0,"avg_pnl":0})
    for t in st:
        r=t.get("reason","?"); rs[r]["count"]+=1; rs[r]["avg_pnl"]+=t.get("pnl_pct",0)
    for r in rs:
        if rs[r]["count"]>0: rs[r]["avg_pnl"]=round(rs[r]["avg_pnl"]/rs[r]["count"],2)

    return {
        "strategy":"实时扫描 v9.0","version":"v9","config":"config/scanner_config.yaml",
        "params":{"min_score":cfg.MIN_S,"max_positions":cfg.MAX_POS,"filter_check":check_cnt,"filter_pass":pass_cnt},
        "period":f"{sd} ~ {ed}","trading_days":len(tds),"stock_pool":len(pool),"capital":cfg.CAPITAL,
        "summary":{"initial_capital":cfg.CAPITAL,"final_equity":round(fe,2),"total_return_pct":round(tr,2),"annual_return_pct":round(tr/(len(tds)/252),2),"max_drawdown_pct":round(mdd,2),"sharpe_ratio":round(sp,2),"calmar_ratio":round(abs(tr/mdd),2) if mdd>0 else 0},
        "trades":{"total_trades":len(bt_),"win_rate_pct":round(wp,2),"avg_win_pct":round(aw,2),"avg_loss_pct":round(al,2),"win_loss_ratio":round(abs(aw/al),2) if al else 0,"avg_hold_days":round(sum(t.get("hold_days",0) for t in st)/len(st),1) if st else 0},
        "exit_reasons":dict(rs),"equity_curve":eq[::max(1,len(eq)//200)],
    }
