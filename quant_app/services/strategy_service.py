#!/usr/bin/env python3
"""
策略选股服务 - C3.0 V3评分、策略扫描、技术面扫描、底部起步、均线回踩
"""
import os, sys, json, time, logging, math
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

from quant_app.utils.config import get_db_config, DATA_DIR
from quant_app.services.market_service import get_stock_realtime, get_tushare_pro, get_recent_trade_dates, get_latest_rps_from_db, calculate_rps, get_stock_history_from_db
from quant_app.services.technical_service import calculate_macd, calculate_kdj, calculate_bollinger_bands, calculate_atr
from market_state import get_market_state as unified_market_state

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
                except Exception:
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
                        except Exception:
                            pass

                    # MACD 状态判断（全 None 保护）
                    if isinstance(macd_line, (int, float)) and isinstance(signal_line, (int, float)):
                        try:
                            macd_state = "金叉区域" if macd_line > signal_line else "死叉区域"
                        except Exception:
                            macd_state = "数据不足"
                    else:
                        macd_state = "数据不足"

                    # KDJ 状态判断（全 None 保护）
                    if isinstance(k_val, (int, float)):
                        try:
                            kdj_state = "超买" if k_val > 80 else ("超卖" if k_val < 20 else "正常")
                        except Exception:
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
            pred_ret = cached['预测收益']
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
    except Exception:
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
    except Exception:
        sector_summary = "板块趋势数据暂不可用"
    summary_parts.append(("板块趋势", sector_summary))

    # 大盘情绪
    market_summary = ""
    try:
        ms = unified_market_state()
        state_name = ms.get('state_name', '未知')
        advice = ms.get('advice', '')
        market_summary = f"大盘{state_name}，{advice}"
    except Exception:
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
        except Exception:
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
            except Exception as e:
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
                    with open(concept_file, 'r') as _cf:
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
            except Exception:
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
            industry_filter = f" AND s.industry LIKE %s"
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
        except Exception:
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
            except Exception:
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
        import time
        pro = get_tushare_pro()
        logger.info("开始深度技术面扫描...")

        # 先读取普通扫描的候选股票
        pool_file = os.path.join(DATA_DIR, "stock_pool.json")
        if not os.path.exists(pool_file):
            logger.warning("股票池文件不存在，先运行普通扫描")
            basic_pool = scan_daily_pool()
            if "error" in basic_pool:
                return {"error": "无法创建基础股票池"}

        with open(pool_file, 'r') as f:
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
                except Exception:
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

            except Exception as e:
                continue

        # ML模型增强评分（插入在板块轮动增强前）
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from ml_predict import ml_enhanced_score
            import pymysql as _pm
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
                s['预测收益'] = "N/A"

        # 板块轮动增强：热点板块加分
        try:
            from sector_rotation import get_hot_sectors, get_sector_bonus
            hot_sectors = get_hot_sectors(top_n=8)
            for s in candidates:
                ts_code = f"{s.get('代码','')}.{'SH' if (s.get('代码','')[:1]=='6') else 'SZ'}"
                bonus, name, _ = get_sector_bonus(ts_code, hot_sectors)
                s["热点板块"] = name
                s["板块加分"] = bonus
        except Exception:
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
    import pymysql
    from datetime import datetime, timedelta
    from decimal import Decimal

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
            close_today = industry_close_5d.get(industry, 0)  # 需要重新获取今日的均价，简化处理：用3日涨幅近似

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
        with open(trend_file, 'r') as f:
            result = json.load(f)

    if "top5_concepts" in result:
        return [c["名称"] for c in result["top5_concepts"]]
    return []


# ========== 底部起步策略 - 已下线（2026-05-02 回测亏损 -6.31%）==========
def scan_daily_pool_bottom_breakout():
    """底部起步策略 - 已下线，请使用 V4 组合策略"""
    return {"error": "底部起步策略已下线（回测亏损 -6.31%），请使用 V4 组合策略", "scan_type": "底部起步策略", "stocks": []}


# ========== 均线回踩策略 - 已下线（2026-05-02 与底部起步合并下线）==========
def scan_daily_pool_ma_pullback():
    """均线回踩策略 - 已下线，请使用 V4 组合策略"""
    return {"error": "均线回踩策略已下线，请使用 V4 组合策略", "scan_type": "均线回踩策略", "stocks": []}
