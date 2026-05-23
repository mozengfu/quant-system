#!/usr/bin/env python3
"""Debug: check alpha scores for today's news"""
import json, re, urllib.request
from datetime import datetime
import pymysql
from quant_app.utils.config import get_db_config

ALPHA_THRESHOLD = 60

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

NOISE_PATTERNS = [
    r'[涨跌停]+！?[！！]+', r'重磅[利好利空]', r'速看|紧急|紧急通知',
    r'抄底|逃顶|满仓|空仓', r'利好消息|重大利好|重大利空',
    r'值得关注|重点关注', r'建议关注|建议投资者',
    r'明日预测|下周走势|后市展望', r'复盘|收评|晚评',
    r'必涨|一定涨停|错过拍大腿',
]

def calculate_alpha_score(text):
    alpha_count = sum(1 for kws in SIGNAL_TYPES.values() for kw in kws if kw in text)
    s_score = min(alpha_count * 10, 40)
    data_count = len(re.findall(r'\d+\.?\d*%?', text)) + len(re.findall(r'亿|万|元', text))
    d_score = min(data_count * 5, 30)
    has_company = bool(re.search(r'[\u4e00-\u9fa5]{2,4}(股份|科技|集团)', text))
    has_policy = bool(re.search(r'指导意见|规划|通知', text))
    spec_score = 30 if (has_company or has_policy) else 10
    return s_score + d_score + spec_score

urls = [
    'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=30&page=1',
    'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1',
]
all_news = []
for url in urls:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('result') and data['result'].get('data'):
                for item in data['result']['data']:
                    all_news.append({
                        'title': item.get('title', ''),
                        'content': item.get('summary', '') or item.get('intro', '') or '',
                    })
    except Exception as e:
        print(f'Fetch error: {e}')

seen = set()
unique = []
for n in all_news:
    key = n['title'][:20]
    if key not in seen:
        seen.add(key)
        unique.append(n)

print(f'Total unique news: {len(unique)}')
print()

high_value = []
for item in unique:
    title = item.get('title', '')
    content = item.get('content', '')
    full_text = title + ' ' + content
    
    # Noise filter
    if any(re.search(p, full_text) for p in NOISE_PATTERNS):
        continue
    
    score = calculate_alpha_score(full_text)
    if score >= ALPHA_THRESHOLD:
        high_value.append({'title': title, 'score': score, 'content': content[:100]})

print(f'High-value (score >= {ALPHA_THRESHOLD}): {len(high_value)}')
for h in sorted(high_value, key=lambda x: -x['score'])[:10]:
    t = h['title'][:80]
    print(f"  Score={h['score']} | {t}")
