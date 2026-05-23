#!/opt/anaconda3/bin/python3
"""
每日增量更新脚本 - 收盘后运行
1. 从Tushare获取最新交易日数据
2. 更新MySQL daily_price（增量插入，忽略已有）
3. 更新stock_pool_strong.json（强势股池）
4. 更新positions.json浮盈数据
"""
import pymysql
import tushare as ts
import json, sys, os
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

import pandas as pd

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config

TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')
DB_CONFIG = get_db_config()
QUANT_ROOT = Path(__file__).parent.parent
LOG_FILE = QUANT_ROOT / "scripts" / "cron_daily.log"

if not TUSHARE_TOKEN:
    print("ERROR: TUSHARE_TOKEN not set in .env")
    sys.exit(1)

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def log(msg):
    ts_str = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts_str}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def get_conn():
    return pymysql.connect(**DB_CONFIG, autocommit=True)

def get_latest_trade_date():
    """获取最新已有交易日期（返回字符串YYYYMMDD，只取数据量足够的完整日期）"""
    conn = get_conn()
    cur = conn.cursor()
    # 只取记录数>=4000的日期，避免用不完整的04-24（盘中只有1000多条）
    cur.execute("""
        SELECT MAX(trade_date) FROM (
            SELECT trade_date FROM quant_db.daily_price 
            GROUP BY trade_date HAVING COUNT(*) >= 4000
        ) t
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row and row[0]:
        d = row[0]
        if hasattr(d, 'strftime'):
            return d.strftime('%Y%m%d')
        return str(d).replace('-', '')
    return None

def get_recent_trade_dates(n=5, min_date=None):
    """从Tushare获取最近n个有实际数据的交易日（只返回>=min_date的日期）"""
    today = datetime.now().strftime('%Y%m%d')
    df = pro.trade_cal(exchange='SSE', start_date=(datetime.now() - timedelta(days=30)).strftime('%Y%m%d'), end_date=today)
    df = df[df['is_open'] == 1]
    trade_dates = sorted(df['cal_date'].tolist())
    # 过滤掉<=min_date的日期
    if min_date:
        trade_dates = [d for d in trade_dates if d > str(min_date)]
    verified = []
    test_stocks = ['000001.SZ', '600000.SH', '300001.SZ']
    for d in reversed(trade_dates):
        has_data = False
        for sc in test_stocks:
            try:
                rd = pro.daily(ts_code=sc, trade_date=d)
                if rd is not None and len(rd) > 0:
                    has_data = True
                    break
            except Exception as _e:
                print(f"Error in update_daily_price_cron.py: {_e}")
        if has_data:
            verified.append(d)
        if len(verified) >= n:
            break
    return verified

def calc_vol_ratio(cur, ts_code, trade_date, today_vol):
    """查MySQL计算量比：今日成交量/前5日均量"""
    try:
        cur.execute("""
            SELECT vol FROM quant_db.daily_price
            WHERE ts_code=%s AND trade_date < %s
            ORDER BY trade_date DESC LIMIT 5
        """, (ts_code, trade_date))
        rows = cur.fetchall()
        if len(rows) >= 5:
            avg_vol = sum(r[0] for r in rows) / 5
            return round(today_vol / avg_vol, 4) if avg_vol > 0 else 0.0
    except Exception as _e:
        print(f"Error in update_daily_price_cron.py: {_e}")
    return 0.0

def update_daily_price():
    """增量更新daily_price（批量查询 + 一次批量算量比）"""
    log("=== 增量更新 daily_price ===")
    latest_db = get_latest_trade_date()
    dates = get_recent_trade_dates(10)
    if not dates:
        log("获取交易日历失败")
        return
    if latest_db:
        dates = [d for d in dates if d > str(latest_db)]
    if not dates:
        log(f"今日无需更新（最新: {latest_db}）")
        return
    log(f"将更新日期: {dates}")

    conn = get_conn()
    cur = conn.cursor()

    for trade_date in dates:
        trade_str = trade_date.replace('-', '')
        try:
            # 批量取 daily 和 daily_basic（后者取 turnover_rate，前者取 pct_chg/vol）
            df_daily = pro.daily(trade_date=trade_str)
            if df_daily is None or len(df_daily) == 0:
                log(f"  {trade_date}: 无日线数据")
                continue
            log(f"  {trade_date}: 获取 {len(df_daily)} 只，批量计算量比...")

            # 从 daily_basic 取 turnover_rate（盘中有数据）
            df_basic = pro.daily_basic(trade_date=trade_str)
            turnover_dict = {}
            if df_basic is not None and len(df_basic) > 0:
                for _, row in df_basic.iterrows():
                    turnover_dict[row['ts_code']] = float(row['turnover_rate'] or 0)

            # 一次查询所有股票近5日均量（用于算量比）
            ts_codes = df_daily['ts_code'].tolist()
            # Step1: 取近5个交易日
            cur.execute("""
                SELECT DISTINCT trade_date FROM quant_db.daily_price
                WHERE trade_date < %s
                ORDER BY trade_date DESC LIMIT 5
            """, (trade_date,))
            recent_dates = [r[0] for r in cur.fetchall()]
            if not recent_dates:
                log(f"  {trade_date}: 无历史数据，跳过")
                continue
            # Step2: 查这些日期的历史成交量（按股票+日期降序，每只股票只留最近5条）
            cur.execute(f"""
                SELECT ts_code, vol FROM quant_db.daily_price
                WHERE ts_code IN ({','.join(['%s']*len(ts_codes))})
                AND trade_date IN ({','.join(['%s']*len(recent_dates))})
                ORDER BY ts_code, trade_date DESC
            """, ts_codes + recent_dates)
            hist_rows = cur.fetchall()

            # 按股票分组，每只股票只取前5条（已是按日期降序）
            from collections import defaultdict
            vol_by_stock = defaultdict(list)
            for r in hist_rows:
                if len(vol_by_stock[r[0]]) < 5:
                    vol_by_stock[r[0]].append(r[1])

            # 构建写入数据
            rows = []
            for _, row in df_daily.iterrows():
                ts_code = row['ts_code']
                today_vol = float(row['vol'] or 0)
                hist = vol_by_stock.get(ts_code, [])
                if len(hist) >= 5:
                    avg_vol = sum(hist) / 5
                    vol_ratio = round(today_vol / avg_vol, 4) if avg_vol > 0 else 0.0
                else:
                    vol_ratio = 0.0
                rows.append((
                    ts_code, trade_date,
                    float(row['open'] or 0), float(row['high'] or 0),
                    float(row['low'] or 0), float(row['close'] or 0),
                    float(row['pre_close'] or 0),
                    today_vol, float(row['amount'] or 0),
                    float(row['pct_chg'] or 0),
                    turnover_dict.get(ts_code, 0.0),  # 从 daily_basic 取
                    vol_ratio  # 自计算
                ))

            cur.executemany("""
                REPLACE INTO daily_price
                (ts_code, trade_date, open, high, low, close, pre_close, vol, amount, pct_chg, turnover_rate, volume_ratio)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, rows)
            conn.commit()
            log(f"  {trade_date}: 写入 {len(rows)} 条")
        except Exception as e:
            log(f"  {trade_date} 错误: {e}")

    cur.close()
    conn.close()

def update_rps_20():
    """更新当日rps_20为真实百分位排名（全市场20日涨幅排序，值域1-99）"""
    log("=== 更新 rps_20（百分位排名） ===")
    latest = get_latest_trade_date()
    if not latest:
        return
    conn = get_conn()

    # 批量读取近21个交易日（约35个日历日）的行情
    df = pd.read_sql("""
        SELECT ts_code, trade_date, close FROM daily_price
        WHERE trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 40 DAY)
        ORDER BY ts_code, trade_date DESC
    """, conn, params=[latest, latest])

    if df.empty:
        log("无数据")
        conn.close()
        return

    # 取每只股票最新21条（head=最新，因为ORDER BY DESC）
    df = df.groupby('ts_code', sort=False).head(21).sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

    # 计算20日涨幅（需满21条数据）— 向量化方式
    df['_rn'] = df.groupby('ts_code', sort=False).cumcount()
    valid_codes = df.groupby('ts_code', sort=False).size()
    valid_codes = valid_codes[valid_codes >= 21].index

    first_prices = df[df['_rn'] == 0].set_index('ts_code')['close']
    last_prices = df[df['_rn'] == 20].set_index('ts_code')['close']

    pct_20d = (last_prices / first_prices - 1) * 100
    pct_20d = pct_20d[pct_20d.index.isin(valid_codes)]

    if pct_20d.empty:
        log("计算无结果")
        conn.close()
        return

    # 百分位排名 (1-99)
    rps_series = pct_20d.rank(pct=True) * 100
    rps_series = rps_series.clip(1, 99).round(1)

    log(f"RPS分布: min={rps_series.min()}, max={rps_series.max()}, 股票数={len(rps_series)}")

    # 批量更新
    cur = conn.cursor()
    update_data = [(rps, code, latest) for code, rps in rps_series.items()]
    cur.executemany("UPDATE daily_price SET rps_20=%s WHERE ts_code=%s AND trade_date=%s", update_data)
    conn.commit()
    cur.close()
    conn.close()
    log(f"更新 rps_20 完成: {len(update_data)} 只（百分位排名）")

def update_ma():
    """更新当日MA均线（窗口函数批量计算，替代逐股N+1查询）"""
    log("=== 更新 MA均线 ===")
    latest = get_latest_trade_date()
    if not latest:
        return
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE quant_db.daily_price d
            JOIN (
                SELECT ts_code, trade_date,
                       AVG(close) OVER (
                           PARTITION BY ts_code ORDER BY trade_date
                           ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
                       ) AS ma5,
                       AVG(close) OVER (
                           PARTITION BY ts_code ORDER BY trade_date
                           ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                       ) AS ma10,
                       AVG(close) OVER (
                           PARTITION BY ts_code ORDER BY trade_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS ma20
                FROM quant_db.daily_price
                WHERE trade_date <= %s
            ) calc ON d.ts_code = calc.ts_code AND d.trade_date = calc.trade_date
            SET d.ma5 = calc.ma5, d.ma10 = calc.ma10, d.ma20 = calc.ma20
            WHERE d.trade_date = %s
        """, [latest, latest])
        conn.commit()
        log(f"MA均线批量更新完成，影响行数: {cur.rowcount}")
    except Exception as e:
        log(f"MA更新失败: {e}")
    finally:
        cur.close()
        conn.close()

def update_stock_pool():
    """更新强势股票池到MySQL stock_pool_snap表"""
    log("=== 更新强势股票池 ===")
    latest = get_latest_trade_date()
    if not latest:
        return
    conn = get_conn()
    cur = conn.cursor()

    # 删掉旧数据（同一日期）
    cur.execute("DELETE FROM quant_db.stock_pool_snap WHERE snap_date = %s", [latest])
    log(f"删除了 {latest} 旧数据")

    # 查询强势股
    cur.execute("""
        SELECT d.ts_code, s.name, s.industry, d.close, d.pct_chg,
               d.turnover_rate, d.volume_ratio,
               d.ma5, d.ma10, d.ma20,
               d.rps_20
        FROM quant_db.daily_price d
        JOIN quant_db.stock_info s ON CAST(d.ts_code AS CHAR) = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date = %s
          AND d.pct_chg BETWEEN 0.5 AND 9.5
          AND d.volume_ratio BETWEEN 1.5 AND 10
          AND d.rps_20 >= 40
          AND d.ma5 > d.ma10 AND d.ma10 > d.ma20
          AND s.is_st = 0
        ORDER BY d.rps_20 DESC, d.pct_chg DESC
        LIMIT 100
    """, [latest])
    rows = cur.fetchall()
    log(f"符合条件的股票: {len(rows)} 只")

    if rows:
        # 行业映射
        industry_map = {r[0]: r[2] for r in rows}

        records = []
        for rank, r in enumerate(rows, 1):
            ts_code = r[0]
            name = r[1] or ""
            industry = r[2] or ""
            price = float(r[3]) if r[3] else 0
            change_pct = float(r[4]) if r[4] else 0
            turnover_rate = float(r[5]) if r[5] else 0
            vol_ratio = float(r[6]) if r[6] else 0
            ma5, ma10, ma20 = r[7], r[8], r[9]
            rps_20 = float(r[10]) if r[10] else 0

            # 计算 quick_score（策略评分）
            score = 50
            if rps_20 >= 70:
                score += 10
            if change_pct >= 3:
                score += 10
            elif change_pct >= 1:
                score += 5
            if vol_ratio >= 3:
                score += 10
            elif vol_ratio >= 2:
                score += 5
            if ma5 > ma10 > ma20:
                score += 10
            score = min(100, score)

            reasons = []
            if change_pct >= 3:
                reasons.append(f"涨幅{change_pct:.2f}%")
            if vol_ratio >= 3:
                reasons.append(f"量比{vol_ratio:.2f}")
            if turnover_rate >= 5:
                reasons.append(f"换手率{turnover_rate:.2f}%")
            if rps_20 >= 70:
                reasons.append(f"RPS20={rps_20:.1f}")
            entry_reason = " | ".join(reasons) if reasons else f"涨幅{change_pct:.2f}% | 量比{vol_ratio:.2f} | 换手率{turnover_rate:.2f}%"

            records.append((
                latest, ts_code, name, industry, price,
                change_pct, turnover_rate, vol_ratio,
                score, entry_reason, rank
            ))

        cur.executemany("""
            INSERT INTO quant_db.stock_pool_snap
            (snap_date, ts_code, name, industry, price, change_pct, turnover_rate, vol_ratio, quick_score, entry_reason, today_rank)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, records)
        conn.commit()
        log(f"写入 stock_pool_snap: {len(records)} 条")

    cur.close()
    conn.close()

    # 同时更新 JSON 文件（兼容旧逻辑）
    stocks = []
    for r in rows:
        stocks.append({
            "ts_code": r[0], "name": r[1], "industry": r[2], "close": float(r[3]),
            "pct_chg": float(r[4]), "volume_ratio": float(r[5]),
            "rps_20": float(r[10]),
            "ma5": float(r[6]), "ma10": float(r[7]), "ma20": float(r[8]),
            "turnover_rate": float(r[9]) if r[9] else 0
        })

    pool_file = QUANT_ROOT / "data" / "stock_pool_strong.json"
    with open(pool_file, "w", encoding="utf-8") as f:
        json.dump({"pool": stocks, "updated": latest}, f, ensure_ascii=False, indent=2)
    log(f"强势股池更新: {len(stocks)} 只")

def update_positions():
    """更新持仓浮盈"""
    positions_file = QUANT_ROOT / "data" / "positions.json"
    if not positions_file.exists():
        return
    with open(positions_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    positions = data.get("positions", [])
    if not positions:
        return

    latest = get_latest_trade_date()
    if not latest:
        return
    conn = get_conn()
    cur = conn.cursor()

    for pos in positions:
        code = pos.get("code", "")
        market = "sz" if code.startswith(("00", "30")) else "sh"
        ts_code = f"{code}.{'SZ' if market == 'sz' else 'SH'}"
        try:
            cur.execute("SELECT close FROM quant_db.daily_price WHERE ts_code=%s AND trade_date=%s", [ts_code, latest])
            row = cur.fetchone()
            if row:
                current_price = float(row[0])
                cost = float(pos.get("成本", 0))
                if cost > 0:
                    pos["当前价"] = current_price
                    pos["浮盈金额"] = round((current_price - cost) * int(pos.get("数量", 0)), 2)
                    pos["浮盈比例"] = round((current_price / cost - 1) * 100, 2)
        except Exception as e:
            log(f"持仓更新失败 {ts_code}: {e}")

    cur.close()
    conn.close()

    with open(positions_file, "w", encoding="utf-8") as f:
        json.dump({"positions": positions}, f, ensure_ascii=False, indent=2)
    log("持仓浮盈更新完成")

def sync_sector_moneyflow(latest_date):
    """同步板块资金流向（当日增量）"""
    try:
        td = str(latest_date)
        df = pro.moneyflow_ind_ths(trade_date=td)
        if df is None or len(df) == 0:
            log(f"板块资金流向: 无数据 ({td})")
            return 0
        rows = []
        for _, row in df.iterrows():
            rows.append((
                td,
                str(row.get('industry', '') or row.get('name', '')),
                float(row.get('net_amount', 0) or 0),
                float(row.get('buy_elg_amount', 0) or 0),
                float(row.get('sell_elg_amount', 0) or 0),
                float(row.get('pct_change', 0) or 0),
            ))
        conn = get_conn()
        cur = conn.cursor()
        sql = """REPLACE INTO sector_moneyflow 
            (trade_date, sector_name, net_amount, buy_elg_amount, sell_elg_amount, pct_change)
            VALUES (%s, %s, %s, %s, %s, %s)"""
        cur.executemany(sql, rows)
        conn.commit()
        cur.close()
        conn.close()
        log(f"板块资金流向: 同步 {len(rows)} 条 ({td})")
        return len(rows)
    except Exception as e:
        log(f"板块资金流向同步失败: {e}")
        return 0

def update_bottom_pool(latest_date):
    """更新底部起步股票池（从 daily_price 筛选符合底部策略的股票）"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT d.ts_code, s.name, s.industry,
                   d.close, d.pct_chg,
                   d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20, d.rps_20
            FROM quant_db.daily_price d
            JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE d.trade_date = %s
              AND d.close > 5
              AND d.ts_code NOT LIKE '688%%'
              AND d.ts_code NOT LIKE '92%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
              AND s.is_st = 0
              AND d.pct_chg BETWEEN 0.5 AND 3.0
              AND d.turnover_rate BETWEEN 1.0 AND 10.0
              AND d.ma5 IS NOT NULL AND d.ma10 IS NOT NULL AND d.ma20 IS NOT NULL
        """, (str(latest_date),))
        rows = cur.fetchall()
        cur.close()
        conn.close()

        stocks = []
        for r in rows:
            ts_code = r[0]
            ma5, ma10, ma20 = float(r[7] or 0), float(r[8] or 0), float(r[9] or 0)
            rps20 = float(r[10] or 0) if r[10] else 0
            reasons = []
            if ma5 > ma10 > ma20 and float(r[3]) > ma5:
                reasons.append("均线多头")
            if 30 <= rps20 <= 60:
                reasons.append(f"RPS={int(rps20)}")
            if reasons:
                code = ts_code.split(".")[0]
                mkt = "sz" if ts_code.endswith(".SZ") else "sh"
                stocks.append({
                    "代码": f"{mkt.upper()}{code}",
                    "名称": r[1] or "",
                    "行业": r[2] or "",
                    "现价": float(r[3]),
                    "涨跌幅": f"{float(r[4]):+.2f}%",
                    "换手率": float(r[5] or 0),
                    "量比": float(r[6] or 0),
                    "入选理由": " | ".join(reasons) if reasons else "底部形态",
                })

        pool_file = QUANT_ROOT / "data" / "stock_pool_bottom.json"
        with open(pool_file, "w", encoding="utf-8") as f:
            json.dump({"scan_date": str(latest_date), "stocks": stocks[:50]}, f, ensure_ascii=False, indent=2)
        log(f"底部股票池: {len(stocks)} 只 → 取前50")
    except Exception as e:
        log(f"底部股票池生成失败: {e}")

def sync_fina_indicator_incremental():
    """增量同步财务指标（仅补缺失）"""
    log("=== 增量同步财务指标 ===")
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 获取最新报告期
        now = datetime.now()
        year = now.year
        if now.month >= 4: latest_period = f"{year-1}1231"
        elif now.month >= 10: latest_period = f"{year-1}0930"
        elif now.month >= 8: latest_period = f"{year}0630"
        else: latest_period = f"{year}0331"

        # 查找缺少财务数据的股票（排除ST/688/北交所）
        cur.execute(f"""
            SELECT DISTINCT d.ts_code FROM daily_price d
            LEFT JOIN fina_indicator f ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(f.ts_code USING utf8mb4) AND f.end_date = %s
            WHERE d.trade_date = (SELECT MAX(trade_date) FROM daily_price)
              AND f.ts_code IS NULL
              AND d.ts_code NOT LIKE '688%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
        """, [latest_period])
        stocks = [r[0] for r in cur.fetchall()]
        if not stocks:
            log("财务指标已完整，跳过")
            return

        log(f"需补充 {len(stocks)} 只股票的财务数据...")
        pro = ts.pro_api()
        updated = 0
        for i, ts_code in enumerate(stocks):
            if (i+1) % 100 == 0:
                log(f"财务同步进度 {i+1}/{len(stocks)}")
            try:
                df = pro.fina_indicator(ts_code=ts_code, period=latest_period,
                                        fields="ts_code,end_date,roe,yoy_sales,grossprofit_margin,netprofit_margin,eps")
                if df is not None and not df.empty:
                    row = df.iloc[0]
                    def to_val(x):
                        import math
                        return None if (x is None or (isinstance(x, float) and math.isnan(x))) else float(x)
                    cur.execute(
                        "INSERT IGNORE INTO fina_indicator VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                        (ts_code, row['end_date'], to_val(row.get('roe')),
                         to_val(row.get('yoy_sales')), to_val(row.get('grossprofit_margin')),
                         to_val(row.get('netprofit_margin')), to_val(row.get('eps')))
                    )
                    updated += 1
            except Exception:
                continue
        conn.commit()
        log(f"财务指标增量同步完成: 更新 {updated} 只")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    log("=== 每日增量更新开始 ===")
    try:
        update_daily_price()
        update_rps_20()
        update_ma()
        update_stock_pool()
        sync_sector_moneyflow(get_latest_trade_date())
        sync_fina_indicator_incremental()
        update_positions()
        log("=== 每日增量更新完成 ===")
    except Exception as e:
        log(f"更新失败: {e}")
        import traceback
        traceback.print_exc()
