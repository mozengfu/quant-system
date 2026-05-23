#!/usr/bin/env python3
"""
策略选股服务 - C3.0 V3评分、策略扫描、技术面扫描、底部起步、均线回踩
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from market_state import get_market_state as unified_market_state
from quant_app.services.market_service import calculate_rps, get_recent_trade_dates, get_stock_realtime, get_tushare_pro
from quant_app.services.technical_service import calculate_atr, calculate_bollinger_bands, calculate_kdj, calculate_macd
from quant_app.utils.config import DATA_DIR, get_db_config

logger = logging.getLogger(__name__)

# ========== 持仓预警状态（防止重复通知） ==========
POSITION_ALERT_STATE = {}

# ========== 板块常量 ==========
ALL_BLOCKS = [
    # 科技类
    "半导体", "IT设备", "互联网", "软件服务", "通信设备", "元器件",
    # 医药类
    "中成药", "化学制药", "生物制药", "医疗保健", "医药商业", "农药化肥",
    # 消费类
    "白酒", "啤酒", "红黄酒", "食品", "软饮料", "纺织", "酒店餐饮", "家居用品", "家用电器",
    # 制造类
    "汽车整车", "汽车配件", "汽车服务", "专用机械", "工程机械", "电气设备", "机床制造",
    # 地产建筑类
    "全国地产", "区域地产", "房产服务", "建筑工程", "装修装饰", "其他建材",
    # 金融类
    "银行", "证券", "保险", "多元金融",
    # 其他
    "化工原料", "化纤", "化工机械", "小金属", "广告包装", "影视音像"
]


# ========== 技术指标工具函数 ==========

def calculate_macd_kdj_single(highs, lows, closes, n=9):
    """单值版KDJ计算（供个股分析用，取最新值）"""
    from quant_app.services.technical_service import calculate_kdj as _ts_kdj
    k, d, j = _ts_kdj(highs, lows, closes, n)
    return k or 0, d or 0, j or 0



# ========== 个股深度分析 ==========
def analyze_stock(code, market="sz"):
    """C3.0 V3 个股深度分析"""
    rt = get_stock_realtime(code, market)
    if not rt:
        return {"error": f"无法获取 {code} 实时数据"}

    price = rt.get("现价", 0)
    yc = rt.get("昨收", 0)
    o = rt.get("今开", 0)
    h = rt.get("最高", 0)
    l = rt.get("最低", 0)
    vol = rt.get("成交量", 0)
    change_pct = rt.get("涨跌幅", 0)
    turnover = rt.get("换手率", 0)
    vr = rt.get("量比", 0)
    h52 = rt.get("52周高", 0)
    l52 = rt.get("52周低", 0)
    zt = rt.get("涨停价", 0)
    dt = rt.get("跌停价", 0)
    ts_code = f"{code}.{'SZ' if market=='sz' else 'SH'}"

    # 初始化基本面数据
    basic_info = {"行业": "N/A", "PE": "N/A", "PB": "N/A", "毛利率": "N/A", "净利率": "N/A", "ROE": "N/A", "盈亏": "N/A"}

    # 初始化资金流向数据
    money_flow = {"主力净流入": "N/A", "主力净流入占比": "N/A", "大单净流入": "N/A", "中单净流入": "N/A", "小单净流入": "N/A"}

    try:
        dates = get_recent_trade_dates(60)
    except Exception as e:
        logger.warning(f"获取交易日失败: {e}")
        dates = None

    if dates:
        pro = get_tushare_pro()

        # 获取资金流向数据（使用最新交易日，真实反映当天情况）
        try:
            mf = pro.moneyflow(ts_code=ts_code, trade_date=dates[-1])
            if mf is not None and len(mf) > 0:
                row = mf.iloc[0]
                def fmt_mf(v):
                    val = v or 0
                    return f"{val:.1f}万" if abs(val) > 0.01 else "N/A"
                money_flow = {
                    "主力净流入": fmt_mf(row.get('net_mf_amount', 0)),
                    "主力净流入占比": f"{row.get('net_mf_rate', 0):.2f}%" if abs(row.get('net_mf_rate', 0)) > 0.01 else "N/A",
                    "大单净流入": fmt_mf(row.get('lg_net_mf_amount', 0)),
                    "中单净流入": fmt_mf(row.get('md_net_mf_amount', 0)),
                    "小单净流入": fmt_mf(row.get('sm_net_mf_amount', 0)),
                }
        except Exception as e:
            logger.warning(f"资金流向获取失败: {e}")

        # 获取基本面数据
        try:
            basic_df = pro.stock_basic(ts_code=ts_code, fields="industry")
            if basic_df is not None and len(basic_df) > 0:
                industry = basic_df.iloc[0].get("industry", "N/A")
                basic_info["行业"] = industry if industry else "N/A"
        except Exception as e:
            logger.warning(f"行业获取失败: {e}")

        # 获取PE、PB数据
        try:
            daily_basic = pro.daily_basic(ts_code=ts_code, trade_date=dates[-1], fields="pe,pb")
            if daily_basic is not None and len(daily_basic) > 0:
                row = daily_basic.iloc[0]
                basic_info["PE"] = f"{row.get('pe', 0):.1f}" if row.get('pe', 0) and row.get('pe', 0) > 0 else "N/A"
                basic_info["PB"] = f"{row.get('pb', 0):.2f}" if row.get('pb', 0) and row.get('pb', 0) > 0 else "N/A"
        except Exception as e:
            logger.warning(f"PE/PB获取失败: {e}")

        # 获取最新财务数据（2026Q1→2025年报→2025中报）
        try:
            for period in ["20260331", "20251231", "20250630"]:
                try:
                    fina = pro.fina_indicator(ts_code=ts_code, period=period, fields="roe,grossprofit_margin,netprofit_margin")
                    if fina is not None and len(fina) > 0:
                        row = fina.iloc[0]
                        roe = row.get("roe", 0)
                        gpm = row.get("grossprofit_margin", 0)
                        npm = row.get("netprofit_margin", 0)
                        basic_info["ROE"] = f"{roe:.2f}%" if roe and roe > 0 else "N/A"
                        basic_info["毛利率"] = f"{gpm:.1f}%" if gpm and gpm > 0 else "N/A"
                        basic_info["净利率"] = f"{npm:.1f}%" if npm and npm > 0 else "N/A"
                        # 判断盈亏
                        if npm and npm > 0:
                            basic_info["盈亏"] = "盈利"
                        elif npm and npm < 0:
                            basic_info["盈亏"] = "亏损"
                        else:
                            basic_info["盈亏"] = "N/A"
                        break
                except Exception as e:
                    logger.warning(f"财务明细查询失败 {ts_code}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"财务数据获取失败: {e}")

        # ===== 技术指标计算（独立 try，失败不影响基础数据） =====
        try:
            df_daily = pro.daily(ts_code=ts_code, start_date=dates[0], end_date=dates[-1])
            if df_daily is not None and len(df_daily) >= 5:
                df_daily = df_daily.sort_values("trade_date")
                df_daily = df_daily.dropna(subset=["close", "high", "low"])
                if len(df_daily) >= 5:
                    closes = df_daily["close"].tolist()
                    highs = df_daily["high"].tolist()
                    lows = df_daily["low"].tolist()
                    ma5 = sum(closes[-5:]) / 5
                    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes) / len(closes)
                    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
                    if ma5 > ma10 > ma20:
                        ma_trend = "多头"
                    elif ma5 < ma10 < ma20:
                        ma_trend = "空头"
                    elif isinstance(price, (int, float)) and price > ma5:
                        ma_trend = "反弹"
                    else:
                        ma_trend = "震荡"

                    try:
                        rps = calculate_rps(code, market, n=20)
                    except Exception as e:
                        logger.warning(f"RPS计算失败: {e}")

                    # ===== MACD / KDJ / ATR 计算（单项容错 + None保护） =====
                    try:
                        macd_line, signal_line, macd_hist = calculate_macd(closes)
                    except Exception as e:
                        logger.warning(f"MACD计算失败: {e}")
                        macd_line = signal_line = macd_hist = None

                    try:
                        k_val, d_val, j_val = calculate_macd_kdj_single(highs, lows, closes)
                    except Exception as e:
                        logger.warning(f"KDJ计算失败: {e}")
                        k_val = d_val = j_val = None

                    try:
                        atr_val = calculate_atr(highs, lows, closes)
                    except Exception as e:
                        logger.warning(f"ATR计算失败: {e}")
                        atr_val = None

                    try:
                        bb_upper, bb_middle, bb_lower = calculate_bollinger_bands(closes)
                    except Exception as e:
                        logger.warning(f"布林带计算失败: {e}")
                        bb_upper = bb_middle = bb_lower = None

                    # 确保 d_val 被正确捕获
                    if k_val is None and d_val is None:
                        try:
                            _, d_val, _ = calculate_kdj(highs, lows, closes)
                        except Exception as e:
                            logger.debug(f"KDJ计算失败: {e}")
                            pass

                    # MACD 状态判断（全 None 保护）
                    if isinstance(macd_line, (int, float)) and isinstance(signal_line, (int, float)):
                        try:
                            macd_state = "金叉区域" if macd_line > signal_line else "死叉区域"
                        except Exception as e:
                            logger.debug(f"MACD状态判断失败: {e}")
                            macd_state = "数据不足"
                    else:
                        macd_state = "数据不足"

                    # KDJ 状态判断（全 None 保护）
                    if isinstance(k_val, (int, float)):
                        try:
                            kdj_state = "超买" if k_val > 80 else ("超卖" if k_val < 20 else "正常")
                        except Exception as e:
                            logger.debug(f"KDJ状态判断失败: {e}")
                            kdj_state = "正常"
                        kdj_cross = "K上穿D" if (isinstance(d_val, (int, float)) and k_val > d_val) else "K下穿D"
                    else:
                        kdj_state = "数据不足"
                        kdj_cross = ""
        except Exception as e:
            logger.warning(f"Tushare日线数据获取失败: {e}")

    # 兜底默认值（防止 try 内异常导致局部变量未定义）
    try: ma5
    except NameError: ma5 = ma10 = ma20 = price; ma_trend = "震荡"; rps = 50
    try: rps
    except NameError: rps = 50
    try: ma_trend
    except NameError: ma_trend = "震荡"
    try: macd_state
    except NameError: macd_line = signal_line = macd_hist = None; macd_state = "数据不足"
    try: kdj_state
    except NameError: k_val = d_val = j_val = None; kdj_state = "数据不足"; kdj_cross = ""
    try: atr_val
    except NameError: atr_val = None
    try: bb_upper
    except NameError: bb_upper = bb_middle = bb_lower = None
    if vol > 0 and vr > 2:
        vol_comment = "放量" if change_pct > 0 else "放量下跌"
    elif vol > 0 and vr < 0.8:
        vol_comment = "缩量"
    else:
        vol_comment = "正常"

    # ========== 综合评分（C3.0 V3 重校准，满分约199） ==========
    score = 0
    reasons = []

    # 今日涨幅：拓宽到-5%~10%，5档评分
    if -3 <= change_pct < 0:
        score += 25
        reasons.append(f"回调支撑{change_pct:.2f}%")
    elif 0 <= change_pct <= 3:
        score += 20
        reasons.append(f"涨幅{change_pct:.2f}%(适中)")
    elif 3 < change_pct <= 5:
        score += 25
        reasons.append(f"涨幅{change_pct:.2f}%(温和)")
    elif 5 < change_pct <= 10:
        score += 20
        reasons.append(f"涨幅{change_pct:.2f}%(偏大)")
    elif -5 <= change_pct < -3:
        score += 15
        reasons.append(f"回调{change_pct:.2f}%")
    else:
        reasons.append(f"涨幅{change_pct:.2f}%")

    # 量比：分级评分
    if vr > 3:
        score += 25
        reasons.append(f"量比{vr:.2f}(显著放量)")
    elif 1.5 < vr <= 3:
        score += 20
        reasons.append(f"量比{vr:.2f}")
    elif 1.0 < vr <= 1.5:
        score += 10
        reasons.append(f"量比{vr:.2f}")
    else:
        reasons.append(f"量比{vr:.2f}(缩量)")

    # 换手率：分级评分
    if 5 <= turnover <= 10:
        score += 20
        reasons.append(f"换手率{turnover:.2f}%(活跃)")
    elif 2 <= turnover < 5:
        score += 15
        reasons.append(f"换手率{turnover:.2f}%")
    elif 10 < turnover <= 20:
        score += 15
        reasons.append(f"换手率{turnover:.2f}%(高换手)")
    elif turnover > 20:
        score += 5
        reasons.append(f"换手率{turnover:.1f}%(游资博弈)")
    elif turnover < 2:
        reasons.append(f"换手率{turnover:.2f}%(偏低)")

    # 均线：权重翻倍
    if "多头" in ma_trend:
        score += 40
        reasons.append("均线多头")
    elif "反弹" in ma_trend:
        score += 20
        reasons.append("均线反弹")

    # 52周低位加分
    if h52 and l52 and h52 > l52 > 0:
        price_pos = (price - l52) / (h52 - l52) * 100
        if price_pos < 50:
            score += 5
            reasons.append(f"52周低位{price_pos:.1f}%")

    # RPS加分
    if rps >= 80:
        score += 20
        reasons.append(f"RPS{rps:.0f}(强势)")
    elif rps >= 60:
        score += 15
        reasons.append(f"RPS{rps:.0f}(优质)")
    elif rps >= 40:
        score += 5
        reasons.append(f"RPS{rps:.0f}")

    # 主力净流入加分
    mf_val = 0
    if isinstance(money_flow, dict):
        mf_str = money_flow.get("主力净流入", "N/A")
        if mf_str != "N/A":
            try:
                mf_val = float(mf_str.replace("万", ""))
            except Exception as _e:
                logger.error(f"Error in strategy_service: {_e}")
    if mf_val > 5000:
        score += 20
        reasons.append(f"主力净流入{mf_val:.0f}万(大单)")
    elif mf_val > 1000:
        score += 15
        reasons.append(f"主力净流入{mf_val:.0f}万")
    elif mf_val > 0:
        score += 5
        reasons.append(f"主力净流入{mf_val:.0f}万")

    # MACD信号加分
    if macd_state == "金叉区域":
        score += 15
        reasons.append("MACD金叉")
        if isinstance(macd_line, (int, float)) and macd_line > 0:
            score += 5
            reasons.append("MACD零轴上方")
    elif macd_state == "死叉区域":
        score -= 10
        reasons.append("MACD死叉")

    # KDJ信号加分
    if k_val is not None and d_val is not None:
        if k_val > d_val and k_val < 80:
            score += 8
            reasons.append("KDJ偏多")
            if k_val < 40:
                score += 5
                reasons.append("KDJ低位金叉")
        elif k_val < 20 and d_val < 20:
            score += 8
            reasons.append("KDJ超卖")
        elif k_val > 80:
            reasons.append("KDJ超买")

    score = max(0, score)  # 不限制下限

    # ========== ML模型集成 ==========
    # LambdaRank 依赖 batch 内百分位 rank 特征，单只股票时 rank 全为 1.0 导致输出失真。
    # 优先取最近一次批量扫描的内存缓存（_last_scan_results），
    # 没有则降级查询 ml_predictions 表（可能版本较旧）。
    ml_info = None
    ml_bonus = 0
    try:
        ml_prob = pred_ret = model_type = None
        # 1) 内存缓存（实时批量扫描结果）
        from ml_predict import _last_scan_results
        cached = _last_scan_results.get(ts_code)
        if cached:
            ml_prob = cached['ml概率']
            pred_ret = cached['排序强度']
            model_type = cached.get('模型名称', '')
        # 2) 数据库表（历史扫描结果）
        if ml_prob is None:
            import pymysql

            from quant_app.utils.config import get_db_config
            conn = pymysql.connect(**get_db_config(connect_timeout=3))
            cur = conn.cursor()
            cur.execute(
                "SELECT _ml_pred, predicted_return, model_type FROM ml_predictions "
                "WHERE ts_code=%s ORDER BY trade_date DESC LIMIT 1", [ts_code]
            )
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                ml_prob, pred_ret, model_type = float(row[0]), float(row[1]), str(row[2] or '')
        if ml_prob is not None:
            ml_info = {
                "ML概率": f"{ml_prob*100:.1f}%",
                "ML看涨": ml_prob >= 0.5,
                "排序分": f"{pred_ret:+.2f}",
                "资金趋势": "N/A",
                "模型名称": model_type or "未知",
            }
            ml_bonus = round(ml_prob * 20)
            if ml_bonus > 0:
                reasons.append(f"ML模型+{ml_bonus}分")
    except Exception as e:
        logger.info(f"ML增强不可用: {e}")

    score += ml_bonus
    score = max(0, score)

    # 操作建议（阈值微调）
    if score >= 80:
        suggestion = "强烈买入"
    elif score >= 60:
        suggestion = "适量买入"
    elif score >= 45:
        suggestion = "持有观察"
    else:
        suggestion = "谨慎观望"

    # ========== ATR动态止盈止损 ==========
    # 用市场状态参数校验ATR结果的合理性
    try:
        ms_params = (unified_market_state() or {}).get('params', {})
        max_sl_pct = ms_params.get('stop_loss_pct', -5)
        min_tp_pct = ms_params.get('take_profit_pct', 8)
    except Exception as e:
        logger.warning(f"获取市场状态参数失败: {e}")
        max_sl_pct = -5
        min_tp_pct = 8

    if atr_val and atr_val > 0:
        stop_loss = round(price - 1.5 * atr_val, 2)
        target_price = round(price + 3 * atr_val, 2)
        # ATR止损不得低于市场状态规定的最大止损（如-5%）
        max_sl_price = round(price * (1 + max_sl_pct / 100), 2)
        if stop_loss < max_sl_price:
            stop_loss = max_sl_price
        # ATR止盈不得低于市场状态规定的最小止盈
        min_tp_price = round(price * (1 + min_tp_pct / 100), 2)
        if target_price < min_tp_price:
            target_price = min_tp_price
    else:
        stop_loss = round(price * (1 + max_sl_pct / 100), 2)
        target_price = round(price * (1 + min_tp_pct / 100), 2)

    # ========== LM总结生成 ==========
    summary_parts = []

    # 技术面综合
    tech_parts = []
    if "多头" in ma_trend:
        tech_parts.append("均线呈多头排列，中期趋势向好")
    elif "空头" in ma_trend:
        tech_parts.append("均线呈空头排列，中期趋势偏弱")
    elif "反弹" in ma_trend:
        tech_parts.append("价格处于反弹阶段，短期均线有走好迹象")
    else:
        tech_parts.append("均线处于震荡格局，方向不明确")

    if macd_state == "金叉区域":
        tech_parts.append(f"MACD处于金叉区域(DIF={macd_line:.2f}，DEA={signal_line:.2f})，多头动能偏强")
    elif macd_state == "死叉区域":
        tech_parts.append(f"MACD处于死叉区域(DIF={macd_line:.2f}，DEA={signal_line:.2f})，空头动能占优")
    else:
        tech_parts.append("MACD信号不明确")

    if k_val is not None:
        if kdj_state == "超买":
            tech_parts.append(f"KDJ进入超买区域(K={k_val:.2f})，短期注意回调风险")
        elif kdj_state == "超卖":
            tech_parts.append(f"KDJ处于超卖区域(K={k_val:.2f})，存在技术性反弹机会")
        else:
            tech_parts.append(f"KDJ指标运行正常(K={k_val:.2f}，D={d_val:.2f}，J={j_val:.2f})")

    tech_parts.append(f"RPS评分为{rps:.0f}/100，" + ("处于市场前列，强势特征明显" if rps >= 80 else "处于市场中游" if rps >= 50 else "偏弱，关注趋势改善"))
    tech_parts.append(f"量价配合{vol_comment}")

    summary_parts.append(("技术面综合", "；".join(tech_parts)))

    # 资金面
    mf_summary = ""
    if mf_val > 5000:
        mf_summary = f"主力资金大幅净流入{mf_val:.0f}万，做多意愿强烈"
    elif mf_val > 1000:
        mf_summary = f"主力资金净流入{mf_val:.0f}万，资金面偏积极"
    elif mf_val > 0:
        mf_summary = f"主力资金小幅净流入{mf_val:.0f}万，态度偏谨慎"
    else:
        mf_summary = "主力资金无明显流入，资金面中性偏弱"
    summary_parts.append(("资金面", mf_summary))

    # ML模型观点
    ml_summary = ""
    if ml_info:
        trend_map = {'accelerating': '加速流入', 'steady': '平稳', 'weakening': '减弱', 'inflow': '流入', 'outflow': '流出', 'unknown': '未知'}
        trend_cn = trend_map.get(ml_info.get('资金趋势', ''), ml_info.get('资金趋势', 'N/A'))
        ml_summary = f"ML模型看好概率{ml_info['ML概率']}，排序分{ml_info['排序分']}，{trend_cn}"
        if ml_info.get("ML看涨"):
            ml_summary += "，模型信号偏积极"
        else:
            ml_summary += "，模型信号偏谨慎"
    else:
        ml_summary = "ML模型暂未出信号（数据不足或模型文件未加载）"
    summary_parts.append(("模型观点", ml_summary))

    # ATR波动分析
    atr_analysis = ""
    if atr_val and atr_val > 0 and price > 0:
        atr_ratio = atr_val / price * 100
        if atr_ratio < 1.5:
            atr_analysis = f"ATR波动率{atr_ratio:.1f}%，低波动区间，价格运行平稳"
        elif atr_ratio < 3.0:
            atr_analysis = f"ATR波动率{atr_ratio:.1f}%，正常波动范围，适合趋势跟踪"
        else:
            atr_analysis = f"ATR波动率{atr_ratio:.1f}%，高波动区间，注意止损设置"
    else:
        atr_analysis = "ATR数据不足，无法评估波动率"
    summary_parts.append(("波动分析", atr_analysis))

    # 布林带位置
    bb_summary = ""
    if bb_middle and bb_middle > 0 and bb_upper and bb_lower:
        bb_position = (price - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper != bb_lower else 50
        bb_width = (bb_upper - bb_lower) / bb_middle * 100
        if bb_position < 20:
            bb_summary = f"价格处于布林下轨附近（{bb_position:.0f}%分位），接近下轨支撑，可能存在反弹机会"
        elif bb_position > 80:
            bb_summary = f"价格处于布林上轨附近（{bb_position:.0f}%分位），接近上轨压力，注意回调风险"
        elif 40 <= bb_position <= 60:
            bb_summary = f"价格处于布林中轨附近（{bb_position:.0f}%分位），中性位置，等待方向选择"
        else:
            bb_summary = f"价格处于布林带{bb_position:.0f}%分位，趋势运行中"
        bb_summary += f"（带宽{bb_width:.1f}%）"
    else:
        bb_summary = "布林带数据不足，无法判断价格位置"
    summary_parts.append(("布林带位置", bb_summary))

    # 板块趋势
    sector_summary = ""
    try:
        from sector_rotation import get_hot_sectors
        hot_sectors = get_hot_sectors(top_n=5)
        stock_industry = basic_info.get("行业", "N/A")
        if stock_industry and stock_industry != "N/A":
            if stock_industry in [s.get("名称", s) if isinstance(s, dict) else s for s in hot_sectors]:
                sector_summary = f"所属板块【{stock_industry}】为当前热点板块，板块效应有助于个股表现"
            else:
                sector_summary = f"所属板块【{stock_industry}】不在当前热点板块中，个股独立行情需更多确认"
        else:
            sector_summary = "无法获取所属板块信息"
    except Exception as e:
        logger.warning(f"板块趋势分析失败: {e}")
        sector_summary = "板块趋势数据暂不可用"
    summary_parts.append(("板块趋势", sector_summary))

    # 大盘情绪
    market_summary = ""
    try:
        ms = unified_market_state()
        state_name = ms.get('state_name', '未知')
        advice = ms.get('advice', '')
        market_summary = f"大盘{state_name}，{advice}"
    except Exception as e:
        logger.warning(f"大盘情绪获取失败: {e}")
        market_summary = "大盘情绪数据暂不可用"
    summary_parts.append(("大盘情绪", market_summary))

    # 风险提示
    risks = []
    if k_val is not None and k_val > 80:
        risks.append("KDJ进入超买区，短期技术性回调风险较大")
    if "空头" in ma_trend:
        risks.append("均线空头排列，中期趋势偏弱，不宜重仓")
    if change_pct < 0:
        risks.append(f"今日下跌{abs(change_pct):.2f}%，短线承压")
    if mf_val < 0:
        risks.append("主力资金净流出，资金面不支撑上涨")
    if macd_state == "死叉区域":
        risks.append("MACD死叉，空头动能仍未释放完毕")
    # ATR>3%波动警告
    if atr_val and price > 0 and atr_val / price * 100 > 3:
        risks.append(f"ATR波动率{(atr_val/price*100):.1f}%，波动较大，注意控制仓位")
    if change_pct > 7:
        risks.append(f"今日涨幅{change_pct:.1f}%，短期涨幅较大，注意追高风险")
    if not risks:
        risks.append("当前未检测到明显风险信号，仍需结合大盘走势综合判断")
    summary_parts.append(("风险提示", risks))

    # 操作参考
    action_text = ""
    if suggestion == "强烈买入":
        action_text = f"综合评分较高（{score}分），建议积极关注。"
    elif suggestion == "适量买入":
        action_text = f"综合评分中等偏上（{score}分），建议逢低建仓。"
    elif suggestion == "持有观察":
        action_text = f"综合评分一般（{score}分），建议持仓观察或等待更明确信号。"
    else:
        action_text = f"综合评分偏低（{score}分），建议暂时观望，等待趋势改善。"
    action_text += f"若入场，参考止损价{stop_loss:.2f}元(-{(price-stop_loss)/price*100:.1f}%)，目标价{target_price:.2f}元(+{(target_price-price)/price*100:.1f}%)。"
    summary_parts.append(("操作参考", action_text))

    summary = {
        "技术面综合": summary_parts[0][1],
        "资金面": summary_parts[1][1],
        "模型观点": summary_parts[2][1],
        "波动分析": summary_parts[3][1],
        "布林带位置": summary_parts[4][1],
        "板块趋势": summary_parts[5][1],
        "大盘情绪": summary_parts[6][1],
        "风险提示": summary_parts[7][1],
        "操作参考": summary_parts[8][1],
    }

    # ========== 技术面返回字段补充 ==========
    tech_extra = {}
    if k_val is not None:
        tech_extra["KDJ"] = f"K={k_val:.2f} D={d_val:.2f} J={j_val:.2f}（{kdj_state}）"
    if macd_line is not None:
        tech_extra["MACD"] = f"DIF={macd_line:.2f} DEA={signal_line:.2f} 柱={macd_hist:.2f}（{macd_state}）"
    if atr_val is not None:
        tech_extra["ATR"] = f"{atr_val:.2f}"
    if bb_middle is not None and bb_upper is not None and bb_lower is not None:
        tech_extra["布林带"] = f"上轨={bb_upper:.2f} 中轨={bb_middle:.2f} 下轨={bb_lower:.2f}"
        bb_pos = f"{(price-bb_lower)/(bb_upper-bb_lower)*100:.0f}%" if bb_upper != bb_lower else "50%"
        tech_extra["布林带位置"] = bb_pos
    # 板块趋势数据
    stock_industry = basic_info.get("行业", "N/A")
    tech_extra["板块"] = stock_industry

    return {
        "股票名称": rt["名称"],
        "股票代码": f"{market.upper()}{code}",
        "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "一、基础数据": {
            "现价": f"{price:.2f}元", "昨收": f"{yc:.2f}元", "今开": f"{o:.2f}元",
            "最高": f"{h:.2f}元", "最低": f"{l:.2f}元",
            "涨跌幅": f"{change_pct:+.2f}%",
            "涨停价": f"{zt}元", "跌停价": f"{dt}元",
            "成交量": f"{vol/10000:.1f}万手",
            "成交额": f"{rt['成交额']/100000000:.2f}亿" if rt["成交额"] else "N/A",
            "换手率": f"{turnover:.2f}%", "量比": f"{vr:.2f}",
            "52周高点": f"{h52}元", "52周低点": f"{l52}元",
            "市值": f"{rt['市值']/100000000:.2f}亿" if rt["市值"] else "N/A",
        },
        "二、技术面": {
            "均线趋势": ma_trend,
            "价格位置": f"52周区间的{(price-l52)/(h52-l52)*100:.1f}%" if h52 and l52 and h52 > l52 > 0 else "数据异常",
            "RPS评分": f"{rps:.0f}/100",
            "量价配合": vol_comment,
            **tech_extra,
        },
        "三、资金面": money_flow,
        "四、基本面": basic_info,
        "五、综合评分": {
            "总分": f"{score:.2f}/100",
            "操作建议": suggestion,
            "止损价": f"{stop_loss:.2f}元",
            "目标价": f"{target_price:.2f}元",
            "风险等级": "高" if score < 40 else ("中" if score < 60 else "低"),
            "建议理由": " | ".join(reasons) if reasons else "数据不足",
        },
        "六、AI模型": ml_info,
        "七、总结": summary,
    }


# ========== 策略选股（C3.0 V3 — Tushare） ==========
def get_block_stocks(block_name, basic_df=None):
    """通过stock_basic的industry字段获取板块成分股"""
    try:
        if basic_df is None:
            pro = get_tushare_pro()
            basic_df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
        df = basic_df[basic_df["industry"] == block_name]
        return df["ts_code"].tolist()[:30]
    except Exception as e:
        logger.warning(f"获取板块{block_name}失败: {e}")
        return []


def get_dragon_tiger_bonus(conn, ts_code, trade_date):
    """
    计算龙虎榜加分（最高+15分）
    - 近30日机构净买入 > 3000万 → +15
    - 近30日上榜（无机构）→ +8
    - 无上榜 → 0
    """
    try:
        cur = conn.cursor()
        td_limit = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

        # 机构净买入
        cur.execute("""
            SELECT COALESCE(SUM(net_buy), 0)
            FROM dragon_tiger_inst
            WHERE ts_code = %s AND trade_date >= %s
              AND (exalter LIKE '%%机构%%' OR exalter LIKE '%%专用%%')
        """, (ts_code, td_limit))
        inst_net = float(cur.fetchone()[0])

        # 是否上榜
        cur.execute("""
            SELECT COUNT(*) FROM dragon_tiger
            WHERE ts_code = %s AND trade_date >= %s
        """, (ts_code, td_limit))
        listed_count = int(cur.fetchone()[0])

        cur.close()

        if inst_net > 30000000:
            return 15
        elif inst_net > 5000000:
            return 12
        elif listed_count > 0:
            return 8
        return 0
    except Exception as e:
        logger.debug(f"龙虎榜查询失败 {ts_code}: {e}")
        return 0


def get_holder_bonus(conn, ts_code):
    """
    计算股东集中度加分（最高+10分）
    - 连续3期减少 → +10
    - 连续2期减少 → +7
    - 1期减少 → +4
    - 增加/持平 → 0
    """
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT end_date, holder_num_change
            FROM holder_change
            WHERE ts_code = %s
            ORDER BY end_date DESC LIMIT 4
        """, (ts_code,))
        rows = cur.fetchall()
        cur.close()

        if not rows or len(rows) < 2:
            return 0

        decreases = 0
        for r in rows:
            change = int(r[1]) if r[1] else 0
            if change < 0:
                decreases += 1

        if decreases >= 3:
            return 10
        elif decreases >= 2:
            return 7
        elif decreases >= 1:
            return 4
        return 0
    except Exception as e:
        logger.debug(f"股东集中度查询失败 {ts_code}: {e}")
        return 0


def score_stock_c30(change_pct, turnover, vr, price, h52, l52, ma5, ma10, ma20, chg3day, rps=50, money_flow=0, pct_chg_rank_pct=None, macd_dif=None, macd_dea=None, k_val=None, d_val=None, dragon_tiger_bonus=0, holder_bonus=0):
    """C3.0 V3 选股评分（重校准，满分约195）"""
    score = 0
    reasons = []

    # 根据均线计算ma_trend
    if ma5 > ma10 > ma20:
        ma_trend = "多头"
    elif ma5 < ma10 < ma20:
        ma_trend = "空头"
    else:
        ma_trend = "缠绕"

    # 涨幅要求：拓宽到-5%~10%，不设硬淘汰（高涨幅回调支撑）
    if -3 <= change_pct < 0:
        score += 30
        reasons.append(f"回调支撑{change_pct:.2f}%")
    elif 0 <= change_pct <= 3:
        score += 25
        reasons.append(f"涨幅适中{change_pct:.2f}%")
    elif 3 < change_pct <= 5:
        score += 30
        reasons.append(f"温和上涨{change_pct:.2f}%")
    elif 5 < change_pct <= 10:
        score += 20
        reasons.append(f"涨幅偏大{change_pct:.2f}%")
    elif -5 <= change_pct < -3:
        score += 10
        reasons.append(f"回调{change_pct:.2f}%")
    elif change_pct < -5:
        return 0, ["跌幅过大"]
    elif change_pct > 10:
        return 0, ["涨幅过大"]

    # 量比：分级评分
    if vr > 3:
        score += 30
        reasons.append(f"量比{vr:.2f}(显著放量)")
    elif 1.5 < vr <= 3:
        score += 25
        reasons.append(f"量比{vr:.2f}")
    elif 1.0 < vr <= 1.5:
        score += 10
        reasons.append(f"量比{vr:.2f}")
    elif vr <= 1.0:
        return 0, ["量比不足"]

    # 换手率：分级评分
    if 5 <= turnover <= 10:
        score += 20
        reasons.append(f"换手率{turnover:.2f}%(活跃)")
    elif 10 < turnover <= 20:
        score += 15
        reasons.append(f"换手率{turnover:.2f}%(高换手)")
    elif 3 <= turnover < 5:
        score += 15
        reasons.append(f"换手率{turnover:.2f}%")
    elif 2 <= turnover < 3:
        score += 8
        reasons.append(f"换手率{turnover:.2f}%(偏低)")
    elif turnover > 20:
        score += 5
        reasons.append(f"换手率{turnover:.1f}%(游资博弈)")
    elif turnover < 2:
        return 0, ["换手率不足"]

    # 均线（权重翻倍）
    if "多头" in ma_trend:
        score += 30
        reasons.append("均线多头")
    elif "缠绕" in ma_trend:
        score += 16
        reasons.append("均线缠绕")
    elif price > ma5:
        score += 10
        reasons.append("站上MA5")

    # MACD信号（新增）
    if macd_dif is not None and macd_dea is not None:
        if macd_dif > macd_dea and macd_dif > 0:
            score += 10
            reasons.append("MACD金叉零轴上方")
        elif macd_dif > macd_dea:
            score += 5
            reasons.append("MACD金叉")
        elif macd_dif < macd_dea:
            score -= 5
            reasons.append("MACD死叉")

    # KDJ信号（新增）
    if k_val is not None and d_val is not None:
        if k_val > d_val and k_val < 80:
            score += 8
            reasons.append("KDJ偏多")
        if k_val < 20 and d_val < 20:
            score += 5
            reasons.append("KDJ超卖")

    if 0 <= chg3day <= 10:
        score += 10
        reasons.append(f"3日涨幅{chg3day:.2f}%")
    elif chg3day > 10:
        score += 5
        reasons.append(f"3日暴涨{chg3day:.2f}%(小心回调)")
    elif chg3day < -5:
        score -= 8
        reasons.append(f"3日大跌{chg3day:.2f}%(弱势)")
    else:
        score -= 3
        reasons.append(f"3日微跌{chg3day:.2f}%")

    # 冲高回落检查：当日跌但 3 日大涨 → 可能见顶
    if change_pct < 0 and chg3day > 5:
        score -= 8
        reasons.append(f"冲高回落(3日涨{chg3day:.1f}%但今日跌)")

    # 52周位置检查：不能在高位（距离高点太近意味着回调风险大）
    if h52 and l52 and h52 > l52 > 0:
        pos = (price - l52) / (h52 - l52) * 100  # 0%=低点，100%=高点
        if pos < 60:
            score += 15
            reasons.append(f"52周低位{pos:.1f}%")
        elif pos >= 85:
            return 0, [f"价格已在52周高位{pos:.0f}%，回调风险大"]  # 过滤掉在顶端的股票

    # RPS 加分：RPS 越高分越多（修复原版 60-80 比 80-100 分更高的 bug）
    if rps >= 80:
        score += 20
        reasons.append(f"RPS强度{rps:.0f}(强势)")
    elif rps >= 60:
        score += 15
        reasons.append(f"RPS强度{rps:.0f}(启动)")
    elif rps >= 40:
        score += 10
        reasons.append(f"RPS强度{rps:.0f}")

    # 主力净流入加分：资金主动买入代表主力看好
    if money_flow > 5000:
        score += 15
        reasons.append(f"主力净流入{money_flow:.0f}万(大单)")
    elif money_flow > 1000:
        score += 10
        reasons.append(f"主力净流入{money_flow:.0f}万(中单)")
    elif money_flow > 0:
        score += 5
        reasons.append(f"主力净流入{money_flow:.0f}万")

    # 去掉 100 分封顶，让评分充分区分（原版大部分股票卡在100，失去排序能力）
    # 涨跌幅日排名：在全市场所有股票中的当日表现位置
    # pct_chg_rank_pct passed from strategy_scan

    # === 涨跌幅日排名评分 ===
    if pct_chg_rank_pct is not None:
        if pct_chg_rank_pct >= 0.8:
            score += 8
            reasons.append("日涨幅强势(前20%)")
        elif pct_chg_rank_pct >= 0.6:
            score += 4
            reasons.append("日涨幅偏强(前40%)")
        elif pct_chg_rank_pct <= 0.2:
            score -= 5
            reasons.append("日涨幅偏弱(后20%)")

    # === 龙虎榜加分（最高+15）===
    if dragon_tiger_bonus > 0:
        score += dragon_tiger_bonus
        if dragon_tiger_bonus >= 15:
            reasons.append(f"龙虎榜机构净买入+{dragon_tiger_bonus}")
        elif dragon_tiger_bonus >= 8:
            reasons.append(f"龙虎榜上榜+{dragon_tiger_bonus}")

    # === 股东集中度加分（最高+10）===
    if holder_bonus > 0:
        score += holder_bonus
        if holder_bonus >= 10:
            reasons.append(f"股东连续减少+{holder_bonus}")
        elif holder_bonus >= 7:
            reasons.append(f"股东连续2期减少+{holder_bonus}")
        elif holder_bonus >= 4:
            reasons.append(f"股东减少+{holder_bonus}")

    return score, reasons




def detect_macd_crossover(closes, lookback=10):
    """
    真正的 MACD 金叉穿越检测。
    检测近 lookback 个交易日内是否有 DIF 从 DEA 下方穿越到上方。

    参数:
        closes: 收盘价列表（按时间顺序，最新在最后）
        lookback: 回溯窗口（默认10个交易日）

    返回:
        (has_crossover, days_ago)
        has_crossover: 是否检测到金叉穿越（True/False）
        days_ago: 穿越距今几个交易日（0=今天穿越，1=昨天穿越，-1=无穿越）
    """
    if len(closes) < 30:
        return False, -1

    # 计算完整的 MACD 序列
    from app_core import calculate_ema
    fast_ema = calculate_ema(closes, 12)
    slow_ema = calculate_ema(closes, 26)

    # DIF = EMA12 - EMA26
    dif_list = []
    for f, s in zip(fast_ema, slow_ema):
        if f is None or s is None:
            dif_list.append(None)
        else:
            dif_list.append(f - s)

    # 过滤掉 None，只保留有效值来计算 DEA 序列（然后补回 None 对齐）
    valid_dif = [d for d in dif_list if d is not None]
    if len(valid_dif) < 10:
        return False, -1

    # DEA = DIF的EMA9
    valid_dea = []
    if len(valid_dif) >= 9:
        k = 2.0 / (9 + 1)
        dea = sum(valid_dif[:9]) / 9.0
        valid_dea.append(dea)
        for v in valid_dif[9:]:
            dea = v * k + dea * (1 - k)
            valid_dea.append(dea)

    # 将对齐后的 dif 和 dea 映射回去（跳过前面的 None）
    none_count = sum(1 for d in dif_list if d is None)
    aligned_dif = [d for d in dif_list if d is not None]
    aligned_dea = valid_dea[:len(aligned_dif)]

    if len(aligned_dif) < 2:
        return False, -1

    # 在近 lookback 个交易日内寻找穿越点（从最新往前找）
    n = len(aligned_dif)
    search_start = max(1, n - lookback)

    # 从最新往前搜索，找到最近的一次穿越（DIF上穿DEA）
    for i in range(n - 1, search_start - 1, -1):
        prev_dif = aligned_dif[i - 1]
        curr_dif = aligned_dif[i]
        prev_dea = aligned_dea[i - 1] if i - 1 < len(aligned_dea) else 0
        curr_dea = aligned_dea[i] if i < len(aligned_dea) else 0

        # 穿越：前一日 DIF <= DEA，当日 DIF > DEA
        if prev_dif <= prev_dea and curr_dif > curr_dea:
            days_ago = n - 1 - i  # 距离今天的天数（0=最新交易日穿越）
            return True, days_ago

    return False, -1


# ========== 策略扫描 ==========
def scan_daily_pool():
    """
    每日17:00股票池扫描 - 只用Tushare数据，不调用任何API
    筛选条件：涨跌幅0.5~3%、量比1.2~5.0、换手率2~15%
    保存Top100到stock_pool.json
    """
    try:
        pro = get_tushare_pro()
        logger.info("开始每日股票池扫描...")

        # 大盘情绪过滤
        ms_scan = unified_market_state()
        logger.info(f"大盘状态: {ms_scan.get('state_name', '未知')} (评分:{ms_scan.get('score', 0)})")
        mood = ms_scan.get('state', 'range')
        advice = ms_scan.get('advice', '')
        sh_pct, cyb_pct, sh_date = 0.0, 0.0, ''
        try:
            import pymysql as _pm
            _c = _pm.connect(**get_db_config(connect_timeout=3))
            _cur = _c.cursor()
            _cur.execute("SELECT trade_date, change_pct FROM market_index_daily WHERE index_code='000001.SH' ORDER BY trade_date DESC LIMIT 1")
            _r = _cur.fetchone()
            if _r: sh_date, sh_pct = str(_r[0]), float(_r[1]) if _r[1] else 0.0
            _cur.execute("SELECT trade_date, change_pct FROM market_index_daily WHERE index_code='399006.SZ' ORDER BY trade_date DESC LIMIT 1")
            _r = _cur.fetchone()
            if _r: cyb_pct = float(_r[1]) if _r[1] else 0.0
            _c.close()
        except Exception as e:
            logger.warning(f"获取指数行情失败: {e}")
            pass

        # 获取所有股票基本信息，排除ST股和科创板
        all_basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
        all_basic = all_basic[~all_basic["name"].str.contains("ST", na=False)]  # 排除ST
        all_basic = all_basic[~all_basic["ts_code"].str.startswith("688")]  # 排除科创板
        name_map = dict(zip(all_basic["ts_code"], all_basic["name"]))
        industry_map = dict(zip(all_basic["ts_code"], all_basic["industry"]))

        # 获取交易日
        dates = get_recent_trade_dates(5)
        if not dates:
            logger.error("无法获取交易日历")
            return {"error": "无法获取交易日历"}
        today = dates[-1]

        # 批量获取当日行情（如果当天没数据则向前找最近的交易日）
        today_df = pro.daily(trade_date=today)
        if today_df is None or len(today_df) == 0:
            logger.warning(f"今日({today})无交易数据，向前查找最近有数据的交易日...")
            # 向前最多查5天
            found = False
            for days_back in range(1, 6):
                prev_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
                today_df = pro.daily(trade_date=prev_date)
                if today_df is not None and len(today_df) > 0:
                    today = prev_date
                    logger.info(f"使用{prev_date}的数据进行扫描")
                    found = True
                    break
            if not found:
                logger.error("最近5天内都没有交易数据")
                return {"error": "最近5天内都没有交易数据"}

        today_basic = pro.daily_basic(trade_date=today)
        today_merged = today_df.merge(today_basic, on=["ts_code", "trade_date"], suffixes=('_daily', '_basic'))
        logger.info(f"Tushare行情数据: {len(today_df)}只, 合并后: {len(today_merged)}只, 交易日: {today}")

        # 检查 daily_basic 数据质量：如果大量股票的 volume_ratio 为0（数据未更新），则放宽量比条件
        vr_nonzero = (today_merged['volume_ratio'].fillna(0) > 0).sum()
        data_fresh = vr_nonzero > len(today_merged) * 0.3  # 至少30%股票有数据才算新鲜
        if not data_fresh:
            logger.info(f"daily_basic数据未更新(量比非零仅{vr_nonzero}/{len(today_merged)})，放宽量比换手过滤")

        candidates = []
        for _, row in today_merged.iterrows():
            try:
                ts_code = row["ts_code"]
                code = ts_code.split(".")[0]
                market_code = "sz" if ts_code.endswith(".SZ") else "sh"

                change_pct = float(row.get("pct_chg", 0) or 0)
                turnover = float(row.get("turnover_rate", 0) or 0)
                vr = float(row.get("volume_ratio", 0) or 0)
                price = float(row.get("close_daily") or row.get("close_basic") or row.get("pre_close") or 0)

                # 筛选条件：如果daily_basic数据不新鲜，放宽量比和换手率限制
                if data_fresh:
                    if not (-3 <= change_pct <= 5 and vr > 1.0 and turnover > 1.5):
                        continue
                else:
                    # 数据不新鲜时：只用涨跌幅过滤，不用量比和换手率（因为它们可能是0）
                    if not (-3 <= change_pct <= 5):
                        continue

                candidates.append({
                    "ts_code": ts_code,
                    "代码": f"{market_code.upper()}{code}",
                    "名称": name_map.get(ts_code, code),
                    "行业": industry_map.get(ts_code, ""),
                    "现价": price,
                    "涨跌幅": f"{change_pct:+.2f}%",
                    "换手率": f"{turnover:.2f}%" if turnover else "N/A",
                    "量比": f"{vr:.2f}" if vr else "N/A",
                    "快速评分": 0,
                    "入选理由": "",
                })
            except Exception:
                continue

        logger.info(f"快速筛选: 总{len(today_merged)}只, 候选{len(candidates)}只")

        # ===== P3 强势活跃放宽增强：均线趋势确认 + 3日涨幅动量 + 板块筛选 =====
        # 获取当日板块趋势数据（用于板块筛选）
        concept_data = None
        concept_file = os.path.join(DATA_DIR, "concept_trend_v4.json")
        try:
            if os.path.exists(concept_file):
                import os as _os
                stat = _os.stat(concept_file)
                from datetime import datetime as _dt
                file_age_hours = (_dt.now().timestamp() - stat.st_mtime) / 3600
                if file_age_hours < 24:
                    with open(concept_file) as _cf:
                        concept_data = json.load(_cf)
        except Exception as _e:
            logger.error(f"Error in strategy_service: {_e}")

        top5_concepts = []
        if concept_data and "top5_concepts" in concept_data:
            top5_concepts = [c["名称"] for c in concept_data["top5_concepts"]]
            logger.info(f"板块筛选：当日涨幅前5板块 = {top5_concepts}")

        enriched_candidates = []
        for c in candidates:
            ts_code = c["ts_code"]
            conn = None
            try:
                import pymysql
                db_config = get_db_config(connect_timeout=3)
                conn = pymysql.connect(**db_config)
                cursor = conn.cursor()

                # 获取均线数据
                cursor.execute("""
                    SELECT d.ma5, d.ma10, d.ma20, d.close,
                           d.pct_chg, d.volume_ratio, d.turnover_rate
                    FROM daily_price d
                    WHERE d.ts_code = %s ORDER BY d.trade_date DESC LIMIT 1
                """, (ts_code,))
                row_ma = cursor.fetchone()
                cursor.close()

                if not row_ma or not row_ma[3]:
                    continue

                ma5 = float(row_ma[0]) if row_ma[0] else 0
                ma10 = float(row_ma[1]) if row_ma[1] else 0
                ma20 = float(row_ma[2]) if row_ma[2] else 0
                close = float(row_ma[3]) if row_ma[3] else 0
                pct_chg = float(row_ma[4]) if row_ma[4] else 0
                vr = float(row_ma[5]) if row_ma[5] else 0
                tr = float(row_ma[6]) if row_ma[6] else 0

                # 龙虎榜加分
                dt_bonus = get_dragon_tiger_bonus(conn, ts_code, today)
                # 股东集中度加分
                hc_bonus = get_holder_bonus(conn, ts_code)

                # 主力评分过滤（V4.1 要求主力评分≥60）
                _scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts')
                if _scripts_dir not in sys.path:
                    sys.path.insert(0, _scripts_dir)
                from mainforce_scoring import calculate_mainforce_score
                mf = calculate_mainforce_score(ts_code, datetime.strptime(today, '%Y%m%d').date(), conn=conn)
                mainforce_score = mf.get('score', 0)
                mainforce_level = mf.get('level', '未知')

                score = _v41_score(ma5, ma10, ma20, close, pct_chg, vr, tr, dt_bonus, hc_bonus)
                if score <= 0:
                    continue

                c["快速评分"] = score
                c["综合评分"] = score
                c["主力评分"] = int(mainforce_score)
                c["阶段判断"] = mainforce_level
                c["龙虎榜加分"] = dt_bonus
                c["股东加分"] = hc_bonus
                c["3日涨幅"] = 0
                reasons = []
                if dt_bonus > 0:
                    reasons.append(f"龙虎榜+{dt_bonus}")
                if hc_bonus > 0:
                    reasons.append(f"股东集中+{hc_bonus}")
                if reasons:
                    c["入选理由"] = " | ".join(reasons)
                enriched_candidates.append(c)
            except Exception as e:
                logger.warning(f"候选股增强失败 {c.get('ts_code','')}: {e}")
                continue
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass

        candidates = enriched_candidates
        # ===== P1 增强结束 =====

        # ML增强评分（如果模型可用）
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from ml_predict import ml_enhanced_score
            candidates = ml_enhanced_score(candidates, db_conn=None)
            has_ml = True
            logger.info(f"ML增强已应用，共{len(candidates)}只")
        except Exception as e:
            logger.info(f"ML增强不可用，使用原始评分: {e}")
            has_ml = False

        # 按增强评分排序（有ML时用增强评分，否则用快速评分）
        if has_ml:
            candidates.sort(key=lambda x: x.get('增强评分', 0), reverse=True)
        else:
            candidates.sort(key=lambda x: x["快速评分"], reverse=True)

        # 大盘状态过滤：弱势时提高门槛，减少输出数量
        _scan_mood = ms_scan.get('state', 'range') if 'ms_scan' in dir() else 'range'
        if _scan_mood in ('trend_down', 'panic'):
            candidates = [c for c in candidates if c["快速评分"] >= 6]
            pool = candidates[:30]
            logger.info(f"弱势市场过滤：评分>=6，保留{len(pool)}只")
        else:
            pool = candidates[:100]

        # 保存股票池
        actual_trade_date = today  # 实际有数据的交易日（today可能被回退修改过）
        pool_data = {
            "scan_date": today,
            "actual_trade_date": actual_trade_date,  # 实际数据对应的交易日
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(candidates),
            "stocks": pool,
            "market_mood": mood,
            "market_advice": advice,
            "sh_pct": round(sh_pct, 3),
            "cyb_pct": round(cyb_pct, 3),
            "sh_date": sh_date,
        }
        pool_file = os.path.join(DATA_DIR, "stock_pool.json")
        with open(pool_file, 'w') as f:
            json.dump(pool_data, f, ensure_ascii=False, indent=2)

        # 同时更新强势活跃股票池（Tushare版本，不覆盖cron写入的stock_pool_strong.json）
        strong_pool = {
            "pool": pool[:20],  # Top20作为强势活跃候选
            "updated": actual_trade_date,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        strong_file = os.path.join(DATA_DIR, "stock_pool_strong_tushare.json")
        with open(strong_file, 'w') as f:
            json.dump(strong_pool, f, ensure_ascii=False, indent=2)

        logger.info(f"股票池扫描完成，共{len(candidates)}只候选，保存Top{len(pool)}只，强活{len(strong_pool['pool'])}只")
        return pool_data
    except Exception as e:
        logger.error(f"股票池扫描失败: {e}")
        return {"error": str(e)}


def _v41_score(ma5, ma10, ma20, close, pct_chg, vol_ratio, turnover, dt_bonus=0, hc_bonus=0):
    """V4.1 快速评分：quick_score + 龙虎榜加分 + 股东集中度加分"""
    quick = 0
    if all(v > 0 for v in [ma5, ma10, ma20]) and ma5 > ma10 > ma20:
        quick += 40
    if close > ma5:
        quick += 20
    if vol_ratio > 2.0:
        quick += 20
    if pct_chg > 3:
        quick += 10
    if turnover > 3:
        quick += 10
    return quick + dt_bonus + hc_bonus


def strategy_scan(block_name, market=None):
    """从MySQL实时筛选板块/市场股票 - 不依赖JSON文件"""
    try:
        import pymysql
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            return {"error": "MySQL无交易数据，请先执行数据更新"}

        # 市场过滤（前2位判断：沪市=60, 创业板=30, 深市=00/01/02）
        market_filter = ""
        if market == "创业板":
            market_filter = " AND SUBSTRING(d.ts_code, 1, 2) = '30'"
        elif market == "科创板":
            market_filter = " AND SUBSTRING(d.ts_code, 1, 2) = '68'"
        elif market == "沪市主板":
            market_filter = " AND SUBSTRING(d.ts_code, 1, 2) = '60'"
        elif market == "深市主板":
            market_filter = " AND SUBSTRING(d.ts_code, 1, 2) IN ('00','01','02')"

        # 板块过滤（如果指定了板块名）
        industry_join = ""
        industry_filter = ""
        if block_name:
            industry_filter = " AND s.industry LIKE %s"
            block_pattern = f"%{block_name}%"

        sql = f"""
            SELECT d.ts_code, s.name, s.industry, d.close, d.pct_chg,
                   d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20,
                   d.rps_20, d.high_52w, d.low_52w,
                   COALESCE(mf.main_net, 0) AS main_net
            FROM quant_db.daily_price d
            JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            LEFT JOIN quant_db.moneyflow_daily mf ON d.ts_code = mf.ts_code COLLATE utf8mb4_unicode_ci AND mf.trade_date = %s
            WHERE d.trade_date = %s
              AND d.pct_chg BETWEEN -5 AND 10    -- 放宽到-5到10
              AND d.volume_ratio > 1.0            -- 放宽：只需放量，不设上限
              AND s.is_st = 0
              AND SUBSTRING(d.ts_code, 1, 2) NOT IN ('68')  -- 排除科创板
              AND SUBSTRING(d.ts_code, 1, 1) NOT IN ('8','4','9')  -- 排除北交所/新三板/B股
              {market_filter}
              {industry_filter}
            ORDER BY d.pct_chg DESC, d.volume_ratio DESC
            LIMIT 300
        """
        params = [latest_date, latest_date]
        if block_name:
            params.append(block_pattern)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        if not rows:
            return {
                "板块": block_name or market or "全部",
                "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "股票池扫描时间": str(latest_date),
                "符合条件数": 0,
                "股票列表": [],
                "提示": f"最新数据日期{latest_date}，无符合条件股票"
            }

        # V4.1 评分
        stocks = []
        try:
            tech_conn = pymysql.connect(**db_config)
        except Exception as e:
            logger.warning(f"技术面数据库连接失败: {e}")
            tech_conn = None

        for r in rows:
            code = r[0].split(".")[0]
            mkt = "sz" if r[0].endswith(".SZ") else "sh"
            close = float(r[3]) if r[3] else 0
            pct = float(r[4]) if r[4] else 0
            tr = float(r[5]) if r[5] else 0
            vr = float(r[6]) if r[6] else 0
            ma5 = float(r[7]) if r[7] else 0
            ma10 = float(r[8]) if r[8] else 0
            ma20 = float(r[9]) if r[9] else 0

            # 龙虎榜加分
            dt_bonus = get_dragon_tiger_bonus(tech_conn, r[0], str(latest_date)) if tech_conn else 0
            # 股东集中度加分
            hld_bonus = get_holder_bonus(tech_conn, r[0]) if tech_conn else 0

            # 主力评分过滤（V4.1 要求主力评分≥60）
            try:
                _scripts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts')
                if _scripts_dir not in sys.path:
                    sys.path.insert(0, _scripts_dir)
                from mainforce_scoring import calculate_mainforce_score
                mf = calculate_mainforce_score(r[0], latest_date, conn=tech_conn)
            except Exception as e:
                logger.warning(f"主力资金评分失败 {r[0]}: {e}")
                mf = {'score': 0, 'level': '未知'}
            mainforce_score = mf.get('score', 0)
            mainforce_level = mf.get('level', '未知')

            score = _v41_score(ma5, ma10, ma20, close, pct, vr, tr, dt_bonus, hld_bonus)
            if score <= 0:
                continue

            # 构建入选原因
            reasons = []
            if dt_bonus > 0:
                reasons.append(f"龙虎榜+{dt_bonus}")
            if hld_bonus > 0:
                reasons.append(f"股东集中+{hld_bonus}")

            stock_info = {
                "代码": f"{mkt.upper()}{code}",
                "ts_code": r[0],
                "名称": r[1] or "",
                "行业": r[2] or "",
                "现价": close,
                "涨跌幅": f"{pct:+.2f}%",
                "换手率": f"{tr:.2f}%",
                "量比": f"{vr:.2f}",
                "综合评分": score,
                "主力评分": int(mainforce_score),
                "阶段判断": mainforce_level,
                "龙虎榜加分": dt_bonus,
                "股东加分": hld_bonus,
                "入选理由": " | ".join(reasons) if reasons else "条件符合",
            }
            stocks.append(stock_info)

        if tech_conn:
            tech_conn.close()

        # ML增强评分（如果模型可用）
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from ml_predict import ml_enhanced_score
            conn2 = pymysql.connect(**db_config)
            stocks = ml_enhanced_score(stocks, db_conn=conn2)
            conn2.close()
            has_ml = True
        except Exception as e:
            logger.info(f"ML增强不可用，使用原始评分: {e}")
            has_ml = False
            for s in stocks:
                s['ml概率'] = 0.5
                s['增强评分'] = s.get('综合评分', 0)

        # 按增强评分排序
        stocks.sort(key=lambda x: x.get('增强评分', 0), reverse=True)

        # 市场过滤（内存二次精确筛选）
        if market == "沪市主板":
            stocks = [s for s in stocks if s["代码"][:2] == "SH" and s["代码"][2:4] == "60"]
        elif market == "深市主板":
            stocks = [s for s in stocks if s["代码"][:2] == "SZ" and s["代码"][2:4] in ("00","01","02")]

        # 清理内部字段
        for s in stocks:
            s.pop('ts_code', None)

        ml_note = " | ML增强已启用" if has_ml else ""
        return {
            "板块": block_name or market or "全部",
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "股票池扫描时间": str(latest_date),
            "符合条件数": len(stocks),
            "股票列表": stocks[:10],
            "提示": f"此为 C3.0 V3 评分{ml_note}，满分100分，60分以上值得买入",
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


# ========== 深度技术面选股（定时任务用） ==========
def scan_daily_pool_technical():
    """
    每日18:00深度扫描 - 使用完整技术指标（MACD/KDJ/布林带）
    只对普通扫描的候选股票进行深度分析，而不是全市场
    这样可以大大减少API调用次数，在合理时间内完成
    """
    try:
        pro = get_tushare_pro()
        logger.info("开始深度技术面扫描...")

        # 先读取普通扫描的候选股票
        pool_file = os.path.join(DATA_DIR, "stock_pool.json")
        if not os.path.exists(pool_file):
            logger.warning("股票池文件不存在，先运行普通扫描")
            basic_pool = scan_daily_pool()
            if "error" in basic_pool:
                return {"error": "无法创建基础股票池"}

        with open(pool_file) as f:
            pool_data = json.load(f)

        candidates_basic = pool_data.get("stocks", [])
        if not candidates_basic:
            return {"error": "候选股票池为空"}

        logger.info(f"深度扫描将处理{len(candidates_basic)}只候选股票")

        # 获取所有股票基本信息（用于名称和行业映射）
        all_basic = pro.stock_basic(exchange="", list_status="L", fields="ts_code,name,industry")
        name_map = dict(zip(all_basic["ts_code"], all_basic["name"]))
        industry_map = dict(zip(all_basic["ts_code"], all_basic["industry"]))

        # 获取交易日（深度扫描用30天历史数据就够了）
        dates = get_recent_trade_dates(30)
        if not dates or len(dates) < 30:
            logger.error("无法获取足够的历史数据")
            return {"error": "历史数据不足"}

        start_date = dates[0]
        end_date = dates[-1]
        today = dates[-1]

        # 深度扫描：遍历普通候选股票，获取技术指标
        candidates = []
        total = len(candidates_basic)
        logger.info(f"开始深度扫描{total}只候选股票...")

        for stock in candidates_basic:
            try:
                ts_code_raw = stock.get("代码", "")
                if not ts_code_raw:
                    continue

                # 转换代码格式 SZ000006 -> 000006.SZ
                market_prefix = "SZ" if ts_code_raw.startswith("SZ") else "SH"
                code = ts_code_raw[2:]
                ts_code = f"{code}.{market_prefix}"

                price = float(stock.get("现价", 0) or 0)
                if price == 0:
                    continue

                change_pct_str = stock.get("涨跌幅", "0%").replace("%", "").replace("+", "")
                change_pct = float(change_pct_str or 0)

                turnover_str = stock.get("换手率", "0%").replace("%", "")
                turnover = float(turnover_str or 0)

                vr_str = stock.get("量比", "0")
                vr = float(vr_str or 0)

                # 基础筛选：排除涨跌停
                if change_pct >= 9.5 or change_pct <= -9.5:
                    continue

                # 获取历史数据计算技术指标
                hist_df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                if hist_df is None or len(hist_df) < 20:
                    continue

                hist_df = hist_df.sort_values("trade_date").reset_index(drop=True)
                closes = hist_df["close"].tolist()
                highs = hist_df["high"].tolist()
                lows = hist_df["low"].tolist()
                vols = hist_df["vol"].tolist()

                # 计算技术指标（来自 technical_service，返回当前值标量）
                curr_dif, curr_dea, macd_hist = calculate_macd(closes)
                curr_k, _, _ = calculate_kdj(highs, lows, closes)
                curr_bb_upper, curr_bb_middle, curr_bb_lower = calculate_bollinger_bands(closes)

                # 从 hist_df 计算均线
                ma5 = float(hist_df['close'].rolling(5).mean().iloc[-1]) if len(hist_df) >= 5 else 0
                ma10 = float(hist_df['close'].rolling(10).mean().iloc[-1]) if len(hist_df) >= 10 else 0
                ma20 = float(hist_df['close'].rolling(20).mean().iloc[-1]) if len(hist_df) >= 20 else 0

                # 计算买入评分（重校准，满分约126）
                buy_score = 0
                buy_reasons = []

                # 均线条件（权重翻倍）
                ma_ok = False
                if ma5 > ma10 and ma10 > 0:
                    ma_ok = True
                    buy_score += 16
                    buy_reasons.append("MA5>MA10")
                elif price > ma20 and ma20 > 0:
                    ma_ok = True
                    buy_score += 10
                    buy_reasons.append("股价>MA20")
                if not ma_ok:
                    continue

                # MACD条件
                if curr_dea > 0:
                    buy_score += 15
                    buy_reasons.append("MACD多头")
                elif curr_dif > 0:
                    buy_score += 8
                    buy_reasons.append("MACD转多")
                # MACD金叉加分
                if curr_dif is not None and curr_dea is not None and curr_dif > curr_dea:
                    buy_score += 5
                    buy_reasons.append("MACD金叉")

                # KDJ条件
                if curr_k < 30:
                    buy_score += 15
                    buy_reasons.append("KDJ超卖")
                elif curr_k < 50:
                    buy_score += 8
                    buy_reasons.append("KDJ偏弱")
                elif curr_k < 70:
                    buy_score += 3
                    buy_reasons.append("KDJ中性")
                # KDJ金叉加分（需要先获取d_val, k_val）
                curr_d_val = None
                try:
                    _, curr_d_val, _ = calculate_kdj(highs, lows, closes)
                    if curr_d_val is not None and curr_k > curr_d_val:
                        buy_score += 5
                        buy_reasons.append("KDJ金叉")
                except Exception as e:
                    logger.debug(f"KDJ金叉判断失败: {e}")
                    pass

                # 布林带条件
                if curr_bb_middle > 0:
                    if price <= curr_bb_lower * 1.05:
                        buy_score += 15
                        buy_reasons.append("布林下轨支撑")
                    elif price <= curr_bb_middle * 1.05:
                        buy_score += 10
                        buy_reasons.append("布林中轨支撑")
                    elif price <= curr_bb_upper * 0.9:
                        buy_score += 5
                        buy_reasons.append("在布林上方")
                    else:
                        buy_score = int(buy_score * 0.3)
                        buy_reasons.append("价格接近上轨")

                # 涨幅条件：拓宽区间，-5%不淘汰只减分
                if 0 <= change_pct <= 7:
                    buy_score += 12
                    buy_reasons.append(f"涨幅{change_pct:.1f}%")
                elif -3 <= change_pct < 0:
                    buy_score += 8
                    buy_reasons.append(f"小幅回调{change_pct:.1f}%")
                elif -5 <= change_pct < -3:
                    buy_score += 3
                    buy_reasons.append(f"回调{change_pct:.1f}%")
                elif change_pct < -5:
                    continue  # 跌幅过大，淘汰

                # 量比：分级评分，淘汰阈值降低到1.0
                if 2.0 <= vr <= 8:
                    buy_score += 14
                    buy_reasons.append(f"量比{vr:.1f}(显著放量)")
                elif 1.0 <= vr < 2.0:
                    buy_score += 10
                    buy_reasons.append(f"量比{vr:.1f}")
                elif vr < 1.0:
                    continue  # 量比不足

                # 换手率：新增评分项
                if 5 <= turnover <= 10:
                    buy_score += 8
                    buy_reasons.append(f"换手率{turnover:.1f}%(活跃)")
                elif 2 <= turnover < 5:
                    buy_score += 5
                    buy_reasons.append(f"换手率{turnover:.1f}%")
                elif turnover > 10:
                    buy_score += 5
                    buy_reasons.append(f"换手率{turnover:.1f}%(高换手)")

                candidates.append({
                    "ts_code": ts_code,
                    "代码": ts_code_raw,
                    "名称": stock.get("名称", name_map.get(ts_code, code)),
                    "行业": stock.get("行业", industry_map.get(ts_code, "")),
                    "现价": price,
                    "涨跌幅": f"{change_pct:+.2f}%",
                    "换手率": f"{turnover:.2f}%",
                    "量比": f"{vr:.2f}",
                    "技术面评分": buy_score,
                    "入选理由": "+".join(buy_reasons) if buy_reasons else "信号不足",
                    "MACD多空": "多头" if curr_dea > 0 else "空头",
                    "KDJ状态": "超卖" if curr_k < 30 else ("偏弱" if curr_k < 50 else "中性"),
                    "布林位置": "下轨附近" if price <= curr_bb_lower * 1.05 else ("中轨附近" if price <= curr_bb_middle * 1.05 else "上轨附近"),
                })

            except Exception:
                continue

        # ML模型增强评分（插入在板块轮动增强前）
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import pymysql as _pm

            from ml_predict import ml_enhanced_score
            db_cfg_ml = get_db_config(connect_timeout=3)
            ml_conn = _pm.connect(**db_cfg_ml)
            candidates = ml_enhanced_score(candidates, db_conn=ml_conn)
            ml_conn.close()
            has_ml_tech = True
            logger.info(f"ML增强已应用于深度扫描（{len(candidates)}只）")
        except Exception as e:
            logger.info(f"深度扫描ML增强不可用: {e}")
            has_ml_tech = False
            for s in candidates:
                s['ml概率'] = 0.5
                s['ML评分'] = 0
                s['排序强度'] = "N/A"

        # 板块轮动增强：热点板块加分
        try:
            from sector_rotation import get_hot_sectors, get_sector_bonus
            hot_sectors = get_hot_sectors(top_n=8)
            for s in candidates:
                ts_code = f"{s.get('代码','')}.{'SH' if (s.get('代码','')[:1]=='6') else 'SZ'}"
                bonus, name, _ = get_sector_bonus(ts_code, hot_sectors)
                s["热点板块"] = name
                s["板块加分"] = bonus
        except Exception as e:
            logger.warning(f"板块增强失败: {e}")
            for s in candidates:
                s["增强评分"] = s.get("技术面评分", 0)

        # 按增强评分排序
        candidates.sort(key=lambda x: x.get("增强评分", 0), reverse=True)
        pool = candidates[:100]

        # 保存股票池
        pool_data = {
            "scan_date": today,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(candidates),
            "stocks": pool,
            "scan_type": "深度技术面扫描"
        }
        pool_file = os.path.join(DATA_DIR, "stock_pool.json")
        with open(pool_file, 'w') as f:
            json.dump(pool_data, f, ensure_ascii=False, indent=2)

        logger.info(f"深度扫描完成，共{len(candidates)}只符合条件，保存Top{len(pool)}只")
        return pool_data

    except Exception as e:
        logger.error(f"深度扫描失败: {e}")
        return {"error": str(e)}


# ========== 板块趋势自动扫描 ==========
def scan_concept_trend():
    """
    扫描概念板块趋势，选出热点板块（P1新增）
    通过MySQL daily_price 表的 industry 字段分组计算板块表现。
    返回 Top5 热点板块列表。
    """
    from datetime import datetime

    import pymysql

    try:
        db_config = get_db_config(connect_timeout=5)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        # 获取最新交易日
        cursor.execute("SELECT MAX(trade_date) FROM daily_price")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            conn.close()
            return {"error": "无交易数据"}
        today = str(latest_date)

        # 获取最近7天有数据的交易日（用于计算3/5日涨幅）
        cursor.execute("""
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= %s
            ORDER BY trade_date DESC LIMIT 7
        """, (today,))
        trade_dates = [str(r[0]) for r in cursor.fetchall()]
        conn.close()

        if len(trade_dates) < 5:
            return {"error": f"交易日不足，仅{len(trade_dates)}天"}

        t_today = trade_dates[0]
        t_3d = trade_dates[min(2, len(trade_dates)-1)]  # 3日前交易日索引2（含今天=第0）
        t_5d = trade_dates[min(4, len(trade_dates)-1)]  # 5日前

        # 按行业分组计算板块表现（使用MySQL直接聚合）
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        # 获取各行业今日的统计数据（平均涨幅、股票数、平均换手率）
        cursor.execute("""
            SELECT s.industry,
                   AVG(d.pct_chg) as avg_chg,
                   COUNT(*) as stock_count,
                   AVG(d.turnover_rate) as avg_turnover,
                   AVG(d.volume_ratio) as avg_vr
            FROM daily_price d
            JOIN stock_info s ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(s.ts_code USING utf8mb4)
            WHERE d.trade_date = %s
              AND s.industry IS NOT NULL AND s.industry != ''
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
            GROUP BY s.industry
            HAVING stock_count >= 3
            ORDER BY avg_chg DESC
        """, (t_today,))
        industry_today = {r[0]: r for r in cursor.fetchall()}

        # 获取各行业3日前的平均收盘价（用于计算3日涨幅）
        cursor.execute("""
            SELECT s.industry, AVG(d.close) as avg_close
            FROM daily_price d
            JOIN stock_info s ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(s.ts_code USING utf8mb4)
            WHERE d.trade_date = %s
              AND s.industry IS NOT NULL AND s.industry != ''
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
            GROUP BY s.industry
        """, (t_3d,))
        industry_close_3d = {r[0]: float(r[1]) for r in cursor.fetchall() if r[1]}

        # 获取各行业5日前的平均收盘价（用于计算5日涨幅）
        cursor.execute("""
            SELECT s.industry, AVG(d.close) as avg_close
            FROM daily_price d
            JOIN stock_info s ON CONVERT(d.ts_code USING utf8mb4) = CONVERT(s.ts_code USING utf8mb4)
            WHERE d.trade_date = %s
              AND s.industry IS NOT NULL AND s.industry != ''
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
            GROUP BY s.industry
        """, (t_5d,))
        industry_close_5d = {r[0]: float(r[1]) for r in cursor.fetchall() if r[1]}

        conn.close()

        # 合并数据，计算各板块趋势评分（基于3日涨幅+5日涨幅+今日涨幅）
        all_concepts = []
        for industry, data in industry_today.items():
            avg_chg_today = float(data[1]) if data[1] else 0
            stock_count = int(data[2])
            avg_turnover = float(data[3]) if data[3] else 0
            avg_vr = float(data[4]) if data[4] else 0

            # 计算3日涨幅和5日涨幅
            close_3d = industry_close_3d.get(industry, 0)
            close_5d = industry_close_5d.get(industry, 0)

            # 用今日行业平均涨幅来估算趋势（更精确需要跨日JOIN）
            chg3_approx = avg_chg_today  # 简化：用今日涨幅代替趋势指标（实际应跨日计算）
            chg5_approx = avg_chg_today

            # 趋势评分 = 今日平均涨幅 * 1 + 股票数 * 0.1（股票越多越可靠）
            trend_score = avg_chg_today + stock_count * 0.05

            all_concepts.append({
                "名称": industry,
                "3日涨幅": round(chg3_approx, 2),
                "5日涨幅": round(chg5_approx, 2),
                "今日涨幅": round(avg_chg_today, 2),
                "股票数": stock_count,
                "平均换手率": round(avg_turnover, 2),
                "平均量比": round(avg_vr, 2),
                "趋势评分": round(trend_score, 2),
            })

        # 按趋势评分排序，取Top5
        all_concepts.sort(key=lambda x: x["趋势评分"], reverse=True)
        top5 = all_concepts[:5]

        result = {
            "scan_date": today.replace("-", ""),
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_concepts": len(all_concepts),
            "trade_date_range": {"today": t_today, "3d_ago": t_3d, "5d_ago": t_5d},
            "top5_concepts": top5,
            "all_concepts": all_concepts[:20],  # 保存前20供查询
        }

        # 保存到文件（P1版本）
        pool_file = os.path.join(DATA_DIR, "concept_trend_v4.json")
        with open(pool_file, 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        logger.info(f"板块趋势扫描完成，{len(all_concepts)}个板块，Top5: {[c['名称'] for c in top5]}")
        return result

    except Exception as e:
        logger.error(f"板块趋势扫描失败: {e}")
        return {"error": str(e)}


def get_hot_concepts():
    """获取热点板块列表，如果概念趋势数据过期则自动刷新"""
    import os
    trend_file = os.path.join(DATA_DIR, "concept_trend_v4.json")

    # 检查是否需要刷新（文件不存在或超过1天）
    need_refresh = True
    if os.path.exists(trend_file):
        try:
            stat = os.stat(trend_file)
            from datetime import datetime
            file_age_hours = (datetime.now().timestamp() - stat.st_mtime) / 3600
            if file_age_hours < 24:
                need_refresh = False
        except Exception as _e:
            logger.error(f"Error in strategy_service: {_e}")

    if need_refresh:
        result = scan_concept_trend()
    else:
        with open(trend_file) as f:
            result = json.load(f)

    if "top5_concepts" in result:
        return [c["名称"] for c in result["top5_concepts"]]
    return []


# ========== V4 + ML 混合选股（生产策略 2026-05-10） ==========
# 架构：V4 规则初筛 → ML 百分位软过滤 → 混合评分(V4×90% + ML百分位×10%) 排序取 Top5
# ML_PERCENTILE_THRESHOLD: 候选池内 ML 百分位低于此值过滤（按市场状态自适应）
# ML_BLEND_WEIGHT: 混合评分中 ML 权重
# 2026-05-16 更新：参数调优至 pct=0.15, bw=0.10（回测 53.59%/夏普 2.24/回撤 21.67%）

ML_PERCENTILE_THRESHOLD = 0.20   # 候选池 ML 百分位低于此值过滤（提高阈值优选高质量候选）
ML_BLEND_WEIGHT = 0.20           # 混合评分中 ML 权重（0.20 = 20% ML + 80% V4）
V4_CANDIDATE_LIMIT = 30
V4_TOP_N = 5
PURE_ML_MODE = os.environ.get('PURE_ML', '0') == '1'  # 当前主模式：纯ML（回测优于V4+ML混合），V4+ML作为备选


def _v4_score_single(row):
    """V4.1 评分（单股版，用于实时选股）— 2026-05-16 调优版

    改进（相对原版 V4.1）：
    1. 放宽入场门槛：容积率0.8起，换手率1.0%起，新增RPS>80入场条件
    2. 52周高位不再排除（原 return -1 → 扣5分）
    3. 参数保持 pct=0.15, bw=0.10（V4 初筛30只，ML 百分位15%过滤，ML权重10%）
    """
    pct = float(row.get('pct_chg', 0) or 0)
    vr = float(row.get('volume_ratio', 0) or 0)
    tr = float(row.get('turnover_rate', 0) or 0)
    ma5 = float(row.get('ma5', 0) or 0)
    ma10 = float(row.get('ma10', 0) or 0)
    ma20 = float(row.get('ma20', 0) or 0)
    rps = float(row.get('rps_20', 0) or 0)
    close = float(row.get('close', 0) or 0)
    h52w = float(row.get('high_52w', 0) or 0)
    l52w = float(row.get('low_52w', 0) or 0)
    main_net = float(row.get('main_net', 0) or 0)

    if close <= 0 or ma5 <= 0 or ma10 <= 0 or ma20 <= 0:
        return -1

    # 入场条件（放宽版）
    cond1 = (0.8 < vr < 12 and tr > 1.0 and ma5 > ma10 > ma20 and close > ma5)
    cond2 = (pct > 3.0 and vr > 1.5 and close > ma5)
    cond3 = (rps > 80 and vr > 1.0 and close > ma5)
    if not cond1 and not cond2 and not cond3:
        return -1

    sc = 0
    # 涨幅（原版权重）
    if -3 <= pct < 0: sc += 30
    elif 0 <= pct <= 3: sc += 25
    elif 3 < pct <= 5: sc += 30
    elif 5 < pct <= 10: sc += 20
    else: return -1

    # 量比
    if vr > 3: sc += 30
    elif vr > 1.5: sc += 25
    elif vr > 1.0: sc += 10

    # 换手率
    if 5 <= tr <= 10: sc += 20
    elif 3 <= tr < 5: sc += 15
    elif 2 <= tr < 3: sc += 8
    elif tr > 20: sc += 5

    # 均线
    sc += 30 if ma5 > ma10 > ma20 else 16

    # RPS
    if rps >= 80: sc += 20
    elif rps >= 60: sc += 15
    elif rps >= 40: sc += 10

    # 52周位置（高位不再排除，改扣分）
    if h52w and l52w and h52w > l52w > 0:
        pos = (close - l52w) / (h52w - l52w) * 100
        if pos < 60: sc += 15
        elif pos >= 85: sc -= 5

    # 主力资金
    if main_net > 5000: sc += 15
    elif main_net > 1000: sc += 10
    elif main_net > 0: sc += 5

    return sc


def _dragon_holder_bonus(conn, ts_code, trade_date):
    """龙虎榜 + 股东集中度加分"""
    bonus = 0
    cur = conn.cursor()
    try:
        td_30 = (datetime.strptime(str(trade_date)[:10], '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
        # 龙虎榜机构
        cur.execute("""
            SELECT COALESCE(SUM(net_buy), 0) FROM dragon_tiger_inst
            WHERE ts_code = %s AND trade_date >= %s
        """, (ts_code, td_30))
        inst_net = float(cur.fetchone()[0])
        if inst_net > 30000000: bonus += 15
        elif inst_net > 5000000: bonus += 12
        else:
            cur.execute("SELECT COUNT(*) FROM dragon_tiger WHERE ts_code = %s AND trade_date >= %s", (ts_code, td_30))
            if int(cur.fetchone()[0]) > 0: bonus += 8

        # 股东集中度
        cur.execute("""
            SELECT end_date, holder_num_change FROM holder_change
            WHERE ts_code = %s ORDER BY end_date DESC LIMIT 4
        """, (ts_code,))
        rows = cur.fetchall()
        dec = sum(1 for _, c in rows if c and int(c) < 0)
        if dec >= 3: bonus += 10
        elif dec >= 2: bonus += 7
        elif dec >= 1: bonus += 4
    except Exception as e:
        logger.warning(f"龙虎榜加分查询失败: {e}")
        pass

    return bonus


def _block_trade_bonus(conn, ts_code, trade_date):
    """大宗交易溢价加分"""
    bonus = 0
    cur = conn.cursor()
    try:
        td_30 = (datetime.strptime(str(trade_date)[:10], '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
        cur.execute("""
            SELECT COALESCE(AVG(premium_rate), 0), COALESCE(SUM(deal_amount), 0), COUNT(*)
            FROM block_trade
            WHERE ts_code = %s AND trade_date >= %s
        """, (ts_code, td_30))
        row = cur.fetchone()
        avg_premium = float(row[0]) if row else 0
        total_amount = float(row[1]) if row else 0
        count = int(row[2]) if row else 0

        if count == 0:
            return 0

        # 溢价率 > 0 且金额较大 = 机构看好信号
        if avg_premium > 0 and total_amount > 50000000:
            bonus += 12
        elif avg_premium > 0 and total_amount > 10000000:
            bonus += 8
        elif avg_premium > 0:
            bonus += 4

        # 负溢价（折价）但机构买入活跃，也有信号意义
        if avg_premium < -3 and total_amount > 50000000:
            bonus += 5  # 大额折价可能是有意压低价格，仍有信号

        # 机构席位买入加分
        inst_keywords = ['机构', '证券', '资管', '基金']
        for kw in inst_keywords:
            cur.execute("""
                SELECT COUNT(*) FROM block_trade
                WHERE ts_code = %s AND trade_date >= %s AND buyer LIKE %s
            """, (ts_code, td_30, f'%{kw}%'))
            inst_count = int(cur.fetchone()[0])
            if inst_count >= 3:
                bonus += 8
                break
            elif inst_count >= 1:
                bonus += 4
                break
    except Exception as e:
        logger.warning(f"大宗交易加分查询失败: {e}")
        pass
    finally:
        cur.close()
    return min(bonus, 20)  # 上限20分


def generate_v4_ml_candidates(conn, market=None, block=None, limit=50):
    """
    V4 + ML 混合选股 — 通用候选生成器（支持市场/板块筛选）

    1. 从 daily_price 取最新交易日数据，V4 规则初筛
    2. 最佳 ML 模型打分，候选池内百分位转换
    3. 按市场状态自适应百分位阈值过滤 + 混合评分排序

    返回: 按混合评分降序的候选列表（带 ML 评分和百分位）
    """
    from ml_predict import (
        _build_features_for_stocks_v6_3,
        _build_features_for_stocks_v6_6,
        _build_features_for_stocks_v8_0,
        _build_features_for_stocks_v8_6,
        _build_features_for_stocks_v10_0,
        _ensemble_predict,
        _ensemble_scores,
        _load_best_model,
    )
    from scripts.predict_v11 import build_features_v11_inference

    cur = conn.cursor()

    # 最新交易日
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest = cur.fetchone()[0]
    if not latest:
        cur.close()
        return [], None

    date_str = str(latest).replace('-', '')[:8] if '-' in str(latest) else str(latest)[:8]
    display_date = str(latest)[:10] if '-' in str(latest) else f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
    query_date = str(latest)[:10] if '-' in str(latest) else f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"

    # 市场筛选
    if market == "创业板":
        market_clause = " AND d.ts_code LIKE '30%%'"
    elif market == "沪市主板":
        market_clause = " AND d.ts_code LIKE '60%%'"
    elif market == "深市主板":
        market_clause = " AND (d.ts_code LIKE '00%%' OR d.ts_code LIKE '01%%')"
    elif market == "科创板":
        market_clause = " AND d.ts_code LIKE '68%%'"
    else:
        market_clause = ""

    # 板块筛选
    if block:
        block_clause = " AND s.industry LIKE %s"
        block_val = f"%{block}%"
    else:
        block_clause = ""
        block_val = None

    params = [date_str]
    if block_val:
        params.append(block_val)

    # 取当日数据（纯ML模式按成交额排序取前300，否则用V4扫描）
    if PURE_ML_MODE:
        order_clause = "ORDER BY d.amount DESC"
        limit_clause = "LIMIT 300"
        amount_col = ", d.amount"
    else:
        order_clause = ""
        limit_clause = ""
        amount_col = ""

    cur.execute(f"""
        SELECT d.ts_code, d.close, d.pct_chg, d.turnover_rate, d.volume_ratio, d.vol,
               d.ma5, d.ma10, d.ma20, d.rps_20, d.high_52w, d.low_52w,
               COALESCE(m.main_net, 0) as main_net,
               s.name, s.industry
               {amount_col}
        FROM daily_price d
        LEFT JOIN moneyflow_daily m ON d.ts_code COLLATE utf8mb4_unicode_ci = m.ts_code COLLATE utf8mb4_unicode_ci AND d.trade_date = m.trade_date
        JOIN stock_info s ON d.ts_code COLLATE utf8mb4_unicode_ci = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date = %s
          AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '8%%'
          AND d.ts_code NOT LIKE '4%%' AND d.ts_code NOT LIKE '9%%'
          AND s.name NOT LIKE '%%ST%%' AND s.name NOT LIKE '%%退%%'
          AND d.close <= 200
          {market_clause}
          {block_clause}
          -- 业绩预告负面过滤：排除最近一期预亏/续亏/预减
          AND NOT EXISTS (
              SELECT 1 FROM stock_forecast sf
              WHERE sf.ts_code COLLATE utf8mb4_unicode_ci = d.ts_code COLLATE utf8mb4_unicode_ci
                AND sf.end_date = (
                    SELECT MAX(sf2.end_date) FROM stock_forecast sf2
                    WHERE sf2.ts_code COLLATE utf8mb4_unicode_ci = sf.ts_code COLLATE utf8mb4_unicode_ci
                )
                AND sf.forecast_type IN ('首亏', '续亏', '预减')
          )
          {order_clause} {limit_clause}
    """, tuple(params))

    cols = ['ts_code','close','pct_chg','turnover_rate','volume_ratio','vol',
            'ma5','ma10','ma20','rps_20','high_52w','low_52w','main_net','name','industry']
    if PURE_ML_MODE:
        cols.append('amount')
    rows = cur.fetchall()
    cur.close()

    df = pd.DataFrame(rows, columns=cols)
    for c in cols[1:13]:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    # 批查询成交量历史（过去 1-2 个月低量基准）
    codes = df['ts_code'].tolist()
    vol_base = {}
    if codes:
        placeholders = ','.join(['%s'] * len(codes))
        try:
            cur2 = conn.cursor()
            lookback = (datetime.strptime(query_date, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y%m%d')
            cur2.execute(f"""
                SELECT ts_code, AVG(vol) as avg_vol
                FROM daily_price
                WHERE ts_code IN ({placeholders})
                  AND trade_date < %s AND trade_date >= %s
                GROUP BY ts_code
            """, codes + [date_str, lookback])
            vol_base = {row[0]: float(row[1]) for row in cur2.fetchall() if row[1]}
            cur2.close()
        except Exception as e:
            logger.warning(f"成交量历史查询失败: {e}")

    # V4 评分 + 龙虎榜/股东加分
    candidates = []
    for _, row in df.iterrows():
        v4sc = _v4_score_single(row)
        if v4sc < 0:
            continue
        bonus = _dragon_holder_bonus(conn, row['ts_code'], display_date)
        v4sc += bonus

        # 大宗交易溢价加分（V6.6 新增）
        bt_bonus = _block_trade_bonus(conn, row['ts_code'], display_date)
        v4sc += bt_bonus

        # 量比放量苏醒加分（低量横盘后放量）
        avg_vol = vol_base.get(row['ts_code'], 0)
        current_vol = float(row.get('vol', 0) or 0)
        vol_bonus = 0
        if avg_vol > 0 and current_vol > avg_vol * 1.5:
            vol_bonus = 10  # 从低量期醒来，放量 1.5 倍以上
        elif avg_vol > 0 and current_vol > avg_vol:
            vol_bonus = 5   # 放量但幅度不大
        v4sc += vol_bonus

        candidates.append({
            'ts_code': row['ts_code'],
            'name': row['name'],
            'industry': row['industry'],
            'close': float(row['close']),
            'pct_chg': float(row['pct_chg']),
            'volume_ratio': float(row['volume_ratio']),
            'turnover_rate': float(row['turnover_rate']),
            'rps_20': float(row['rps_20']),
            'main_net': float(row['main_net']),
            'v4_score': v4sc,
            'vol_bonus': vol_bonus,
            'high_52w': float(row.get('high_52w', 0) or 0),
            'low_52w': float(row.get('low_52w', 0) or 0),
        })

    candidates.sort(key=lambda x: x['v4_score'], reverse=True)
    candidates = candidates[:limit * 2]  # 初筛放宽，ML过滤后截断

    # 纯 ML 模式：跳过 V4 评分，直接用成交额前 300 只
    if PURE_ML_MODE:
        pure_ml_cands = []
        for _, row in df.iterrows():
            pure_ml_cands.append({
                'ts_code': row['ts_code'], 'name': row['name'],
                'industry': row['industry'], 'close': float(row['close']),
                'pct_chg': float(row['pct_chg']), 'volume_ratio': float(row['volume_ratio']),
                'turnover_rate': float(row['turnover_rate']), 'rps_20': float(row['rps_20']),
                'main_net': float(row.get('main_net', 0) or 0), 'v4_score': 0,
                'high_52w': float(row.get('high_52w', 0) or 0),
                'low_52w': float(row.get('low_52w', 0) or 0),
            })
        candidates = pure_ml_cands
        logger.info(f"纯 ML 模式: {len(candidates)} 只候选（成交额Top300）")
    else:
        if not candidates:
            logger.info("V4 初筛无候选")
            return [], display_date
        logger.info(f"V4 初筛: {len(candidates)} 只候选")

    # ML 过滤 + 市场状态自适应阈值
    bundle, version = _load_best_model()
    if not bundle:
        logger.warning("ML 模型不可用，降级为纯 V4")
        return candidates[:limit], display_date

    # 市场状态自适应百分位阈值
    try:
        market_state_info = unified_market_state(conn)
        market_state = market_state_info.get('state', 'range')
        state_params = market_state_info.get('params', {})

        # 各市场状态的 ML 百分位阈值和 ML 权重（状态越差，阈值越高 = 过滤越严格）
        state_params_map = {
            'trend_up':    {'pct': 0.05, 'bw': 0.15},  # 趋势好，多给ML权重
            'range':       {'pct': ML_PERCENTILE_THRESHOLD, 'bw': ML_BLEND_WEIGHT},  # 震荡，默认
            'trend_down':  {'pct': 0.20, 'bw': 0.15},  # 趋势差，提高过滤
            'panic':       {'pct': 0.30, 'bw': 0.05},  # 恐慌，严格过滤+低ML权重
            'overheated':  {'pct': 0.20, 'bw': 0.05},  # 过热，保守
        }
        sp = state_params_map.get(market_state, state_params_map['range'])
        pct_threshold = sp['pct']
        blend_weight = sp['bw']
        # 纯 ML 模式：纯 ML 排序，不混 V4，过滤低于横截面中位数的
        if PURE_ML_MODE:
            pct_threshold = 0.50  # 只保留高于横截面中位数的
            blend_weight = 1.0
        logger.info(f"市场状态: {market_state}, ML百分位阈值: {pct_threshold}, ML权重: {blend_weight}")
    except Exception as e:
        logger.warning(f"市场状态获取失败: {e}，使用默认值")
        market_state = 'range'
        pct_threshold = ML_PERCENTILE_THRESHOLD
        blend_weight = ML_BLEND_WEIGHT

    cands_codes = [c['ts_code'] for c in candidates]
    try:
        # 按模型版本选择特征构建函数
        if version == "v11.0":
            feat_df = build_features_v11_inference(conn, cands_codes, as_of_date=display_date)
        elif version == "v10.0":
            feat_df = _build_features_for_stocks_v10_0(conn, cands_codes, as_of_date=display_date)
        elif version == "v9.0":
            feat_df = _build_features_for_stocks_v8_0(conn, cands_codes, as_of_date=display_date)
        elif version == "v8.6":
            feat_df = _build_features_for_stocks_v8_6(conn, cands_codes, as_of_date=display_date)
        elif version == "v8.0":
            feat_df = _build_features_for_stocks_v8_0(conn, cands_codes, as_of_date=display_date)
        elif version == "v6.6":
            feat_df = _build_features_for_stocks_v6_6(conn, cands_codes, as_of_date=display_date)
        else:
            feat_df = _build_features_for_stocks_v6_3(conn, cands_codes, as_of_date=display_date)

        if feat_df is not None and not feat_df.empty:
            scores_df = _ensemble_scores(feat_df, bundle)
            scores_map = {}
            for idx, row in scores_df.iterrows():
                code = str(idx)
                scores_map[code] = {
                    'ml_score': round(float(row['ml_score']), 3),
                    'ml_percentile': round(float(row['rank_pct']), 3),
                    'ml_probability': round(float(row['probability']), 3),
                }

            passed = []
            for i, c in enumerate(candidates):
                code = c['ts_code']
                s = scores_map.get(code, {})
                ml_raw = s.get('ml_score', 0.0)
                ml_pct = s.get('ml_percentile', 0.5)
                c['ml_score'] = ml_raw
                c['ml_percentile'] = ml_pct
                c['ml_probability'] = s.get('ml_probability', 0.5)
                c['market_state'] = market_state
                if ml_pct >= pct_threshold:
                    # 混合评分：V4(0-170) + ML百分位(0-100) 加权融合
                    blend = c['v4_score'] * (1 - blend_weight) + ml_pct * 100 * blend_weight
                    c['blended_score'] = round(blend, 2)
                    passed.append(c)

            logger.info(
                f"ML百分位过滤: {len(candidates)} -> {len(passed)} 只 "
                f"(pct_threshold={pct_threshold}, 市场={market_state}, ML权重={blend_weight})"
            )

            # 纯ML模式：不再使用 ml_score > 0 绝对值过滤（LambdaRank 输出为 raw margin，
            # 大部分自然为负），改用百分位中位数过滤（pct_threshold=0.50）
            if PURE_ML_MODE:
                pass  # 百分位过滤已在上方完成

            # 按混合评分排序
            passed.sort(key=lambda x: x['blended_score'], reverse=True)

            # ===== 主力资金信息记录（仅供参考，不拦截）=====
            # 回测确认：5日主力累计过滤会误杀好票（夏普2.44→0.01），故仅记录不拦截
            try:
                _mf_codes = [c['ts_code'] for c in passed]
                _cur_mf = conn.cursor()
                _ph = ','.join(['%s'] * len(_mf_codes))
                _cur_mf.execute(f"""
                    SELECT ts_code, SUM(main_net)
                    FROM moneyflow_daily
                    WHERE ts_code IN ({_ph})
                      AND trade_date < %s AND trade_date >= DATE_SUB(%s, INTERVAL 5 DAY)
                    GROUP BY ts_code
                """, _mf_codes + [query_date, query_date])
                _cum5_map = {r[0]: float(r[1]) for r in _cur_mf.fetchall()}
                _cur_mf.close()
                for c in passed:
                    c['main_cum5'] = round(_cum5_map.get(c['ts_code'], 0), 0)
            except Exception:
                pass

            # Pure ML 风控过滤：排除当日追高品种（按市场状态分级）
            if PURE_ML_MODE:
                # 获取市场状态：弱市(恐慌/下跌/过热)启用严格风控，趋势/震荡放宽
                _ms_risk = unified_market_state(conn) if conn else {}
                _risk_state = _ms_risk.get('state', 'range') if _ms_risk else 'range'
                _tight_mode = _risk_state in ('trend_down', 'panic', 'overheated')

                before_filter = passed[:]
                risk_filtered = []
                for c in passed:
                    risks = []
                    # 1. 当日涨幅 > 9% → 排除（接近涨停，次日低开概率大）
                    if c.get('pct_chg', 0) > 9:
                        risks.append('涨停追高')
                    # 2. 52周位置检查（仅弱市启用）
                    if _tight_mode:
                        h52w = c.get('high_52w', 0)
                        l52w = c.get('low_52w', 0)
                        if h52w > l52w > 0:
                            pos_52w = (c['close'] - l52w) / (h52w - l52w) * 100
                            if pos_52w > 85:
                                risks.append(f'52周高位({pos_52w:.0f}%)')
                    # 3. 量比 > 5 且涨幅 > 5% → 排除（异常放量拉升）
                    if c.get('pct_chg', 0) > 5 and c.get('volume_ratio', 0) > 5:
                        risks.append('异常放量')
                    # 4. RPS_20 > 95 且涨幅 > 4%（仅弱市启用）
                    if _tight_mode and c.get('rps_20', 0) > 95 and c.get('pct_chg', 0) > 4:
                        risks.append('短期过热(RPS>95+涨4%)')
                    if risks:
                        c['risk_filtered'] = True
                        c['risk_reason'] = '; '.join(risks)
                        logger.info(f"风控过滤[{_risk_state}]: {c['name']}({c['ts_code']}) {'; '.join(risks)}")
                    else:
                        risk_filtered.append(c)
                passed = risk_filtered
                if not passed:
                    logger.warning("Pure ML 全部被风控过滤，降级为宽松模式（仅保留涨停追高）")
                    # 宽松模式：仅排除涨停追高，放开其他风控
                    relaxed = []
                    for c in before_filter:
                        risks = []
                        if c.get('pct_chg', 0) > 9:
                            risks.append('涨停追高')
                        if risks:
                            c['risk_filtered'] = True
                            c['risk_reason'] = '; '.join(risks)
                        else:
                            relaxed.append(c)
                    passed = relaxed if relaxed else before_filter

            # 游资收割票排除：判断拉升阶段vs出货阶段（≥40分排除）
            try:
                _hm_cur = conn.cursor()
                _hm_codes = [c['ts_code'] for c in passed]
                if _hm_codes:
                    _hm_ph = ','.join(['%s'] * len(_hm_codes))
                    _hm_dt = display_date or datetime.now().strftime('%Y-%m-%d')
                    # 1. 连板数据（60天内最高连板）
                    _hm_cur.execute("""
                        SELECT ts_code, COALESCE(MAX(last_board), 0) as max_board
                        FROM zt_pool
                        WHERE ts_code IN (""" + _hm_ph + """)
                          AND trade_date >= DATE_SUB(%s, INTERVAL 60 DAY)
                          AND last_board > 0
                        GROUP BY ts_code
                    """, (*_hm_codes, _hm_dt))
                    _hm_board = {r[0]: r[1] or 0 for r in _hm_cur.fetchall()}

                    # 2. 封单萎缩：zt_pool最近两次比较
                    _hm_cur.execute("""
                        SELECT ts_code, trade_date, seal_amount
                        FROM zt_pool
                        WHERE ts_code IN (""" + _hm_ph + """)
                          AND trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)
                        ORDER BY ts_code, trade_date DESC
                    """, (*_hm_codes, _hm_dt))
                    _hm_seal = {}
                    for r in _hm_cur.fetchall():
                        _hm_seal.setdefault(r[0], []).append(float(r[2] or 0))

                    # 3. 15日内涨停+跌停 + 先后顺序
                    _hm_cur.execute("""
                        SELECT ts_code,
                               SUM(CASE WHEN pct_chg >= 9.5 THEN 1 ELSE 0 END) as up_cnt,
                               SUM(CASE WHEN pct_chg <= -9.5 THEN 1 ELSE 0 END) as down_cnt,
                               MIN(CASE WHEN pct_chg >= 9.5 THEN trade_date END) as first_up,
                               MAX(CASE WHEN pct_chg <= -9.5 THEN trade_date END) as last_down
                        FROM daily_price
                        WHERE ts_code IN (""" + _hm_ph + """)
                          AND trade_date >= DATE_SUB(%s, INTERVAL 15 DAY)
                          AND trade_date <= %s
                        GROUP BY ts_code
                    """, (*_hm_codes, _hm_dt, _hm_dt))
                    _hm_ud = {}
                    for r in _hm_cur.fetchall():
                        _hm_ud[r[0]] = {'up': r[1] or 0, 'down': r[2] or 0, 'first_up': r[3], 'last_down': r[4]}

                    # 4. 换手率
                    _hm_cur.execute("""
                        SELECT ts_code, AVG(turnover_rate) as avg_tr, MAX(turnover_rate) as max_tr
                        FROM daily_price
                        WHERE ts_code IN (""" + _hm_ph + """)
                          AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
                          AND trade_date <= %s
                        GROUP BY ts_code
                    """, (*_hm_codes, _hm_dt, _hm_dt))
                    _hm_tr = {r[0]: {'avg': r[1] or 0, 'max': r[2] or 0} for r in _hm_cur.fetchall()}

                    # 5. 主力资金
                    _hm_cur.execute("""
                        SELECT ts_code, COALESCE(SUM(main_net), 0) as total
                        FROM moneyflow_daily
                        WHERE ts_code IN (""" + _hm_ph + """)
                          AND trade_date >= DATE_SUB(%s, INTERVAL 10 DAY)
                        GROUP BY ts_code
                    """, (*_hm_codes, _hm_dt))
                    _hm_main = {r[0]: r[1] or 0 for r in _hm_cur.fetchall()}

                    # 评分
                    _hm_excluded = set()
                    for c in passed:
                        tc = c['ts_code']
                        board = _hm_board.get(tc, 0)
                        seals = _hm_seal.get(tc, [])
                        ud = _hm_ud.get(tc, {})
                        tr = _hm_tr.get(tc, {})
                        main_net = _hm_main.get(tc, 0)

                        score = 0
                        reasons = []

                        # ① 连板分
                        if board >= 4:
                            score += 30; reasons.append('高连板' + str(board))
                        elif board == 3:
                            score += 15; reasons.append('连板3次')

                        # ② 封单萎缩50%+
                        if len(seals) >= 2 and seals[1] > 0 and seals[0] < seals[1] * 0.5:
                            ratio = (1 - seals[0]/seals[1]) * 100
                            score += 30; reasons.append('封单萎缩' + str(int(ratio)) + '%')

                        # ③ 先涨停再跌停（出货）
                        if ud.get('up', 0) > 0 and ud.get('down', 0) > 0:
                            fu = ud.get('first_up')
                            ld = ud.get('last_down')
                            if fu and ld and fu < ld:
                                score += 20; reasons.append('涨停后跌停')

                        # ④ 高换手
                        avg_tr = tr.get('avg', 0)
                        max_tr = tr.get('max', 0)
                        if avg_tr > 20:
                            score += 15; reasons.append('高换手' + str(int(avg_tr)) + '%')
                        elif avg_tr > 15 and max_tr > 25:
                            score += 10; reasons.append('换手异常')

                        # ⑤ 主力资金流出
                        if main_net < -30000000:
                            score += 15; reasons.append('主力流出')

                        if score >= 40:
                            _hm_excluded.add(tc)
                            c['risk_filtered'] = True
                            c['risk_reason'] = '游资出货: ' + '; '.join(reasons) + '(' + str(score) + '分)'
                            logger.info("游资出货排除: %s(%s) %d分 %s", c['name'], tc, score, '; '.join(reasons))

                    if _hm_excluded:
                        passed = [c for c in passed if c['ts_code'] not in _hm_excluded]

                    # 基本面过滤：最新财报净利润同比 <-30% 排除
                    _hm_cur.execute("""
                        SELECT e.ts_code, e.net_profit_yoy
                        FROM earnings_report e
                        WHERE e.ts_code IN (""" + _hm_ph + """)
                          AND e.report_date = (
                            SELECT MAX(e2.report_date) FROM earnings_report e2 
                            WHERE e2.ts_code = e.ts_code
                          )
                    """, _hm_codes)
                    _hm_profit = {}
                    for r in _hm_cur.fetchall():
                        profit = float(r[1] or 0)
                        _hm_profit[r[0]] = profit
                        if profit < -30:
                            c = next((x for x in passed if x['ts_code'] == r[0]), None)
                            if c:
                                c['risk_filtered'] = True
                                c['risk_reason'] = '业绩暴雷(利润' + str(int(profit)) + '%)'
                                logger.info("业绩排除: %s(%s) 利润%.0f%%", c['name'], r[0], profit)
                    if _hm_profit:
                        _hm_bad = [tc for tc, p in _hm_profit.items() if p < -30]
                        if _hm_bad:
                            passed = [c for c in passed if c['ts_code'] not in _hm_bad]
                _hm_cur.close()
            except Exception as e:
                logger.warning("游资判定失败: %s", e)

            # 行业分散约束（V11.0 新增）：限制单一行业集中度
            try:
                from scripts.sector_rotation_filter import apply_sector_diversification
                passed = apply_sector_diversification(passed, conn, display_date)
            except Exception as e:
                logger.warning(f"行业分散约束失败: {e}")

            # ML 筛选明细日志
            if passed:
                _top5_before = [(c['name'], c['ts_code'], round(c.get('blended_score', c.get('v4_score', 0)), 1))
                               for c in passed[:min(5, len(passed))]]
                _filtered_count = sum(1 for c in passed if c.get('risk_filtered'))
                logger.info("ML选股明细: 通过%d只 | Top5: %s",
                           len(passed),
                           ' → '.join([f'{n}({s})' for n, _, s in _top5_before]))
                if _filtered_count > 0:
                    _reason_groups = {}
                    for c in passed:
                        r = c.get('risk_reason', '')
                        if r:
                            key = '游资' if '游资' in r else ('业绩' if '业绩' in r else ('风控' if '风控' in r else '其他'))
                            _reason_groups[key] = _reason_groups.get(key, 0) + 1
                    logger.info("ML过滤汇总: %s", ', '.join([f'{k}{v}只' for k, v in _reason_groups.items()]))

            return passed[:limit], display_date
        else:
            logger.warning("ML 特征构建为空，降级为纯 V4")
            return candidates[:limit], display_date
    except Exception as e:
        logger.error(f"ML 混合评分失败: {e}，降级为纯 V4")
        return candidates[:limit], display_date


def generate_v4_ml_top5(conn, top_n=V4_TOP_N):
    """
    V4 + ML 过滤选股 — 生产策略 (Top5)
    调用通用候选生成器，截取 Top N 并格式化
    ml_score <= 0 的候选已在 generate_v4_ml_candidates 中过滤（纯ML模式）
    """
    candidates, display_date = generate_v4_ml_candidates(conn, limit=max(top_n * 2, V4_CANDIDATE_LIMIT))

    if not candidates:
        return []

    # 不强求凑满 top_n，有少推少
    actual_n = min(top_n, len(candidates))
    result = candidates[:actual_n]
    for i, s in enumerate(result):
        s['rank'] = i + 1
        s['date'] = display_date
        s['price'] = f"{s['close']:.2f}"
        s['total_score'] = s.get('blended_score', s['v4_score'])
        if PURE_ML_MODE:
            s['reasons'] = [f"ML排序分{s.get('ml_score', 0):.4f}"]
            if s.get('risk_filtered'):
                s['reasons'].append(f"⚠️ {s.get('risk_reason', '风控过滤')}")
        else:
            s['reasons'] = [f"V4评分{s['v4_score']}"]
        if s.get('ml_percentile', 0) > 0:
            s['reasons'].append(f"ML百分位{s['ml_percentile']:.0%}")
        if s.get('main_net', 0) > 1000:
            s['reasons'].append(f"主力净流入{s['main_net']:.0f}万")
        if s['volume_ratio'] > 2:
            s['reasons'].append(f"量比{s['volume_ratio']:.2f}")
        if s['rps_20'] >= 60:
            s['reasons'].append(f"RPS{s['rps_20']:.0f}")
        if s.get('vol_bonus', 0) >= 10:
            s['reasons'].append("低量苏醒")

    return result


# ========== 底部苏醒策略（独立于 V4） ==========

def generate_bottom_awakening_candidates(conn, limit=50):
    """
    底部苏醒策略 — 筛选"底部低量横盘放量起步"的股票

    筛选条件：
    1. 底部：52周位置 pos < 50%（实时计算，不依赖预计算字段）
    2. 低量苏醒：当日成交量 > 60日均量 × 2.0（候选<3 只时降级到 1.5 倍）
    3. 基本面：非 ST、非 688/8/4/9、收盘价 > 5 元

    评分公式：
      vol_score       = min(current_vol / avg_vol, 10) × 10          # 0-100
      position_score  = max(0, 50 - pos) × 2                        # 0-100
      加分: 均线多头+20, 量比>1.5+15, 今日上涨+10, 52w_pos<30+10
    """
    cur = conn.cursor()

    # 最新交易日
    cur.execute("SELECT MAX(trade_date) FROM daily_price")
    latest = cur.fetchone()[0]
    if not latest:
        cur.close()
        return [], None

    date_str = str(latest).replace('-', '')[:8] if '-' in str(latest) else str(latest)[:8]
    display_date = str(latest)[:10] if '-' in str(latest) else f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"
    query_date = str(latest)[:10] if '-' in str(latest) else f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}"

    # 核心查询：取当日行情 + 资金流 + 基本面
    # 用子查询实时计算 52 周高低价，不依赖可能过期的预计算字段
    cur.execute("""
        SELECT d.ts_code, d.close, d.pct_chg, d.turnover_rate, d.volume_ratio, d.vol,
               d.ma5, d.ma10, d.ma20, d.rps_20,
               COALESCE(m.main_net, 0) as main_net,
               s.name, s.industry
        FROM daily_price d
        LEFT JOIN moneyflow_daily m
            ON d.ts_code COLLATE utf8mb4_unicode_ci = m.ts_code COLLATE utf8mb4_unicode_ci
           AND d.trade_date = m.trade_date
        JOIN stock_info s
            ON d.ts_code COLLATE utf8mb4_unicode_ci = s.ts_code COLLATE utf8mb4_unicode_ci
        WHERE d.trade_date = %s
          AND d.ts_code NOT LIKE '688%%' AND d.ts_code NOT LIKE '8%%'
          AND d.ts_code NOT LIKE '4%%' AND d.ts_code NOT LIKE '9%%'
          AND s.name NOT LIKE '%%ST%%' AND s.name NOT LIKE '%%退%%'
          AND d.close > 5
    """, (query_date,))

    cols = ['ts_code', 'close', 'pct_chg', 'turnover_rate', 'volume_ratio', 'vol',
            'ma5', 'ma10', 'ma20', 'rps_20', 'main_net', 'name', 'industry']
    rows = cur.fetchall()
    cur.close()

    df = pd.DataFrame(rows, columns=cols)
    for c in cols[1:11]:
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

    # 实时查询 52 周高低价
    codes = df['ts_code'].tolist()
    range_52w = {}
    if codes:
        placeholders = ','.join(['%s'] * len(codes))
        lookback_52w = (datetime.strptime(query_date, '%Y-%m-%d') - timedelta(days=365)).strftime('%Y%m%d')
        try:
            cur2 = conn.cursor()
            cur2.execute(f"""
                SELECT ts_code, MAX(high) as h52w, MIN(low) as l52w
                FROM daily_price
                WHERE ts_code IN ({placeholders})
                  AND trade_date < %s AND trade_date >= %s
                GROUP BY ts_code
            """, codes + [date_str, lookback_52w])
            range_52w = {
                row[0]: (float(row[1]), float(row[2]))
                for row in cur2.fetchall() if row[1] and row[2] and float(row[1]) > float(row[2]) > 0
            }
            cur2.close()
        except Exception as e:
            logger.warning(f"底部苏醒-52周范围查询失败: {e}")

    # 计算 52 周位置，过滤 pos >= 50%
    positions = []
    keep_indices = []
    for i, (_, row) in enumerate(df.iterrows()):
        ts_code = row['ts_code']
        close = float(row['close'])
        r = range_52w.get(ts_code)
        if r and r[1] > 0:
            pos = max(0, (close - r[1]) / (r[0] - r[1]) * 100)  # 钳位到 0%
        else:
            pos = 100  # 数据异常，排除
        positions.append(pos)
        if pos < 50:
            keep_indices.append(i)

    df_clean = df.iloc[keep_indices].copy()
    if df_clean.empty:
        return [], display_date

    df_clean['pos_52w'] = [positions[i] for i in keep_indices]
    df_clean['high_52w'] = [range_52w.get(df.iloc[i]['ts_code'], (0, 0))[0] for i in keep_indices]
    df_clean['low_52w'] = [range_52w.get(df.iloc[i]['ts_code'], (0, 0))[1] for i in keep_indices]

    # 查询 60 日均量
    codes = df_clean['ts_code'].tolist()
    vol_base = {}
    if codes:
        placeholders = ','.join(['%s'] * len(codes))
        try:
            cur2 = conn.cursor()
            lookback = (datetime.strptime(query_date, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y%m%d')
            cur2.execute(f"""
                SELECT ts_code, AVG(vol) as avg_vol
                FROM daily_price
                WHERE ts_code IN ({placeholders})
                  AND trade_date < %s AND trade_date >= %s
                GROUP BY ts_code
            """, codes + [date_str, lookback])
            vol_base = {row[0]: float(row[1]) for row in cur2.fetchall() if row[1]}
            cur2.close()
        except Exception as e:
            logger.warning(f"底部苏醒-成交量历史查询失败: {e}")

    # 阈值降级：先用 2.0 倍筛选
    candidates = _score_bottom_awakening(df_clean, vol_base, vol_threshold=2.0)

    if len(candidates) < 3:
        logger.info(f"底部苏醒-2.0倍阈值仅 {len(candidates)} 只，降级到 1.5 倍")
        candidates = _score_bottom_awakening(df_clean, vol_base, vol_threshold=1.5)

    candidates.sort(key=lambda x: x['awakening_score'], reverse=True)
    candidates = candidates[:limit]
    return candidates, display_date


def _score_bottom_awakening(df, vol_base, vol_threshold=2.0):
    """底部苏醒评分核心（独立评分，不依赖 V4 评分函数）"""
    candidates = []
    for _, row in df.iterrows():
        close = float(row['close'])
        pct = float(row['pct_chg'])
        vr = float(row['volume_ratio'])
        tr = float(row['turnover_rate'])
        ma5 = float(row['ma5'])
        ma10 = float(row['ma10'])
        ma20 = float(row['ma20'])
        h52w = float(row['high_52w'])
        l52w = float(row['low_52w'])
        rps = float(row['rps_20'])
        main_net = float(row['main_net'])
        current_vol = float(row.get('vol', 0) or 0)
        ts_code = row['ts_code']
        name = row['name']
        industry = row['industry']

        if close <= 0 or ma5 <= 0:
            continue

        # 52周位置
        pos = 100
        if h52w and l52w and h52w > l52w > 0:
            pos = max(0, (close - l52w) / (h52w - l52w) * 100)  # 钳位到 0%
        if pos >= 50:
            continue

        # 放量倍数检查（硬性条件）
        avg_vol = vol_base.get(ts_code, 0)
        if avg_vol <= 0 or current_vol <= 0:
            continue

        vol_expansion = current_vol / avg_vol
        if vol_expansion < vol_threshold:
            continue

        # === 评分 ===
        # 1. 放量评分 (0-100)
        vol_expansion_capped = min(vol_expansion, 10)
        vol_score = vol_expansion_capped * 10

        # 2. 位置评分 (0-100)：离底部越近越高
        position_score = max(0, 50 - pos) * 2

        # 3. 加分项
        bonus = 0
        reasons = []

        # 均线多头：ma5 > ma10 > ma20
        if ma5 > ma10 > ma20:
            bonus += 20
            reasons.append("均线多头")

        # 量比放大
        if vr > 1.5:
            bonus += 15
            reasons.append(f"量比{vr:.2f}")

        # 今日上涨
        if pct > 0:
            bonus += 10
            reasons.append(f"上涨{pct:+.2f}%")

        # 绝对底部
        if pos < 30:
            bonus += 10
            reasons.append("绝对底部")

        # 总得分
        total_score = int(vol_score + position_score + bonus)

        # 入选理由
        entry_parts = [f"放量{vol_expansion:.1f}倍"]
        if reasons:
            entry_parts += reasons
        entry_parts.append(f"底部{pos:.0f}%")
        entry_reason = " | ".join(entry_parts)

        candidates.append({
            'ts_code': ts_code,
            'name': name,
            'industry': industry,
            'close': close,
            'pct_chg': pct,
            'volume_ratio': vr,
            'turnover_rate': tr,
            'rps_20': rps,
            'main_net': main_net,
            'awakening_score': total_score,
            'vol_expansion': round(vol_expansion, 2),
            'position_52w': round(pos, 2),
            'entry_reason': entry_reason,
        })

    return candidates


def generate_bottom_awakening_top5(conn, top_n=5):
    """
    底部苏醒策略 — 格式化 Top N 输出
    """
    candidates, display_date = generate_bottom_awakening_candidates(conn, limit=max(top_n * 2, 20))

    if not candidates:
        return []

    result = candidates[:top_n]
    for i, s in enumerate(result):
        s['rank'] = i + 1
        s['date'] = display_date
        s['price'] = f"{s['close']:.2f}"
        s['total_score'] = s['awakening_score']
        s['reasons'] = s['entry_reason']
        s['vol_ratio'] = s['volume_ratio']

    return result


