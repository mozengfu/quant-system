"""
认证模块 - 密码哈希和验证
"""
import hashlib
import secrets

import bcrypt


def hash_pw(password):
    """使用 bcrypt 哈希密码"""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_pw(password, stored):
    """验证密码，返回 (是否匹配, 新哈希)
    兼容旧格式(salt$hash=SHA256)和新格式(bcrypt)。
    旧格式验证成功后返回新 bcrypt 哈希以便升级存储。
    """
    # 旧格式: salt$hash (SHA256)
    if "$" in stored and not stored.startswith("$2"):
        try:
            salt, h = stored.split("$", 1)
            if hashlib.sha256((salt + password).encode()).hexdigest() == h:
                return True, hash_pw(password)
            return False, None
        except Exception:
            return False, None
    # 新格式: $2b$... (bcrypt)
    if stored.startswith("$2"):
        try:
            if bcrypt.checkpw(password.encode(), stored.encode()):
                return True, None
            return False, None
        except Exception:
            return False, None
    return False, None


def make_token(username):
    """生成会话令牌"""
    return secrets.token_hex(32)
