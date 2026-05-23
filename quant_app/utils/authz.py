"""
授权工具 — 基于角色的访问控制

角色值从 MySQL users 表读取。
用法:
    from quant_app.utils.authz import require_admin, is_admin
"""
from fastapi import HTTPException

from quant_app.routes.auth import _load_users


def is_admin(username):
    """检查用户是否为管理员"""
    if not username:
        return False
    users = _load_users()
    user_data = users.get(username, {})
    return user_data.get("role") == "admin"


def require_admin(username):
    """要求当前用户是管理员，否则抛 403"""
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="需要管理员权限")
