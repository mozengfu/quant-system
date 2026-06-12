"""
管理后台路由模块 - 日志、用户审核管理
"""

import json
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie

from quant_app.routes.auth import (
    _approve_user_in_db,
    _load_pending_users,
    _load_users,
    _reject_user_in_db,
    get_current_user,
)
from quant_app.services.notification_service import send_email, send_feishu
from quant_app.utils.authz import is_admin, require_admin
from quant_app.utils.config import get_db_config
from quant_app.utils.persistence import ACCESS_LOG_FILE, _classify_module

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/access_log")
def get_access_log(token: str = Cookie(None)):
    user = get_current_user(token)
    require_admin(user)
    try:
        if not ACCESS_LOG_FILE.exists():
            return {"logs": []}
        return {"logs": json.loads(ACCESS_LOG_FILE.read_text())}
    except Exception:
        return {"logs": []}


@router.get("/log_stats")
def get_log_stats(days: int = 7, username: str = "", module: str = "", action: str = "", token: str = Cookie(None)):
    """获取日志统计数据（从 MySQL 查询），支持字段筛选"""
    user = get_current_user(token)
    require_admin(user)
    try:
        import pymysql

        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        where_clauses = ["timestamp >= %s"]
        params = [cutoff]
        if username:
            where_clauses.append("username LIKE %s")
            params.append(f"%{username}%")
        if module:
            where_clauses.append("module = %s")
            params.append(module)
        if action:
            where_clauses.append("action LIKE %s")
            params.append(f"%{action}%")
        where_sql = " AND ".join(where_clauses)

        cursor.execute(
            f"SELECT DATE(timestamp) as date, COUNT(*) as count FROM system_logs WHERE {where_sql} GROUP BY DATE(timestamp) ORDER BY date",
            params,
        )
        daily_counts = cursor.fetchall()

        cursor.execute(
            f"SELECT username, COUNT(*) as count FROM system_logs WHERE {where_sql} GROUP BY username ORDER BY count DESC LIMIT 10",
            params,
        )
        top_users = cursor.fetchall()

        cursor.execute(
            f"SELECT action, COUNT(*) as count FROM system_logs WHERE {where_sql} GROUP BY action ORDER BY count DESC LIMIT 20",
            params,
        )
        action_counts = cursor.fetchall()

        cursor.execute(
            f"SELECT module, COUNT(*) as count FROM system_logs WHERE {where_sql} GROUP BY module ORDER BY count DESC",
            params,
        )
        module_counts = cursor.fetchall()

        cursor.execute(
            f"SELECT * FROM system_logs WHERE {where_sql} ORDER BY timestamp DESC LIMIT 50",
            params,
        )
        recent_logs = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) as total FROM system_logs WHERE timestamp >= %s", (cutoff,))
        total = cursor.fetchone()["total"]

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) as c FROM system_logs WHERE DATE(timestamp) = %s", (today,))
        today_count = cursor.fetchone()["c"]
        cursor.execute("SELECT COUNT(*) as c FROM system_logs WHERE DATE(timestamp) = %s", (yesterday,))
        yesterday_count = cursor.fetchone()["c"]

        cursor.execute(
            "SELECT COUNT(DISTINCT username) as uc FROM system_logs WHERE timestamp >= %s",
            (cutoff,),
        )
        unique_users = cursor.fetchone()["uc"]

        cursor.execute("SELECT DISTINCT username FROM system_logs ORDER BY username")
        user_options = [r["username"] for r in cursor.fetchall()]

        cursor.execute("SELECT DISTINCT action FROM system_logs ORDER BY action")
        action_options = [r["action"] for r in cursor.fetchall()]

        conn.close()

        return {
            "daily_counts": [{"date": str(r["date"]), "count": r["count"]} for r in daily_counts],
            "top_users": [{"username": r["username"], "count": r["count"]} for r in top_users],
            "action_counts": [{"action": r["action"], "count": r["count"]} for r in action_counts],
            "module_counts": [{"module": r["module"], "count": r["count"]} for r in module_counts],
            "recent_logs": recent_logs,
            "total": total,
            "today_count": today_count,
            "yesterday_count": yesterday_count,
            "unique_users": unique_users,
            "user_options": user_options,
            "action_options": action_options,
        }
    except Exception:
        logger.exception("获取日志统计失败")
        return {
            "error": "内部错误",
            "daily_counts": [],
            "top_users": [],
            "action_counts": [],
            "module_counts": [],
            "recent_logs": [],
            "total": 0,
            "today_count": 0,
            "yesterday_count": 0,
            "unique_users": 0,
        }


@router.post("/log_import")
def import_logs_from_json(token: str = Cookie(None)):
    """从 access_log.json 导入历史数据到 MySQL（一次性迁移）"""
    user = get_current_user(token)
    require_admin(user)
    try:
        if not ACCESS_LOG_FILE.exists():
            return {"status": "no file", "imported": 0}
        logs = json.loads(ACCESS_LOG_FILE.read_text())
        if not logs:
            return {"status": "empty", "imported": 0}

        import pymysql

        conn = pymysql.connect(**get_db_config())
        cursor = conn.cursor()
        imported = 0
        for log in logs:
            try:
                module = _classify_module(log.get("action", ""))
                cursor.execute(
                    "INSERT INTO system_logs (username, ip, action, module, timestamp) VALUES (%s, %s, %s, %s, %s)",
                    (
                        log.get("username", "unknown"),
                        log.get("ip", "unknown"),
                        log.get("action", "unknown"),
                        module,
                        log.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    ),
                )
                imported += 1
            except Exception as _e:
                logger.debug(f"跳过日志条目: {_e}")
        conn.commit()
        conn.close()
        return {"status": "ok", "imported": imported}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/users")
def get_all_users(token: str = Cookie(None)):
    user = get_current_user(token)
    require_admin(user)
    users = _load_users()

    formatted_users = {}
    for username, user_data in users.items():
        expire_date = user_data.get("expire_date", "")
        created_at = user_data.get("created_at", "")

        if is_admin(username):
            expire_date = ""

        formatted_users[username] = {
            "email": user_data.get("email", "-"),
            "register_time": created_at or "-",
            "expire_date": expire_date,
        }

    return {"users": formatted_users}


@router.get("/pending")
def get_pending_users(token: str = Cookie(None)):
    """获取待审核用户列表（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)
    pending = _load_pending_users()
    return {"pending": pending}


@router.post("/approve")
def approve_user(data: dict, token: str = Cookie(None)):
    """审核通过用户（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)

    username = data.get("username", "").strip()
    duration = data.get("duration", 365)
    pending = _load_pending_users()

    if username not in pending:
        return {"error": "用户不在待审核列表"}

    user_info = pending[username]
    expire_date = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d")
    _approve_user_in_db(username, expire_date)

    try:
        approve_msg = "✅ 用户审核通过\n\n用户名：{}\n邮箱：{}\n\n该用户现在可以登录系统了".format(
            username, user_info.get("email", "未知")
        )
        send_feishu(approve_msg)
    except Exception as e:
        logger.warning("发送审核通过通知失败: %s", e)

    user_email = user_info.get("email", "")
    if user_email:
        try:
            email_subject = "【智能量化系统】您的账户已审核通过"
            email_content = f"尊敬的用户 {username}，您好！\n\n恭喜您！您的账户已通过审核，现在可以登录智能量化系统了。\n\n登录地址：https://lh.mozengfu.com.cn\n用户名：{username}\n\n如有任何问题，请联系管理员：259563977@qq.com\n\n祝您投资顺利！\n智能量化系统\n"
            send_email(user_email, email_subject, email_content)
        except Exception as e:
            logger.warning("发送用户邮件通知失败: %s", e)

    return {"message": f"用户 {username} 审核通过"}


@router.post("/reject")
def reject_user(data: dict, token: str = Cookie(None)):
    """拒绝用户（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)

    username = data.get("username", "").strip()
    _reject_user_in_db(username)
    return {"message": f"用户 {username} 已拒绝"}
