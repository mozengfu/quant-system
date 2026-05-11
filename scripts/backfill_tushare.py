#!/opt/anaconda3/bin/python3
"""
Tushare 付费数据回填脚本
- 补全 stock_info（主板+创业板，排除ST）
- 补全 daily_price 历史日线数据
- 补全 market_index_daily（沪深指数）
- 断点续传：进度记录在 progress.json
"""
import pymysql
import tushare as ts
import json, time, sys, os
from datetime import datetime, timedelta
from pathlib import Path

# ========== 配置 ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from quant_app.utils.config import get_db_config
DB_CONFIG = get_db_config()
BATCH_SIZE = 100        # 每批回填天数
STOCK_BATCH = 50        # 每批股票数
PROGRESS_FILE = Path(__file__).parent / "progress.json"
LOG_FILE = Path(__file__).parent / "backfill.log"

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def log(msg):
    ts_str = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts_str}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def load_progress():
    if PROGRESS_FILE.exists():
        return json.load(open(PROGRESS_FILE))
    return {"stage": "init", "stock_idx": 0, "date_idx": 0, "stock_infos": 0, "index_daily": False}

def save_progress(p):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(p, f)

def get_conn():
    return pymysql.connect(**DB_CONFIG, autocommit=True)

# ========== Stage 1: 补全 stock_info ==========
def backfill_stock_info(progress):
    log("=== Stage 1: 补全 stock_info ===")
    conn = get_conn()
    cur = conn.cursor()

    # 拉取主板+创业板（排除ST）
    df = pro.stock_basic(exchange='', list_status='L', 
                         fields='ts_code,symbol,name,industry,market,list_date')
    # 只保留主板(SZ/SH)+创业板(SZ)，排除科创板/北交所
    allowed = {'主板', '创业板'}
    df = df[df['market'].isin(allowed)]
    # 排除ST
    df = df[~df['name'].str.contains('ST', na=False)]
    
    log(f"获取到 {len(df)} 只股票（主板+创业板，排除ST）")

    # 清理旧数据（只保留北交所/科创板等不在此次范围的）
    cur.execute("SELECT ts_code FROM stock_info WHERE market IN ('SZ','SH')")
    existing = set(r[0] for r in cur.fetchall())
    
    new_stocks = []
    for _, row in df.iterrows():
        if row['ts_code'] in existing:
            continue
        new_stocks.append({
            'ts_code': row['ts_code'],
            'code': row['symbol'],
            'name': row['name'],
            'market': 'SZ' if row['ts_code'].endswith('.SZ') else 'SH',
            'industry': row['industry'] or '',
            'list_date': row['list_date'],
            'is_st': 1 if 'ST' in str(row['name']) else 0
        })
    
    if new_stocks:
        cur.executemany(
            """INSERT IGNORE INTO stock_info (ts_code, code, name, market, industry, list_date, is_st)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [(s['ts_code'], s['code'], s['name'], s['market'], s['industry'], s['list_date'], s['is_st']) for s in new_stocks]
        )
        log(f"新增 {len(new_stocks)} 只股票到 stock_info")
    else:
        log("stock_info 已是最新")

    cur.close()
    conn.close()
    progress['stage'] = 'stock_info'
    progress['stock_infos'] = len(new_stocks)
    save_progress(progress)

# ========== Stage 2: 补全 daily_price 历史数据 ==========
def backfill_daily_price(progress):
    log("=== Stage 2: 补全 daily_price ===")
    conn = get_conn()
    cur = conn.cursor()

    # 获取已有数据的日期范围
    cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM daily_price")
    row = cur.fetchone()
    if row[0] and row[1]:
        log(f"现有数据范围: {row[0]} ~ {row[1]}")
        start_date = (datetime.strptime(str(row[0]), '%Y-%m-%d') - timedelta(days=5)).strftime('%Y%m%d')
    else:
        start_date = '20240101'

    end_date = datetime.now().strftime('%Y%m%d')
    log(f"回填范围: {start_date} ~ {end_date}")

    # 获取需要回填的股票（主板+创业板）
    cur.execute("""SELECT ts_code FROM stock_info 
                   WHERE market IN ('SZ','SH') AND is_st=0
                   ORDER BY ts_code""")
    all_stocks = [r[0] for r in cur.fetchall()]
    log(f"共 {len(all_stocks)} 只股票需要回填")

    stock_idx = progress.get('stock_idx', 0)
    date_idx = progress.get('date_idx', 0)

    # 分股票批次
    total_stocks = len(all_stocks)
    for si in range(stock_idx, total_stocks, STOCK_BATCH):
        batch_stocks = all_stocks[si:si+STOCK_BATCH]
        pct = si / total_stocks * 100
        log(f"股票进度: {si}/{total_stocks} ({pct:.1f}%)")

        for ts_code in batch_stocks:
            try:
                df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if df is None or df.empty:
                    continue

                # 获取基本面数据（最近交易日）
                trade_dates = sorted(df['trade_date'].tolist())
                if not trade_dates:
                    continue
                latest_date = trade_dates[-1]

                basic = pro.daily_basic(ts_code=ts_code, trade_date=latest_date,
                                        fields='ts_code,trade_date,pe,pb,turnover_rate')
                
                pe = pb = turnover_rate = None
                if basic is not None and not basic.empty:
                    pe = basic['pe'].iloc[0] if 'pe' in basic.columns else None
                    pb = basic['pb'].iloc[0] if 'pb' in basic.columns else None
                    tr = basic['turnover_rate'].iloc[0] if 'turnover_rate' in basic.columns else None
                    if pe is not None and (pe == 0 or str(pe) == 'nan'):
                        pe = None
                    if pb is not None and (str(pb) == 'nan'):
                        pb = None
                    if tr is not None and (str(tr) == 'nan'):
                        turnover_rate = None
                    else:
                        turnover_rate = tr

                rows = []
                for _, drow in df.iterrows():
                    rows.append((
                        drow['ts_code'], drow['trade_date'], drow['open'], drow['high'],
                        drow['low'], drow['close'], drow['pre_close'], drow['vol'],
                        drow['amount'], drow['pct_chg'], pe if drow['trade_date'] == latest_date else None,
                        pb if drow['trade_date'] == latest_date else None,
                        turnover_rate if drow['trade_date'] == latest_date else None
                    ))

                cur.executemany(
                    """INSERT IGNORE INTO daily_price 
                       (ts_code,trade_date,open,high,low,close,pre_close,vol,amount,pct_chg,pe,pb,turnover_rate)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows
                )
            except Exception as e:
                log(f"  {ts_code} 失败: {e}")
                continue

        progress['stock_idx'] = si + STOCK_BATCH
        progress['date_idx'] = 0
        save_progress(progress)
        time.sleep(0.5)  # 避免频率限制

    cur.close()
    conn.close()
    progress['stage'] = 'daily_price_done'
    save_progress(progress)
    log("daily_price 回填完成！")

# ========== Stage 3: 补全市场指数 ==========
def backfill_market_index(progress):
    log("=== Stage 3: 补全 market_index_daily ===")
    indices = [
        ('000001.SH', '上证指数'),
        ('399001.SZ', '深证成指'),
        ('399006.SZ', '创业板指'),
        ('000300.SH', '沪深300'),
        ('000016.SH', '上证50'),
        ('000688.SH', '科创50'),
    ]
    conn = get_conn()
    cur = conn.cursor()

    for ts_code, name in indices:
        try:
            df = pro.index_daily(ts_code=ts_code, start_date='20240101')
            if df is None or df.empty:
                log(f"  {name} 无数据")
                continue
            rows = [(ts_code, r['trade_date'], r['open'], r['high'], r['low'],
                     r['close'], r['vol'], r['pct_chg']) for _, r in df.iterrows()]
            cur.executemany(
                """INSERT IGNORE INTO market_index_daily 
                   (ts_code, trade_date, open, high, low, close, vol, pct_chg)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows
            )
            log(f"  {name}: {len(rows)} 条")
        except Exception as e:
            log(f"  {name} 失败: {e}")
        time.sleep(0.3)

    cur.close()
    conn.close()
    progress['stage'] = 'index_done'
    save_progress(progress)
    log("market_index_daily 回填完成！")

# ========== 主流程 ==========
def main():
    progress = load_progress()
    stage = progress.get('stage', 'init')
    log(f"启动，当前阶段: {stage}")

    if stage == 'init':
        backfill_stock_info(progress)
        backfill_daily_price(progress)
        backfill_market_index(progress)
    elif stage == 'stock_info':
        backfill_daily_price(progress)
        backfill_market_index(progress)
    elif stage == 'daily_price':
        # 重新运行从断点继续（实际上会从 stock_idx 继续）
        backfill_daily_price(progress)
        backfill_market_index(progress)
    else:
        log("数据已完整，或阶段未知，重新开始 stock_info + index")
        backfill_stock_info(progress)
        backfill_market_index(progress)

    log("=== 全部完成 ===")
    # 打印统计
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM daily_price")
    daily_count = cur.fetchone()[0]
    cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM daily_price")
    dr = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM stock_info WHERE market IN ('SZ','SH')")
    stock_count = cur.fetchone()[0]
    log(f"结果: daily_price {daily_count} 条 ({dr[0]}~{dr[1]}), stock_info {stock_count} 条")
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
