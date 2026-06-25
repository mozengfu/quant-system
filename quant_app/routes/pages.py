"""
页面路由模块 - 仅保留认证页面路由。
所有 SPA 路由由 app_api.py 的 serve_spa 处理。
旧管理/市场/看板等模板均已迁移至 SPA 或删除。
"""

import os
from pathlib import Path
from fastapi import APIRouter, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["pages"])

BASE_DIR = Path(__file__).parent.parent.parent


@router.get("/logout")
def logout(token: str = Cookie(None)):
    from quant_app.routes.auth import SESSIONS, sessions_lock

    with sessions_lock:
        if token and token in SESSIONS:
            del SESSIONS[token]
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("token")
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(BASE_DIR, "templates", "login.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/register", response_class=HTMLResponse)
def register_page():
    with open(os.path.join(BASE_DIR, "templates", "register.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(token: str = ""):
    from quant_app.routes.auth import RESET_TOKENS

    if not token or token not in RESET_TOKENS:
        return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>链接失效</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5;}
.container{background:white;padding:40px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.1);text-align:center;}
.error{color:#dc3545;font-size:18px;margin-bottom:20px;}</style></head>
<body><div class="container"><div class="error">链接已失效或不存在</div>
<p>该密码重置链接已过期或无效。</p>
<p>请重新申请密码重置。</p>
<p><a href="/login" style="color:#667eea;">返回登录</a></p></div></body></html>"""

    token_data = RESET_TOKENS[token]
    import html as html_mod
    safe_token = html_mod.escape(token)
    safe_user = html_mod.escape(token_data.get("username", ""))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>重置密码</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
               min-height: 100vh; display: flex; justify-content: center; align-items: center; }}
        .container {{ background: white; padding: 40px; border-radius: 12px; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                      width: 100%; max-width: 420px; margin: 20px; }}
        h1 {{ text-align: center; color: #333; margin-bottom: 24px; font-size: 24px; }}
        .form-group {{ margin-bottom: 20px; }}
        label {{ display: block; margin-bottom: 6px; color: #555; font-size: 14px; font-weight: 500; }}
        input[type="password"] {{ width: 100%; padding: 12px 16px; border: 2px solid #e0e0e0; border-radius: 8px;
                                  font-size: 14px; transition: border-color 0.3s; outline: none; }}
        input[type="password"]:focus {{ border-color: #667eea; }}
        button {{ width: 100%; padding: 12px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                  color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600;
                  cursor: pointer; transition: opacity 0.3s; }}
        button:hover {{ opacity: 0.9; }}
        button:disabled {{ opacity: 0.6; cursor: not-allowed; }}
        .message {{ margin-top: 16px; padding: 12px; border-radius: 6px; text-align: center; font-size: 14px; display: none; }}
        .message.success {{ display: block; background: #d4edda; color: #155724; }}
        .message.error {{ display: block; background: #f8d7da; color: #721c24; }}
        .user-info {{ background: #f8f9fa; padding: 15px; border-radius: 6px; margin-bottom: 20px; text-align: center; }}
        .back-link {{ text-align: center; margin-top: 20px; }}
        .back-link a {{ color: #667eea; text-decoration: none; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>重置密码</h1>
        <div class="user-info">用户名：<strong>{safe_user}</strong></div>
        <form id="resetForm">
            <input type="hidden" id="token" value="{safe_token}">
            <div class="form-group">
                <label for="password">新密码</label>
                <input type="password" id="password" name="password" required minlength="6" placeholder="请输入新密码（至少6位）">
            </div>
            <div class="form-group">
                <label for="confirmPassword">确认新密码</label>
                <input type="password" id="confirmPassword" name="confirmPassword" required minlength="6" placeholder="请再次输入新密码">
            </div>
            <button type="submit" id="submitBtn">重置密码</button>
        </form>
        <div id="message" class="message"></div>
        <div class="back-link"><a href="/login">返回登录</a></div>
    </div>
    <script>
        document.getElementById('resetForm').addEventListener('submit', async function(e) {{
            e.preventDefault();
            const password = document.getElementById('password').value;
            const confirmPassword = document.getElementById('confirmPassword').value;
            const token = document.getElementById('token').value;
            const submitBtn = document.getElementById('submitBtn');
            const message = document.getElementById('message');

            if (password !== confirmPassword) {{
                message.className = 'message error';
                message.textContent = '两次输入的密码不一致';
                return;
            }}
            if (password.length < 6) {{
                message.className = 'message error';
                message.textContent = '密码长度至少6位';
                return;
            }}

            submitBtn.disabled = true;
            submitBtn.textContent = '处理中...';

            try {{
                const response = await fetch('/api/auth/reset-password', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{token, password}})
                }});
                const result = await response.json();

                if (result.error) {{
                    message.className = 'message error';
                    message.textContent = result.error;
                    submitBtn.disabled = false;
                    submitBtn.textContent = '重置密码';
                }} else {{
                    message.className = 'message success';
                    message.textContent = '密码重置成功！即将跳转到登录页面...';
                    setTimeout(() => {{ window.location.href = '/login'; }}, 2000);
                }}
            }} catch (err) {{
                message.className = 'message error';
                message.textContent = '网络错误，请重试';
                submitBtn.disabled = false;
                submitBtn.textContent = '重置密码';
            }}
        }});
    </script>
</body></html>"""
