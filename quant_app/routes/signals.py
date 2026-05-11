# -*- coding: utf-8 -*-
"""
交易信号 CRUD API 路由
"""
import os, json, time, logging, sys
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Cookie, Request as FastAPIRequest, HTTPException
from app_core import (
    get_current_user, save_access_log, get_client_ip,
    generate_order_id, add_to_positions, sync_positions,
)
from quant_app.utils.authz import require_admin

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"

router = APIRouter(tags=["signals"])


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
