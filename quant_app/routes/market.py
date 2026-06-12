"""
行情分析相关 API 路由
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException
from fastapi import Request as FastAPIRequest

from quant_app.routes.auth import get_current_user
from quant_app.services.market_service import get_tushare_pro
from quant_app.utils.persistence import (
    get_client_ip,
    save_access_log,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["market"])

# Ensure scripts/ directory is in sys.path for mainforce_scoring imports
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ========== 盘前预判 ==========


@router.get("/api/market/premarket")
def market_premarket(force_refresh: bool = False):
    """
    盘前预判状态 - 用于选股页面显示大盘仓位建议
    force_refresh=True 时强制重新获取数据
    """
    try:
        cache_file = DATA_DIR / "premarket_analysis.json"
        today_str = datetime.now().strftime("%Y-%m-%d")

        def _fetch_premarket_realtime():
            """获取实时大盘数据（统一 realtime_service + 本地文件缓存）"""
            from quant_app.services.realtime_service import get_market_indices

            data = get_market_indices()
            data["ver"] = 4
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return data

        # 检查缓存
        if cache_file.exists():
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)

            cached_date = data.get("date", "")

            if force_refresh or cached_date != today_str:
                fresh_data = _fetch_premarket_realtime()
                if fresh_data:
                    return {"fresh": True, "data": fresh_data}
                if cached_date:
                    return {"fresh": False, "warning": f"数据为{cached_date}日旧数据，实时获取失败", "data": data}

            data_time_str = data.get("time", "")
            try:
                data_time = datetime.strptime(data_time_str, "%H:%M:%S")
                now = datetime.now()
                diff = (now - data_time).total_seconds() / 3600
                if diff > 0.5:
                    fresh_data = _fetch_premarket_realtime()
                    if fresh_data:
                        return {"fresh": True, "data": fresh_data}
            except Exception as e:
                logger.error(f"时间解析失败: {e}")

            return {"fresh": True, "data": data}
        else:
            fresh_data = _fetch_premarket_realtime()
            if fresh_data:
                return {"fresh": True, "data": fresh_data}
            return {"fresh": False, "warning": "暂无盘前数据，请稍后刷新", "data": None}
    except Exception as e:
        logger.error(f"获取盘前状态失败: {e}")
        return {"error": str(e)}


# ========== 大盘研判 ==========


@router.get("/api/market/analysis")
def market_analysis(token: str = Cookie(None)):
    """
    大盘研判模块 - 沪指、深指、创业板三大盘面分析
    """
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    try:
        pro = get_tushare_pro()
        today = datetime.now()
        today_str = today.strftime("%Y%m%d")
        today_display = today.strftime("%Y年%m月%d日")

        indices = {
            "上证指数": {"code": "000001.SH", "market": "主板"},
            "深证成指": {"code": "399001.SZ", "market": "深市主板"},
            "创业板指": {"code": "399006.SZ", "market": "创业板"},
        }

        indices_analysis = {}
        for name, info in indices.items():
            try:
                df = pro.index_daily(
                    ts_code=info["code"],
                    start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
                    end_date=today_str,
                )
                if df is not None and len(df) > 0:
                    df = df.sort_values("trade_date", ascending=True)
                    indices_analysis[name] = {
                        "市场": info["market"],
                        "K线分析": analyze_market_kline(df),
                        "涨跌原因": analyze_market_reason(df),
                    }
            except Exception as e:
                logger.warning(f"获取{name}数据失败: {e}")

        sector_analysis = analyze_sectors()
        news_summary = get_market_news_summary()
        overall_analysis = analyze_three_markets(indices_analysis)
        prediction = predict_market_trend_overall(indices_analysis, sector_analysis)

        return {
            "分析日期": today_display,
            "数据日期": today_str,
            "三大盘面": indices_analysis,
            "综合分析": overall_analysis,
            "板块分析": sector_analysis,
            "主要资讯": news_summary,
            "趋势预测": prediction,
        }
    except Exception as e:
        logger.error(f"大盘研判失败: {e}")
        return {"error": f"分析失败: {str(e)}"}


# ========== 辅助函数（大盘分析） ==========


def analyze_three_markets(indices_analysis):
    """分析三大盘面（沪指、深指、创业板）的综合情况"""
    analysis = []

    if not indices_analysis:
        return "无法获取指数数据"

    changes = {}
    for name, data in indices_analysis.items():
        if "K线分析" in data and "今日涨跌" in data["K线分析"]:
            change_str = data["K线分析"]["今日涨跌"]
            try:
                changes[name] = float(change_str.replace("%", ""))
            except Exception:
                changes[name] = 0

    if not changes:
        return "数据解析失败"

    avg_change = sum(changes.values()) / len(changes)

    if avg_change > 1:
        market_status = "强势上涨"
    elif avg_change > 0.5:
        market_status = "温和上涨"
    elif avg_change > -0.5:
        market_status = "震荡整理"
    elif avg_change > -1:
        market_status = "温和调整"
    else:
        market_status = "明显调整"

    analysis.append(f"今日三大盘面整体{market_status}，平均涨跌幅{avg_change:.2f}%")

    if len(changes) >= 2:
        max_change = max(changes.values())
        min_change = min(changes.values())
        diff = max_change - min_change

        if diff > 1:
            strong_index = max(changes, key=changes.get)
            weak_index = min(changes, key=changes.get)
            analysis.append(
                f"市场分化明显，{strong_index}({changes[strong_index]:.2f}%)强于{weak_index}({changes[weak_index]:.2f}%)，差距{diff:.2f}%"
            )

            if "创业板" in strong_index:
                analysis.append("创业板表现强势，显示成长股受资金青睐，市场风险偏好较高")
            elif "上证指数" in strong_index:
                analysis.append("上证指数领涨，显示权重股表现较好，市场偏向价值风格")
        else:
            analysis.append("三大盘面涨跌同步，市场一致性较强")

    vol_status = []
    for name, data in indices_analysis.items():
        if "K线分析" in data and "量能状态" in data["K线分析"]:
            vol_status.append(f"{name}{data['K线分析']['量能状态']}")

    if vol_status:
        analysis.append("；".join(vol_status))

    return "；".join(analysis)


def predict_market_trend_overall(indices_analysis, sector_analysis):
    """基于三大盘面综合预测趋势"""
    if not indices_analysis:
        return {"短期预测": "数据不足，无法预测", "信心度": "50%", "建议": "观望为主"}

    changes = []
    for name, data in indices_analysis.items():
        if "K线分析" in data and "今日涨跌" in data["K线分析"]:
            try:
                changes.append(float(data["K线分析"]["今日涨跌"].replace("%", "")))
            except Exception:
                pass

    if not changes:
        return {"短期预测": "数据解析失败", "信心度": "50%", "建议": "观望为主"}

    avg_change = sum(changes) / len(changes)
    positive_count = sum(1 for c in changes if c > 0)
    negative_count = len(changes) - positive_count

    if avg_change > 1 and positive_count == len(changes):
        prediction = "三大盘面同步上涨，短期有望延续强势，但需关注量能持续性"
        confidence = "70%"
        suggestion = "可积极参与，关注领涨板块"
    elif avg_change > 0.5 and positive_count >= 2:
        prediction = "多数指数上涨，市场情绪偏暖，短期震荡上行概率较大"
        confidence = "65%"
        suggestion = "精选个股，控制仓位"
    elif avg_change < -1 and negative_count == len(changes):
        prediction = "三大盘面同步下跌，短期调整压力较大，注意风险控制"
        confidence = "70%"
        suggestion = "减仓观望，等待企稳"
    elif avg_change < -0.5 and negative_count >= 2:
        prediction = "多数指数调整，市场情绪谨慎，短期或继续震荡"
        confidence = "65%"
        suggestion = "控制仓位，防御为主"
    else:
        prediction = "盘面分化，市场方向不明，短期震荡整理概率大"
        confidence = "55%"
        suggestion = "均衡配置，精选业绩确定性高的个股"

    return {"短期预测": prediction, "信心度": confidence, "建议": suggestion}


def get_market_news_summary():
    """获取市场主要资讯总结 - 多数据源聚合"""
    try:
        today = datetime.now().strftime("%Y%m%d")

        news_data = {"宏观政策": [], "行业动态": [], "国际市场": [], "公司要闻": [], "总结": ""}

        # 1. 尝试Tushare重大新闻
        try:
            pro = get_tushare_pro()
            df = pro.major_news(src="sina", start_date=today, end_date=today)
            if df is not None and len(df) > 0:
                for _, row in df.head(10).iterrows():
                    title = row.get("title", "")
                    if any(k in title for k in ["央行", "发改委", "国务院", "财政部", "证监会"]):
                        news_data["宏观政策"].append(title)
                    elif any(k in title for k in ["美股", "港股", "日经", "欧股", "美联储"]):
                        news_data["国际市场"].append(title)
                    elif any(k in title for k in ["业绩", "财报", "订单", "中标"]):
                        news_data["公司要闻"].append(title)
                    else:
                        news_data["行业动态"].append(title)
        except Exception as e:
            logger.warning(f"Tushare重大新闻获取失败: {e}")

        # 2. 尝试东方财富财经要闻
        if sum(len(v) for v in news_data.values() if isinstance(v, list)) < 5:
            try:
                eastmoney_news = crawl_eastmoney_finance_news()
                if eastmoney_news:
                    for key, value in eastmoney_news.items():
                        if key != "总结" and isinstance(value, list):
                            news_data[key].extend(value)
                            news_data[key] = list(dict.fromkeys(news_data[key]))[:5]
            except Exception as e:
                logger.warning(f"东方财富财经要闻获取失败: {e}")

        # 3. 尝试新浪财经
        if sum(len(v) for v in news_data.values() if isinstance(v, list)) < 5:
            try:
                sina_news = crawl_sina_news()
                if sina_news:
                    for key, value in sina_news.items():
                        if key != "总结" and isinstance(value, list):
                            news_data[key].extend(value)
                            news_data[key] = list(dict.fromkeys(news_data[key]))[:5]
            except Exception as e:
                logger.warning(f"新浪财经获取失败: {e}")

        has_data = any(news_data[k] for k in ["宏观政策", "行业动态", "国际市场", "公司要闻"])

        if has_data:
            summary_parts = []
            if news_data["宏观政策"]:
                summary_parts.append("政策面释放积极信号")
            if news_data["行业动态"]:
                summary_parts.append("行业景气度分化明显")
            if news_data["国际市场"]:
                summary_parts.append("外围市场波动影响情绪")
            if news_data["公司要闻"]:
                summary_parts.append("个股业绩表现分化")

            if summary_parts:
                news_data["总结"] = "；".join(summary_parts) + "，建议关注政策受益板块和业绩确定性高的个股。"
            else:
                news_data["总结"] = "今日市场资讯相对平淡，建议关注技术面和资金面变化。"
        else:
            news_data["宏观政策"] = ["央行维持流动性合理充裕，支持实体经济发展"]
            news_data["行业动态"] = ["市场关注政策面变化，板块轮动加快"]
            news_data["国际市场"] = ["外围市场波动，对A股情绪有一定影响"]
            news_data["总结"] = "整体环境平稳，建议关注业绩确定性高的个股，控制仓位。"

        return news_data
    except Exception as e:
        logger.error(f"资讯获取失败: {e}")
        return {
            "宏观政策": ["央行维持流动性合理充裕，支持实体经济发展"],
            "行业动态": ["市场关注政策面变化，板块轮动加快"],
            "国际市场": ["外围市场波动，对A股情绪有一定影响"],
            "公司要闻": ["个股业绩陆续披露，市场关注超预期标的"],
            "总结": "整体环境平稳，建议关注业绩确定性高的个股，控制仓位。",
        }


def crawl_eastmoney_finance_news():
    """爬取东方财富财经要闻"""
    try:
        import json
        import urllib.request

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        url = "https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param=%7B%22uid%22%3A%22%22%2C%22keyword%22%3A%22A%E8%82%A1%22%2C%22type%22%3A%5B%22cmsArticleWebOld%22%5D%2C%22client%22%3A%22web%22%2C%22clientType%22%3A%22web%22%2C%22clientVersion%22%3A%22curr%22%2C%22page%22%3A%7B%22index%22%3A1%2C%22size%22%3A20%7D%7D"

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode("utf-8")
            json_str = content[content.find("(") + 1 : content.rfind(")")]
            data = json.loads(json_str)

        news_data = {"宏观政策": [], "行业动态": [], "国际市场": [], "公司要闻": [], "总结": ""}

        if "result" in data and "cmsArticleWebOld" in data["result"]:
            items = data["result"]["cmsArticleWebOld"]
            for item in items[:15]:
                title = item.get("title", "")

                if any(k in title for k in ["央行", "发改委", "国务院", "财政部", "证监会", "监管", "政策"]):
                    news_data["宏观政策"].append(title)
                elif any(k in title for k in ["美股", "港股", "日经", "欧股", "美联储", "加息", "降息", "外围"]):
                    news_data["国际市场"].append(title)
                elif any(k in title for k in ["业绩", "财报", "订单", "中标", "合同", "净利润"]):
                    news_data["公司要闻"].append(title)
                elif any(k in title for k in ["行业", "产业", "板块", "概念", "涨价", "销量"]):
                    news_data["行业动态"].append(title)

        for key in news_data:
            if key != "总结" and isinstance(news_data[key], list):
                news_data[key] = news_data[key][:5]

        return news_data if any(news_data.values()) else None
    except Exception as e:
        logger.warning(f"东方财富财经要闻爬虫失败: {e}")
        return None


def crawl_sina_news():
    """爬取新浪财经"""
    try:
        import json
        import urllib.request

        url = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2515&k=&num=20&page=1&r=0.123"

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        news_data = {"宏观政策": [], "行业动态": [], "国际市场": [], "公司要闻": [], "总结": ""}

        if "result" in data and "data" in data["result"]:
            items = data["result"]["data"]
            for item in items[:15]:
                title = item.get("title", "")

                if any(k in title for k in ["央行", "发改委", "国务院", "财政部", "证监会", "监管", "政策"]):
                    news_data["宏观政策"].append(title)
                elif any(k in title for k in ["美股", "港股", "日经", "欧股", "美联储", "加息", "降息", "外围"]):
                    news_data["国际市场"].append(title)
                elif any(k in title for k in ["业绩", "财报", "订单", "中标", "合同", "净利润"]):
                    news_data["公司要闻"].append(title)
                elif any(k in title for k in ["行业", "产业", "板块", "概念", "涨价", "销量"]):
                    news_data["行业动态"].append(title)

        for key in news_data:
            if key != "总结" and isinstance(news_data[key], list):
                news_data[key] = news_data[key][:5]

        return news_data if any(news_data.values()) else None
    except Exception as e:
        logger.warning(f"新浪财经爬虫失败: {e}")
        return None


def analyze_market_kline(df):
    """大盘K线技术分析"""
    if len(df) < 5:
        return {"error": "数据不足"}

    import pandas as pd

    latest = df.iloc[-1]
    df.iloc[-2]

    close = float(latest["close"])

    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()

    ma5 = float(df["ma5"].iloc[-1]) if not pd.isna(df["ma5"].iloc[-1]) else close
    ma10 = float(df["ma10"].iloc[-1]) if not pd.isna(df["ma10"].iloc[-1]) else close
    ma20 = float(df["ma20"].iloc[-1]) if not pd.isna(df["ma20"].iloc[-1]) else close

    trend = ""
    if close > ma5 > ma10 > ma20:
        trend = "强势多头排列"
    elif close > ma5 > ma10:
        trend = "短期多头"
    elif close < ma5 < ma10 < ma20:
        trend = "空头排列"
    elif close < ma5 < ma10:
        trend = "短期空头"
    else:
        trend = "震荡整理"

    vol_avg = df["vol"].rolling(5).mean().iloc[-1]
    vol_today = latest["vol"]
    vol_status = "放量" if vol_today > vol_avg * 1.2 else ("缩量" if vol_today < vol_avg * 0.8 else "平量")

    return {
        "当前点位": round(close, 2),
        "今日涨跌": f"{latest['pct_chg']:.2f}%",
        "MA5": round(ma5, 2),
        "MA10": round(ma10, 2),
        "MA20": round(ma20, 2),
        "趋势判断": trend,
        "量能状态": vol_status,
        "成交额": f"{latest['amount'] / 1e8:.0f}亿",
    }


def analyze_market_reason(df):
    """涨跌原因分析"""
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    change = latest["pct_chg"]
    prev_change = prev["pct_chg"] if len(df) > 1 else 0

    reasons = []

    if change > 2:
        reasons.append(f"大盘强势上涨{change:.2f}%，市场情绪极度乐观，资金积极入场")
    elif change > 1:
        reasons.append(f"大盘明显上涨{change:.2f}%，市场情绪偏暖，板块轮动活跃")
    elif change > 0:
        reasons.append(f"大盘小幅上涨{change:.2f}%，市场情绪温和，个股分化明显")
    elif change > -1:
        reasons.append(f"大盘小幅调整{change:.2f}%，属于正常技术回调，消化前期涨幅")
    elif change > -2:
        reasons.append(f"大盘明显下跌{change:.2f}%，市场情绪转谨慎，获利盘兑现压力")
    else:
        reasons.append(f"大盘大幅下跌{change:.2f}%，市场情绪悲观，避险情绪升温")

    if change > 0 and prev_change > 0:
        reasons.append("连续两日上涨，短期趋势向好")
    elif change < 0 and prev_change < 0:
        reasons.append("连续两日调整，短期承压")
    elif change > 0 and prev_change < 0:
        reasons.append("今日反弹，收复昨日部分失地")
    elif change < 0 and prev_change > 0:
        reasons.append("昨日上涨后今日回调，正常技术整理")

    vol_avg = df["vol"].rolling(5).mean().iloc[-1]
    vol_ratio = latest["vol"] / vol_avg if vol_avg > 0 else 1

    if vol_ratio > 1.5:
        reasons.append(f"成交量大幅放量({vol_ratio:.1f}倍)，资金进出活跃，市场关注度提升")
    elif vol_ratio > 1.2:
        reasons.append(f"成交量温和放大({vol_ratio:.1f}倍)，资金逐步流入")
    elif vol_ratio < 0.7:
        reasons.append(f"成交量明显萎缩({vol_ratio:.1f}倍)，市场观望情绪浓厚")
    elif vol_ratio < 0.9:
        reasons.append(f"成交量小幅缩减({vol_ratio:.1f}倍)，交投趋于谨慎")
    else:
        reasons.append("成交量与近期持平，市场运行平稳")

    close = latest["close"]
    high_5 = df["high"].tail(5).max()
    low_5 = df["low"].tail(5).min()

    if close >= high_5 * 0.99:
        reasons.append("股价接近5日高点，面临前期压力")
    elif close <= low_5 * 1.01:
        reasons.append("股价接近5日低点，获得短期支撑")

    return {
        "涨跌幅度": f"{change:.2f}%",
        "昨日涨跌": f"{prev_change:.2f}%",
        "主要原因": reasons,
        "技术形态": "结合均线和成交量综合判断",
    }


def analyze_sectors():
    """板块涨跌分析 - 使用Tushare真实数据"""
    try:
        pro = get_tushare_pro()
        today = datetime.now()
        today_str = today.strftime("%Y%m%d")

        df = pro.moneyflow_ind_ths(trade_date=today_str)

        if df is None or len(df) == 0:
            yesterday = today - timedelta(days=1)
            while yesterday.weekday() >= 5:
                yesterday = yesterday - timedelta(days=1)
            yesterday_str = yesterday.strftime("%Y%m%d")
            logger.info(f"当天数据未更新，尝试获取昨天数据: {yesterday_str}")
            df = pro.moneyflow_ind_ths(trade_date=yesterday_str)
            data_date = yesterday_str
        else:
            data_date = today_str

        if df is not None and len(df) > 0:
            result = analyze_sectors_from_tushare(df)
            result["数据日期"] = f"{data_date[:4]}-{data_date[4:6]}-{data_date[6:]}"
            result["数据说明"] = "基于Tushare行业资金流向数据"
            return result

        logger.info("Tushare无数据，尝试东方财富爬虫")
        eastmoney_data = crawl_eastmoney_sectors()
        if eastmoney_data:
            eastmoney_data["数据日期"] = today_str
            eastmoney_data["数据说明"] = "基于东方财富实时数据"
            return eastmoney_data

        logger.warning("无实时数据，使用模拟数据")
        mock_data = get_mock_sector_analysis()
        mock_data["数据日期"] = today_str
        mock_data["数据说明"] = "模拟数据（仅供测试）"
        return mock_data
    except Exception as e:
        logger.warning(f"板块分析失败: {e}")
        mock_data = get_mock_sector_analysis()
        mock_data["数据日期"] = datetime.now().strftime("%Y%m%d")
        mock_data["数据说明"] = "模拟数据（仅供测试）"
        return mock_data


def analyze_sectors_from_tushare(df):
    """从Tushare数据解析板块分析"""
    df = df.sort_values("pct_change", ascending=False)

    top_df = df.head(5)
    bottom_df = df.tail(5)

    top_analysis = []
    for _, row in top_df.iterrows():
        name = row.get("industry", "未知")
        change = row.get("pct_change", 0)
        net_amount = row.get("net_amount", 0)

        reason = analyze_sector_reason_by_moneyflow(name, change, net_amount, True)

        top_analysis.append(
            {"name": name, "change": round(change, 2), "主力资金": f"{net_amount:.2f}亿", "reason": reason}
        )

    bottom_analysis = []
    for _, row in bottom_df.iterrows():
        name = row.get("industry", "未知")
        change = row.get("pct_change", 0)
        net_amount = row.get("net_amount", 0)

        reason = analyze_sector_reason_by_moneyflow(name, change, net_amount, False)

        bottom_analysis.append(
            {"name": name, "change": round(change, 2), "主力资金": f"{net_amount:.2f}亿", "reason": reason}
        )

    overall = analyze_sector_overall_real(top_analysis, bottom_analysis)

    return {
        "数据来源": "Tushare行业资金流向",
        "领涨板块": top_analysis,
        "领跌板块": bottom_analysis,
        "综合分析": overall,
    }


def analyze_sector_reason_by_moneyflow(name, change, net_amount, is_rise):
    """根据资金流向分析板块涨跌原因"""
    if net_amount > 50:
        money_flow = "主力资金大幅净流入"
    elif net_amount > 20:
        money_flow = "主力资金净流入"
    elif net_amount > 0:
        money_flow = "主力资金小幅流入"
    elif net_amount > -20:
        money_flow = "主力资金小幅流出"
    elif net_amount > -50:
        money_flow = "主力资金净流出"
    else:
        money_flow = "主力资金大幅净流出"

    if is_rise:
        if change > 3:
            return f"强势上涨，{money_flow}，资金积极抢筹，板块热度高"
        elif change > 1:
            return f"稳步上涨，{money_flow}，资金持续流入，趋势向好"
        else:
            return f"小幅上涨，{money_flow}，资金关注度提升"
    else:
        if change < -3:
            return f"大幅调整，{money_flow}，资金撤离明显，短期承压"
        elif change < -1:
            return f"震荡下行，{money_flow}，资金流出，情绪谨慎"
        else:
            return f"小幅回调，{money_flow}，正常技术调整"


def crawl_eastmoney_sectors():
    """爬取东方财富板块数据"""
    try:
        import json
        import urllib.request

        url = "http://push2ex.eastmoney.com/getTopicZS?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.zs&Pageindex=0&pagesize=100&sort=f3&sorttype=1"

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

        if "data" in data and "diff" in data["data"]:
            sectors = data["data"]["diff"]

            sector_list = []
            for code, info in sectors.items():
                name = info.get("f14", "")
                change = info.get("f3", 0)
                if name and change != 0:
                    sector_list.append({"name": name, "change": change / 100})

            sector_list.sort(key=lambda x: x["change"], reverse=True)

            top = sector_list[:5]
            bottom = sector_list[-5:]

            top_analysis = []
            for s in top:
                reason = analyze_sector_reason(s["name"], s["change"], True)
                top_analysis.append({"name": s["name"], "change": round(s["change"], 2), "reason": reason})

            bottom_analysis = []
            for s in bottom:
                reason = analyze_sector_reason(s["name"], s["change"], False)
                bottom_analysis.append({"name": s["name"], "change": round(s["change"], 2), "reason": reason})

            overall = analyze_sector_overall_real(top_analysis, bottom_analysis)

            return {
                "数据来源": "东方财富实时数据",
                "领涨板块": top_analysis,
                "领跌板块": bottom_analysis,
                "综合分析": overall,
            }
    except Exception as e:
        logger.warning(f"东方财富爬虫失败: {e}")
        return None


def analyze_sector_overall_real(top_sectors, bottom_sectors):
    """基于真实数据的板块综合分析"""
    analysis = []

    if top_sectors:
        top_avg = sum(s["change"] for s in top_sectors) / len(top_sectors)
        top_names = [s["name"] for s in top_sectors[:3]]

        tech_keywords = ["半导体", "计算机", "电子", "通信", "传媒", "新能源", "人工智能"]
        cycle_keywords = ["有色", "煤炭", "化工", "钢铁", "石油"]
        finance_keywords = ["银行", "证券", "保险"]

        if any(k in str(top_names) for k in tech_keywords):
            sector_type = "科技成长"
        elif any(k in str(top_names) for k in cycle_keywords):
            sector_type = "周期复苏"
        elif any(k in str(top_names) for k in finance_keywords):
            sector_type = "金融权重"
        else:
            sector_type = "消费医药"

        analysis.append(
            f"今日领涨板块为{', '.join(top_names)}，平均涨幅{top_avg:.2f}%，显示资金偏好{sector_type}方向，市场风险偏好{'较高' if top_avg > 2 else '温和'}"
        )

    if bottom_sectors:
        bottom_avg = sum(s["change"] for s in bottom_sectors) / len(bottom_sectors)
        bottom_names = [s["name"] for s in bottom_sectors[:3]]

        analysis.append(
            f"领跌板块为{', '.join(bottom_names)}，平均跌幅{abs(bottom_avg):.2f}%，{'短期承压明显' if bottom_avg < -2 else '正常调整'}"
        )

    if top_sectors and bottom_sectors:
        if top_avg > 3 and bottom_avg > -1:
            analysis.append("市场呈现普涨格局，赚钱效应良好，可积极操作")
        elif top_avg > 2 and bottom_avg < -2:
            analysis.append("市场分化严重，结构性行情明显，建议精选个股")
        elif top_avg < 1 and bottom_avg < -1:
            analysis.append("市场整体偏弱，多数板块下跌，建议控制仓位")
        else:
            analysis.append("板块轮动较快，建议均衡配置，关注业绩确定性")

    return "；".join(analysis)


def analyze_sector_reason(name, change, is_rise):
    """分析板块涨跌原因"""
    reasons = {
        "半导体": {
            "rise": ["国产替代加速，政策支持力度加大", "全球芯片短缺，行业景气度提升", "技术突破，龙头企业订单饱满"],
            "fall": ["前期涨幅过大，获利盘兑现", "海外需求放缓，出口承压", "技术迭代风险，部分企业业绩不及预期"],
        },
        "新能源": {
            "rise": ["政策利好持续，双碳目标推进", "原材料价格回落，成本压力缓解", "海外订单增长，出口需求旺盛"],
            "fall": ["产能过剩担忧，价格战加剧", "原材料价格波动，成本压力增大", "政策补贴退坡，短期需求放缓"],
        },
        "人工智能": {
            "rise": ["ChatGPT带动AI热潮，应用场景拓展", "大模型技术突破，商业化加速", "政策大力支持数字经济发展"],
            "fall": ["估值过高，获利回吐压力", "技术落地不及预期，商业化困难", "监管政策趋严，不确定性增加"],
        },
        "银行": {
            "rise": ["息差改善预期，业绩修复", "高股息率吸引资金配置", "经济复苏带动信贷需求"],
            "fall": ["息差收窄压力，盈利预期下调", "房地产风险暴露，资产质量担忧", "经济增速放缓，信贷需求疲软"],
        },
        "房地产": {
            "rise": ["政策放松预期，融资环境改善", "销售数据回暖，市场信心恢复", "城中村改造等政策利好"],
            "fall": ["销售数据不及预期，库存压力", "债务风险暴露，信用事件频发", "人口结构变化，长期需求看淡"],
        },
        "医药": {
            "rise": [
                "创新药审批加速，研发管线价值重估",
                "流感等季节性疾病高发，需求增加",
                "老龄化加速，医疗需求刚性增长",
            ],
            "fall": ["集采压力持续，利润空间压缩", "医保控费，支付能力受限", "研发失败风险，管线价值重估"],
        },
        "有色金属": {
            "rise": ["美联储降息预期，美元走弱", "新能源需求增长，锂铜等价格上涨", "供给受限，库存低位"],
            "fall": ["全球经济放缓，需求预期下调", "美元走强，大宗商品承压", "产能释放，供给增加"],
        },
        "煤炭": {
            "rise": ["夏季用电高峰，需求增加", "进口受限，供给偏紧", "高股息率，防御属性"],
            "fall": ["新能源替代加速，长期需求看淡", "价格调控，政策压制", "季节性淡季，需求回落"],
        },
        "化工": {
            "rise": ["原材料价格上涨，产品提价", "下游需求回暖，库存去化", "产能整合，行业集中度提升"],
            "fall": ["原材料价格波动，成本压力", "下游需求疲软，库存累积", "产能过剩，价格战"],
        },
        "白酒": {
            "rise": ["消费复苏预期，宴席需求增加", "中秋国庆旺季备货", "龙头提价，业绩确定性高"],
            "fall": ["消费复苏不及预期，动销疲软", "库存高企，渠道压力", "年轻人消费偏好变化"],
        },
        "家电": {
            "rise": ["地产竣工回暖，装修需求增加", "高温带动空调销售", "出口订单增长，汇率利好"],
            "fall": ["地产低迷，新增需求不足", "消费疲软，更新换代放缓", "原材料价格上涨，成本压力"],
        },
        "汽车": {
            "rise": ["政策支持，购置税减免", "新车型上市，产品力提升", "出口增长，海外需求旺盛"],
            "fall": ["价格战加剧，利润空间压缩", "需求提前释放，后续乏力", "原材料成本上升"],
        },
    }

    if name in reasons:
        reason_list = reasons[name]["rise" if is_rise else "fall"]
        import random

        return random.choice(reason_list)

    if is_rise:
        generic_reasons = [
            "政策利好刺激，资金关注度提升",
            "业绩超预期，基本面改善",
            "技术面突破，资金跟风买入",
            "行业景气度回升，订单增长",
            "估值修复，资金配置需求",
        ]
    else:
        generic_reasons = [
            "前期涨幅过大，获利盘兑现",
            "业绩不及预期，基本面恶化",
            "技术面破位，资金出逃",
            "行业景气度下行，订单减少",
            "估值过高，资金撤离",
        ]

    import random

    return random.choice(generic_reasons)


def get_mock_sector_analysis():
    """模拟板块分析数据（当API无法获取时使用）"""
    return {
        "领涨板块": [
            {"name": "半导体", "change": 3.2, "reason": "国产替代加速，大基金三期成立刺激"},
            {"name": "新能源", "change": 2.8, "reason": "政策利好持续，储能需求爆发"},
            {"name": "人工智能", "change": 2.5, "reason": "大模型应用落地，商业化加速"},
            {"name": "有色金属", "change": 2.1, "reason": "美联储降息预期，大宗商品涨价"},
            {"name": "化工", "change": 1.8, "reason": "产能整合，产品提价"},
        ],
        "领跌板块": [
            {"name": "房地产", "change": -2.5, "reason": "销售数据不及预期，债务风险担忧"},
            {"name": "医药", "change": -1.8, "reason": "集采压力持续，利润空间压缩"},
            {"name": "银行", "change": -1.2, "reason": "息差收窄预期，资产质量担忧"},
            {"name": "煤炭", "change": -0.9, "reason": "季节性淡季，需求回落"},
            {"name": "白酒", "change": -0.6, "reason": "消费复苏不及预期，库存压力"},
        ],
        "综合分析": "今日市场呈现明显的结构性行情，科技成长板块（半导体、AI、新能源）在政策利好和景气度提升带动下领涨；而传统周期板块（地产、银行）受基本面压力影响表现较弱。市场风格偏向成长，建议关注业绩确定性高的科技龙头，同时警惕高位股回调风险。",
    }


# ========== 热点板块 ==========


@router.get("/api/sectors/hot")
def api_hot_sectors(top_n: int = 5, lookback_days: int = 5, token: str = Cookie(None)):
    """获取热点板块列表"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        from sector_rotation import get_hot_sectors

        return get_hot_sectors(top_n=top_n, lookback_days=lookback_days)
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/stock/{ts_code}/fund_flow")
def api_fund_flow(ts_code: str, lookback: int = 5, token: str = Cookie(None)):
    """获取个股资金流向连续性"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        from sector_rotation import get_fund_flow_continuity

        return get_fund_flow_continuity(ts_code=ts_code, lookback=lookback)
    except Exception as e:
        return {"error": str(e)}


# ========== 主力资金追踪 ==========


@router.get("/api/mainforce_scan")
def mainforce_scan(request: FastAPIRequest, token: str = Cookie(None)):
    """主力资金评分扫描 - 返回主力评分 Top 50"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        cache_file = os.path.join(DATA_DIR, "mainforce_top50.json")
        if os.path.exists(cache_file):
            stat = os.stat(cache_file)
            age_hours = (time.time() - stat.st_mtime) / 3600
            if age_hours < 24:
                with open(cache_file, encoding="utf-8") as f:
                    return json.load(f)

        save_access_log(user, get_client_ip(request), "主力资金扫描")

        from mainforce_scoring import calculate_mainforce_score, get_db_conn

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT ts_code, name FROM stock_info
            WHERE is_st = 0 AND ts_code NOT LIKE '688%%'
        """)
        all_stocks = cur.fetchall()
        cur.close()
        conn.close()

        results = []
        for ts_code, name in all_stocks:
            try:
                result = calculate_mainforce_score(ts_code)
                if result["score"] >= 50:
                    results.append(result)
            except Exception as e:
                logger.error(f"主力评分失败: {e}")

        results.sort(key=lambda x: x["score"], reverse=True)
        top50 = results[:50]

        output_data = {
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(results),
            "stocks": top50,
        }

        os.makedirs(DATA_DIR, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)

        return output_data
    except Exception as e:
        return {"error": str(e), "stocks": []}


@router.get("/api/mainforce_stock/{ts_code}")
def mainforce_stock(ts_code: str, token: str = Cookie(None)):
    """个股主力分析"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        from mainforce_scoring import calculate_mainforce_score

        result = calculate_mainforce_score(ts_code)
        return result
    except Exception as e:
        return {"error": str(e)}
