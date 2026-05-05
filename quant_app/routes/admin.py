"""
管理后台路由模块 - 日志、用户审核管理
"""
import json
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Cookie, HTTPException
from quant_app.utils.authz import require_admin, is_admin
from app_core import (
    load_users, save_users, save_access_log,
    load_pending_users, save_pending_users,
    get_current_user, require_auth, _classify_module,
)
from quant_app.utils.persistence import ACCESS_LOG_FILE
from quant_app.utils.config import get_db_config
from quant_app.services.notification_service import send_email, send_feishu
from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


@router.get("/api/admin/access_log")
async def get_access_log(token: str = Cookie(None)):
    user = get_current_user(token)
    require_admin(user)
    try:
        if not ACCESS_LOG_FILE.exists():
            return {"logs": []}
        return {"logs": json.loads(ACCESS_LOG_FILE.read_text())}
    except Exception:
        return {"logs": []}


@router.get("/api/admin/log_stats")
async def get_log_stats(days: int = 7, username: str = "", module: str = "", action: str = "", token: str = Cookie(None)):
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
    except Exception as e:
        return {"error": str(e), "daily_counts": [], "top_users": [], "action_counts": [],
                "module_counts": [], "recent_logs": [], "total": 0,
                "today_count": 0, "yesterday_count": 0, "unique_users": 0}


@router.post("/api/admin/log_import")
async def import_logs_from_json(token: str = Cookie(None)):
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
                    (log.get("username", "unknown"), log.get("ip", "unknown"),
                     log.get("action", "unknown"), module,
                     log.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
                )
                imported += 1
            except Exception as _e:
                logger.debug(f"跳过日志条目: {_e}")
        conn.commit()
        conn.close()
        return {"status": "ok", "imported": imported}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/api/admin/users")
async def get_all_users(token: str = Cookie(None)):
    user = get_current_user(token)
    require_admin(user)
    users = load_users()
    pending = load_pending_users()

    formatted_users = {}
    for username, user_data in users.items():
        if isinstance(user_data, dict):
            pending_info = pending.get(username, {})
            expire_date = user_data.get("expire_date", "")
            register_time = user_data.get("register_time", "") or pending_info.get("register_time", "")

            if is_admin(username):
                expire_date = ""
            elif not expire_date and not register_time:
                try:
                    # 使用 users.json 文件修改时间作为参考
                    base_dir = Path(__file__).parent.parent.parent
                    users_file = base_dir / "data" / "users.json"
                    if users_file.exists():
                        mtime = users_file.stat().st_mtime
                        reg_dt = datetime.fromtimestamp(mtime)
                        expire_dt = reg_dt + timedelta(days=365)
                        expire_date = expire_dt.strftime("%Y-%m-%d")
                        register_time = reg_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as _e:
                    logger.error(f"Error in admin: {_e}")
            elif not expire_date and register_time:
                try:
                    reg_dt = datetime.strptime(register_time, "%Y-%m-%d %H:%M:%S")
                    expire_dt = reg_dt + timedelta(days=365)
                    expire_date = expire_dt.strftime("%Y-%m-%d")
                except Exception as _e:
                    logger.error(f"Error in admin: {_e}")

            formatted_users[username] = {
                "email": user_data.get("email", pending_info.get("email", "-")) or "-",
                "phone": user_data.get("phone", pending_info.get("phone", "-")) or "-",
                "register_time": register_time if register_time else "-",
                "expire_date": expire_date,
            }
        else:
            pending_info = pending.get(username, {})
            expire_date = ""
            register_time = pending_info.get("register_time", "")

            if is_admin(username):
                expire_date = ""
            elif register_time:
                try:
                    reg_dt = datetime.strptime(register_time, "%Y-%m-%d %H:%M:%S")
                    expire_dt = reg_dt + timedelta(days=365)
                    expire_date = expire_dt.strftime("%Y-%m-%d")
                except Exception:
                    expire_date = ""
            else:
                try:
                    base_dir = Path(__file__).parent.parent.parent
                    users_file = base_dir / "data" / "users.json"
                    if users_file.exists():
                        mtime = users_file.stat().st_mtime
                        reg_dt = datetime.fromtimestamp(mtime)
                        expire_dt = reg_dt + timedelta(days=365)
                        expire_date = expire_dt.strftime("%Y-%m-%d")
                        register_time = reg_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    expire_date = ""

            formatted_users[username] = {
                "email": pending_info.get("email", "-"),
                "phone": pending_info.get("phone", "-"),
                "register_time": pending_info.get("register_time", register_time if register_time else "-"),
                "expire_date": pending_info.get("expire_date", expire_date),
            }

    return {"users": formatted_users}


@router.get("/api/admin/pending")
async def get_pending_users(token: str = Cookie(None)):
    """获取待审核用户列表（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)
    pending = load_pending_users()
    return {"pending": pending}


@router.post("/api/admin/approve")
async def approve_user(data: dict, token: str = Cookie(None)):
    """审核通过用户（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)

    username = data.get("username", "").strip()
    duration = data.get("duration", 365)
    pending = load_pending_users()

    if username not in pending:
        return {"error": "用户不在待审核列表"}

    users = load_users()
    pending_user = pending[username]
    if is_admin(username):
        user_data = pending_user["password_hash"]
    else:
        from datetime import datetime, timedelta
        expire_date = (datetime.now() + timedelta(days=duration)).strftime("%Y-%m-%d")
        user_data = {
            "password_hash": pending_user["password_hash"],
            "email": pending_user.get("email", ""),
            "phone": pending_user.get("phone", ""),
            "register_time": pending_user.get("register_time", ""),
            "expire_date": expire_date,
            "duration_days": duration,
        }
    users[username] = user_data
    save_users(users)

    user_info = pending[username]
    del pending[username]
    save_pending_users(pending)

    try:
        approve_msg = f"✅ 用户审核通过\n\n用户名：{username}\n邮箱：{user_info.get('email', '未知')}\n手机号：{user_info.get('phone', '未填写')}\n注册时间：{user_info.get('register_time', '未知')}\n\n该用户现在可以登录系统了"
        send_feishu(approve_msg)
    except Exception as e:
        logger.warning(f"发送审核通过通知失败: {e}")

    user_email = user_info.get("email", "")
    if user_email:
        try:
            email_subject = "【智能量化系统】您的账户已审核通过"
            email_content = f"""尊敬的用户 {username}，您好！

恭喜您！您的账户已通过审核，现在可以登录智能量化系统了。

登录地址：https://lh.mozengfu.com.cn
用户名：{username}

如有任何问题，请联系管理员：259563977@qq.com

祝您投资顺利！
智能量化系统
"""
            send_email(user_email, email_subject, email_content)
        except Exception as e:
            logger.warning(f"发送用户邮件通知失败: {e}")

    user_phone = user_info.get("phone", "")
    if user_phone:
        try:
            expire_date = users[username].get("expire_date", "")
            sms_content = f"【量化系统】账户已审核通过！登录：lh.mozengfu.com.cn 用户名：{username} 有效期至：{expire_date}"
            script = f'''osascript -e 'tell application "Messages" to send "{sms_content}" to buddy "{user_phone}"' '''
            logger.info(f"请手动执行以下命令发送短信：{script}")

            from quant_app.utils.config import DATA_DIR
            sms_queue_file = DATA_DIR / "sms_queue.txt"
            with open(sms_queue_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {user_phone} - {sms_content}\n")
                f.write(f"执行命令：{script}\n")
                f.write("-" * 50 + "\n")
        except Exception as e:
            logger.warning(f"短信队列记录失败: {e}")

    return {"message": f"用户 {username} 审核通过"}


@router.post("/api/admin/reject")
async def reject_user(data: dict, token: str = Cookie(None)):
    """拒绝用户（仅管理员）"""
    user = get_current_user(token)
    require_admin(user)

    username = data.get("username", "").strip()
    pending = load_pending_users()

    if username in pending:
        del pending[username]
        save_pending_users(pending)

    return {"message": f"用户 {username} 已拒绝"}
