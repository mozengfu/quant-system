# -*- coding: utf-8 -*-
"""
策略选股相关 API 路由
"""
import os, json, time, logging, sys, math, numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Cookie, Request as FastAPIRequest, HTTPException
from fastapi.responses import JSONResponse
from app_core import (
    # Strategy
    strategy_scan, get_block_stocks, scan_daily_pool,
    scan_daily_pool_bottom_breakout, scan_daily_pool_ma_pullback,
    scan_daily_pool_technical,
    analyze_stock, ALL_BLOCKS,
    # Data
    get_stock_realtime, get_tushare_pro, get_recent_trade_dates,
    # Signals
    get_signals_path, read_signals, write_signals,
    # Tracking
    load_track_data, save_track_data, record_recommendation, update_stock_results,
    # Auth
    get_current_user, require_auth,
    # Notifications
    send_feishu,
    # Other
    generate_order_id, save_access_log, get_client_ip,
    add_to_positions, sync_positions,
)
from quant_app.utils.authz import require_admin, is_admin

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["strategy"])


# ========== 个股分析 ==========

@router.get("/api/analysis/{market}/{code}")
async def analyze(market: str, code: str, request: FastAPIRequest, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"分析个股 {market.upper()}{code}")
    return analyze_stock(code, market)


@router.get("/api/sentiment")
async def get_sentiment(token: str = Cookie(None)):
    """市场情绪接口 - 涨停/跌停/涨跌家数比"""
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        pro = get_tushare_pro()
        today = get_recent_trade_dates(1)
        if not today:
            return {"error": "无法获取今日交易日"}
        trade_date = today[-1]
        df = pro.daily(trade_date=trade_date)
        if df is None or len(df) == 0:
            return {"error": f"今日({trade_date})无交易数据"}
        rise_count = int((df["pct_chg"] >= 9.5).sum())   # 涨停（≥9.5%）
        fall_count = int((df["pct_chg"] <= -9.5).sum())  # 跌停（≤-9.5%）
        up_count = int((df["pct_chg"] > 0).sum())
        down_count = int((df["pct_chg"] < 0).sum())
        total = len(df)
        rise_ratio = up_count / max(down_count, 1)
        if rise_ratio >= 2:
            sentiment = "极度乐观"
        elif rise_ratio >= 1.2:
            sentiment = "偏乐观"
        elif rise_ratio >= 0.8:
            sentiment = "中性"
        elif rise_ratio >= 0.5:
            sentiment = "偏悲观"
        else:
            sentiment = "极度悲观"
        return {
            "trade_date": trade_date,
            "涨停家数": rise_count,
            "跌停家数": fall_count,
            "上涨家数": up_count,
            "下跌家数": down_count,
            "涨跌比": round(rise_ratio, 2),
            "市场情绪": sentiment,
            "备注": "涨停/跌停以±9.5%计算",
        }
    except Exception as e:
        logger.error(f"市场情绪获取失败: {e}")
        return {"error": str(e)}


@router.get("/api/blocks")
async def get_blocks(token: str = Cookie(None)):
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    return {"blocks": ALL_BLOCKS}


# ========== 交易信号 API ==========

@router.get("/api/signals")
async def signals(token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    require_admin(user)

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        cursor.execute("SELECT id, signal_type, ts_code, stock_name, price, qty, reason, signal_date, status, close_price, close_date, pnl FROM trade_signals ORDER BY signal_date DESC")
        rows = cursor.fetchall()
        signals = []

        # 收集持仓中的股票代码用于批量获取实时行情
        holding_codes = []
        for r in rows:
            if r[8] == '持仓中':
                ts_code = r[2] or ""
                if len(ts_code) >= 6:
                    pure_code = ts_code[:6]
                    market = 'sh' if ts_code.endswith('.SH') else 'sz'
                    holding_codes.append(f"{market}{pure_code}")

        # 批量获取实时价格
        current_prices = {}
        if holding_codes:
            import urllib.request
            q_str = ','.join(holding_codes)
            url = f'http://qt.gtimg.cn/q={q_str}'
            try:
                raw = urllib.request.urlopen(url, timeout=10).read().decode('gbk')
                for line in raw.strip().split(';'):
                    if not line.strip():
                        continue
                    parts = line.split('~')
                    if len(parts) > 3:
                        code_key = parts[2] if len(parts[2]) == 6 else parts[0].split('_')[-1]
                        try:
                            current_prices[code_key] = float(parts[3])
                        except (ValueError, IndexError):
                            pass
            except Exception as e:
                logger.error(f"获取实时行情失败: {e}")

        for r in rows:
            sig = {
                "id": r[0],
                "type": r[1],
                "code": r[2],
                "name": r[3],
                "price": float(r[4]) if r[4] else 0,
                "qty": r[5],
                "reason": r[6] or "",
                "date": str(r[7]) if r[7] else "",
                "status": r[8] or "持仓中",
                "close_price": float(r[9]) if r[9] else None,
                "close_date": str(r[10]) if r[10] else None,
                "pnl": float(r[11]) if r[11] else None,
                "current_price": None,
                "float_pct": None,
            }
            # 持仓中：用实时价格计算浮动盈亏
            if sig["status"] == "持仓中":
                ts_code = r[2] or ""
                pure_code = ts_code[:6] if len(ts_code) >= 6 else ""
                if pure_code in current_prices:
                    cp = current_prices[pure_code]
                    sig["current_price"] = cp
                    sig["pnl"] = round((cp - sig["price"]) * sig["qty"], 2)
                    sig["float_pct"] = round((cp - sig["price"]) / sig["price"] * 100, 2)

            signals.append(sig)

        cursor.close()
        conn.close()
        return {"signals": signals}
    except Exception as e:
        return {"signals": [], "error": str(e)}


@router.post("/api/signals")
async def add_signal(req: FastAPIRequest, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        body = await req.json()
        code = body.get("code", "")
        # Convert code to ts_code format
        if len(code) == 6:
            market = 'SZ' if code.startswith(('00', '30')) else 'SH'
            ts_code = '%s.%s' % (code, market)
        elif code[:2].isalpha() and len(code) == 8:
            ts_code = '%s.%s' % (code[2:], code[:2])
        else:
            ts_code = code

        import uuid
        sig_id = str(uuid.uuid4())[:8]
        sig_date = body.get("date", datetime.now().strftime("%Y-%m-%d"))

        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO trade_signals
        (id, signal_type, ts_code, stock_name, price, qty, reason, signal_date, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '持仓中')
        ''', (sig_id, body.get("type", "买入"), ts_code, body.get("name", ""),
              float(body.get("price", 0)), int(body.get("qty", 0)),
              body.get("reason", ""), sig_date))
        conn.commit()
        cursor.close()
        conn.close()

        sig = {
            "id": sig_id,
            "type": body.get("type", "买入"),
            "code": ts_code,
            "name": body.get("name", ""),
            "price": float(body.get("price", 0)),
            "qty": int(body.get("qty", 0)),
            "reason": body.get("reason", ""),
            "date": sig_date,
            "status": "持仓中",
            "close_price": None,
            "close_date": None,
            "pnl": None,
        }

        # 如果是买入信号，自动添加到持仓监控
        if sig["type"] == "买入":
            add_to_positions(sig)

        sync_positions()
        return {"success": True, "signal": sig}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.put("/api/signals/{sig_id}")
async def close_signal(sig_id: str, req: FastAPIRequest, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        body = await req.json()
        close_price = float(body.get("close_price", 0))
        close_date = body.get("close_date", datetime.now().strftime("%Y-%m-%d"))

        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()

        # Get original signal
        cursor.execute("SELECT id, signal_type, price, qty, stock_name FROM trade_signals WHERE id = %s", (sig_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return {"success": False, "error": "信号不存在"}

        # Calculate PnL
        pnl = round((close_price - float(row[2])) * int(row[3]), 2)
        if row[1] == "卖出":
            pnl = -pnl

        cursor.execute('''
        UPDATE trade_signals SET status='已平仓', close_price=%s, close_date=%s, pnl=%s WHERE id=%s
        ''', (close_price, close_date, pnl, sig_id))
        conn.commit()
        cursor.close()
        conn.close()

        sync_positions()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.delete("/api/signals/{sig_id}")
async def delete_signal(sig_id: str, token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trade_signals WHERE id = %s", (sig_id,))
        conn.commit()
        cursor.close()
        conn.close()
        sync_positions()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    """AI每日精选TOP5：四层过滤（ML→基本面→技术面→综合排序）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), "AI每日精选TOP5")

    try:
        import json
        from ml_daily_top5 import generate_top5
        import pymysql
        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        top5 = generate_top5(conn)
        conn.close()

        return {
            "stocks": top5,
            "count": len(top5),
            "date": datetime.now().strftime("%Y-%m-%d"),
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
    """V4.1→V6.5 级联策略扫描：技术筛选 → V6.5 ML排序 → TOP推荐"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    save_access_log(user, get_client_ip(request), f"组合策略扫描 {block or market}")

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            return {"stocks": [], "scan_type": "V4组合策略", "error": "无交易数据"}
        today_str = str(latest_date)

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
            block_clause = f" AND s.industry LIKE '%%{block}%%'"
        else:
            block_clause = ""

        sql = f"""
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
              {market_clause}
              {block_clause}
              AND (
                  (d.ma5 > d.ma10 AND d.ma10 > d.ma20 AND d.ma5 IS NOT NULL AND d.ma20 IS NOT NULL AND d.close > d.ma5 AND d.volume_ratio > 1.5)
                  OR (d.pct_chg > 4.0 AND d.volume_ratio > 2.0 AND d.close > d.ma5)
              )
            ORDER BY d.pct_chg DESC
            LIMIT 200
        """
        cursor.execute(sql, (today_str,))
        candidates = cursor.fetchall()

        # 加载龙虎榜数据（近30天）
        dt_30 = (datetime.strptime(today_str, '%Y-%m-%d') - timedelta(days=30)).strftime('%Y-%m-%d')
        cursor.execute("""SELECT ts_code, trade_date, net_buy FROM dragon_tiger
                          WHERE trade_date >= %s AND net_buy != 0""", (dt_30,))
        dt_map = {}
        for r in cursor.fetchall():
            dt_map.setdefault(r[0], []).append((str(r[1]), float(r[2] or 0)))

        # 加载龙虎榜机构席位
        cursor.execute("""SELECT ts_code, trade_date, net_buy, exalter FROM dragon_tiger_inst
                          WHERE trade_date >= %s AND net_buy != 0""", (dt_30,))
        dti_map = {}
        for r in cursor.fetchall():
            dti_map.setdefault(r[0], []).append((str(r[1]), float(r[2] or 0), r[3] or ''))

        # 加载股东人数变化（最近2年数据，够用）
        hc_from = (datetime.strptime(today_str, '%Y-%m-%d') - timedelta(days=730)).strftime('%Y-%m-%d')
        cursor.execute("""SELECT ts_code, end_date, holder_num_change FROM holder_change
                          WHERE end_date >= %s AND end_date <= %s ORDER BY ts_code, end_date DESC""",
                       (hc_from, today_str))
        hc_map = {}
        for r in cursor.fetchall():
            hc_map.setdefault(r[0], []).append((str(r[1]), int(r[2] or 0)))

        cursor.close()
        conn.close()

        if not candidates:
            return {
                "stocks": [],
                "scan_type": "V4组合策略",
                "scan_date": today_str,
                "error": "技术筛选无结果"
            }

        # 调用主力评分模块
        scripts_dir = str(Path(__file__).resolve().parent.parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from mainforce_scoring import calculate_mainforce_score

        # 龙虎榜加分函数
        def get_dragon_bonus(ts_code):
            """机构净买入>3000万→15, >500万→12, 上榜→8"""
            inst_net = sum(nb for td, nb, _ in dti_map.get(ts_code, []) if td >= dt_30)
            if inst_net > 30000000: return 15
            elif inst_net > 5000000: return 12
            listed = sum(1 for td, _ in dt_map.get(ts_code, []) if td >= dt_30)
            if listed > 0: return 8
            return 0

        # 股东集中度加分函数
        def get_holder_bonus(ts_code):
            """连续减少期数：3期+→10, 2期→7, 1期→4"""
            rows = [(td, chg) for td, chg in hc_map.get(ts_code, []) if td <= today_str]
            rows.sort(key=lambda x: x[0], reverse=True)
            if len(rows) < 2: return 0
            decreases = sum(1 for _, chg in rows[:4] if chg < 0)
            if decreases >= 3: return 10
            elif decreases >= 2: return 7
            elif decreases >= 1: return 4
            return 0

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

            if price <= 0:
                continue

            try:
                mf = calculate_mainforce_score(ts_code, latest_date)
            except Exception:
                mf = {'score': 0, 'level': '未知'}
            mainforce_score = mf.get('score', 0)
            mainforce_level = mf.get('level', '未知')

            quick_score = 0
            if ma5 > ma10 > ma20 and ma20 > 0:
                quick_score += 40
            if price > ma5:
                quick_score += 20
            if vol_ratio > 2.0:
                quick_score += 20
            if pct_chg > 3:
                quick_score += 10
            if turnover > 3:
                quick_score += 10

            # 新因子加分：龙虎榜 + 股东集中度
            dt_bonus = get_dragon_bonus(ts_code)
            hc_bonus = get_holder_bonus(ts_code)

            code_raw = ts_code.split(".")[0]
            mkt = "sz" if ts_code.endswith(".SZ") else "sh"
            code_full = f"{mkt.upper()}{code_raw}"

            # 构建入选原因
            reasons = []
            if dt_bonus > 0:
                reasons.append(f"龙虎榜+{dt_bonus}")
            if hc_bonus > 0:
                reasons.append(f"股东集中+{hc_bonus}")

            stocks.append({
                "代码": code_full,
                "交易所": mkt,
                "名称": name,
                "行业": industry,
                "现价": price,
                "涨跌幅": f"{pct_chg:+.2f}%",
                "换手率": f"{turnover:.2f}%",
                "量比": f"{vol_ratio:.2f}",
                "主力评分": int(mainforce_score),
                "阶段判断": mainforce_level,
                "综合评分": quick_score + dt_bonus + hc_bonus,
                "基础评分": quick_score,
                "龙虎榜加分": dt_bonus,
                "股东加分": hc_bonus,
                "入选原因": " | ".join(reasons) if reasons else "",
                "ts_code": ts_code,
            })

        # ML增强评分（用于重排）
        try:
            from ml_predict import ml_enhanced_score
            conn2 = pymysql.connect(**db_config)
            stocks = ml_enhanced_score(stocks, db_conn=conn2)
            conn2.close()
        except Exception as e:
            logger.info(f"ML增强不可用: {e}")
            for s in stocks:
                s['预测收益'] = 0.0
                s['ml概率'] = 0.5
                s['增强评分'] = round(s.get('综合评分', 0), 1)

        # ML 重排：V4.1筛选 → V6.5 排序（按预测收益降序）
        stocks.sort(key=lambda x: x.get('预测收益', 0), reverse=True)

        return {
            "scan_date": latest_date.strftime("%Y%m%d") if hasattr(latest_date, 'strftime') else today_str,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_candidates": len(stocks),
            "stocks": stocks,
            "scan_type": "V4组合策略",
        }
    except Exception as e:
        import traceback
        logger.error(f"combo_scan error: {traceback.format_exc()}")
        try: conn.close()
        except Exception: pass
        return {"stocks": [], "scan_type": "V4组合策略", "error": str(e)}


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
            block_clause = f" AND s.industry LIKE '%%{block}%%'"
        else:
            block_clause = ""

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
        cursor.execute(sql, (today_str,))
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


# ========== 建仓推荐 ==========

RECOMMEND_CACHE_TTL = 1800  # 缓存有效期30分钟

def _load_recommend_cache():
    """读取缓存文件"""
    cache_file = DATA_DIR / "recommend_cache.json"
    if not cache_file.exists():
        return None
    try:
        with open(cache_file) as f:
            cached = json.load(f)
        if cached.get("cache_time") and time.time() - cached["cache_time"] < RECOMMEND_CACHE_TTL:
            return cached.get("data")
    except Exception:
        pass
    return None


def _save_recommend_cache(data):
    """写入缓存文件"""
    cache_file = DATA_DIR / "recommend_cache.json"
    try:
        with open(cache_file, "w") as f:
            json.dump({"cache_time": time.time(), "data": data}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"建仓推荐缓存写入失败: {e}")


@router.get("/api/recommend")
async def get_recommend(force_refresh: bool = False, token: str = Cookie(None)):
    """
    建仓推荐 - 三策略各选1只：底部起步 + 强势活跃 + 组合策略
    每只策略选 ML+综合得分最高 + 资金加速流入 + 热点板块的股票
    有30秒缓存，设置 force_refresh=true 强制刷新
    """
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    # 缓存命中直接返回
    if not force_refresh:
        cached = _load_recommend_cache()
        if cached:
            return cached

    try:
        import pymysql
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)

        cursor = conn.cursor()
        cursor.execute("SELECT MAX(trade_date) FROM quant_db.daily_price")
        latest_date = cursor.fetchone()[0]
        cursor.close()
        if not latest_date:
            conn.close()
            return {"error": "无交易数据"}
        today_str = str(latest_date)

        recommendations = []

        # 组合策略 TOP3（底部起步/强势活跃已下线）
        combo_recs = _pick_best_from_combo(db_config, conn, latest_date)
        if combo_recs:
            for rec in combo_recs[:3]:
                rec['策略来源'] = '组合策略'
                recommendations.append(rec)

        conn.close()

        if not recommendations:
            return {
                "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "策略": "V4 组合策略 TOP3",
                "推荐股票": [],
                "cache_date": today_str,
                "error": "无符合条件的股票"
            }

        result = {
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "策略": "V4 组合策略 TOP3",
            "推荐股票": recommendations,
            "cache_date": today_str,
        }

        if recommendations:
            try:
                record_recommendation(recommendations, "V4 组合策略 TOP3")
                logger.info(f"推荐股票已记录: {[s['名称'] for s in recommendations]}")
            except Exception as e:
                logger.warning(f"记录推荐失败: {e}")

        # 写入缓存
        _save_recommend_cache(result)
        return result
    except Exception as e:
        logger.error(f"建仓推荐失败: {e}")
        try: conn.close()
        except Exception: pass
        return {"error": str(e)}


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
        # 复用现有 TOP5 管道（无板块筛选）
        import pymysql
        from quant_app.utils.config import get_db_config
        from ml_daily_top5 import generate_top5
        _conn = pymysql.connect(**get_db_config())
        result = generate_top5(conn=_conn)
        _conn.close()
        return {"stocks": result, "date": datetime.now().strftime("%Y-%m-%d")}
    else:
        # 复用 AI模型选股管道，取 top 10 或 top 50
        return await scan_aimodel(request, block=block, market=market, token=token)


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


@router.get("/api/recommend/strong")
async def get_recommend_strong(token: str = Cookie(None)):
    """
    建仓推荐 - 强势活跃策略版（直接从 MySQL 读取，无缓存）
    """
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    try:
        cache_file = os.path.join(DATA_DIR, "recommend_strong_cache.json")

        import pymysql
        from datetime import datetime as dt
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=5)
        conn = pymysql.connect(**db_config)
        cursor = conn.cursor()

        cursor.execute("SELECT MAX(snap_date) FROM stock_pool_snap")
        latest_date = cursor.fetchone()[0]
        if not latest_date:
            cursor.close()
            conn.close()
            return {"error": "股票池为空", "recommendations": []}

        cursor.execute("""
            SELECT snap_date, ts_code, name, industry, price, change_pct,
                   turnover_rate, vol_ratio, quick_score, entry_reason, today_rank
            FROM stock_pool_snap
            WHERE snap_date = %s
            ORDER BY quick_score DESC, today_rank ASC
            LIMIT 100
        """, (latest_date,))
        rows = cursor.fetchall()

        stocks = []
        for r in rows:
            code = r[1].split(".")[0]
            mkt = "sz" if r[1].endswith(".SZ") else "sh"
            pct = float(r[5]) if r[5] else 0
            stocks.append({
                "代码": code,
                "交易所": mkt,
                "名称": r[2] or "",
                "行业": r[3] or "",
                "现价": float(r[4]) if r[4] else 0,
                "涨跌幅": f"{pct:+.2f}%",
                "换手率": float(r[6]) if r[6] else 0,
                "量比": float(r[7]) if r[7] else 0,
                "评分": int(r[8]) if r[8] else 0,
                "入选理由": r[9] or "",
                "今日排名": int(r[10]) if r[10] else 0,
                "snap_date": str(r[0]),
            })

        # 批量查询3个交易日前的收盘价
        if stocks:
            today = dt.now().strftime('%Y-%m-%d')
            cursor3 = conn.cursor()
            cursor3.execute("""
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date <= %s
                ORDER BY trade_date DESC LIMIT 4
            """, (today,))
            dates_3d = [str(r[0]) for r in cursor3.fetchall()]
            date_3d_ago = dates_3d[3] if len(dates_3d) > 3 else dates_3d[-1]

            ts_codes = [s.get("代码", "").upper() + "." + s.get("交易所", "").upper() for s in stocks]
            close_map = {}
            if ts_codes:
                placeholders = ','.join(['%s'] * len(ts_codes))
                cursor3.execute(f"""
                    SELECT ts_code, close FROM daily_price
                    WHERE trade_date = %s AND ts_code IN ({placeholders})
                """, (date_3d_ago, *ts_codes))
                for r in cursor3.fetchall():
                    close_map[r[0]] = float(r[1])
            cursor3.close()

            for s in stocks:
                ts_code = s.get("代码", "").upper() + "." + s.get("交易所", "").upper()
                cur_close = s.get("现价", 0)
                old_close = close_map.get(ts_code, 0)
                if old_close and cur_close:
                    s["3日涨幅"] = round((cur_close - old_close) / old_close * 100, 2)
                else:
                    s["3日涨幅"] = 0

        # ML增强评分
        try:
            from ml_predict import ml_enhanced_score
            stocks = ml_enhanced_score(stocks, db_conn=conn)
        except Exception:
            for s in stocks:
                s['ml概率'] = 0.5
                s['增强评分'] = s.get('评分', 0)
                s['热点板块'] = ''
                s['资金趋势'] = ''

        cursor.close()
        conn.close()

        if len(stocks) < 3:
            return {"error": f"股票池只有{len(stocks)}只", "recommendations": []}

        primary_thresh = 0.55
        fallback1 = primary_thresh - 0.05
        qualified = [s for s in stocks if s.get('ml概率', 0) >= primary_thresh]
        if not qualified:
            qualified = [s for s in stocks if s.get('ml概率', 0) >= fallback1]
        if not qualified:
            qualified = stocks

        scored = []
        for stock in qualified:
            try:
                code = stock.get("代码", "")
                name = stock.get("名称", "")
                close = float(stock.get("现价", 0))
                if close <= 0:
                    continue

                vr = float(stock.get("量比", 0))
                turnover = float(stock.get("换手率", 0))
                change = float(stock.get("涨跌幅", 0))
                chg3 = float(stock.get("3日涨幅", 0))
                base_score = int(stock.get("评分", 0))
                ml_prob = float(stock.get('ml概率', 0.5))

                if vr >= 3.0:    vr_score = 30
                elif vr >= 2.0:  vr_score = 25
                elif vr >= 1.5:  vr_score = 20
                elif vr >= 1.0:  vr_score = 15
                else:            vr_score = 5

                if 3 <= turnover <= 8:       tu_score = 20
                elif 8 < turnover <= 12:     tu_score = 15
                elif 1.5 <= turnover < 3:    tu_score = 10
                else:                        tu_score = 5

                if 0 <= change <= 3:         chg_score = 25
                elif -3 <= change < 0:       chg_score = 20
                elif 3 < change <= 6:        chg_score = 15
                else:                        chg_score = 5

                if chg3 > 3:              chg3_score = 10
                elif 0 < chg3 <= 3:       chg3_score = 8
                elif -3 <= chg3 <= 0:     chg3_score = 5
                else:                     chg3_score = 2

                ml_score_part = int(ml_prob * 15)
                base_score_part = min(5, base_score // 20)
                total_score = vr_score + tu_score + chg_score + chg3_score + ml_score_part + base_score_part

                if name.startswith('*ST') or name.startswith('ST'):
                    continue

                stop_loss = round(close * 0.95, 2)
                target = round(close * 1.08, 2)

                if ml_prob >= 0.65:        signal = '强'
                elif ml_prob >= 0.55:      signal = '中'
                else:                      signal = '弱'

                scored.append({
                    "代码": code,
                    "名称": name,
                    "现价": close,
                    "涨跌幅": f"{change:+.2f}%",
                    "换手率": f"{turnover:.2f}%",
                    "量比": f"{vr:.2f}",
                    "3日涨幅": f"{chg3:+.2f}%",
                    "综合评分": total_score,
                    "ml概率": round(ml_prob, 3),
                    "信号强度": signal,
                    "入选理由": f"量比{vr:.1f}+换手{turnover:.1f}%+ML{ml_prob:.0%}",
                    "止损价": stop_loss,
                    "目标价": target,
                })
            except Exception:
                continue

        scored.sort(key=lambda x: x["综合评分"], reverse=True)
        top3 = scored[:3]

        result = {
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "数据来源": "强势活跃策略",
            "recommendations": top3,
            "cache_date": datetime.now().strftime("%Y-%m-%d"),
        }

        try:
            with open(cache_file, 'w') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"缓存写入失败: {e}")

        return result
    except Exception as e:
        logger.error(f"强势推荐失败: {e}")
        try: conn.close()
        except Exception: pass
        return {"error": str(e), "recommendations": []}


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


# ========== V5.0 API ==========

@router.get("/api/v5_recommend")
async def get_v5_recommend(token: str = Cookie(None)):
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        v5_file = os.path.join(DATA_DIR, "sector_v5_stocks.json")
        if not os.path.exists(v5_file):
            return {"error": "V5.0 股票池为空"}
        with open(v5_file, 'r', encoding='utf-8') as f:
            v5_data = json.load(f)
        stocks = v5_data.get("top10_stocks", [])
        if len(stocks) < 3:
            return {"error": f"只有{len(stocks)}只"}
        top3 = stocks[:3]
        for stock in top3:
            try:
                code = stock.get("代码", "")
                market = "sz" if code.startswith("0") or code.startswith("3") else "sh"
                code_short = code.replace(".SZ", "").replace(".SH", "")
                rt_data = get_stock_realtime(code_short, market)
                if rt_data:
                    stock["实时价"] = rt_data.get("现价", 0)
                    stock["实时涨跌幅"] = rt_data.get("涨跌幅", "0%")
            except Exception as e:
                logger.error(f"获取实时数据失败: {e}")
        return {"scan_date": v5_data.get("scan_date", ""), "scan_time": v5_data.get("scan_time", ""), "total_stocks": v5_data.get("total_candidates", 0), "top3_stocks": top3, "strategy": "V5.0 板块趋势"}
    except Exception as e:
        return {"error": str(e)}
