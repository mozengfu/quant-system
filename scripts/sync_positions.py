#!/usr/bin/env python3
"""从 QMT 同步真实持仓到 sim_positions（通过 IPC 查询 iQuant 策略）

只能在 Win QMT 机上跑 (Mac 上跑会产生错位文件名)
"""
import os, sys, json, time, platform

# 平台检查: 防止在 Mac/Linux 上跑产生 "C:\Users\..." 错位文件名
if platform.system() != "Windows":
    print("⚠️ sync_positions.py 只能在 Win QMT 机上跑")
    print(f"   当前系统: {platform.system()} ({platform.release()})")
    print("   Mac 端请用 HTTP 桥: scripts/live_trading_scheduler.py status")
    sys.exit(1)

CMD_FILE = r"C:\Users\18978\qmt_cmd.json"
RESULT_FILE = r"C:\Users\18978\qmt_result.json"
DB_CONFIG = {"host":"192.168.10.30","port":3306,"user":"root","password":"root123","database":"quant_db"}

def main():
    # 清理旧文件
    for f in [CMD_FILE, RESULT_FILE]:
        try: os.remove(f)
        except: pass
    # 发送 POSITION 命令
    cmd = {"id":"sync","action":"POSITION","status":"pending","code":"","price":0,"amount":0}
    with open(CMD_FILE,"w",encoding="utf-8") as f:
        json.dump(cmd, f)
    # 等待策略响应（最多 60 秒）
    for _ in range(600):
        time.sleep(0.1)
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE,"r",encoding="utf-8") as f:
                    r = json.load(f)
                if r.get("status") == "ok" and r.get("positions"):
                    # 更新 sim_positions
                    import pymysql
                    conn = pymysql.connect(**DB_CONFIG)
                    cur = conn.cursor()
                    for p in r["positions"]:
                        ts = p["ts_code"]
                        shares = int(p["shares"])
                        cost = float(p.get("cost_price",0))
                        name = p.get("stock_name","")
                        cur.execute("""
                            INSERT INTO sim_positions (ts_code,stock_name,shares,cost_price,created_at,status,updated_at) 
                            VALUES (%s,%s,%s,%s,NOW(),'HOLD',NOW())
                            ON DUPLICATE KEY UPDATE shares=%s,cost_price=%s,updated_at=NOW(),status='HOLD'
                        """, (ts,name,shares,cost,shares,cost))
                    conn.commit()
                    cur.close()
                    conn.close()
                    print(f"Synced {len(r['positions'])} positions: {[p['ts_code']+'('+str(p['shares'])+')' for p in r['positions']]}")
                    # 清理文件
                    try: os.remove(CMD_FILE)
                    except: pass
                    try: os.remove(RESULT_FILE)
                    except: pass
                    return
            except: pass
    print("Sync timeout - strategy not responding")

if __name__ == "__main__":
    main()
