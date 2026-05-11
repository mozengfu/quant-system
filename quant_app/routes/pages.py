"""
页面路由模块 - 所有 HTML 页面路由
"""
import os
import json
import html
import time
from pathlib import Path
from fastapi import APIRouter, Cookie
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["pages"])

BASE_DIR = Path(__file__).parent.parent.parent


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    with open(os.path.join(BASE_DIR, "templates", "login.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/register", response_class=HTMLResponse)
async def register_page():
    with open(os.path.join(BASE_DIR, "templates", "register.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open(os.path.join(BASE_DIR, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/market", response_class=HTMLResponse)
async def market_page():
    with open(os.path.join(BASE_DIR, "templates", "market_analysis.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/ml_top15", response_class=HTMLResponse)
async def ml_top15_page():
    with open(os.path.join(BASE_DIR, "templates", "ml_top15.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/strategy_v41", response_class=HTMLResponse)
async def strategy_v41_page():
    from fastapi.responses import HTMLResponse as HR
    with open(os.path.join(BASE_DIR, "templates", "strategy_v41.html"), encoding="utf-8") as f:
        content = f.read()
    return HR(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/log_analytics", response_class=HTMLResponse)
async def log_analytics_page():
    with open(os.path.join(BASE_DIR, "templates", "log_analytics.html"), encoding="utf-8") as f:
        return f.read()


@router.get("/", response_class=HTMLResponse)
async def landing():
    """公开落地页 - 系统介绍与模块入口"""
    from fastapi.responses import HTMLResponse as HR
    with open(os.path.join(BASE_DIR, "templates", "landing.html"), encoding="utf-8") as f:
        content = f.read()
    return HR(content=content, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(token: str = Cookie(None)):
    """主控面板 - 需要登录"""
    from fastapi.responses import Response
    from starlette.responses import RedirectResponse
    from quant_app.routes.auth import SESSIONS
    if not token or token not in SESSIONS:
        return RedirectResponse(url="/login")
    with open(os.path.join(BASE_DIR, "templates", "index.html"), encoding="utf-8") as f:
        html_content = f.read()
    return Response(content=html_content, media_type="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(token: str = ""):
    """密码重置页面"""
    from quant_app.routes.auth import RESET_TOKENS, save_reset_tokens
    if not token or token not in RESET_TOKENS:
        return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>链接失效</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5;}
.container{background:white;padding:40px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.1);text-align:center;}
.error{color:#dc3545;font-size:18px;margin-bottom:20px;}</style></head>
<body><div class="container"><div class="error">⚠️ 链接已失效或不存在</div>
<p>该密码重置链接已过期或无效。</p>
<p>请重新申请密码重置。</p>
<a href="/login" style="color:#667eea;">返回登录</a></div></body></html>"""

    # 检查令牌是否过期
    token_data = RESET_TOKENS[token]
    if token_data.get("expires", 0) < time.time():
        del RESET_TOKENS[token]
        save_reset_tokens()
        return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>链接失效</title>
<style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f5f5f5;}
.container{background:white;padding:40px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,0.1);text-align:center;}
.error{color:#dc3545;font-size:18px;margin-bottom:20px;}</style></head>
<body><div class="container"><div class="error">⚠️ 链接已过期</div>
<p>该密码重置链接已过期（有效期1小时）。</p>
<p>请重新申请密码重置。</p>
<a href="/login" style="color:#667eea;">返回登录</a></div></body></html>"""

    # 返回密码重置页面
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>重置密码 - 智能量化选股系统</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }}
        .container {{
            background: white;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
        }}
        h1 {{
            text-align: center;
            color: #333;
            margin-bottom: 30px;
            font-size: 24px;
        }}
        .form-group {{
            margin-bottom: 20px;
        }}
        label {{
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-size: 14px;
        }}
        input {{
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-size: 14px;
            transition: border-color 0.3s;
        }}
        input:focus {{
            outline: none;
            border-color: #667eea;
        }}
        button {{
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            cursor: pointer;
            transition: opacity 0.3s;
        }}
        button:hover {{
            opacity: 0.9;
        }}
        button:disabled {{
            opacity: 0.6;
            cursor: not-allowed;
        }}
        .message {{
            margin-top: 15px;
            padding: 12px;
            border-radius: 6px;
            font-size: 14px;
            display: none;
        }}
        .message.success {{
            background: #d4edda;
            color: #155724;
            display: block;
        }}
        .message.error {{
            background: #f8d7da;
            color: #721c24;
            display: block;
        }}
        .back-link {{
            text-align: center;
            margin-top: 20px;
        }}
        .back-link a {{
            color: #667eea;
            text-decoration: none;
            font-size: 14px;
        }}
        .back-link a:hover {{
            text-decoration: underline;
        }}
        .user-info {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
            text-align: center;
        }}
        .user-info strong {{
            color: #667eea;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔐 重置密码</h1>
        <div class="user-info">
            用户名：<strong>{html.escape(token_data['username'])}</strong>
        </div>
        <form id="resetForm">
            <input type="hidden" id="token" value="{html.escape(token)}">
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
        <div class="back-link">
            <a href="/login">返回登录</a>
        </div>
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
                    body: JSON.stringify({{token: token, password: password}})
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
                    setTimeout(() => {{
                        window.location.href = '/login';
                    }}, 2000);
                }}
            }} catch (err) {{
                message.className = 'message error';
                message.textContent = '网络错误，请重试';
                submitBtn.disabled = false;
                submitBtn.textContent = '重置密码';
            }}
        }});
    </script>
</body>
</html>"""
