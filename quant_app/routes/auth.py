"""
认证路由模块 - 用户注册、登录、密码管理
包含全局 SESSIONS/RESET_TOKENS 状态
"""
import json
import time
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Cookie, HTTPException, Request as FastAPIRequest
from fastapi.responses import HTMLResponse
from starlette.responses import RedirectResponse
from quant_app.utils.auth import hash_pw, verify_pw, make_token
from quant_app.utils.persistence import (
    load_users, save_users, save_access_log, load_reset_tokens, save_reset_tokens as _persistence_save_reset_tokens,
    load_pending_users, save_pending_users, _load_sessions, get_client_ip,
)
from quant_app.services.notification_service import send_email, send_feishu
from app_core import (
    get_recent_trade_dates, get_recent_trade_dates_fallback,
    get_tushare_pro, get_latest_rps_from_db,
    generate_order_id,
)
from quant_app.utils.config import DATA_DIR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

# ========== 会话管理 ==========
SESSIONS = _load_sessions()
sessions_lock = threading.Lock()
logger.info(f"Sessions 加载完成，共 {len(SESSIONS)} 个有效会话")


def _save_sessions():
    """保存 sessions 到文件（使用局部 SESSIONS）"""
    from quant_app.utils.persistence import _save_sessions as _base_save
    _base_save(SESSIONS)


def get_current_user(token: str = Cookie(None)):
    with sessions_lock:
        if not token or token not in SESSIONS:
            return None
        if SESSIONS[token].get("expires", 0) < time.time():
            del SESSIONS[token]
            _save_sessions()
            return None
        return SESSIONS[token]["username"]


def require_auth(token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


# ========== 密码重置令牌 ==========
RESET_TOKENS = {}

# 启动时加载重置令牌
_load_reset_tokens_inner = load_reset_tokens  # alias to persistence function
try:
    loaded = _load_reset_tokens_inner()
    if loaded:
        # loaded 是 dict[str, dict]：{token: {username, email, expires}}
        RESET_TOKENS.update(loaded)
        logger.info(f"重置令牌加载完成，共 {len(RESET_TOKENS)} 个有效令牌")
except Exception as e:
    logger.warning(f"加载重置令牌失败: {e}")
    RESET_TOKENS = {}


def save_reset_tokens():
    """保存重置令牌到文件"""
    try:
        reset_file = DATA_DIR / "reset_tokens.json"
        valid_tokens = {k: v for k, v in RESET_TOKENS.items() if v.get("expires", 0) > time.time()}
        with open(reset_file, "w", encoding="utf-8") as f:
            json.dump(valid_tokens, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"保存重置令牌失败: {e}")


# ========== 支付订单存储 ==========
PAYMENT_ORDERS = {}  # {order_id: {username, email, time, verified}}


# ========== 认证路由 ==========

@router.post("/api/auth/register")
async def register(data: dict):
    username = data.get("username", "").strip()
    password = data.get("password", "")
    email = data.get("email", "").strip()
    phone = data.get("phone", "").strip()
    pay_remark = data.get("pay_remark", "").strip()
    pay_wxid = data.get("pay_wxid", "").strip()

    if len(username) < 3 or len(username) > 20:
        return {"error": "用户名需3-20位"}
    if not username.isalnum() and "_" not in username:
        return {"error": "用户名只能为英文字母或数字"}
    if len(password) < 6:
        return {"error": "密码至少6位"}
    if not email or "@" not in email:
        return {"error": "请填写正确的邮箱地址"}

    users = load_users()
    if username in users:
        return {"error": "用户名已存在"}

    pending = load_pending_users()
    if username in pending:
        return {"error": "该用户名已提交审核，请耐心等待"}

    pay_amount = data.get("pay_amount", 99)
    try:
        pay_amount = int(pay_amount)
    except Exception:
        pay_amount = 99

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

    pending[username] = {
        "password_hash": hash_pw(password),
        "email": email,
        "phone": phone,
        "pay_remark": pay_remark,
        "pay_wxid": pay_wxid,
        "pay_amount": pay_amount,
        "package_name": package_name,
        "valid_days": valid_days,
        "register_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "pending",
        "expire_date": expire_date,
    }
    save_pending_users(pending)

    try:
        package_info = f"{package_name} (¥{pay_amount})"
        admin_msg = f"🆕 新用户注册待审核\n\n用户名：{username}\n邮箱：{email}\n手机号：{phone or '未填写'}\n套餐：{package_info}\n有效期至：{expire_date}\n付款备注：{pay_remark}\n微信号后4位：{pay_wxid}\n注册时间：{pending[username]['register_time']}\n\n请核对收款后审核通过"
        send_feishu(admin_msg)
    except Exception as e:
        logger.warning(f"发送审核通知失败: {e}")

    return {"message": "提交成功，请完成支付后等待审核"}


@router.post("/api/auth/login")
async def do_login(request: FastAPIRequest, data: dict):
    username = data.get("username", "").strip()
    password = data.get("password", "")

    pending = load_pending_users()
    if username in pending:
        return {"error": "账户正在审核中，请等待管理员审核通过"}

    users = load_users()
    if username not in users:
        return {"error": "用户名或密码错误"}

    user_data = users[username]
    if isinstance(user_data, dict):
        verified, new_hash = verify_pw(password, user_data.get("password_hash", ""))
        if not verified:
            return {"error": "用户名或密码错误"}
        if new_hash:
            user_data["password_hash"] = new_hash
            save_users(users)
        if username not in ["mozengfu", "admin"]:
            expire_date = user_data.get("expire_date", "")
            if expire_date:
                try:
                    expire = datetime.strptime(expire_date, "%Y-%m-%d")
                    if datetime.now() > expire:
                        return {"error": f"您的账户已于 {expire_date} 过期，请联系管理员续费：259563977@qq.com"}
                except Exception as _e:
                    logger.error(f"Error in auth: {_e}")
    else:
        verified, _ = verify_pw(password, user_data)
        if not verified:
            return {"error": "用户名或密码错误"}

    token = make_token(username)
    expires = time.time() + 86400 * 7
    with sessions_lock:
        SESSIONS[token] = {"username": username, "expires": expires}
    _save_sessions()
    save_access_log(username, get_client_ip(request), "login")
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"success": True, "redirect": "/dashboard"})
    import os
    is_secure = os.environ.get("ENV", "development") == "production"
    resp.set_cookie(key="token", value=token, httponly=True, samesite="lax", max_age=86400 * 7, secure=is_secure)
    return resp


@router.post("/api/auth/forgot-password")
async def forgot_password(data: dict):
    """申请密码重置"""
    username = data.get("username", "").strip()
    email = data.get("email", "").strip()

    if not username or not email:
        return {"error": "请输入用户名和邮箱"}

    # 防止用户枚举：不管用户名是否存在，都返回统一消息
    users = load_users()
    user_email = ""
    if username in users:
        user_data = users[username]
        if isinstance(user_data, dict):
            user_email = user_data.get("email", "")

    if not user_email:
        pending = load_pending_users()
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
    save_reset_tokens()

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


@router.post("/api/auth/reset-password")
async def reset_password(data: dict):
    """执行密码重置"""
    token = data.get("token", "")
    new_password = data.get("password", "")

    if not token or not new_password:
        return {"error": "参数错误"}

    if token not in RESET_TOKENS:
        return {"error": "重置链接已失效，请重新申请"}

    token_data = RESET_TOKENS[token]

    if token_data.get("expires", 0) < time.time():
        del RESET_TOKENS[token]
        save_reset_tokens()
        return {"error": "重置链接已过期，请重新申请"}

    if len(new_password) < 6:
        return {"error": "密码长度至少6位"}

    username = token_data["username"]

    users = load_users()
    if username not in users:
        return {"error": "用户不存在"}

    user_data = users[username]
    if isinstance(user_data, dict):
        user_data["password_hash"] = hash_pw(new_password)
    else:
        users[username] = {"password_hash": hash_pw(new_password), "expire_date": ""}

    save_users(users)

    del RESET_TOKENS[token]
    save_reset_tokens()

    logger.info(f"用户 {username} 密码重置成功")
    return {"message": "密码重置成功"}


@router.post("/api/auth/change-password")
async def change_password(data: dict, token: str = Cookie(None)):
    """用户修改密码（需要登录）"""
    user = get_current_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if not old_password or not new_password:
        return {"error": "请填写旧密码和新密码"}

    if len(new_password) < 6:
        return {"error": "新密码至少6位"}

    users = load_users()
    if user not in users:
        return {"error": "用户不存在"}

    user_data = users[user]

    if isinstance(user_data, dict):
        verified, _ = verify_pw(old_password, user_data.get("password_hash", ""))
        if not verified:
            return {"error": "旧密码错误"}
        user_data["password_hash"] = hash_pw(new_password)
    else:
        verified, _ = verify_pw(old_password, user_data)
        if not verified:
            return {"error": "旧密码错误"}
        users[user] = {"password_hash": hash_pw(new_password), "expire_date": ""}

    save_users(users)

    try:
        msg = f"🔐 密码修改通知\n\n用户：{user}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n您的密码已被修改，如非本人操作请立即联系管理员。"
        send_feishu(msg)
    except Exception as e:
        logger.warning(f"发送密码修改通知失败: {e}")

    logger.info(f"用户 {user} 修改密码成功")
    return {"message": "密码修改成功"}


@router.get("/logout")
async def get_logout(token: str = Cookie(None)):
    with sessions_lock:
        if token and token in SESSIONS:
            del SESSIONS[token]
            _save_sessions()
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("token")
    return resp


@router.get("/api/auth/me")
async def me(token: str = Cookie(None)):
    user = get_current_user(token)
    if not user:
        return {"user": None}
    return {"user": user}


@router.post("/api/auth/pay-qrcode")
async def get_pay_qrcode(data: dict):
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
