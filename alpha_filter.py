#!/usr/bin/env python3
"""
A股Alpha内容过滤器 V4 — 多频道增强版
1. 从新浪财经多频道获取新闻
2. 过滤噪音，提取Alpha信号
3. 识别新闻涉及的股票并写入 alpha_signals 表
4. 供ML模型读取作为新增特征
"""

import json
import re
import urllib.request
from datetime import datetime

import pymysql

from quant_app.utils.config import get_db_config

DB_CONFIG = get_db_config()

# ============ 配置 ============
ALPHA_THRESHOLD = 50   # V3:60 → V4:50，提升召回
MAX_ITEMS = 10

# 信号分类
SIGNAL_TYPES = {
    '业绩': ['业绩预增', '预增', '净利润增长', '营收增长', '业绩预告', '年报', '季报', '营收',
             '净利润', '同比', '环比', '中报', '扭亏', '大幅增长', '大幅上升'],
    '政策': ['政策', '新规', '指导意见', '规划', '通知', '实施方案', '若干措施', '补贴', '产业规划',
             '印发', '推动', '鼓励', '支持', '促进'],
    '业务': ['中标', '签订', '合同', '订单', '签约', '合作', '投产', '扩建', '交付', '供货',
             '获得订单', '重大合同'],
    '资本': ['重组', '并购', '定增', '减持', '回购', '增持', '配股', '分红', '送转'],
    '产业': ['产能', '投产', '扩产', '生产线', '首次', '量产', '批量供货', '扩建', '新产线'],
    '价格': ['涨价', '提价', '调价', '上调', '降价', '价格上调'],
    '技术': ['突破', '获批', '通过', '上市', '研发', '专利', '获得批准'],
    '风险': ['风险提示', '退市', '立案调查', '处罚', '警示', '违规', 'ST', '戴帽'],
}

# 噪音词库 — 收紧
NOISE_PATTERNS = [
    r'[涨跌停]+！?[！！]+', r'重磅[利好利空]',
    r'抄底|逃顶|满仓|空仓', r'值得关注|重点关注',
    r'明日预测|下周走势|后市展望', r'复盘|收评|晚评',
    r'必涨|一定涨停|错过拍大腿',
]
NOISE_KEYWORDS = ['大v说', '股评', '明日', '下周', '抄底', '逃顶']

# 新浪财经频道ID（覆盖宏观/产经/公司/个股等）
SINA_CHANNELS = [
    # lid=频道ID, num=每页条数
    {'pageid': 153, 'lid': 2516, 'num': 30},   # 新浪财经-重点
    {'pageid': 153, 'lid': 2509, 'num': 30},   # 新浪财经-宏观
    {'pageid': 153, 'lid': 2510, 'num': 30},   # 新浪财经-产经
    {'pageid': 153, 'lid': 2512, 'num': 30},   # 新浪财经-公司
    {'pageid': 153, 'lid': 2513, 'num': 30},   # 新浪财经-行业
    {'pageid': 155, 'lid': 2565, 'num': 30},   # 新浪财经-要闻
    {'pageid': 155, 'lid': 2564, 'num': 30},   # 新浪财经-证券
]

# 公司名后缀，用于名称匹配
COMPANY_SUFFIXES = '股份|科技|集团|公司|银行|电子|药业|证券|医药|通信|软件|能源|材料|工业|实业|控股|发展|建设|装备|航空|航天|电力|地产|传媒|食品|汽车|化工|机械'


def init_signals_table(conn):
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
    """V4: 代码匹配 + 名称模糊匹配"""
    found = []
    cur = conn.cursor()

    # 1. 提取 A 股代码
    for m in re.finditer(r'(6\d{5})|(0\d{5})|(3\d{5})', text):
        code = m.group(0)
        suffix = 'SH' if code.startswith('6') else 'SZ'
        ts_code = f"{code}.{suffix}"
        cur.execute("SELECT name FROM stock_info WHERE ts_code=%s", (ts_code,))
        row = cur.fetchone()
        if row:
            found.append({'ts_code': ts_code, 'name': row[0]})

    # 2. 名称匹配（无代码时走）
    if not found:
        companies = re.findall(
            rf'([\u4e00-\u9fa5]{{2,6}}({COMPANY_SUFFIXES}))', text
        )
        matched = set()
        for full_name, _ in companies[:10]:
            prefix = full_name[:4]
            cur.execute(
                "SELECT ts_code, name FROM stock_info WHERE name LIKE %s LIMIT 1",
                (f"%{prefix}%",)
            )
            row = cur.fetchone()
            if row and row[0] not in matched:
                found.append({'ts_code': row[0], 'name': row[1]})
                matched.add(row[0])

    cur.close()
    return found


def classify_signal(text):
    sig_type = "其他"
    sentiment = 0.0
    for stype, keywords in SIGNAL_TYPES.items():
        if any(kw in text for kw in keywords):
            sig_type = stype
            break
    negatives = ['减持', '退市', '立案调查', '风险提示', '预降', '亏损', '下滑', '利空']
    if any(kw in text for kw in negatives):
        sentiment = -0.5
        if '立案调查' in text or '退市' in text:
            sentiment = -1.0
    else:
        if sig_type in ['业绩', '业务', '产业', '价格']:
            sentiment = 1.0
        elif sig_type == '风险':
            sentiment = -0.8
        elif sig_type == '资本':
            if '减持' in text:
                sentiment = -0.6
            elif '回购' in text or '增持' in text:
                sentiment = 0.8
            else:
                sentiment = 0.2
    return sig_type, sentiment


def calculate_alpha_score(text):
    """V4 评分（0-100）"""
    # 信号密度 (40分)
    kw_count = sum(1 for kw_list in SIGNAL_TYPES.values() for kw in kw_list if kw in text)
    s_score = min(kw_count * 10, 40)

    # 数据密度 (30分)
    data_count = len(re.findall(r'\d+\.?\d*%?', text)) + len(re.findall(r'亿|万|元', text))
    d_score = min(data_count * 5, 30)

    # 特异性 (30分)
    has_company = bool(re.search(rf'[\u4e00-\u9fa5]{{2,6}}({COMPANY_SUFFIXES})', text))
    has_code = bool(re.search(r'(6|0|3)\d{5}', text))
    has_policy = bool(re.search(r'指导意见|规划|通知|方案|措施', text))
    spec_score = 30 if (has_company or has_code or has_policy) else 10

    # 代码加分
    code_bonus = 5 if has_code else 0

    return min(s_score + d_score + spec_score + code_bonus, 100)


def fetch_news():
    """从新浪财经多频道获取新闻"""
    seen = set()
    all_news = []

    for ch in SINA_CHANNELS:
        url = (f'https://feed.mix.sina.com.cn/api/roll/get?'
               f'pageid={ch["pageid"]}&lid={ch["lid"]}&k=&num={ch["num"]}&page=1')
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
                'Referer': 'https://finance.sina.com.cn/'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if data.get('result') and data['result'].get('data'):
                    count = 0
                    for item in data['result']['data']:
                        title = item.get('title', '')
                        key = title[:20]
                        if key not in seen:
                            seen.add(key)
                            all_news.append({
                                'title': title,
                                'content': (item.get('summary', '') or
                                            item.get('intro', '') or
                                            item.get('text', '') or ''),
                                'source': item.get('media_name', '') or
                                          item.get('channel_name', '') or '新浪财经',
                                'time': datetime.fromtimestamp(
                                    int(item.get('ctime', 0))
                                ).strftime('%Y-%m-%d %H:%M') if item.get('ctime') else '',
                            })
                            count += 1
                    print(f"  频道{ch['lid']}: {count} 条")
        except Exception as e:
            print(f"  频道{ch['lid']} 获取失败: {e}")

    print(f"  合计: {len(all_news)} 条（去重后）")
    return all_news


def process_news(news_items, conn, today, cur):
    """处理新闻并写入数据库"""
    cur.execute("DELETE FROM alpha_signals WHERE signal_date = %s", (today,))
    valid = []

    for item in news_items:
        title = item.get('title', '')
        content = item.get('content', '')
        full_text = title + ' ' + content

        # 噪音过滤
        if any(re.search(p, full_text) for p in NOISE_PATTERNS):
            continue
        if any(kw in full_text for kw in NOISE_KEYWORDS):
            continue

        score = calculate_alpha_score(full_text)
        if score < ALPHA_THRESHOLD:
            continue

        sig_type, sentiment = classify_signal(full_text)
        stocks = extract_stock_info(full_text, conn)

        if stocks:
            for stock in stocks:
                cur.execute(
                    "SELECT COUNT(*) FROM alpha_signals WHERE ts_code=%s AND signal_date=%s",
                    (stock['ts_code'], today)
                )
                if cur.fetchone()[0] == 0:
                    boost = round(score / 100 * sentiment, 2)
                    cur.execute("""
                        INSERT INTO alpha_signals
                        (ts_code, name, signal_date, signal_type, score_boost, source, detail)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (stock['ts_code'], stock['name'], today, sig_type,
                          boost, item.get('source', ''), title))
                    valid.append({'stock': stock['name'], 'type': sig_type,
                                  'boost': boost, 'news': title})
        else:
            # 宏观信号记录
            if sig_type == '政策':
                valid.append({'stock': '全市场', 'type': sig_type,
                              'boost': 0, 'news': title})

    conn.commit()
    return valid


def main(today=None):
    if today is None:
        today = datetime.now().strftime('%Y-%m-%d')

    print("=" * 60)
    print(f"Alpha过滤器 V4 — 多频道增强版 | {today}")
    print("=" * 60)

    # 1. 获取新闻
    print("\n正在获取新闻...")
    news = fetch_news()
    if not news:
        print("未获取到新闻")
        return

    # 2. 过滤 + 写入
    conn = pymysql.connect(**DB_CONFIG)
    init_signals_table(conn)
    cur = conn.cursor()

    # 统计原始新闻中的潜在信号
    above_threshold = sum(1 for n in news if calculate_alpha_score(n['title'] + ' ' + n['content']) >= ALPHA_THRESHOLD)
    print(f"\n原始新闻: {len(news)} 条 (过阈值: {above_threshold} 条)")

    valid_signals = process_news(news, conn, today, cur)
    cur.close()
    conn.close()

    # 按Boost排序输出
    valid_signals.sort(key=lambda s: abs(s['boost']), reverse=True)

    print(f"\n处理完成！提取到 {len(valid_signals)} 条有效Alpha信号")
    print("-" * 60)
    if valid_signals:
        for s in valid_signals[:MAX_ITEMS]:
            print(f"【{s['stock']}】{s['type']} | Boost: {s['boost']:+.2f} | {s['news']}")
        if len(valid_signals) > MAX_ITEMS:
            print(f"  ... 还有 {len(valid_signals) - MAX_ITEMS} 条未展示")
    else:
        print("今日无有效信号")

    # 输出统计摘要给cron日志
    print(f"\n[SUMMARY] alpha_filter_v4: {len(valid_signals)} signals from {len(news)} news")


if __name__ == '__main__':
    main()
