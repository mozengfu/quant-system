"""
授权工具 — 基于角色的访问控制

角色值从 users.json 的 role 字段读取：
- "admin" — 管理员，有完全访问权限
- "user" — 普通用户，有限权限
- 缺失或 None — 视为普通用户

用法:
    from quant_app.utils.authz import require_admin, is_admin
"""
from fastapi import HTTPException
from quant_app.utils.persistence import load_users


def is_admin(username):
    """检查用户是否为管理员"""
    if not username:
        return False
    users = load_users()
    user_data = users.get(username, {})
    if isinstance(user_data, dict):
        return user_data.get("role") == "admin"
    # 旧格式：纯字符串密码 — 兼容已存在的旧数据
    # TODO: 旧数据迁移完成后移除硬编码
    return username == "mozengfu"


def require_admin(username):
    """要求当前用户是管理员，否则抛 403"""
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="需要管理员权限")
