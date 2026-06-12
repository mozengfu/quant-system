import json
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host': '127.0.0.1', 'port': 3306, 'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', ''), 'database': 'quant_db', 'unix_socket': '/tmp/mysql.sock'
}
PROGRESS_FILE = Path(__file__).parent / "tech_progress.json"
BATCH_COMMIT = 30000

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def main():
    if PROGRESS_FILE.exists():
        saved = json.load(open(PROGRESS_FILE))
        if saved.get("done"):
            log("已完成，跳过")
            return

    start = time.time()
    conn = pymysql.connect(**DB_CONFIG, autocommit=False)
    cur = conn.cursor()

    # 读取全量数据
    log("读取 daily_price 全量...")
    cur.execute("SELECT ts_code, trade_date, close, vol FROM daily_price ORDER BY ts_code, trade_date ASC")
    rows = cur.fetchall()
    log(f"读取 {len(rows):,} 条，耗时 {time.time()-start:.1f}s")

    # 转为 DataFrame
    df = pd.DataFrame(rows, columns=['ts_code', 'trade_date', 'close', 'vol'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').fillna(0)
    df['vol'] = pd.to_numeric(df['vol'], errors='coerce').fillna(0)
    df = df.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)

    log("计算技术指标...")
    calc_start = time.time()

    grp_close = df.groupby('ts_code')['close']
    grp_vol = df.groupby('ts_code')['vol']

    df['ma5'] = grp_close.transform(lambda x: x.rolling(5, min_periods=1).mean()).round(3).fillna(0)
    df['ma10'] = grp_close.transform(lambda x: x.rolling(10, min_periods=1).mean()).round(3).fillna(0)
    df['ma20'] = grp_close.transform(lambda x: x.rolling(20, min_periods=1).mean()).round(3).fillna(0)

    vol_ma5 = grp_vol.transform(lambda x: x.rolling(5, min_periods=1).mean())
    df['volume_ratio'] = (df['vol'] / vol_ma5.replace(0, 1)).clip(0.1, 20).round(4).fillna(1.0)

    df['high_52w'] = grp_close.transform(lambda x: x.expanding().max()).round(3).fillna(0)
    df['low_52w'] = grp_close.transform(lambda x: x.expanding().min()).round(3).fillna(0)

    chg_20d = grp_close.pct_change(20).fillna(0) * 100
    df['rps_20'] = (50 + chg_20d * 5).clip(1, 99).round(2).fillna(50.0)
    cnt_within = df.groupby('ts_code').cumcount()
    df.loc[cnt_within < 20, 'rps_20'] = 50.0

    log(f"计算完成: {time.time()-calc_start:.1f}s")

    # 检查数据
    log(f"df 列: {df.columns.tolist()}")
    log(f"df dtypes:\n{df.dtypes}")
    log(f"df 前3行:\n{df.head(3)}")

    # trade_date 格式
    log(f"trade_date 类型: {type(df['trade_date'].iloc[0])}")
    log(f"trade_date 示例: {df['trade_date'].iloc[0]}")

    # 构建 UPDATE 数据 - 确保 trade_date 是字符串格式 YYYY-MM-DD
    update_data = []
    for _, row in df.iterrows():
        td = row['trade_date']
        if hasattr(td, 'strftime'):
            td_str = td.strftime('%Y-%m-%d')
        elif isinstance(td, str):
            # 已经是字符串，转换为 YYYY-MM-DD
            if '-' in td:
                td_str = td
            else:
                td_str = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        else:
            td_str = str(td)
        update_data.append((
            float(row['ma5']), float(row['ma10']), float(row['ma20']),
            float(row['volume_ratio']), float(row['high_52w']), float(row['low_52w']),
            float(row['rps_20']), row['ts_code'], td_str
        ))

    log(f"update_data 第一条: {update_data[0]}")
    log(f"update_data 最后一条: {update_data[-1]}")

    # 分批 UPDATE
    log("开始分批 UPDATE...")
    total = len(update_data)
    n_batches = (total + BATCH_COMMIT - 1) // BATCH_COMMIT
    sql = """
        UPDATE daily_price SET
            ma5=%s, ma10=%s, ma20=%s, volume_ratio=%s,
            high_52w=%s, low_52w=%s, rps_20=%s
        WHERE ts_code=%s AND trade_date=%s
    """

    conn.commit()  # 提交读取事务
    t0 = time.time()
    for batch_i in range(n_batches):
        start_i = batch_i * BATCH_COMMIT
        end_i = min((batch_i + 1) * BATCH_COMMIT, total)
        batch = update_data[start_i:end_i]
        cur.executemany(sql, batch)
        conn.commit()
        elapsed = time.time() - t0
        pct = end_i / total * 100
        log(f"  UPDATE {end_i:,}/{total:,} ({pct:.1f}%), 耗时{elapsed:.0f}s")

    cur.close()
    conn.close()

    total_time = time.time() - start
    log(f"=== 完成！总耗时 {total_time:.0f}s ===")

    # 验证
    conn2 = pymysql.connect(**DB_CONFIG, autocommit=True)
    cur2 = conn2.cursor()
    cur2.execute("SELECT COUNT(*) FROM daily_price WHERE ma5 != 0 AND ma5 IS NOT NULL")
    ma5 = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(*) FROM daily_price WHERE volume_ratio > 0")
    vr = cur2.fetchone()[0]
    cur2.execute("SELECT COUNT(*) FROM daily_price WHERE rps_20 != 50")
    rps = cur2.fetchone()[0]
    cur2.execute("SELECT ts_code, trade_date, ma5, ma10, ma20, volume_ratio, rps_20 FROM daily_price WHERE ts_code='000001.SZ' ORDER BY trade_date DESC LIMIT 5")
    rows2 = cur2.fetchall()
    log(f"ma5非零: {ma5:,}, volume_ratio非零: {vr:,}, rps非50: {rps:,}")
    log("000001.SZ 最近5条:")
    for r in rows2:
        print(f"  {r}")
    cur2.close()
    conn2.close()

    with open(PROGRESS_FILE, 'w') as f:
        json.dump({"done": True, "time": datetime.now().isoformat()}, f)

if __name__ == "__main__":
    main()
