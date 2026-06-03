# -*- coding: gbk -*-
"""
国信 iQuant 量化桥接策略
功能：从 MySQL 读取交易信号 → 通过 passorder 自动下单
使用方法：在 iQuant 中新建 Python 模型，粘贴此代码，运行即可
"""

import json
import time as _time

# iQuant 策略必须实现的两个函数
def init(ContextInfo):
    """初始化"""
    # 输出到 iQuant 输出窗口
    print("=" * 50)
    print("量化桥接策略 v1.0 已启动")
    print("等待交易信号...")
    print("=" * 50)
    
    # 每30秒检查一次 MySQL 信号
    ContextInfo.run_time("check_signals", 30, "nSecond", 0)


def handlebar(ContextInfo):
    """每根K线/每个tick调用（这里不用，我们用定时器）"""
    pass


def check_signals(ContextInfo):
    """定时检查 MySQL 中的待执行信号"""
    try:
        import pymysql
        
        # 连接数据库
        conn = pymysql.connect(
            host="192.168.10.30",
            port=3306,
            user="root",
            password="root123",
            database="quant_db",
            charset="utf8mb4"
        )
        cur = conn.cursor()
        
        # 查询待执行的买入信号
        sql = """SELECT id, ts_code, action, price, amount 
                 FROM sim_signals 
                 WHERE status='待执行' AND action='BUY'
                 ORDER BY created_at ASC LIMIT 1"""
        cur.execute(sql)
        row = cur.fetchone()
        
        if row:
            sig_id, ts_code, action, price, amount = row
            print(f"发现信号: {ts_code} {action} {amount}股 @ {price}")
            
            # 转换代码格式: 301396.SZ -> 301396.SZ (已有market后缀)
            # 或者 300438 -> 300438.SZ
            code = ts_code.strip()
            if "." not in code:
                code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            
            # 查询资金账号
            try:
                accounts = get_trade_detail_data("", "STOCK", "ACCOUNT")
                if accounts:
                    aid = accounts[0].m_strAccountID
                    print(f"使用账号: {aid}")
                    
                    # 下单买入
                    # passorder(23=买入, 1101=限价, accountid, 代码, 11=限价, 价格, 数量, ContextInfo)
                    order_id = passorder(23, 1101, aid, code, 11, float(price), int(amount), ContextInfo)
                    print(f"下单返回: {order_id}")
                    
                    if order_id and order_id > 0:
                        # 更新信号状态
                        cur.execute("UPDATE sim_signals SET status='已执行', executed_at=NOW(), order_ref=%s WHERE id=%s", 
                                   (str(order_id), sig_id))
                        conn.commit()
                        print(f"✅ 已执行: {code} {amount}股 @ {price} (订单:{order_id})")
                    else:
                        print(f"❌ 下单失败")
                else:
                    print("❌ 未找到资金账号")
            except Exception as e:
                print(f"❌ 交易异常: {e}")
        
        cur.close()
        conn.close()
        
    except ImportError:
        print("⚠️ 未安装 pymysql，无法连接数据库")
    except Exception as e:
        print(f"⚠️ 检查信号异常: {e}")
