#!/usr/bin/env python3
"""
从 QMT xtdata 导出的 JSON 文件导入 MySQL daily_price
替代 tushare update_daily_price_cron.py

用法:
  python3 scripts/import_qmt_daily.py               # 导入已有 JSON
  python3 scripts/import_qmt_daily.py --pull         # 拉取后导入
  python3 scripts/import_qmt_daily.py --pull --followup  # 拉取+导入+后续计算
  python3 scripts/import_qmt_daily.py --ssh-trigger  # SSH触发Win导出+拉取+导入+后续计算
"""
import json, os, sys, subprocess, datetime, time, math, math
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
DATA_DIR = PROJECT_ROOT / "data" / "qmt_daily"
LOG_FILE = PROJECT_ROOT / "logs" / "qmt_import.log"

WIN_HOST = "192.168.10.25"
WIN_USER = "mozf"
WIN_KEY = os.path.expanduser("~/.ssh/id_ed25519_qmt")
WIN_JSON_DIR = "C:/Users/Public/qmt_daily"
WIN_SCRIPT = "C:/Users/Public/qmt_standalone_export.py"

DB_CONFIG = {
    "host": "127.0.0.1", "port": 3306,
    "user": "root", "password": "root123",
    "database": "quant_db", "charset": "utf8mb4",
    "autocommit": True,
}

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def ssh_trigger_export():
    """SSH 到 Windows 触发 QMT 导出，等待完成"""
    log("SSH 触发 Windows 导出...")
    t0 = time.time()
    result = subprocess.run(
        ["ssh", "-i", WIN_KEY, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         f"{WIN_USER}@{WIN_HOST}",
         f"C:\\Python312\\python.exe -u {WIN_SCRIPT}"],
        capture_output=True, timeout=120
    )
    elapsed = time.time() - t0
    stdout = result.stdout.decode("utf-8","replace") if result.stdout else ""
    stderr = result.stderr.decode("gbk","replace") if result.stderr else ""
    if result.returncode == 0:
        log(f"Windows 导出完成 ({elapsed:.0f}s)")
        return True
    else:
        log(f"Windows 导出失败 ({elapsed:.0f}s): {result.stderr[:200]}")
        return False

def pull_from_windows(date_str=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    if date_str:
        fname = f"daily_{date_str}.json"
        remote = f"{WIN_USER}@{WIN_HOST}:{WIN_JSON_DIR}/{fname}"
        local = DATA_DIR / fname
        r = subprocess.run(
            ["scp", "-i", WIN_KEY, "-o", "StrictHostKeyChecking=no", remote, str(local)],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            log(f"拉取: {fname}")
            return [fname]
        log(f"拉取失败 {fname}: {r.stderr[:100]}")
        return []
    result = subprocess.run(
        ["ssh", "-i", WIN_KEY, "-o", "StrictHostKeyChecking=no",
         f"{WIN_USER}@{WIN_HOST}", f"cmd /c dir /b \"{WIN_JSON_DIR}\""],
        capture_output=True, timeout=10
    )
    if result.returncode != 0:
        log(f"SSH dir 失败: {result.stderr.decode("gbk", errors="replace")[:100]}")
        return []
    pulled = []
    lines = result.stdout.decode('utf-8','replace') if isinstance(result.stdout, bytes) else result.stdout
    for fname in sorted([f.strip() for f in lines.strip().split(chr(10)) if f.strip()], reverse=True)[:3]:
        local_path = DATA_DIR / fname
        if local_path.exists():
            continue
        remote = f"{WIN_USER}@{WIN_HOST}:{WIN_JSON_DIR}/{fname}"
        r = subprocess.run(
            ["scp", "-i", WIN_KEY, "-o", "StrictHostKeyChecking=no", remote, str(local_path)],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            pulled.append(fname)
            log(f"拉取: {fname} ({local_path.stat().st_size/1024:.0f}KB)")
    return pulled

def import_to_mysql(date_str=None):
    import pymysql
    json_files = sorted(DATA_DIR.glob("daily_*.json"))
    if date_str:
        json_files = [f for f in json_files if date_str in f.name]
    if not json_files:
        log("没有找到 JSON 文件")
        return False
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    imported_any = False
    for fpath in json_files:
        with open(fpath) as f:
            data = json.load(f)
        trade_date = data["date"]
        stocks = data["stocks"]
        cur.execute("SELECT COUNT(*) FROM daily_price WHERE trade_date=%s", (trade_date,))
        existing = cur.fetchone()[0]
        if existing >= len(stocks) * 0.9:
            log(f"{trade_date}: 已有 {existing} 条, 跳过")
            continue
        def _v(x, t=float):
            if x is None: return 0
            if isinstance(x, float) and math.isnan(x): return 0
            return t(x)
        rows = []
        for s in stocks:
            rows.append((
                s["ts_code"], trade_date,
                _v(s.get("open")), _v(s.get("high")), _v(s.get("low")), _v(s.get("close")),
                _v(s.get("pre_close")), _v(s.get("vol"), int), _v(s.get("amount")),
                _v(s.get("pct_chg")), 0.0, 0.0
            ))
        for i in range(0, len(rows), 500):
            cur.executemany(
                "REPLACE INTO daily_price "
                "(ts_code,trade_date,open,high,low,close,pre_close,"
                "vol,amount,pct_chg,turnover_rate,volume_ratio) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows[i:i+500])
        conn.commit()
        imported_any = True
        log(f"{trade_date}: 导入 {len(rows)} 条")
    cur.close()
    conn.close()
    return imported_any

def run_followup():
    from scripts.update_daily_price_cron import (
        update_rps_20, update_ma, update_stock_pool,
        sync_sector_moneyflow, sync_fina_indicator_incremental, update_positions
    )
    import pymysql
    conn = pymysql.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest = cur.fetchone()[0]
    cur.close(); conn.close()
    log(f"后续计算: 最新交易日 {latest}")
    # 用成分股等权平均填充缺失的板块日线数据（无需akshare）
    try:
        _bc = pymysql.connect(**DB_CONFIG)
        _bc_cur = _bc.cursor()
        _bc_cur.execute("SELECT COUNT(*) FROM board_concept_hist WHERE trade_date=%s", (latest,))
        _has = _bc_cur.fetchone()[0]
        if _has < 100:
            _bc_cur.execute("""
                INSERT INTO board_concept_hist
                  (trade_date, board_code, board_name, pct_change, open, close, high, low, volume, amount)
                SELECT %s, c.board_code, COALESCE(b.board_name, c.board_code),
                       ROUND(AVG(d.pct_chg),2), ROUND(AVG(d.open),2), ROUND(AVG(d.close),2),
                       ROUND(AVG(d.high),2), ROUND(AVG(d.low),2), SUM(d.vol), SUM(d.amount)
                FROM board_concept_cons c
                JOIN daily_price d ON c.ts_code=d.ts_code AND d.trade_date=%s
                LEFT JOIN board_concept b ON c.board_code=b.board_code
                WHERE (c.is_latest=1 OR c.is_latest IS NULL) AND d.close>0.01 AND d.pct_chg IS NOT NULL
                GROUP BY c.board_code
                ON DUPLICATE KEY UPDATE pct_change=VALUES(pct_change),board_name=VALUES(board_name)
            """, (latest, latest))
            _bc.commit()
            log(f"板块日线填充: {_bc_cur.rowcount} 行 (来源:成分股)")
        else:
            log(f"板块日线已存在: {_has} 行, 跳过")
        _bc_cur.close(); _bc.close()
    except Exception as _be:
        log(f"板块日线填充跳过: {_be}")

    update_rps_20()
    update_ma()
    update_stock_pool()
    try:
        sync_sector_moneyflow(latest)
        sync_fina_indicator_incremental()
    except Exception as e:
        log(f"资金流/财务同步跳过: {e}")
    update_positions()
    log("后续计算完成")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", action="store_true", help="从 Windows 拉取 JSON")
    parser.add_argument("--ssh-trigger", action="store_true", help="SSH触发Win导出+拉取+导入+后续")
    parser.add_argument("--date", help="指定日期 YYYYMMDD")
    parser.add_argument("--followup", action="store_true", help="运行后续计算(MA/RPS/股票池)")
    args = parser.parse_args()
    log("=== QMT 数据导入 ===")
    today = args.date or datetime.datetime.now().strftime("%Y%m%d")
    if args.ssh_trigger:
        ok = ssh_trigger_export()
        if ok:
           pull_from_windows(None)
           import_to_mysql(None)
           run_followup()
        else:
           log("SSH 触发失败，尝试拉取已有文件...")
           pull_from_windows(None)
           import_to_mysql(None)
           if args.followup:
               run_followup()
    else:
        if args.pull:
            pull_from_windows(args.date)
        import_to_mysql(args.date)
        if args.followup:
            run_followup()
    log("=== 完成 ===")
