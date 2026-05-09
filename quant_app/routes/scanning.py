# -*- coding: utf-8 -*-
"""
策略选股 API 路由 — 扫描 / 筛选 / ML预测
"""
import os, json, time, logging, sys, math
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Cookie, Request as FastAPIRequest, HTTPException
from fastapi.responses import JSONResponse
from app_core import (
    strategy_scan, get_block_stocks, scan_daily_pool,
    scan_daily_pool_bottom_breakout, scan_daily_pool_ma_pullback,
    scan_daily_pool_technical,
    get_stock_realtime, get_tushare_pro, get_recent_trade_dates,
    analyze_stock, ALL_BLOCKS,
    get_current_user, save_access_log, get_client_ip,
)
from quant_app.utils.authz import require_admin, is_admin

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["scanning"])


# ========== 策略选股 ==========

@router.get("/api/scan")
async def scan(request: FastAPIRequest, block: str = "", market: str = "", token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if not block and not market:
        return {"error": "请选择板块或市场"}
    save_access_log(user, get_client_ip(request), f"策略选股扫描 {block or market}")
    result = strategy_scan(block if block else "", market if market else None)
    return result


@router.get("/api/scan_pool")
async def scan_pool(request: FastAPIRequest, token: str = Cookie(None)):
    """触发每日股票池扫描（17:00自动运行）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "触发股票池扫描")
    result = scan_daily_pool()
    return result


@router.get("/api/scan_bottom")
async def scan_bottom(request: FastAPIRequest, token: str = Cookie(None)):
    """底部起步策略已下线（回测亏损，详见CLAUDE.md）"""
    return {"error": "底部起步策略已下线，请使用V4组合策略", "scan_type": "底部起步策略", "stocks": []}


@router.get("/api/scan/strong")
async def scan_strong(request: FastAPIRequest, block: str = "", market: str = "", token: str = Cookie(None)):
    """获取强势活跃策略股票池（从MySQL stock_pool_snap表读取）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "查看强势活跃股票池")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        # 最新扫描日期
        cursor.execute("SELECT MAX(snap_date) FROM quant_db.stock_pool_snap")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            return {"stocks": [], "scan_type": "强势活跃策略", "error": "股票池为空，请先执行扫描"}

        # 市场过滤
        market_filter = ""
        market_pattern = None
        if market == "创业板":
            market_filter = " AND ts_code LIKE %s"
            market_pattern = "30%"
        elif market == "沪市主板":
            market_filter = " AND ts_code LIKE %s"
            market_pattern = "60%"
        elif market == "深市主板":
            market_filter = " AND (ts_code LIKE %s OR ts_code LIKE %s)"
            market_pattern = ("00%", "01%")
        elif market == "科创板":
            market_filter = " AND ts_code LIKE %s"
            market_pattern = "68%"

        # 板块过滤
        block_filter = ""
        block_pattern = None
        if block and block not in ['沪市主板', '深市主板', '创业板', '科创板']:
            block_filter = " AND industry LIKE %s"
            block_pattern = f"%{block}%"

        # 构建参数列表
        params = [latest_date]
        if isinstance(market_pattern, tuple):
            params.extend(market_pattern)
        elif market_pattern:
            params.append(market_pattern)
        if block_pattern:
            params.append(block_pattern)

        sql = f"""
            SELECT snap_date, ts_code, name, industry, price, change_pct,
                   turnover_rate, vol_ratio, quick_score, entry_reason, today_rank
            FROM quant_db.stock_pool_snap
            WHERE snap_date = %s{market_filter}{block_filter}
            ORDER BY quick_score DESC, today_rank ASC
            LIMIT 100
        """
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        stocks = []
        for r in rows:
            code = r[1].split(".")[0]
            mkt = "sz" if r[1].endswith(".SZ") else "sh"
            code_full = f"{mkt.upper()}{code}"
            pct = float(r[5]) if r[5] else 0
            tr = float(r[6]) if r[6] else 0
            vr = float(r[7]) if r[7] else 0
            score = float(r[8]) if r[8] else 0

            stocks.append({
                "代码": code_full,
                "交易所": mkt,
                "名称": r[2] or "",
                "行业": r[3] or "",
                "现价": float(r[4]) if r[4] else 0,
                "涨跌幅": f"{pct:+.2f}%",
                "换手率": f"{tr:.2f}%",
                "量比": f"{vr:.2f}",
                "综合评分": round(score, 0),
                "入选理由": r[9] or "",
                "今日排名": r[10] or 0,
            })

        # ML增强评分
        try:
            from ml_predict import ml_enhanced_score
            conn2 = pymysql.connect(**db_config)
            stocks = ml_enhanced_score(stocks, db_conn=conn2)
            conn2.close()
        except Exception as e:
            logger.info(f"ML增强不可用: {e}")
            for s in stocks:
                s['ml概率'] = 0.5
                s['增强评分'] = s.get('综合评分', 0)
                s['市场状态'] = ''
                s['热点板块'] = ''
                s['资金趋势'] = ''

        # 获取板块趋势信息
        top5_concepts = []
        concept_file = os.path.join(DATA_DIR, "concept_trend_v4.json")
        try:
            if os.path.exists(concept_file):
                with open(concept_file, 'r') as _cf:
                    concept_data = json.load(_cf)
                if "top5_concepts" in concept_data:
                    top5_concepts = concept_data["top5_concepts"]
        except Exception as e:
            logger.error(f"读取概念趋势失败: {e}")

        # 大盘情绪
        try:
            from market_state import get_market_state
            ms_strong = get_market_state()
            ms_name = ms_strong.get('state_name', '')
            ms_advice = ms_strong.get('advice', '')
            market_stance = f"{ms_name}，{ms_advice}" if ms_name else "震荡整理"
        except Exception:
            market_stance = "震荡整理"

        return {
            "scan_date": latest_date.strftime("%Y%m%d") if hasattr(latest_date, 'strftime') else str(latest_date),
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(stocks),
            "stocks": stocks,
            "scan_type": "强势活跃策略",
            "top5_concepts": top5_concepts,
            "market_stance": market_stance,
        }
    except Exception as e:
        return {"stocks": [], "scan_type": "强势活跃策略", "error": str(e)}


@router.get("/api/scan/aimodel")
async def scan_aimodel(request: FastAPIRequest, block: str = "", market: str = "", token: str = Cookie(None)):
    """AI模型选股：V3 LightGBM横截面排序模型"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"AI模型选股 {block or market or '全市场'}")

    try:
        import pymysql
        import numpy as np
        import pandas as pd
        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)

        # ML模型选股 — 自动选择最佳模型(V6.2 > V6)
        from ml_predict import _load_best_model, _build_features_for_stocks_v6_2, _build_features_for_stocks_v6

        bundle, version = _load_best_model()
        if bundle is None:
            conn.close()
            return {"stocks": [], "error": "ML模型不可用"}

        # 获取全市场 ts_code 列表
        cur = conn.cursor()
        cur.execute("SELECT ts_code FROM stock_info WHERE ts_code NOT LIKE '688%%' AND ts_code NOT LIKE '8%%' AND ts_code NOT LIKE '4%%' AND ts_code NOT LIKE '9%%'")
        all_codes = [r[0] for r in cur.fetchall()]
        cur.close()

        # 根据版本选择特征构建器和推理方式
        if version == "v6.2":
            features = _build_features_for_stocks_v6_2(conn, all_codes)
        else:
            features = _build_features_for_stocks_v6(conn, all_codes)
        if features.empty:
            conn.close()
            return {"stocks": [], "error": "特征构建失败"}

        latest_date = features['trade_date'].max() if 'trade_date' in features.columns else None

        feature_cols = bundle['feature_cols']
        medians = bundle.get('global_medians', {})
        for col in feature_cols:
            if col not in features.columns:
                features[col] = medians.get(col, 0.0)
            elif features[col].isna().any():
                features[col] = features[col].fillna(medians.get(col, 0.0))

        X = features[feature_cols].values.astype(np.float32)

        if version == "v6.2" and 'models' in bundle:
            # 集成预测：所有子模型取均值
            preds = np.zeros((len(X), len(bundle['models'])))
            for i, model in enumerate(bundle['models']):
                preds[:, i] = model.predict(X)
            pred_returns = np.mean(preds, axis=1)
        else:
            pred_returns = bundle['model'].predict(X)
        features['ml_score'] = pred_returns
        features['rank_pct'] = (pred_returns.argsort().argsort() + 1) / len(pred_returns) * 100

        # 按ML得分排序
        features = features.sort_values('ml_score', ascending=False)

        # 先关联stock_info获取industry（用于板块过滤）
        cursor = conn.cursor()
        cursor.execute("SELECT ts_code, industry FROM stock_info")
        industry_map = {r[0]: r[1] for r in cursor.fetchall()}
        features['industry'] = features['ts_code'].map(industry_map).fillna('')

        # 市场过滤
        if market == "创业板":
            features = features[features['ts_code'].str.startswith('30')]
        elif market == "沪市主板":
            features = features[features['ts_code'].str.startswith('60')]
        elif market == "深市主板":
            features = features[features['ts_code'].str.startswith(('00', '01'))]
        elif market == "科创板":
            features = features[features['ts_code'].str.startswith('68')]
        elif block and block not in ['沪市主板', '深市主板', '创业板', '科创板', '全市场']:
            features = features[features['industry'].astype(str).str.contains(block, na=False)]

        # 取Top 50
        top_stocks = features.head(50).copy()

        # 获取最新价格数据 + 股票名称
        latest_str = latest_date.strftime('%Y%m%d') if hasattr(latest_date, 'strftime') else str(latest_date).replace('-', '')
        cursor = conn.cursor()

        top_codes = list(top_stocks['ts_code'])
        placeholders = ','.join(['%s'] * len(top_codes))

        cursor.execute(f"""
            SELECT ts_code, name, industry FROM stock_info WHERE ts_code IN ({placeholders})
        """, top_codes)
        name_map = {}
        for row in cursor.fetchall():
            name_map[row[0]] = {'name': row[1], 'industry': row[2]}

        cursor.execute("""
            SELECT ts_code, close, pct_chg, turnover_rate, volume_ratio
            FROM daily_price WHERE trade_date = %s
        """, (latest_str,))
        price_map = {}
        for row in cursor.fetchall():
            price_map[row[0]] = {
                'close': row[1], 'pct_chg': row[2],
                'turnover_rate': row[3], 'volume_ratio': row[4]
            }
        cursor.close()

        stocks = []
        for _, row in top_stocks.iterrows():
            code = row['ts_code']
            code_short = code.split('.')[0]
            info = name_map.get(code, {})
            p = price_map.get(code, {})

            rank_pct = row['rank_pct']
            if rank_pct >= 99:
                reason = "⭐ AI强推荐"
            elif rank_pct >= 95:
                reason = "🔥 AI推荐"
            else:
                reason = "✅ AI关注"

            stocks.append({
                "code": code_short,
                "name": info.get('name', code_short),
                "industry": info.get('industry', ''),
                "price": f"{p.get('close', 0):.2f}" if p.get('close') else '--',
                "change_pct": f"{p.get('pct_chg', 0):+.2f}%" if p.get('pct_chg') is not None else '--',
                "turnover_rate": f"{p.get('turnover_rate', 0):.1f}%" if p.get('turnover_rate') else '--',
                "vol_ratio": f"{p.get('volume_ratio', 0):.2f}" if p.get('volume_ratio') else '--',
                "ml_score": float(row['ml_score']),
                "rank_pct": float(rank_pct),
                "reason": reason,
            })

        conn.close()

        return {
            "stocks": stocks,
            "model": "V3 LightGBM",
            "auc": "0.596",
            "scan_date": latest_str,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_scored": len(features),
            "total_returned": len(stocks),
        }
    except Exception as e:
        import traceback
        logger.error(f"AI模型选股失败: {e}\n{traceback.format_exc()}")
        try: conn.close()
        except Exception: pass
        return {"stocks": [], "error": f"扫描失败: {str(e)}"}


# ========== AI每日精选 TOP5 ==========

@router.get("/api/scan/top5")
async def scan_top5_daily(request: FastAPIRequest, token: str = Cookie(None)):
    """AI每日精选TOP5：V4规则初筛 + ML V6.5负向过滤"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "AI每日精选TOP5")

    try:
        import json
        from quant_app.services.strategy_service import generate_v4_ml_top5
        import pymysql
        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        top5 = generate_v4_ml_top5(conn)
        conn.close()

        return {
            "stocks": top5,
            "count": len(top5),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "strategy": "V4+ML Filter",
        }
    except Exception as e:
        import traceback
        logger.error(f"AI精选TOP5失败: {e}\n{traceback.format_exc()}")
        return {"stocks": [], "error": str(e)}


# ========== AI模拟组合 API ==========

@router.get("/api/ai_sim/performance")
async def ai_sim_performance(request: FastAPIRequest, token: str = Cookie(None)):
    """AI模拟组合性能报告"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "AI模拟组合查询")

    try:
        import pymysql
        from ai_sim_trading import get_performance_report, init_tables, update_performance, compute_summary
        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        init_tables(conn)
        update_performance(conn)
        compute_summary(conn)
        report = get_performance_report(conn)
        conn.close()

        return report
    except Exception as e:
        import traceback
        logger.error(f"AI模拟组合查询失败: {e}\n{traceback.format_exc()}")
        return {"error": str(e)}


@router.post("/api/ai_sim/run")
async def ai_sim_run_today(request: FastAPIRequest, token: str = Cookie(None)):
    """手动运行今日AI模拟组合记录"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "AI模拟组合记录")

    try:
        import pymysql
        from ai_sim_trading import init_tables, record_daily_top5, update_performance, compute_summary
        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        init_tables(conn)
        record_daily_top5(conn)
        update_performance(conn)
        compute_summary(conn)
        conn.close()

        return {"status": "ok", "message": "今日AI精选已记录"}
    except Exception as e:
        import traceback
        logger.error(f"AI模拟组合记录失败: {e}\n{traceback.format_exc()}")
        return {"error": str(e)}


# ========== 底部起步 / 组合策略 ==========

@router.get("/api/combo_scan")
async def scan_combo(request: FastAPIRequest, block: str = "", market: str = "", token: str = Cookie(None)):
    """V4+ML 过滤策略扫描（2026-05-09 起）：V4 技术筛选 + ML 负向过滤"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"组合策略扫描 {block or market}")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        from quant_app.services.strategy_service import generate_v4_ml_candidates
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)

        candidates, display_date = generate_v4_ml_candidates(
            conn, market=market if market else None,
            block=block if block else None, limit=50
        )
        conn.close()

        if not candidates:
            return {
                "stocks": [],
                "scan_type": "V4+ML过滤策略",
                "scan_date": display_date or datetime.now().strftime('%Y-%m-%d'),
                "error": "技术筛选无结果"
            }

        stocks = []
        for c in candidates:
            ts_code = c['ts_code']
            code_raw = ts_code.split(".")[0]
            mkt = "sz" if ts_code.endswith(".SZ") else "sh"
            code_full = f"{mkt.upper()}{code_raw}"

            # ML 得分 → 预测收益 / ml概率 映射
            ml_score = c.get('ml_score', 0)
            # ml_score 为模型预测收益，概率用 sigmoid 简单映射
            import math
            ml_prob = 1 / (1 + math.exp(-ml_score)) if ml_score != 0 else 0.5

            reasons = []
            if c.get('main_net', 0) > 1000:
                reasons.append(f"主力净流入{c['main_net']:.0f}万")
            if c['volume_ratio'] > 2:
                reasons.append(f"量比{c['volume_ratio']:.2f}")
            if c['rps_20'] >= 60:
                reasons.append(f"RPS{c['rps_20']:.0f}")
            if not reasons:
                reasons.append("V4+ML达标")

            stocks.append({
                "代码": code_full,
                "交易所": mkt,
                "名称": c['name'],
                "行业": c.get('industry', ''),
                "现价": c['close'],
                "涨跌幅": f"{c['pct_chg']:+.2f}%",
                "换手率": f"{c['turnover_rate']:.2f}%",
                "量比": f"{c['volume_ratio']:.2f}",
                "V4评分": c['v4_score'],
                "ML得分": ml_score,
                "综合评分": c['v4_score'],
                "预测收益": round(ml_score, 2),
                "ml概率": round(ml_prob, 4),
                "主力评分": 0,
                "阶段判断": "",
                "龙虎榜加分": 0,
                "股东加分": 0,
                "入选理由": " | ".join(reasons),
                "入选原因": " | ".join(reasons),
                "ts_code": ts_code,
            })

        return {
            "scan_date": display_date or datetime.now().strftime('%Y-%m-%d'),
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(stocks),
            "stocks": stocks,
            "scan_type": "V4+ML过滤策略",
        }
    except Exception as e:
        import traceback
        logger.error(f"combo_scan error: {traceback.format_exc()}")
        return {"stocks": [], "scan_type": "V4+ML过滤策略", "error": str(e)}


@router.get("/api/scan/v5")
async def scan_v5(request: FastAPIRequest, block: str = "", market: str = "", token: str = Cookie(None)):
    """V5 缩量回踩策略扫描"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"V5缩量回踩扫描 {block or market}")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            return {"stocks": [], "scan_type": "V5缩量回踩", "error": "无交易数据"}
        today_str = str(latest_date)

        # 获取前一个交易日
        cursor.execute("""
            SELECT trade_date FROM quant_db.daily_price
            WHERE trade_date < %s ORDER BY trade_date DESC LIMIT 1
        """, (today_str,))
        prev_row = cursor.fetchone()
        prev_date = str(prev_row[0]) if prev_row else None

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

        if block and block not in ['沪市主板', '深市主板', '创业板', '科创板']:
            block_clause = " AND s.industry LIKE %s"
            block_val = f"%{block}%"
        else:
            block_clause = ""
            block_val = None
        params = [today_str]
        if block_val:
            params.append(block_val)

        # 第一步：获取今日候选（不含前日涨幅条件，后续在Python中过滤）
        sql = f"""
            SELECT d.ts_code, s.name, s.industry,
                   d.close, d.pct_chg, d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20, d.rps_20
            FROM quant_db.daily_price d
            JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE d.trade_date = %s
              AND d.close >= 5
              AND d.pct_chg BETWEEN -1.0 AND 3.0
              AND d.turnover_rate >= 1.5
              AND d.volume_ratio >= 1.2
              AND d.rps_20 >= 70
              AND d.ma5 > d.ma10 AND d.ma10 > d.ma20
              AND d.close > d.ma10
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '68%%'
              AND d.ts_code NOT LIKE '92%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
              {market_clause}
              {block_clause}
        """
        cursor.execute(sql, tuple(params))
        candidates = cursor.fetchall()

        # 如果需要前日涨幅过滤，获取所有候选股的前日数据
        prev_data = {}
        if prev_date and candidates:
            codes = [r[0] for r in candidates]
            placeholders = ','.join(['%s'] * len(codes))
            cursor.execute(f"""
                SELECT ts_code, pct_chg FROM quant_db.daily_price
                WHERE trade_date = %s AND ts_code IN ({placeholders})
            """, (prev_date, *codes))
            for r in cursor.fetchall():
                prev_data[r[0]] = float(r[1]) if r[1] else 0

        # 获取近3日量比数据（用于缩量过滤）
        recent_vr = {}
        if candidates:
            codes = [r[0] for r in candidates]
            placeholders = ','.join(['%s'] * len(codes))
            # 获取近3个交易日数据
            cursor.execute(f"""
                SELECT trade_date FROM quant_db.daily_price
                WHERE trade_date <= %s ORDER BY trade_date DESC LIMIT 3
            """, (today_str,))
            recent_dates = [str(r[0]) for r in cursor.fetchall()]

            if len(recent_dates) >= 2 and codes:
                date_placeholders = ','.join(['%s'] * len(recent_dates))
                code_placeholders = ','.join(['%s'] * len(codes))
                cursor.execute(f"""
                    SELECT ts_code, trade_date, volume_ratio FROM quant_db.daily_price
                    WHERE trade_date IN ({date_placeholders})
                    AND trade_date < %s
                    AND ts_code IN ({code_placeholders})
                """, (*recent_dates, today_str, *codes))
                for r in cursor.fetchall():
                    key = r[0]
                    if key not in recent_vr:
                        recent_vr[key] = []
                    recent_vr[key].append(float(r[2]) if r[2] else 0)

        cursor.close()
        conn.close()

        # 第二步：Python 中应用 V5 精细过滤
        stocks = []
        for r in candidates:
            ts_code = r[0]
            name = r[1] or ""
            industry = r[2] or ""
            price = float(r[3]) if r[3] else 0
            pct_chg = float(r[4]) if r[4] else 0
            turnover = float(r[5]) if r[5] else 0
            vol_ratio = float(r[6]) if r[6] else 0
            ma5 = float(r[7]) if r[7] else 0
            ma10 = float(r[8]) if r[8] else 0
            ma20 = float(r[9]) if r[9] else 0
            rps = float(r[10]) if r[10] else 0

            # 前日涨幅 -2% ~ 1%
            if prev_date and ts_code in prev_data:
                prev_pct = prev_data[ts_code]
                if prev_pct < -2.0 or prev_pct > 1.0:
                    continue

            # 近3日缩量（至少有一天量比 <= 0.7）
            if ts_code in recent_vr and len(recent_vr[ts_code]) >= 1:
                if min(recent_vr[ts_code]) > 0.7:
                    continue

            # 均线支撑距离（距MA10 <= 5% 或 MA20 <= 6%）
            d10 = abs(price - ma10) / ma10 if ma10 > 0 else 999
            d20 = abs(price - ma20) / ma20 if ma20 > 0 else 999
            if d10 > 0.05 and d20 > 0.06:
                continue

            # 综合评分
            score = 0
            if vol_ratio > 2.0: score += 20
            elif vol_ratio > 1.5: score += 15
            else: score += 8
            if d10 < 0.02: score += 20
            elif d20 < 0.03: score += 15
            else: score += 5
            if rps >= 80: score += 10
            elif rps >= 70: score += 8

            code_raw = ts_code.split(".")[0]
            mkt = "sz" if ts_code.endswith(".SZ") else "sh"
            code_full = f"{mkt.upper()}{code_raw}"

            stocks.append({
                "代码": code_full,
                "交易所": mkt,
                "名称": name,
                "行业": industry,
                "现价": price,
                "涨跌幅": f"{pct_chg:+.2f}%",
                "换手率": f"{turnover:.2f}%",
                "量比": f"{vol_ratio:.2f}",
                "RPS": int(rps),
                "综合评分": score,
                "ts_code": ts_code,
                "前日涨幅": f"{prev_data.get(ts_code, 0):+.2f}%",
            })

        # 按综合评分排序
        stocks.sort(key=lambda x: x.get('综合评分', 0), reverse=True)

        return {
            "scan_date": latest_date.strftime("%Y%m%d") if hasattr(latest_date, "strftime") else today_str,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(stocks),
            "stocks": stocks,
            "scan_type": "V5缩量回踩",
        }
    except Exception as e:
        import traceback
        logger.error(f"scan_v5 error: {traceback.format_exc()}")
        try: conn.close()
        except Exception: pass
        return {"stocks": [], "scan_type": "V5缩量回踩", "error": str(e)}


@router.get("/api/pool_bottom")
async def get_pool_bottom(token: str = Cookie(None)):
    """底部起步策略已下线"""
    return {"stocks": [], "scan_type": "底部起步策略", "error": "策略已下线，请使用V4组合策略"}


@router.get("/api/pool_ma_pullback")
async def get_pool_ma_pullback(token: str = Cookie(None)):
    """获取均线回踩策略股票池"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    pool_file = os.path.join(DATA_DIR, "stock_pool_ma_pullback.json")
    if not os.path.exists(pool_file):
        return {"stocks": [], "scan_type": "均线回踩策略", "error": "股票池为空"}
    with open(pool_file, 'r') as f:
        return json.load(f)


@router.get("/api/scan_ma_pullback")
async def scan_ma_pullback(request: FastAPIRequest, token: str = Cookie(None)):
    """触发均线回踩策略扫描"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "触发均线回踩扫描")
    result = scan_daily_pool_ma_pullback()
    return result


# ========== 大盘状态 ==========

@router.get("/api/market_state")
@router.get("/api/market/state")
async def api_market_state(token: str = Cookie(None)):
    """返回当前大盘状态（统一5状态模型 + 实时快照）"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        from market_state import get_market_state
        from ml_predict import get_market_info
        ms = get_market_state()
        rt = get_market_info()
        return {
            'success': True,
            'state': ms.get('state', 'range'),
            'state_name': ms.get('state_name', '震荡'),
            'score': ms.get('score', 0),
            'advice': ms.get('advice', ''),
            'params': ms.get('params', {}),
            'mkt_chg': rt.get('mkt_chg', 0),
            'breadth_ratio': rt.get('breadth_ratio', 50),
            'up_cnt': rt.get('up_cnt', 0),
            'total_cnt': rt.get('total_cnt', 0),
            'date': rt.get('date', ''),
            'source': rt.get('source', ''),
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


# ========== 合并后的选股 API（P0）==========

@router.get("/api/scan/rule")
async def scan_rule(request: FastAPIRequest, mode: str = "bottom", block: str = "", market: str = "", token: str = Cookie(None)):
    """
    统一规则选股 API — 合并底部起步/强势活跃/组合策略
    mode: bottom | strong | combo
    """
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"规则选股({mode}) {block or market}")

    if mode in ("bottom", "strong"):
        return {"error": f"该策略已下线（{mode}），请使用 V4 组合策略", "mode": mode}
    elif mode == "combo":
        return await scan_combo(request, block=block, market=market, token=token)
    elif mode == "v5":
        return await scan_v5(request, block=block, market=market, token=token)
    elif mode == "awakening":
        return await scan_bottom_awakening(request, token=token)
    else:
        return await scan_strong(request, block=block, market=market, token=token)


@router.get("/api/scan/ml")
async def scan_ml(request: FastAPIRequest, mode: str = "all", block: str = "", market: str = "", token: str = Cookie(None)):
    """
    统一 AI 选股 API — 合并 AI模型选股/AI精选TOP5
    mode: all | top10 | top5
    """
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"AI选股({mode}) {block or market}")

    if mode == "top5":
        import pymysql
        from quant_app.utils.config import get_db_config
        from quant_app.services.strategy_service import generate_v4_ml_top5
        _conn = pymysql.connect(**get_db_config())
        top5 = generate_v4_ml_top5(_conn)
        _conn.close()

        stocks = []
        for s in top5:
            code = s['ts_code'].split('.')[0]
            stocks.append({
                'rank': s.get('rank', 0),
                'code': code,
                'name': s['name'],
                'industry': s.get('industry', ''),
                'price': f"{s['close']:.2f}",
                'change': f"{s['pct_chg']:+.2f}%",
                'ml_score': s.get('ml_score', 0),
                'total_score': s.get('total_score', s.get('v4_score', 0)),
                'reasons': s.get('reasons', []),
            })

        return {"stocks": stocks, "date": top5[0].get('date', datetime.now().strftime('%Y-%m-%d')) if top5 else ''}
    else:
        # 复用 AI模型选股管道，取 top 10 或 top 50
        return await scan_aimodel(request, block=block, market=market, token=token)


@router.get("/api/scan/bottom-awakening")
async def scan_bottom_awakening(request: FastAPIRequest, token: str = Cookie(None)):
    """底部苏醒策略 — 底部低量横盘放量起步"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "底部苏醒选股")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        from quant_app.services.strategy_service import generate_bottom_awakening_top5

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        top5 = generate_bottom_awakening_top5(conn)
        conn.close()

        stocks = []
        for s in top5:
            code = s['ts_code'].split('.')[0]
            stocks.append({
                'rank': s.get('rank', 0),
                'code': code,
                'name': s['name'],
                'industry': s.get('industry', ''),
                'price': f"{s['close']:.2f}",
                'change_pct': f"{s['pct_chg']:+.2f}%",
                'turnover_rate': f"{s['turnover_rate']:.2f}%",
                'vol_ratio': round(float(s.get('volume_ratio', 0)), 2),
                'vol_expansion': round(float(s.get('vol_expansion', 0)), 2),
                'position_52w': round(float(s.get('position_52w', 0)), 1),
                'awakening_score': s.get('awakening_score', 0),
                'rps_20': round(float(s.get('rps_20', 0)), 1),
                'main_net': f"{float(s.get('main_net', 0)):.0f}万",
                'reasons': s.get('entry_reason', '') if isinstance(s.get('entry_reason'), str) else ' | '.join(s.get('reasons', [])),
                'ts_code': s['ts_code'],
            })

        return {
            "stocks": stocks,
            "scan_date": top5[0].get('date', '') if top5 else '',
            "count": len(stocks),
            "scan_type": "底部苏醒策略",
        }
    except Exception as e:
        import traceback
        logger.error(f"底部苏醒扫描失败: {e}\n{traceback.format_exc()}")
        return {"stocks": [], "error": str(e)}


def _pick_best_from_combo(db_config, conn, latest_date):
    """从组合策略逻辑中选最佳"""
    try:
        cursor = conn.cursor()

        sql = """
            SELECT d.ts_code, s.name, s.industry,
                   d.close, d.pct_chg,
                   d.turnover_rate, d.volume_ratio,
                   d.ma5, d.ma10, d.ma20
            FROM quant_db.daily_price d
            JOIN quant_db.stock_info s ON d.ts_code = s.ts_code COLLATE utf8mb4_unicode_ci
            WHERE d.trade_date = %s
              AND d.close > 5
              AND d.pct_chg > 1
              AND d.pct_chg < 9.5
              AND d.turnover_rate > 1.5
              AND s.is_st = 0
              AND d.ts_code NOT LIKE '688%%'
              AND d.ts_code NOT LIKE '92%%'
              AND d.ts_code NOT LIKE '8%%'
              AND d.ts_code NOT LIKE '4%%'
              AND (
                  ((d.ma5 > d.ma10) AND (d.ma10 > d.ma20) AND d.close > d.ma5 AND d.volume_ratio > 1.5)
                  OR (d.pct_chg > 4.0 AND d.volume_ratio > 2.0 AND d.close > d.ma5)
              )
            ORDER BY d.pct_chg DESC
            LIMIT 200
        """
        cursor.execute(sql, (str(latest_date),))
        candidates = cursor.fetchall()
        cursor.close()

        if not candidates:
            return None

        # 主力评分（仅用于展示，不做过滤）
        scripts_dir = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from mainforce_scoring import calculate_mainforce_score

        stocks = []
        for r in candidates:
            ts_code = r[0]
            try:
                mf = calculate_mainforce_score(ts_code, latest_date)
            except Exception:
                mf = {'score': 0}
            mainforce_score = mf.get('score', 0)

            code_raw = ts_code.split(".")[0]
            mkt = "sz" if ts_code.endswith(".SZ") else "sh"
            code_full = f"{mkt.upper()}{code_raw}"
            reasons = []
            if r[7] and r[8] and r[9] and float(r[7]) > float(r[8]) > float(r[9]):
                reasons.append("均线多头")
            if float(r[6] or 0) > 1.5:
                reasons.append("量比充足")
            if mf.get('net_flow', 0) > 0:
                reasons.append("主力净流入")

            stocks.append({
                "代码": code_full,
                "名称": r[1] or "",
                "行业": r[2] or "",
                "现价": float(r[3]) if r[3] else 0,
                "涨跌幅": f"{float(r[4]):+.2f}%",
                "换手率": f"{float(r[5] or 0):.2f}%",
                "量比": f"{float(r[6] or 0):.2f}",
                "综合评分": mainforce_score,
                "主力评分": mainforce_score,
                "入选理由": " + ".join(reasons) if reasons else "技术面达标",
            })

        if not stocks:
            return None

        # ML增强
        try:
            from ml_predict import ml_enhanced_score
            stocks = ml_enhanced_score(stocks, db_conn=conn)
        except Exception:
            for s in stocks:
                s['ml概率'] = 0.5
                s['预测收益'] = 0.0
                s['增强评分'] = round(s.get('综合评分', 0), 1)
                s['热点板块'] = ''
                s['资金趋势'] = ''

        # V4.1筛选 → V6.5 ML排序：按预测收益降序
        stocks.sort(key=lambda x: x.get('预测收益', 0), reverse=True)
        top3 = stocks[:3]

        result = []
        for best in top3:
            pred_ret = best.get('预测收益', 0)
            if pred_ret > 0:
                signal = '强'
            elif pred_ret > -1:
                signal = '中'
            else:
                signal = '弱'
            price = best.get('现价', 0)

            result.append({
                "代码": best.get('代码', ''),
                "名称": best.get('名称', ''),
                "行业": best.get('行业', ''),
                "现价": price,
                "涨跌幅": best.get('涨跌幅', ''),
                "换手率": best.get('换手率', ''),
                "量比": best.get('量比', ''),
                "综合评分": best.get('综合评分', 0),
                "预测收益": best.get('预测收益', 0),
                "ml概率": best.get('ml概率', 0.5),
                "热点板块": best.get('热点板块', ''),
                "资金趋势": best.get('资金趋势', ''),
                "信号强度": signal,
                "入选理由": best.get('入选理由', ''),
                "止损价": round(price * 0.97, 2),
            })
        return result
    except Exception as e:
        logger.info(f"组合策略推荐失败: {e}")
        return None


# ========== 股票池管理 ==========

@router.get("/api/pool")
async def get_pool(token: str = Cookie(None)):
    """获取已保存的选股池"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    pool_file = os.path.join(DATA_DIR, "stock_pool.json")
    if not os.path.exists(pool_file):
        return {"stocks": [], "last_scan": None, "strategy": None}
    with open(pool_file, 'r') as f:
        return json.load(f)


@router.delete("/api/pool")
async def clear_pool(token: str = Cookie(None)):
    """清空调股池"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    pool_file = os.path.join(DATA_DIR, "stock_pool.json")
    with open(pool_file, 'w') as f:
        json.dump({"last_scan": None, "stocks": [], "strategy": None}, f, ensure_ascii=False, indent=2)
    return {"ok": True}


# ========== ML Top15 独立选股 ==========

@router.get("/api/ml_top15")
async def ml_top15_scan(request: FastAPIRequest, token: str = Cookie(None)):
    """ML V6.5 独立选股 — Top15 按预测概率排序"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "ML Top15选股")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        # 先查缓存（当天）
        cursor.execute("SELECT MAX(trade_date) FROM daily_price")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            return {"stocks": [], "model": "none", "error": "无交易数据"}

        date_str = str(latest_date)

        # 查 ml_predictions 表是否有当天数据
        cursor.execute("SELECT COUNT(*) FROM ml_predictions WHERE trade_date = %s", (date_str,))
        if cursor.fetchone()[0] > 0:
            # 用缓存数据
            cursor.execute("""
                SELECT ts_code, _ml_pred, predicted_return, model_type
                FROM ml_predictions WHERE trade_date = %s
                ORDER BY _ml_pred DESC LIMIT 15
            """, (date_str,))
            ml_rows = cursor.fetchall()

            if ml_rows:
                codes = [r[0] for r in ml_rows]
                placeholders = ','.join(['%s'] * len(codes))
                cursor.execute(f"""
                    SELECT s.ts_code, name, industry, close, pct_chg,
                           volume_ratio, turnover_rate, rps_20, amount
                    FROM stock_info s
                    JOIN daily_price d ON s.ts_code COLLATE utf8mb4_unicode_ci = d.ts_code COLLATE utf8mb4_unicode_ci AND d.trade_date = %s
                    WHERE s.ts_code IN ({placeholders})
                """, (date_str, *codes))
                stock_map = {}
                for r in cursor.fetchall():
                    stock_map[r[0]] = {
                        'ts_code': r[0], 'name': r[1], 'industry': r[2],
                        'close': float(r[3] or 0), 'pct_chg': float(r[4] or 0),
                        'volume_ratio': float(r[5] or 0), 'turnover_rate': float(r[6] or 0),
                        'rps_20': float(r[7] or 0), 'amount': float(r[8] or 0),
                    }

                stocks = []
                for row in ml_rows:
                    tc, pred, ret, mtype = row
                    info = stock_map.get(tc, {})
                    stocks.append({
                        'ts_code': tc,
                        'name': info.get('name', ''),
                        'industry': info.get('industry', ''),
                        'close': info.get('close', 0),
                        'pct_chg': info.get('pct_chg', 0),
                        'volume_ratio': info.get('volume_ratio', 0),
                        'turnover_rate': info.get('turnover_rate', 0),
                        'rps_20': info.get('rps_20', 0),
                        'amount': info.get('amount', 0),
                        'ml_prob': round(pred, 3),
                        'ml_pred_return': round(ret, 2),
                        'model_type': mtype,
                    })

                cursor.close()
                conn.close()
                return {
                    "stocks": stocks,
                    "scan_date": date_str,
                    "model": ml_rows[0][3] if ml_rows else "unknown",
                    "cache": True,
                }

        # 无缓存，实时预测
        cursor.close()
        conn.close()

        from ml_predict import predict_batch
        conn2 = pymysql.connect(**db_config)
        cursor2 = conn2.cursor()
        cursor2.execute("SELECT ts_code FROM daily_price WHERE trade_date = %s", (date_str,))
        all_codes = [r[0] for r in cursor2.fetchall()]
        cursor2.close()
        conn2.close()

        results = predict_batch(all_codes, as_of_date=latest_date)

        # 排序取 Top15
        scored = [(tc, p['probability'], p['predicted_return'], p['model_type'])
                  for tc, p in results.items()]
        scored.sort(key=lambda x: x[1], reverse=True)
        top15 = scored[:15]

        # 写入库
        conn3 = pymysql.connect(**db_config)
        cursor3 = conn3.cursor()
        cursor3.execute("DELETE FROM ml_predictions WHERE trade_date = %s", (date_str,))
        for tc, prob, ret, mt in scored:
            cursor3.execute(
                """INSERT INTO ml_predictions (ts_code, trade_date, _ml_pred, predicted_return, model_type)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tc, date_str, prob, ret, mt)
            )
        conn3.commit()

        # 获取股票信息
        top_codes = [tc for tc, _, _, _ in top15]
        placeholders = ','.join(['%s'] * len(top_codes))
        cursor3.execute(f"""
            SELECT s.ts_code, name, industry, close, pct_chg,
                   volume_ratio, turnover_rate, rps_20, amount
            FROM stock_info s
            JOIN daily_price d ON s.ts_code COLLATE utf8mb4_unicode_ci = d.ts_code COLLATE utf8mb4_unicode_ci AND d.trade_date = %s
            WHERE s.ts_code IN ({placeholders})
        """, (date_str, *top_codes))
        stock_map = {}
        for r in cursor3.fetchall():
            stock_map[r[0]] = {
                'name': r[1], 'industry': r[2], 'close': float(r[3] or 0),
                'pct_chg': float(r[4] or 0), 'volume_ratio': float(r[5] or 0),
                'turnover_rate': float(r[6] or 0), 'rps_20': float(r[7] or 0),
                'amount': float(r[8] or 0),
            }
        cursor3.close()
        conn3.close()

        stocks = []
        for tc, prob, ret, mt in top15:
            info = stock_map.get(tc, {})
            stocks.append({
                'ts_code': tc,
                'name': info.get('name', ''),
                'industry': info.get('industry', ''),
                'close': info.get('close', 0),
                'pct_chg': info.get('pct_chg', 0),
                'volume_ratio': info.get('volume_ratio', 0),
                'turnover_rate': info.get('turnover_rate', 0),
                'rps_20': info.get('rps_20', 0),
                'amount': info.get('amount', 0),
                'ml_prob': round(prob, 3),
                'ml_pred_return': round(ret, 2),
                'model_type': mt,
            })

        return {
            "stocks": stocks,
            "scan_date": date_str,
            "model": top15[0][3] if top15 else "unknown",
            "cache": False,
        }

    except Exception as e:
        logger.error(f"ML Top15 扫描失败: {e}")
        import traceback
        traceback.print_exc()
        return {"stocks": [], "error": str(e)}
