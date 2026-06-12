#!/usr/bin/env python3
"""
实时市场状态监控守护进程

每30秒拉取：
- 上证指数实时涨跌（腾讯财经）
- 全市场涨跌比（东方财富）
- 市场状态判定（正常/偏弱/逆市/恐慌）

写入 data/market_state.json 供调度器和前端读取。
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('market_monitor')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, 'data', 'market_state.json')
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

def _fetch_json(url, timeout=5):
    """带UA的HTTP GET JSON"""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer': 'https://quote.eastmoney.com/'
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def _get_sh_from_qmt():
    """从 QMT /market/index 获取上证指数（主数据源）"""
    try:
        import requests
        r = requests.get("http://192.168.10.25:1430/market/index", timeout=3)
        data = r.json()
        for idx in data.get("indices", []):
            if idx["code"] == "000001.SH":
                return {
                    "price": float(idx["last"]),
                    "pct_chg": float(idx.get("pctChg", 0)),
                    "source": "QMT",
                }
    except Exception:
        pass
    return None

def _get_sh_from_tencent():
    """从腾讯财经获取上证指数（降级数据源）"""
    try:
        url = "http://qt.gtimg.cn/q=sh000001"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = resp.read().decode("gbk")
        if "~" in data:
            parts = data.strip().rstrip(";").split("~")
            return {
                "price": float(parts[3]),
                "pct_chg": float(parts[32]),
                "high": float(parts[33]),
                "low": float(parts[34]),
                "volume": float(parts[6]),
                "amount": float(parts[37]),
                "source": "腾讯",
            }
    except Exception:
        pass
    return None

def get_sh_index():
    """获取上证指数实时涨跌（QMT优先 → 腾讯降级）"""
    # 优先 QMT
    result = _get_sh_from_qmt()
    if result:
        return result
    # 降级腾讯
    result = _get_sh_from_tencent()
    if result:
        logger.debug("QMT不可用，降级到腾讯财经获取上证指数")
        return result
    logger.warning("所有指数数据源均不可用")
    return None

def get_market_breadth():
    """获取全市场实时涨跌比（东方财富行情中心）"""
    try:
        # 东方财富全市场统计
        url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f2,f3,f4,f12,f14"
        data = _fetch_json(url)

        # 更可靠的方式：用东方财富的 market 概览接口
        # 上证涨家数 + 深证涨家数
        up_count = 0
        down_count = 0

        # 分别拉上证和深证的涨跌统计
        for market_code in ['1.000001', '0.399001']:  # 上证, 深证
            try:
                url2 = f"https://push2.eastmoney.com/api/qt/stock/trends2/get?fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&secid={market_code}"
                # 这个接口不太对，换一个
                pass
            except:
                pass

        # 用东方财富板块统计接口
        url3 = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&fields=f2,f3,f4,f12,f13,f14&secids=1.000001,0.399001,0.399006,1.000688"
        idx_data = _fetch_json(url3, timeout=3)
        if idx_data and 'data' in idx_data and idx_data['data'].get('diff'):
            for item in idx_data['data']['diff']:
                code = item.get('f12', '')
                pct = item.get('f3', 0)
                logger.debug(f"  {code}: {pct:+.2f}%")
    except Exception as e:
        logger.warning("涨跌比获取失败: %s", e)

    # fallback: 用 qt.gtimg.cn 批量拉指数
    return _get_breadth_from_gtimg()

def _get_breadth_from_gtimg():
    """从腾讯财经获取市场统计（备选方案）"""
    try:
        # 上证+深证的综合统计
        codes = ['sh000001', 'sz399001', 'sz399006', 'sh000688']
        q = ','.join(codes)
        url = f"http://qt.gtimg.cn/q={q}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = resp.read().decode("gbk")

        results = {}
        for line in data.strip().split('\n'):
            if '~' not in line:
                continue
            parts = line.strip().rstrip(';').split('~')
            code = parts[2]
            results[code] = {
                'name': parts[1],
                'price': float(parts[3]),
                'pct_chg': float(parts[32]),
            }
        return results
    except Exception as e:
        logger.warning("腾讯指数数据获取失败: %s", e)
        return {}

def calc_breadth_from_individual():
    """从全市场个股涨跌计算实时涨跌比（备用，数据量大）"""
    try:
        # 东方财富全A股行情概览 - 分页获取统计数据
        url = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f2,f3,f12&_=" + str(int(time.time()*1000))
        data = _fetch_json(url, timeout=3)
        if data and 'data' in data and 'total' in data['data']:
            total = data['data']['total']
            # 爬前100只估算涨跌比（数据量太大，只抽样）
            return {'total': total, 'sampled': True}
    except:
        pass
    return None

def determine_state(sh_index, breadth=None):
    """根据实时数据判定市场状态"""
    if sh_index is None:
        return {"state": "unknown", "state_name": "数据异常", "is_bear": False}

    pct = sh_index['pct_chg']
    is_bear = False
    state = "normal"
    state_name = "常态"

    # 恐慌：暴跌 > 2.5%
    if pct < -2.5:
        state = "panic"
        state_name = "恐慌"
        is_bear = True
    # 逆市：跌 > 1.0%
    elif pct < -1.0:
        state = "bear"
        state_name = "逆市"
        is_bear = True
    # 偏弱：跌 0.3~1.0%
    elif pct < -0.3:
        state = "weak"
        state_name = "偏弱"
    # 正常/偏强
    else:
        state = "normal"
        state_name = "常态" if pct >= 0 else "微调"

    return {
        "state": state,
        "state_name": state_name,
        "is_bear": is_bear,
        "sh_pct": pct,
        "sh_price": sh_index['price'],
        "updated_at": datetime.now().isoformat(),
    }

def is_trading_time():
    """判断是否在交易时段"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    morning_start = t >= datetime.strptime("09:25", "%H:%M").time()
    morning_end = t <= datetime.strptime("11:35", "%H:%M").time()
    afternoon_start = t >= datetime.strptime("12:55", "%H:%M").time()
    afternoon_end = t <= datetime.strptime("15:05", "%H:%M").time()
    return (morning_start and morning_end) or (afternoon_start and afternoon_end)

def run_once():
    """执行一次市场状态采集"""
    sh = get_sh_index()
    state = determine_state(sh)

    # 写入状态文件
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    logger.info("市场状态: %s | 上证 %.2f (%+.2f%%)",
               state['state_name'], state.get('sh_price', 0), state.get('sh_pct', 0))
    return state

def main():
    logger.info("市场监控守护进程启动")
    logger.info("状态文件: %s", STATE_FILE)

    while True:
        try:
            if is_trading_time():
                state = run_once()
                # 恐慌状态额外告警
                if state['state'] == 'panic':
                    logger.warning("恐慌状态! 上证 %.2f%%", state['sh_pct'])
                sleep = 30  # 交易时段每30秒刷新
            else:
                # 非交易时段每5分钟检查一次（用于显示盘前数据）
                sleep = 300
                try:
                    sh = get_sh_index()
                    if sh:
                        state = determine_state(sh)
                        logger.debug("非交易时段: 上证%.2f %+.2f%% -> %s",
                                   state['sh_price'], state['sh_pct'], state['state_name'])
                        with open(STATE_FILE, 'w') as f:
                            json.dump(state, f, ensure_ascii=False, indent=2)
                except:
                    pass

            time.sleep(sleep)
        except KeyboardInterrupt:
            logger.info("监控守护进程退出")
            break
        except Exception as e:
            logger.error("监控异常: %s", e)
            time.sleep(60)

if __name__ == '__main__':
    main()
