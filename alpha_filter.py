#!/usr/bin/env python3
"""
A股Alpha内容过滤器 V3 — 模型集成版
1. 过滤噪音，提取Alpha新闻
2. 识别新闻涉及的股票
3. 将非结构化新闻转化为结构化信号存入数据库
4. 供ML模型读取作为新增特征
"""

import json
import os
import re
import pymysql
import urllib.request
import urllib.parse
from datetime import datetime

# ============ 配置 ============
ALPHA_THRESHOLD = 60  # 最低Alpha得分
MAX_ITEMS = 5  # 最多推送条数

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

# 信号分类定义
SIGNAL_TYPES = {
    '业绩': ['业绩预增', '预增', '净利润增长', '营收增长', '业绩预告', '年报', '季报', '营收'],
    '政策': ['政策', '新规', '指导意见', '规划', '通知', '实施方案', '若干措施'],
    '业务': ['中标', '签订', '合同', '订单'],
    '资本': ['重组', '并购', '定增', '减持', '回购'],
    '产业': ['产能', '投产', '扩产', '生产线', '首次', '量产', '批量供货'],
    '价格': ['涨价', '提价', '调价'],
    '技术': ['突破', '获批', '通过', '上市'],
    '风险': ['风险提示', '退市', '立案调查'],
}

# 噪音词库（标题党、情绪化、滞后信息）
NOISE_PATTERNS = [
    r'[涨跌停]+！?[！！]+', r'重磅[利好利空]', r'速看|紧急|紧急通知',
    r'抄底|逃顶|满仓|空仓', r'利好消息|重大利好|重大利空',
    r'值得关注|重点关注', r'建议关注|建议投资者',
    r'明日预测|下周走势|后市展望', r'复盘|收评|晚评',
    r'必涨|一定涨停|错过拍大腿',
]
NOISE_KEYWORDS = ['大v说', '股评', '明日', '下周', '抄底', '逃顶']

# 股票名称与代码的正则 (A股)
STOCK_CODE_PATTERNS = [
    r'([\d]{6})\.S[Z|H]',  # 300001.SZ 或 600519.SH
    r'([\d]{6})',          # 纯6位代码 (需验证)
    r'[（(](\d{6})[）)]',  # （600519）
]

def init_signals_table(conn):
    """初始化 Alpha 信号表"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alpha_signals (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ts_code VARCHAR(20) NOT NULL,
            name VARCHAR(50),
            signal_date DATE NOT NULL,
            signal_type VARCHAR(20),
            score_boost DECIMAL(5,2) DEFAULT 0,
            source VARCHAR(100),
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_date_stock (signal_date, ts_code),
            INDEX idx_type (signal_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()

def extract_stock_info(text, conn):
    """从文本中提取股票名称和代码"""
    found_stocks = []
    cur = conn.cursor()
    
    # 1. 提取代码
    codes = []
    for pattern in STOCK_CODE_PATTERNS:
        codes.extend(re.findall(pattern, text))
    
    codes = list(set(codes))
    for code in codes:
        # 标准化代码
        if code.startswith('6'): suffix = 'SH'
        elif code.startswith('3') or code.startswith('0'): suffix = 'SZ'
        else: continue
        
        ts_code = f"{code}.{suffix}"
        # 查库获取名称
        cur.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
        row = cur.fetchone()
        if row:
            found_stocks.append({'ts_code': ts_code, 'name': row[0]})
    
    # 2. 如果没找到代码，尝试匹配名称 (模糊匹配前4-6个字)
    if not found_stocks:
        # 提取所有可能是公司名的实体 (简化版：连续汉字+公司/股份/集团等)
        companies = re.findall(r'([\u4e00-\u9fa5]{2,6}(股份|科技|集团|公司|银行|电子|药业|证券))', text)
        if companies:
            for comp, suffix in companies:
                full_name = comp + suffix
                # 尝试在 stock_info 中模糊查询
                cur.execute("SELECT ts_code, name FROM stock_info WHERE name LIKE %s LIMIT 1", (f"%{comp}%",))
                row = cur.fetchone()
                if row:
                    found_stocks.append({'ts_code': row[0], 'name': row[1]})
    
    cur.close()
    # 去重
    seen = set()
    unique = []
    for s in found_stocks:
        if s['ts_code'] not in seen:
            seen.add(s['ts_code'])
            unique.append(s)
    return unique

def classify_signal(text):
    """判断信号类型和情感倾向 (1.0 利好, -1.0 利空)"""
    signal_type = "其他"
    sentiment = 0.0
    
    for stype, keywords in SIGNAL_TYPES.items():
        if any(kw in text for kw in keywords):
            signal_type = stype
            break
            
    # 情感判定逻辑
    # 利空关键词
    negatives = ['减持', '退市', '立案调查', '风险提示', '预降', '亏损', '下滑']
    if any(kw in text for kw in negatives):
        sentiment = -0.5
        if '立案调查' in text or '退市' in text: sentiment = -1.0
    else:
        # 默认利好 (除非是中性描述)
        if signal_type in ['业绩', '业务', '产业', '价格']: sentiment = 1.0
        elif signal_type == '风险': sentiment = -0.8
        elif signal_type == '资本':
            if '减持' in text: sentiment = -0.6
            elif '回购' in text or '增持' in text: sentiment = 0.8
            else: sentiment = 0.2
            
    return signal_type, sentiment

def calculate_alpha_score(text):
    """Alpha得分 (0-100) - 简化版"""
    # 1. 信号匹配度 (40分)
    alpha_count = sum(1 for kws in SIGNAL_TYPES.values() for kw in kws if kw in text)
    s_score = min(alpha_count * 10, 40)
    
    # 2. 数据密度 (30分) - 有具体数字 (%, 亿元) 加分
    data_count = len(re.findall(r'\d+\.?\d*%?', text)) + len(re.findall(r'亿|万|元', text))
    d_score = min(data_count * 5, 30)
    
    # 3. 特异性 (30分) - 出现具体公司名、政策文件名的加分
    has_company = bool(re.search(r'[\u4e00-\u9fa5]{2,4}(股份|科技|集团)', text))
    has_policy = bool(re.search(r'指导意见|规划|通知', text))
    spec_score = 30 if (has_company or has_policy) else 10
    
    return s_score + d_score + spec_score

def fetch_news_from_sina():
    """获取新浪财经新闻"""
    urls = [
        'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=30&page=1',
        'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1',
    ]
    all_news = []
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
                        all_news.append({
                            'title': item.get('title', ''),
                            'content': item.get('summary', '') or item.get('intro', '') or '',
                            'source': item.get('media_name', '') or item.get('channel_name', ''),
                            'time': datetime.fromtimestamp(int(item.get('ctime', 0))).strftime('%Y-%m-%d %H:%M') if item.get('ctime') else '',
                        })
        except Exception as e:
            print(f"获取新闻失败: {e}")
    
    # 去重
    seen = set()
    unique = []
    for n in all_news:
        key = n['title'][:20]
        if key not in seen:
            seen.add(key)
            unique.append(n)
    return unique

if __name__ == '__main__':
    print("="*60)
    print("Alpha过滤器 V3 — 模型集成测试")
    print("="*60)
    
    # 1. 获取新闻
    print("正在获取新闻...")
    news = fetch_news_from_sina()
    if not news:
        print("未获取到新闻")
        exit()
    print(f"获取到 {len(news)} 条原始新闻")
    
    # 2. 过滤 + 结构化
    conn = pymysql.connect(**DB_CONFIG)
    init_signals_table(conn)
    
    today = datetime.now().strftime('%Y-%m-%d')
    cur = conn.cursor()
    
    # 清理今日旧信号 (幂等性)
    if input("确认写入数据库? (y/N): ").lower() != 'y':
        print("已取消")
        exit()
    cur.execute("DELETE FROM alpha_signals WHERE signal_date = %s", (today,))
    
    valid_signals = []
    
    for item in news:
        title = item.get('title', '')
        content = item.get('content', '')
        full_text = title + ' ' + content
        
        # 简单噪音过滤
        if any(re.search(p, full_text) for p in NOISE_PATTERNS):
            continue
            
        score = calculate_alpha_score(full_text)
        if score < ALPHA_THRESHOLD:
            continue
            
        # 提取股票
        stocks = extract_stock_info(full_text, conn)
        sig_type, sentiment = classify_signal(full_text)
        
        if stocks:
            for stock in stocks:
                # 如果该股票今日已有信号，不重复插入
                cur.execute("SELECT COUNT(*) FROM alpha_signals WHERE ts_code=%s AND signal_date=%s", (stock['ts_code'], today))
                if cur.fetchone()[0] == 0:
                    boost = score / 100 * sentiment  # 归一化 boost
                    cur.execute("""
                        INSERT INTO alpha_signals 
                        (ts_code, name, signal_date, signal_type, score_boost, source, detail)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        stock['ts_code'], stock['name'], today, sig_type,
                        round(boost, 2), item['source'], title
                    ))
                    valid_signals.append({
                        'stock': stock['name'],
                        'type': sig_type,
                        'boost': round(boost, 2),
                        'news': title
                    })
        else:
            # 宏观新闻，不关联特定股票，但记录下来
            if sig_type == '政策' or '宏观' in full_text:
                valid_signals.append({
                    'stock': '全市场',
                    'type': sig_type,
                    'boost': 0,
                    'news': title
                })
    
    conn.commit()
    conn.close()
    
    print(f"\n处理完成! 提取到 {len(valid_signals)} 条有效Alpha信号")
    print("-"*60)
    for s in valid_signals:
        print(f"【{s['stock']}】{s['type']} | 模型Boost: {s['boost']:+.2f} | {s['news']}")
