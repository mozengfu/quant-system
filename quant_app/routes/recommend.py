"""
建仓推荐 API 路由
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException

from app_core import (
    get_current_user,
    get_stock_realtime,
    record_recommendation,
)

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
def get_recommend(force_refresh: bool = False, token: str = Cookie(None)):
    """
    建仓推荐 - ML选股策略（2026-05-16起）
    V11.0堆叠集成模型 + V4初筛混合选Top3，V4候选池30只 → V11.0 ML排序 → 混合评分
    有30分钟缓存，设置 force_refresh=true 强制刷新
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

        from quant_app.services.strategy_service import generate_v4_ml_top5
        from quant_app.utils.config import get_db_config
        db_config = get_db_config(connect_timeout=3)
        conn = pymysql.connect(**db_config)

        recommendations = []
        top3 = generate_v4_ml_top5(conn, top_n=3)
        conn.close()

        if top3:
            for s in top3:
                ml_pct = s.get('ml_percentile', 0.5)
                ml_forward = 1.0 - ml_pct  # rank_pct越小越好，反转为正向
                ml_prob = s.get('ml_probability', 0.5)
                price = float(s.get('close', s.get('price', 0)))
                recommendations.append({
                    '代码': s['ts_code'].split('.')[0],
                    '名称': s['name'],
                    '行业': s.get('industry', ''),
                    '现价': price,
                    '涨跌幅': f"{s.get('pct_chg', 0):+.2f}%",
                    '评分': int(s.get('blended_score', s.get('v4_score', 0))),
                    '综合评分': int(s.get('blended_score', s.get('v4_score', 0))),
                    'ML得分': f"{s.get('ml_score', 0):.3f}",
                    '排序强度': round(ml_forward * 100, 0),
                    'ml概率': round(ml_prob, 4),
                    '量比': float(s.get('volume_ratio', 0)),
                    '换手率': float(s.get('turnover_rate', 0)),
                    '入选理由': ' | '.join(s.get('reasons', [])),
                    '策略来源': 'V11.0 纯ML选股',
                    '止损价': round(price * 0.95, 2),
                })

        today_str = top3[0].get('date', datetime.now().strftime('%Y-%m-%d')) if top3 else datetime.now().strftime('%Y-%m-%d')

        if not recommendations:
            return {
                "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "策略": "V11.0 Pure ML TOP3" if __import__("os").environ.get("PURE_ML", "0") == "1" else "V11.0 V4+ML混合 TOP3",
                "推荐股票": [],
                "cache_date": today_str,
                "error": "无符合条件的股票"
            }

        result = {
            "扫描时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "策略": "ML选股策略 TOP3",
            "推荐股票": recommendations,
            "cache_date": today_str,
        }

        if recommendations:
            try:
                record_recommendation(recommendations, "ML选股策略 TOP3")
                logger.info(f"推荐股票已记录: {[s['名称'] for s in recommendations]}")
            except Exception as e:
                logger.warning(f"记录推荐失败: {e}")

        # 写入缓存
        _save_recommend_cache(result)
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
        with open(v5_file, encoding='utf-8') as f:
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
