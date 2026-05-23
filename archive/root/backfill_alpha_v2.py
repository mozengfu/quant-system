#!/usr/bin/env python3
"""
Alpha历史新闻回补 V2 — 用新浪财经翻页API回补
不限流，速度快
"""
import sys, os, re, json, time, urllib.request
import pymysql
from datetime import datetime, timedelta
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
    signal_type = "其他"
    sentiment = 0.0
    for stype, keywords in SIGNAL_TYPES.items():
        if any(kw in text for kw in keywords):
            signal_type = stype
            break
    negatives = ['减持', '退市', '立案调查', '风险提示', '预降', '亏损', '下滑']
    if any(kw in text for kw in negatives):
        sentiment = -0.5
        if '立案调查' in text or '退市' in text: sentiment = -1.0
    else:
        if signal_type in ['业绩', '业务', '产业', '价格']: sentiment = 1.0
        elif signal_type == 'risk': sentiment = -0.8
        elif signal_type == '资本':
            if '减持' in text: sentiment = -0.6
            elif '回购' in text or '增持' in text: sentiment = 0.8
            else: sentiment = 0.2
    return signal_type, sentiment

def extract_stock_codes(text, conn):
    found = []
    cur = conn.cursor()
    codes = re.findall(r'(\d{6})', text)
    codes = list(set(codes))
    for code in codes:
        if code.startswith('6'): suffix = 'SH'
        elif code.startswith('3') or code.startswith('0'): suffix = 'SZ'
        else: continue
        ts_code = f"{code}.{suffix}"
        cur.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
        row = cur.fetchone()
        if row: found.append({'ts_code': ts_code, 'name': row[0]})
    if not found:
        companies = re.findall(r'([\u4e00-\u9fa5]{2,6}(股份|科技|集团|公司|银行|电子|药业|证券))', text)
        for comp, _ in companies[:5]:
            cur.execute("SELECT ts_code, name FROM stock_info WHERE name LIKE %s LIMIT 1", (f"%{comp}%",))
            row = cur.fetchone()
            if row and row[0] not in [f['ts_code'] for f in found]:
                found.append({'ts_code': row[0], 'name': row[1]})
    cur.close()
    return found

def fetch_sina_news_pages(max_pages=50):
    """用新浪财经翻页拉取历史新闻（不限流）"""
    all_news = []
    for page in range(1, max_pages + 1):
        urls = [
            f'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=30&page={page}',
            f'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=30&page={page}',
        ]
        page_found = False
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                    'Referer': 'https://finance.sina.com.cn/'
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    if data.get('result') and data['result'].get('data'):
                        for item in data['result']['data']:
                            ctime = int(item.get('ctime', 0))
                            if ctime == 0: continue
                            dt = datetime.fromtimestamp(ctime)
                            all_news.append({
                                'title': item.get('title', ''),
                                'content': item.get('summary', '') or item.get('intro', '') or '',
                                'time': dt,
                                'date': dt.strftime('%Y-%m-%d'),
                            })
                            page_found = True
            except Exception as e:
                pass
        if not page_found:
            print(f"  第 {page} 页无数据，停止翻页")
            break
        if page % 10 == 0:
            print(f"  已拉取 {page} 页, {len(all_news)} 条新闻")
        time.sleep(0.5)  # 每页0.5秒
    return all_news

def main():
    conn = pymysql.connect(**DB_CONFIG)
    
    # 获取已有日期
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT signal_date FROM alpha_signals")
    existing_dates = set()
    for row in cur.fetchall():
        d = row[0]
        if isinstance(d, datetime): d = d.strftime('%Y-%m-%d')
        existing_dates.add(d)
    cur.close()
    
    print(f"已有 {len(existing_dates)} 天数据，开始拉取历史新闻...")
    
    news = fetch_sina_news_pages(max_pages=30)
    print(f"\n拉取到 {len(news)} 条历史新闻")
    
    # 获取交易日列表（用于过滤非交易日）
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT 60")
    trade_dates = set()
    for row in cur.fetchall():
        d = row[0]
        if isinstance(d, datetime): d = d.strftime('%Y-%m-%d')
        else: d = str(d)[:10]
        trade_dates.add(d)
    cur.close()
    print(f"最近交易日: {len(trade_dates)} 天")
    
    # 过滤非交易日新闻
    news = [n for n in news if n['date'] in trade_dates]
    print(f"交易日新闻: {len(news)} 条")
    
    # 去重
    seen = set()
    unique_news = []
    for n in news:
        key = n['title'][:30] + n['date']
        if key not in seen:
            seen.add(key)
            unique_news.append(n)
    news = unique_news
    print(f"去重后: {len(news)} 条")
    
    # 分析并写入
    total_signals = 0
    skipped_existing = 0
    for n in news:
        text = f"{n['title']} {n['content']}"
        if len(text) < 5: continue
        
        stocks = extract_stock_codes(text, conn)
        if not stocks: continue
        
        signal_type, sentiment = classify_signal(text)
        if sentiment == 0.0: continue
        
        for stock in stocks:
            date_str = n['date']
            if date_str in existing_dates:
                skipped_existing += 1
                continue
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO alpha_signals 
                    (ts_code, name, signal_date, signal_type, score_boost, source)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (stock['ts_code'], stock['name'], date_str, signal_type, sentiment, f"sina:{n['title'][:50]}"))
                conn.commit()
                total_signals += 1
                cur.close()
            except Exception as e:
                if 'Duplicate' not in str(e):
                    print(f"  插入失败: {stock['ts_code']} - {e}")
    
    conn.close()
    print(f"\n回补完成! 新增信号: {total_signals}, 跳过已有: {skipped_existing}")

if __name__ == '__main__':
    main()
