"""
认证路由模块 - 用户注册、登录、密码管理
用户数据存 MySQL users 表，会话和重置令牌为纯内存。
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Cookie, HTTPException
from fastapi import Request as FastAPIRequest
from pydantic import BaseModel, Field

from quant_app.services.notification_service import send_email, send_feishu
from quant_app.utils.auth import hash_pw, make_token, verify_pw
from quant_app.utils.authz import is_admin
from quant_app.utils.config import config
from quant_app.utils.persistence import generate_order_id, get_client_ip, save_access_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 登录频率限制：每IP 5次/分钟
_LOGIN_ATTEMPTS: dict[str, list] = {}
_LOGIN_LOCK = threading.Lock()

_LOGIN_ATTEMPTS_MAX_KEYS = 10000  # 防止字典无限增长的硬上限


def _check_login_rate(ip: str) -> bool:
    """检查IP是否超过登录频率限制，返回True表示允许。
    防御性：定期清理长时间无活动的IP，避免字典无限增长。"""
    now = time.time()
    window = 60
    max_attempts = 5
    with _LOGIN_LOCK:
        # 清理过期 IP（窗口外无任何尝试的记录），字典超上限时强制清理
        if len(_LOGIN_ATTEMPTS) > _LOGIN_ATTEMPTS_MAX_KEYS:
            stale = [k for k, v in _LOGIN_ATTEMPTS.items() if not v or now - v[-1] >= window]
            for k in stale:
                _LOGIN_ATTEMPTS.pop(k, None)
            # 仍超限时，按最旧时间淘汰 20%
            if len(_LOGIN_ATTEMPTS) > _LOGIN_ATTEMPTS_MAX_KEYS:
                sorted_keys = sorted(_LOGIN_ATTEMPTS, key=lambda k: _LOGIN_ATTEMPTS[k][-1] if _LOGIN_ATTEMPTS[k] else 0)
                for k in sorted_keys[: len(sorted_keys) // 5]:
                    _LOGIN_ATTEMPTS.pop(k, None)
        attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < window]
        _LOGIN_ATTEMPTS[ip] = attempts
        if len(attempts) >= max_attempts:
            return False
        attempts.append(now)
        return True


# ========== MySQL 用户操作辅助函数 ==========


def _db_conn():
    import pymysql

    return pymysql.connect(**config.mysql.get_connection_params())


def _load_users() -> dict:
    """从 MySQL 读取所有已审批用户，返回 {username: user_dict}"""
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT username, password_hash, email, role, status, expire_date, created_at "
            "FROM users WHERE status = 'approved'"
        )
        users = {}
        for row in cur.fetchall():
            users[row[0]] = {
                "password_hash": row[1],
                "email": row[2] or "",
                "role": row[3],
                "status": row[4],
                "expire_date": str(row[5])[:10] if row[5] else "",
                "created_at": str(row[6])[:10] if row[6] else "",
            }
        cur.close()
        return users
    finally:
        conn.close()


def _load_pending_users() -> dict:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT username, password_hash, email, role, status, expire_date, created_at "
            "FROM users WHERE status = 'pending'"
        )
        pending = {}
        for row in cur.fetchall():
            pending[row[0]] = {
                "password_hash": row[1],
                "email": row[2] or "",
                "role": row[3],
                "status": row[4],
                "expire_date": str(row[5])[:10] if row[5] else "",
                "created_at": str(row[6])[:10] if row[6] else "",
            }
        cur.close()
        return pending
    finally:
        conn.close()


def _save_user(username: str, password_hash: str, email: str = "", status: str = "pending", role: str = "user"):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (username, password_hash, email, role, status) VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE password_hash = VALUES(password_hash), email = VALUES(email)",
            (username, password_hash, email, role, status),
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _approve_user_in_db(username: str, expire_date: str = None):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        if expire_date:
            cur.execute(
                "UPDATE users SET status = 'approved', expire_date = %s WHERE username = %s",
                (expire_date, username),
            )
        else:
            cur.execute("UPDATE users SET status = 'approved' WHERE username = %s", (username,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _reject_user_in_db(username: str):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE username = %s AND status = 'pending'", (username,))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _update_user_status(username: str, status: str):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET status = %s WHERE username = %s", (status, username))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _update_user_password(username: str, password_hash: str):
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE username = %s", (password_hash, username))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _user_exists(username: str) -> bool:
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        exists = cur.fetchone() is not None
        cur.close()
        return exists
    finally:
        conn.close()


# ========== 会话管理（纯内存，重启需重新登录）==========

SESSIONS: dict = {}
sessions_lock = threading.Lock()
logger.info("会话管理初始化完成（纯内存模式）")


def get_current_user(token: str = None):
    with sessions_lock:
        if not token or token not in SESSIONS:
            return None
        if SESSIONS[token].get("expires", 0) < time.time():
            del SESSIONS[token]
            return None
        return SESSIONS[token]["username"]


def require_auth(token: str = None):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# ========== 密码重置令牌（纯内存）==========

RESET_TOKENS: dict = {}


# ========== 支付订单存储 ==========
PAYMENT_ORDERS = {}  # {order_id: {username, email, time, verified}}


# ========== Pydantic 验证模型 ==========

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6)
    email: str = Field(..., min_length=5)
    phone: str = ""
    pay_remark: str = ""
    pay_wxid: str = ""
    pay_amount: int = 99

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)

class ForgotPasswordRequest(BaseModel):
    username: str = Field(..., min_length=1)
    email: str = Field(..., min_length=5)

class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)

class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6)

# ========== 认证路由 ==========


@router.post("/register")
def register(data: RegisterRequest):
    username = data.username.strip()
    password = data.password
    email = data.email.strip()
    phone = data.phone.strip()
    pay_remark = data.pay_remark.strip()
    pay_wxid = data.pay_wxid.strip()

    if not username.isalnum() and "_" not in username:
        return {"error": "用户名只能为英文字母、数字或下划线"}

    users = _load_users()
    if username in users:
        return {"error": "用户名已存在"}

    pending = _load_pending_users()
    if username in pending:
        return {"error": "该用户名已提交审核，请耐心等待"}

    pay_amount = data.pay_amount

    if pay_amount == 1:
        valid_days = 1
        package_name = "试用1天"
    elif pay_amount == 99:
        valid_days = 365
        package_name = "包年365天"
    else:
        valid_days = 365
        package_name = "包年365天"

    expire_date = (datetime.now() + timedelta(days=valid_days)).strftime("%Y-%m-%d")

    _save_user(username, hash_pw(password), email=email, status="pending")
    pending[username] = {
        "email": email,
        "phone": phone,
        "pay_remark": pay_remark,
        "pay_wxid": pay_wxid,
        "pay_amount": pay_amount,
        "package_name": package_name,
        "valid_days": valid_days,
        "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expire_date": expire_date,
    }

    try:
        package_info = f"{package_name} (¥{pay_amount})"
        admin_msg = f"🆕 新用户注册待审核\n\n用户名：{username}\n邮箱：{email}\n手机号：{phone or '未填写'}\n套餐：{package_info}\n有效期至：{expire_date}\n付款备注：{pay_remark}\n微信号后4位：{pay_wxid}\n注册时间：{pending[username]['register_time']}\n\n请核对收款后审核通过"
        send_feishu(admin_msg)
    except Exception as e:
        logger.warning(f"发送审核通知失败: {e}")

    return {"message": "提交成功，请完成支付后等待审核"}


@router.post("/login")
def do_login(request: FastAPIRequest, data: LoginRequest):
    username = data.username.strip()
    password = data.password

    ip = get_client_ip(request)
    if not _check_login_rate(ip):
        return {"error": "登录尝试过于频繁，请1分钟后再试"}

    pending = _load_pending_users()
    if username in pending:
        return {"error": "账户正在审核中，请等待管理员审核通过"}

    users = _load_users()
    if username not in users:
        return {"error": "用户名或密码错误"}

    user_data = users[username]
    verified, new_hash = verify_pw(password, user_data.get("password_hash", ""))
    if not verified:
        return {"error": "用户名或密码错误"}
    if new_hash:
        _update_user_password(username, new_hash)
    # 所有人（含管理员）都要检查账户过期 — 防止离职/泄露凭据
    expire_date = user_data.get("expire_date", "")
    if expire_date:
        try:
            expire = datetime.strptime(expire_date, "%Y-%m-%d")
            if datetime.now() > expire:
                return {"error": f"您的账户已于 {expire_date} 过期，请联系管理员续费：259563977@qq.com"}
        except Exception as _e:
            logger.warning("expire_date parse error: %s", _e)

    token = make_token(username)
    expires = time.time() + 86400 * 7
    with sessions_lock:
        SESSIONS[token] = {"username": username, "expires": expires}
    save_access_log(username, get_client_ip(request), "login")
    from fastapi.responses import JSONResponse

    resp = JSONResponse({"success": True, "redirect": "/dashboard"})
    resp.set_cookie(key="token", value=token, httponly=True, samesite="strict", max_age=86400 * 7, secure=False)
    return resp


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest):
    """申请密码重置"""
    username = data.username.strip()
    email = data.email.strip()

    if not username or not email:
        return {"error": "请输入用户名和邮箱"}

    # 防止用户枚举：不管用户名是否存在，都返回统一消息
    users = _load_users()
    user_email = ""
    if username in users:
        user_data = users[username]
        user_email = user_data.get("email", "")

    if not user_email:
        pending = _load_pending_users()
        if username in pending:
            user_email = pending[username].get("email", "")

    if user_email:
        if user_email != email:
            # 邮箱不匹配但不泄露用户存在性
            return {"message": "如果账户存在且邮箱匹配，密码重置链接已发送到您的邮箱，请查收"}
    else:
        if username in users or username in pending:
            # 用户有记录但无邮箱，统一消息
            return {"message": "如果账户存在且邮箱匹配，密码重置链接已发送到您的邮箱，请查收"}

    reset_token = make_token(f"reset_{username}")
    expires = time.time() + 3600

    RESET_TOKENS[reset_token] = {
        "username": username,
        "email": user_email,
        "expires": expires,
    }

    reset_url = f"https://lh.mozengfu.com.cn/reset-password?token={reset_token}"

    email_subject = "【智能量化系统】密码重置"
    email_content = f"""尊敬的用户 {username}，您好！

您申请了密码重置，请点击以下链接重置密码：

{reset_url}

该链接1小时内有效，请尽快操作。

如果您没有申请密码重置，请忽略此邮件。

如有任何问题，请联系管理员：259563977@qq.com

智能量化系统
"""

    try:
        send_email(user_email, email_subject, email_content)
        logger.info(f"密码重置邮件已发送给 {username} ({user_email})")
        return {"message": "密码重置链接已发送到您的邮箱，请查收"}
    except Exception as e:
        logger.error(f"发送重置邮件失败: {e}")
        return {"error": "邮件发送失败，请联系管理员"}


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest):
    """执行密码重置"""
    token = data.token
    new_password = data.password

    if not token or not new_password:
        return {"error": "参数错误"}

    if token not in RESET_TOKENS:
        return {"error": "重置链接已失效，请重新申请"}

    token_data = RESET_TOKENS[token]

    if token_data.get("expires", 0) < time.time():
        del RESET_TOKENS[token]
        return {"error": "重置链接已过期，请重新申请"}

    if len(new_password) < 6:
        return {"error": "密码长度至少6位"}

    username = token_data["username"]

    if not _user_exists(username):
        return {"error": "用户不存在"}

    _update_user_password(username, hash_pw(new_password))

    del RESET_TOKENS[token]

    logger.info("用户 %s 密码重置成功", username)
    return {"message": "密码重置成功"}


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, token: str = Cookie(None)):
    """用户修改密码（需要登录）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    old_password = data.old_password
    new_password = data.new_password

    if not old_password or not new_password:
        return {"error": "请填写旧密码和新密码"}

    if len(new_password) < 6:
        return {"error": "新密码至少6位"}

    users = _load_users()
    if user not in users:
        return {"error": "用户不存在"}

    user_data = users[user]
    verified, _ = verify_pw(old_password, user_data.get("password_hash", ""))
    if not verified:
        return {"error": "旧密码错误"}

    _update_user_password(user, hash_pw(new_password))

    try:
        msg = "🔐 密码修改通知\n\n用户：{}\n时间：{}\n\n您的密码已被修改，如非本人操作请立即联系管理员。".format(
            user, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        send_feishu(msg)
    except Exception as e:
        logger.warning(f"发送密码修改通知失败: {e}")

    logger.info(f"用户 {user} 修改密码成功")
    return {"message": "密码修改成功"}


@router.get("/me")
def me(token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        return {"user": None}
    return {"user": user}


@router.post("/pay-qrcode")
def get_pay_qrcode(data: dict):
    """生成支付二维码"""
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()

    if len(username) < 3:
        return {"error": "用户名太短"}

    order_id = generate_order_id()

    PAYMENT_ORDERS[order_id] = {
        "username": username,
        "email": email,
        "time": datetime.now(),
        "verified": False,
    }

    return {
        "order_id": order_id,
        "qr_code": "/static/wechat_pay.png",
        "amount": 99,
        "expire_minutes": 30,
    }
