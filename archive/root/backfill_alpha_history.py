#!/usr/bin/env python3
"""
Alpha历史新闻回补脚本
从Tushare回补最近30天的财经新闻，用alpha_filter.py的逻辑分析并写入alpha_signals表
每批间隔60秒（Tushare限流1次/分钟）
"""
import sys, os, re, time, json
import urllib.request
import pymysql
import pandas as pd
from datetime import datetime, timedelta
import tushare as ts
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    'host': 'localhost',
    'unix_socket': '/tmp/mysql.sock',
    'user': 'root',
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': 'quant_db',
    'charset': 'utf8mb4'
}

SIGNAL_TYPES = {
    '业绩': ['净利润', '营收', '同比', '环比', '季报', '年报', '中报'],
    '业务': ['中标', '签约', '合作', '投产', '扩建', '订单'],
    '产业': ['政策', '补贴', '产业规划', '指导意见'],
    '价格': ['涨价', '提价', '上调', '降价'],
    '资本': ['增持', '减持', '回购', '定增', '配股'],
    '风险': ['立案调查', '处罚', '警示', '违规', '退市', 'ST'],
}

def classify_signal(text):
    """判断信号类型和情感"""
    signal_type = "其他"
    sentiment = 0.0
    
    for stype, keywords in SIGNAL_TYPES.items():
        if any(kw in text for kw in keywords):
            signal_type = stype
            break
    
    negatives = ['减持', '退市', '立案调查', '风险提示', '预降', '亏损', '下滑']
    if any(kw in text for kw in negatives):
        sentiment = -0.5
        if '立案调查' in text or '退市' in text:
            sentiment = -1.0
    else:
        if signal_type in ['业绩', '业务', '产业', '价格']:
            sentiment = 1.0
        elif signal_type == '风险':
            sentiment = -0.8
        elif signal_type == '资本':
            if '减持' in text: sentiment = -0.6
            elif '回购' in text or '增持' in text: sentiment = 0.8
            else: sentiment = 0.2
    
    return signal_type, sentiment

def extract_stock_codes(text, conn):
    """从文本中提取股票代码并查名称"""
    found = []
    cur = conn.cursor()
    
    codes = re.findall(r'(\d{6})', text)
    codes = list(set(codes))
    for code in codes:
        if code.startswith('6'):
            suffix = 'SH'
        elif code.startswith('3') or code.startswith('0'):
            suffix = 'SZ'
        else:
            continue
        
        ts_code = f"{code}.{suffix}"
        cur.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
        row = cur.fetchone()
        if row:
            found.append({'ts_code': ts_code, 'name': row[0]})
    
    # 名称模糊匹配
    if not found:
        companies = re.findall(r'([\u4e00-\u9fa5]{2,6}(股份|科技|集团|公司|银行|电子|药业|证券))', text)
        for comp, _ in companies[:5]:
            cur.execute("SELECT ts_code, name FROM stock_info WHERE name LIKE %s LIMIT 1", (f"%{comp}%",))
            row = cur.fetchone()
            if row and row[0] not in [f['ts_code'] for f in found]:
                found.append({'ts_code': row[0], 'name': row[1]})
    
    cur.close()
    return found

def fetch_tushare_news(pro, date_str):
    """从Tushare获取指定日期的新闻"""
    try:
        # 每5天一批，减少API调用
        dt = datetime.strptime(date_str, '%Y%m%d')
        end_dt = dt + timedelta(days=4)
        end_str = end_dt.strftime('%Y%m%d')
        
        df = pro.news(src='sina', start_date=date_str, end_date=end_str, limit=100)
        if df is not None and len(df) > 0:
            print(f"  {date_str}~{end_str}: {len(df)} 条新闻")
            return df
        else:
            print(f"  {date_str}~{end_str}: 无数据")
            return pd.DataFrame()
    except Exception as e:
        print(f"  {date_str}: 获取失败 - {e}")
        return pd.DataFrame()

def main():
    conn = pymysql.connect(**DB_CONFIG)
    pro = ts.pro_api(os.environ.get('TUSHARE_TOKEN', ''))
    
    # 计算日期范围（最近30个交易日）
    end_date = datetime.now() - timedelta(days=1)
    start_date = end_date - timedelta(days=45)  # 多取几天确保覆盖30个交易日
    
    # 获取交易日列表
    cal = pro.trade_cal(exchange='SSE', start_date=start_date.strftime('%Y%m%d'), 
                        end_date=end_date.strftime('%Y%m%d'))
    trade_dates = cal[cal['is_open']==1]['cal_date'].tolist()
    trade_dates = [d for d in trade_dates if d < end_date.strftime('%Y%m%d')]
    
    # 去重：已处理过的日期跳过
    cur = conn.cursor()
    existing_dates = set()
    cur.execute("SELECT DISTINCT signal_date FROM alpha_signals")
    for row in cur.fetchall():
        existing_dates.add(row[0].strftime('%Y-%m-%d') if isinstance(row[0], datetime) else str(row[0]))
    
    cur.close()
    print(f"已存在信号的日期: {sorted(existing_dates)}")
    
    total_news = 0
    total_signals = 0
    
    # 按交易日处理
    for trade_date in trade_dates:
        date_formatted = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
        
        if date_formatted in existing_dates:
            print(f"跳过 {date_formatted} (已有数据)")
            continue
        
        print(f"\n处理 {date_formatted}...")
        
        # 获取新闻
        news_df = fetch_tushare_news(pro, trade_date)
        if news_df.empty:
            continue
        
        total_news += len(news_df)
        
        # 分析每条新闻
        new_signals = []
        for _, row in news_df.iterrows():
            title = str(row.get('title', ''))
            content = str(row.get('content', ''))
            text = f"{title} {content}"
            
            if len(text) < 5:
                continue
            
            # 提取股票
            stocks = extract_stock_codes(text, conn)
            if not stocks:
                continue
            
            # 分类打分
            signal_type, sentiment = classify_signal(text)
            if sentiment == 0.0:
                continue  # 跳过中性信号
            
            for stock in stocks:
                new_signals.append((
                    stock['ts_code'], stock['name'], date_formatted,
                    signal_type, sentiment, f"sina:{title[:50]}"
                ))
        
        # 写入数据库
        if new_signals:
            cur = conn.cursor()
            for sig in new_signals:
                try:
                    cur.execute("""
                        INSERT INTO alpha_signals 
                        (ts_code, name, signal_date, signal_type, score_boost, source)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, sig)
                    total_signals += 1
                except Exception as e:
                    print(f"  插入失败: {sig[0]} - {e}")
            conn.commit()
            cur.close()
            print(f"  写入 {len(new_signals)} 条信号")
        
        # Tushare限流：每批等60秒
        print(f"  等待60秒（限流保护）...")
        time.sleep(60)
    
    conn.close()
    print(f"\n回补完成! 总新闻: {total_news}, 新信号: {total_signals}")

if __name__ == '__main__':
    main()
