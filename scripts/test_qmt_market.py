#!/usr/bin/env python3
"""
QMT 行情数据读取测试
在 Windows + iQuant 已打开的环境下运行:
    python test_qmt_market.py
"""
from qmt_stock_lists import get_stocks_in_sector
from xtquant import xtdata

# 1. 下载历史K线数据（沪深300成分股示例）
print("=== 下载沪深300成分股列表 ===")
stocks = get_stocks_in_sector("沪深300")
print(f"共 {len(stocks)} 只成分股, 前5只: {stocks[:5]}")

# 2. 下载上证指数日K线
print("\n=== 上证指数日K线（最近5天）===")
data = xtdata.get_market_data_ex(
    field_list=[],               # 空=取全部字段
    stock_list=["000001.SH"],
    period="1d",
    start_time="20250601",
    end_time="20250606",
    count=-1,
    dividend_type="none",
    fill_data=True,
)
print(data)
if "close" in data and "000001.SH" in data["close"]:
    closes = [v for v in data["close"]["000001.SH"].tolist() if v == v]
    print(f"最近收盘价序列: {closes}")

# 3. 读取全推实时行情
print("\n=== 全推实时行情快照（沪深300前3只）===")
try:
    for code in stocks[:3]:
        tick = xtdata.get_full_tick([code])
        if tick and code in tick:
            t = tick[code]
            print(f"{code}: 最新价={t.get('lastPrice','N/A')}, "
                  f"涨跌幅={t.get('pctChg','N/A')}%, "
                  f"成交量={t.get('volume','N/A')}, "
                  f"时间={t.get('time','N/A')}")
except Exception as e:
    print(f"全推行情读取失败: {e}")

# 4. 板块行情
print("\n=== 板块行情 ===")
try:
    sectors = xtdata.get_sector_list()
    print(f"可用板块数: {len(sectors)}, 前10个: {sectors[:10]}")
except Exception as e:
    print(f"板块列表读取失败: {e}")

# 5. 财务数据（试读一只）
print("\n=== 财务数据示例（000001.SZ 平安银行）===")
try:
    fin = xtdata.get_instrument_detail("000001.SZ")
    if fin:
        print(f"名称: {fin.get('InstrumentName','')}, "
              f"上市日期: {fin.get('OpenDate','')}, "
              f"总股本: {fin.get('TotalEquity','')}")
except Exception as e:
    print(f"财务数据读取失败: {e}")

print("\n✅ QMT 行情数据测试完成")
