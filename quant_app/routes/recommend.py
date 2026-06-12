"""
建仓推荐 API 路由
"""

import json
import logging
import math
import os
import time
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException

from quant_app.routes.auth import get_current_user
from quant_app.services.market_service import get_stock_realtime

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["recommend"])


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
def get_recommend(force_refresh: bool = False, top_n: int = 3, token: str = Cookie(None)):
    """
    建仓推荐 - OOS-v2 纯ML直接推理（从ml_predictions缓存读取）（不走策略服务管线）
    成交额前500只 → OOS-v2 30特征 → 预测 → Top3
    """
    cache_file = os.path.join(DATA_DIR, f"recommend_cache_{top_n}.json")

    if not force_refresh:
        try:
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    cached = json.load(f)
                cache_valid = (
                    cached.get("cache_time")
                    and time.time() - cached["cache_time"] < RECOMMEND_CACHE_TTL
                    and cached.get("data", {}).get("cache_date") == date.today().strftime("%Y-%m-%d")
                )
                if cache_valid:
                    data = cached.get("data")
                    # 清理缓存中的 NaN
                    def _fix_cache(v):
                        if isinstance(v, dict): return {k: _fix_cache(vk) for k, vk in v.items()}
                        if isinstance(v, (list, tuple)): return [_fix_cache(x) for x in v]
                        if isinstance(v, float):
                            return 0.0 if math.isnan(v) or math.isinf(v) else v
                        return v
                    return _fix_cache(data)
        except: pass

    try:
        import pymysql

        from quant_app.utils.config import get_db_config

        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)
        cur = conn.cursor()


        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = str(cur.fetchone()[0])
        cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (latest,))
        prev = str(cur.fetchone()[0])

        # OOS-v2 模型优先（IC=0.0859, 30特征）
        ranked = []
        model_ver = "OOS-v2"
        oos_path = os.path.join(DATA_DIR, "ml_stock_model_v11_0_oos_v2.pkl")
        if os.path.exists(oos_path):
            try:
                from scripts.predict_v11_oos import build_features as oos_build
                from scripts.predict_v11_oos import ensemble_predict as oos_predict
                from scripts.predict_v11_oos import load_model as oos_load
                oos_load(oos_path)
                cur.execute("""SELECT ts_code FROM daily_price WHERE trade_date=%s
                    AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200
                    ORDER BY amount DESC LIMIT 500""", (prev,))
                pool_codes = [r[0] for r in cur.fetchall()]
                if pool_codes:
                    feat = oos_build(conn, pool_codes, as_of_date=latest)
                    if feat is not None and not feat.empty:
                        preds = oos_predict(feat)
                        ca = feat['ts_code'].tolist()
                        top_codes = [tc for _, tc in sorted(zip(preds, ca), reverse=True)[:top_n]]
                        cur.execute("""SELECT s.ts_code, s.name, d.close, NULL, s.industry
                            FROM stock_info s LEFT JOIN daily_price d ON s.ts_code=d.ts_code AND d.trade_date=%s
                            WHERE s.ts_code IN (%s)""" % ("%s", ",".join(["%s"]*len(top_codes))),
                            tuple([latest] + top_codes))
                        ranked = [{'ts_code': r[0], 'stock_name': r[1] or '', 'price': float(r[2]) if r[2] else 0,
                                    'ml_score': 0, 'industry': r[4] or ''} for r in cur.fetchall()]
                        # Map scores back
                        score_map = dict(zip(ca, preds))
                        for entry in ranked:
                            entry['ml_score'] = float(score_map.get(entry['ts_code'], 0))
                        ranked.sort(key=lambda x: -x['ml_score'])
                        logger.info("OOS-v2 实时推理 %d 只候选", len(ranked))
            except Exception as e:
                logger.warning("OOS-v2 推理失败，回退 ml_predictions: %s", e)

        # Fallback: 从 ml_predictions 表读取
        if not ranked:
            model_ver = "V11.0"
            cur.execute("""SELECT p.ts_code, s.name, d.close, p._ml_pred, s.industry
                           FROM ml_predictions p
                           LEFT JOIN stock_info s ON p.ts_code = s.ts_code
                           LEFT JOIN daily_price d ON p.ts_code = d.ts_code AND d.trade_date = %s
                           WHERE p.trade_date = %s
                           ORDER BY p._ml_pred DESC LIMIT %s""", (latest, latest, top_n))
        scan_rows = cur.fetchall()
        if scan_rows:
            for r in scan_rows:
                ranked.append({
                    'ts_code': r[0], 'stock_name': r[1] or '', 'price': float(r[2]) if r[2] else 0,
                    'ml_score': float(r[3]) if r[3] else 0, 'industry': r[4] or '',
                })
            logger.info("从 ml_predictions 读取 %d 只候选 (V11.0)", len(scan_rows))

        if not ranked:
            return {"error": "无候选", "推荐股票": [], "cache_date": latest}

        # Build recommendation list
        recommendations = []
        codes = [r["ts_code"] if isinstance(r, dict) else r[0] for r in ranked[:top_n]]
        pl = ",".join(["%s"] * len(codes))

        # Get 52-week position for all candidates
        pos_map = {}
        cur.execute(f"""SELECT d1.ts_code,
            (d1.close - d2.min_low) / NULLIF(d2.max_high - d2.min_low, 0)
            FROM daily_price d1
            JOIN (SELECT ts_code, MAX(high) as max_high, MIN(low) as min_low
                  FROM daily_price WHERE trade_date <= %s AND trade_date >= DATE_SUB(%s, INTERVAL 252 DAY)
                  AND ts_code IN ({pl}) GROUP BY ts_code) d2
            ON d1.ts_code = d2.ts_code
            WHERE d1.trade_date = %s AND d1.ts_code IN ({pl})""",
            tuple([latest]*2 + codes + [latest] + codes))
        for r in cur.fetchall():
            pos_map[r[0]] = float(r[1]) if r[1] is not None and 0 <= float(r[1]) <= 1 else 0.5

        for i, entry in enumerate(ranked[:top_n], 1):
            if isinstance(entry, dict):
                tc = entry["ts_code"]
                sc = entry["ml_score"]
                name = entry.get("stock_name", "")
                industry = entry.get("industry", "")
                price = entry.get("price", 0)
                if not name or name == "?":
                    cur.execute("SELECT name, industry FROM stock_info WHERE ts_code=%s", (tc,))
                    ri = cur.fetchone()
                    name = ri[0] if ri else "?"
                    industry = ri[1] if ri and ri[1] else ""
            else:
                tc, sc = entry[0], entry[1]
                cur.execute("SELECT name, industry FROM stock_info WHERE ts_code=%s", (tc,))
                r = cur.fetchone()
                name = r[0] if r else "?"
                industry = r[1] if r and r[1] else ""
                price = 0

            cur.execute("SELECT close, pct_chg, turnover_rate, volume_ratio FROM daily_price WHERE ts_code=%s AND trade_date=%s", (tc, latest))
            dr = cur.fetchone()
            if dr:
                if not price: price = float(dr[0])
                pct = float(dr[1] or 0)
                turn = float(dr[2] or 0)
                vr = float(dr[3] or 0)
            else:
                pct = turn = vr = 0

            pos_52w = pos_map.get(tc, 0.5)
            chg_20d = 0
            is_extended = pos_52w >= 0.8
            # 短持有信号：如果位置高，用2日持有策略
            strategy_signal = "2日短持" if is_extended else "5日持有"

            recommendations.append({
                "代码": tc.split('.')[0],
                "名称": name,
                "行业": industry,
                "现价": price,
                "涨跌幅": f"{pct:+.2f}%",
                "ML得分": f"{sc:.4f}",
                "排序强度": round(sc * 100, 0),
                "换手率": turn,
                "量比": vr,
                "策略来源": f"{model_ver} 纯ML Top{top_n}",
                "止损价": round(price * 0.93, 2),
                "52周位置": round(pos_52w, 2),
                "20日涨幅": f"{chg_20d*100:.1f}%",
                "持仓建议": strategy_signal,
                "风险提示": "⚠️ 短期涨幅已大，注意回调风险" if is_extended else "位置合理",
            })

        cur.close()
        conn.close()

        today_str = latest
        result = {
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "策略": f"OOS-v2 纯ML Top{top_n}",
            "推荐股票": recommendations,
            "cache_date": today_str,
        }

        # 清理 NaN/Inf 值（JSON 序列化兼容）
        import math as _math
        def _fix_val(v):
            if isinstance(v, dict):
                return {k: _fix_val(vk) for k, vk in v.items()}
            if isinstance(v, (list, tuple)):
                return [_fix_val(x) for x in v]
            if isinstance(v, float):
                if _math.isnan(v) or _math.isinf(v):
                    return 0.0
                return float(v)
            return v
        result = _fix_val(result)

        with open(cache_file, 'w') as f:
            json.dump({"cache_time": time.time(), "data": result}, f, ensure_ascii=False, allow_nan=False)

        return result
    except Exception as e:
        logger.error(f"建仓推荐失败: {e}")
        return {"error": str(e)}

@router.get("/api/recommend/strong")
def get_recommend_strong(token: str = Cookie(None)):
    """
    建仓推荐 - 强势活跃策略版（直接从 MySQL 读取，无缓存）
    """
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")

    try:
        cache_file = os.path.join(DATA_DIR, "recommend_strong_cache.json")

        from datetime import datetime as dt

        import pymysql

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

        cursor.execute(
            """
            SELECT snap_date, ts_code, name, industry, price, change_pct,
                   turnover_rate, vol_ratio, quick_score, entry_reason, today_rank
            FROM stock_pool_snap
            WHERE snap_date = %s
            ORDER BY quick_score DESC, today_rank ASC
            LIMIT 100
        """,
            (latest_date,),
        )
        rows = cursor.fetchall()

        stocks = []
        for r in rows:
            code = r[1].split(".")[0]
            mkt = "sz" if r[1].endswith(".SZ") else "sh"
            pct = float(r[5]) if r[5] else 0
            stocks.append(
                {
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
                }
            )

        # 批量查询3个交易日前的收盘价
        if stocks:
            today = dt.now().strftime("%Y-%m-%d")
            cursor3 = conn.cursor()
            cursor3.execute(
                """
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date <= %s
                ORDER BY trade_date DESC LIMIT 4
            """,
                (today,),
            )
            dates_3d = [str(r[0]) for r in cursor3.fetchall()]
            date_3d_ago = dates_3d[3] if len(dates_3d) > 3 else dates_3d[-1]

            ts_codes = [s.get("代码", "").upper() + "." + s.get("交易所", "").upper() for s in stocks]
            close_map = {}
            if ts_codes:
                placeholders = ",".join(["%s"] * len(ts_codes))
                cursor3.execute(
                    f"""
                    SELECT ts_code, close FROM daily_price
                    WHERE trade_date = %s AND ts_code IN ({placeholders})
                """,
                    (date_3d_ago, *ts_codes),
                )
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
                s["ml概率"] = 0.5
                s["增强评分"] = s.get("评分", 0)
                s["热点板块"] = ""
                s["资金趋势"] = ""

        cursor.close()
        conn.close()

        if len(stocks) < 3:
            return {"error": f"股票池只有{len(stocks)}只", "recommendations": []}

        primary_thresh = 0.55
        fallback1 = primary_thresh - 0.05
        qualified = [s for s in stocks if s.get("ml概率", 0) >= primary_thresh]
        if not qualified:
            qualified = [s for s in stocks if s.get("ml概率", 0) >= fallback1]
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
                ml_prob = float(stock.get("ml概率", 0.5))

                if vr >= 3.0:
                    vr_score = 30
                elif vr >= 2.0:
                    vr_score = 25
                elif vr >= 1.5:
                    vr_score = 20
                elif vr >= 1.0:
                    vr_score = 15
                else:
                    vr_score = 5

                if 3 <= turnover <= 8:
                    tu_score = 20
                elif 8 < turnover <= 12:
                    tu_score = 15
                elif 1.5 <= turnover < 3:
                    tu_score = 10
                else:
                    tu_score = 5

                if 0 <= change <= 3:
                    chg_score = 25
                elif -3 <= change < 0:
                    chg_score = 20
                elif 3 < change <= 6:
                    chg_score = 15
                else:
                    chg_score = 5

                if chg3 > 3:
                    chg3_score = 10
                elif 0 < chg3 <= 3:
                    chg3_score = 8
                elif -3 <= chg3 <= 0:
                    chg3_score = 5
                else:
                    chg3_score = 2

                ml_score_part = int(ml_prob * 15)
                base_score_part = min(5, base_score // 20)
                total_score = vr_score + tu_score + chg_score + chg3_score + ml_score_part + base_score_part

                if name.startswith("*ST") or name.startswith("ST"):
                    continue

                stop_loss = round(close * 0.95, 2)
                target = round(close * 1.08, 2)

                if ml_prob >= 0.65:
                    signal = "强"
                elif ml_prob >= 0.55:
                    signal = "中"
                else:
                    signal = "弱"

                scored.append(
                    {
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
                    }
                )
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
            with open(cache_file, "w") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"缓存写入失败: {e}")

        return result
    except Exception as e:
        logger.error(f"强势推荐失败: {e}")
        return {"error": str(e), "recommendations": []}


# ========== V5.0 API ==========


@router.get("/api/v5_recommend")
def get_v5_recommend(token: str = Cookie(None)):
    if not get_current_user(token):
        raise HTTPException(status_code=401, detail="未登录")
    try:
        v5_file = os.path.join(DATA_DIR, "sector_v5_stocks.json")
        if not os.path.exists(v5_file):
            return {"error": "V5.0 股票池为空"}
        with open(v5_file, encoding="utf-8") as f:
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
        return {
            "scan_date": v5_data.get("scan_date", ""),
            "scan_time": v5_data.get("scan_time", ""),
            "total_stocks": v5_data.get("total_candidates", 0),
            "top3_stocks": top3,
            "strategy": "V5.0 板块趋势",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/recommend/v11")
def get_recommend_v11(force_refresh: bool = False, top_n: int = 3):
    """
    建仓推荐 - OOS-v2 纯ML推理（直接推理，不走策略服务管线）
    成交额前500只 → OOS-v2 30特征 → 集成预测 → Top3
    """
    cache_file = os.path.join(DATA_DIR, "recommend_v11_cache.json")

    if not force_refresh:
        try:
            if os.path.exists(cache_file):
                with open(cache_file) as f:
                    cached = json.load(f)
                cache_valid = (
                    cached.get("cache_time")
                    and time.time() - cached["cache_time"] < RECOMMEND_CACHE_TTL
                    and cached.get("data", {}).get("cache_date") == date.today().strftime("%Y-%m-%d")
                )
                if cache_valid:
                    data = cached.get("data")
                    # 清理缓存中的 NaN
                    def _fix_cache(v):
                        if isinstance(v, dict): return {k: _fix_cache(vk) for k, vk in v.items()}
                        if isinstance(v, (list, tuple)): return [_fix_cache(x) for x in v]
                        if isinstance(v, float):
                            return 0.0 if math.isnan(v) or math.isinf(v) else v
                        return v
                    return _fix_cache(data)
        except: pass

    try:
        import pymysql

        from quant_app.utils.config import get_db_config
        from scripts.predict_v11_oos import build_features as oos_build
        from scripts.predict_v11_oos import ensemble_predict as oos_predict
        from scripts.predict_v11_oos import load_model as oos_load

        db_config = get_db_config()
        conn = pymysql.connect(**db_config)
        cur = conn.cursor()

        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = str(cur.fetchone()[0])
        cur.execute("SELECT MAX(trade_date) FROM daily_price WHERE trade_date < %s", (latest,))
        prev = str(cur.fetchone()[0])

        cur.execute("SELECT ts_code FROM daily_price WHERE trade_date=%s AND LEFT(ts_code,1) NOT IN ('8','4','9') AND close<=200 ORDER BY amount DESC LIMIT 500", (prev,))
        codes = [r[0] for r in cur.fetchall()]

        oos_path = os.path.join(DATA_DIR, "ml_stock_model_v11_0_oos_v2.pkl")
        if not os.path.exists(oos_path):
            return {"error": "OOS模型不存在", "stocks": [], "cache_date": latest}
        oos_load(oos_path)
        feat = oos_build(conn, codes, as_of_date=latest)
        if feat is None or feat.empty:
            return {"error": "特征构建失败", "stocks": [], "cache_date": latest}
        preds = oos_predict(feat)
        ca = feat['ts_code'].tolist()
        ranked = sorted(zip(ca, preds), key=lambda x: -x[1])

        recommendations = []
        for i, (tc, sc) in enumerate(ranked[:top_n], 1):
            cur.execute("SELECT name, industry FROM stock_info WHERE ts_code=%s", (tc,))
            r = cur.fetchone()
            name = r[0] if r and r[0] else tc.split('.')[0]
            industry = r[1] if r and r[1] else ''
            cur.execute("SELECT close, pct_chg FROM daily_price WHERE ts_code=%s AND trade_date=%s", (tc, latest))
            dr = cur.fetchone()
            price = float(dr[0]) if dr else 0
            pct = float(dr[1] or 0) if dr else 0

            recommendations.append({
                "代码": tc.split('.')[0],
                "名称": name,
                "行业": industry,
                "现价": price,
                "涨跌幅": f"{pct:+.2f}%",
                "ML得分": round(sc, 4),
                "策略来源": "OOS-v2 纯ML Top3",
                "止损价": round(price * 0.93, 2),
            })

        cur.close()
        conn.close()

        result = {
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "策略": "OOS-v2 纯ML直接推理 Top3",
            "推荐股票": recommendations,
            "cache_date": latest,
        }

        with open(cache_file, 'w') as f:
            json.dump({"cache_time": time.time(), "data": result}, f)

        return result
    except Exception as e:
        logger.error(f"OOS-v2推荐失败: {e}")
        return {"error": str(e)}
