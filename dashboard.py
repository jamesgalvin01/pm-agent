import os
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

from db import get_connection
from auth import (
    create_magic_link_token,
    consume_magic_link_token,
    create_session_jwt,
    require_auth,
    SESSION_COOKIE_NAME,
    SESSION_DURATION,
    ALLOWED_EMAIL,
)
from mailer import send_magic_link_email
from chat import router as chat_router

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

app = FastAPI()

# Mount chat routes (/chat page + /api/chat/* JSON endpoints)
app.include_router(chat_router)


# ============================================================
# AUTH ROUTES
# ============================================================

@app.get("/login", response_class=HTMLResponse)
def login_get(sent: int = 0):
    msg = ""
    if sent:
        msg = """
        <div style='background:#e8f5e9;color:#1b5e20;padding:14px 18px;border-radius:8px;margin-bottom:16px;'>
            Check your email for a sign-in link. It's valid for 15 minutes.
        </div>"""
    return f"""
    <html>
    <head>
        <title>Sign in — Rowan</title>
        <style>
            body {{ font-family: Arial, sans-serif; background: #f5f7fa; margin: 0; }}
            .wrap {{ max-width: 420px; margin: 80px auto; padding: 32px; background: white; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.08); }}
            h1 {{ color: #1F3864; margin: 0 0 8px; font-size: 24px; }}
            p {{ color: #666; margin: 0 0 24px; }}
            label {{ display:block; font-size: 14px; color: #333; margin-bottom: 6px; }}
            input[type=email] {{ width: 100%; padding: 12px; border: 1px solid #ccc; border-radius: 8px; font-size: 14px; box-sizing: border-box; }}
            button {{ width: 100%; background: #1F3864; color: white; border: none; padding: 12px; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 16px; }}
            button:hover {{ background: #2E5090; }}
        </style>
    </head>
    <body>
        <div class='wrap'>
            <h1>🤖 Rowan</h1>
            <p>Sign in with a one-time link sent to your email.</p>
            {msg}
            <form method='post' action='/login'>
                <label>Email address</label>
                <input type='email' name='email' required autofocus>
                <button type='submit'>Send me a sign-in link</button>
            </form>
        </div>
    </body>
    </html>"""


@app.post("/login")
def login_post(email: str = Form(...)):
    email_norm = email.lower().strip()

    if email_norm == ALLOWED_EMAIL:
        token = create_magic_link_token(email_norm)
        link = f"{PUBLIC_BASE_URL}/auth/verify?token={token}"
        try:
            send_magic_link_email(email_norm, link)
        except Exception as e:
            print(f"[login] failed to send magic link to {email_norm}: {e}")

    return RedirectResponse(url="/login?sent=1", status_code=303)


@app.get("/auth/verify")
def auth_verify(token: str):
    email = consume_magic_link_token(token)
    if not email or email != ALLOWED_EMAIL:
        return HTMLResponse(
            "<html><body style='font-family:Arial;padding:40px;'>"
            "<h2>Link invalid or expired</h2>"
            "<p>Magic links are valid for 15 minutes and can only be used once.</p>"
            "<p><a href='/login'>Request a new link</a></p>"
            "</body></html>",
            status_code=400,
        )

    jwt_token = create_session_jwt(email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=jwt_token,
        max_age=int(SESSION_DURATION.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ============================================================
# EXISTING ROUTES (gated by require_auth)
# ============================================================

@app.post("/complete/{task_id}")
def complete_task(task_id: int, email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'complete' WHERE id = %s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.post("/reopen/{task_id}")
def reopen_task(task_id: int, email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'open' WHERE id = %s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(filter: str = "open", email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, name, status, rag_status FROM projects")
    projects = cur.fetchall()

    if filter == "all":
        cur.execute("""
            SELECT t.id, t.description, t.due_date, t.priority, t.status, p.name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            ORDER BY t.status ASC, t.due_date ASC NULLS LAST
        """)
    else:
        cur.execute("""
            SELECT t.id, t.description, t.due_date, t.priority, t.status, p.name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = %s
            ORDER BY t.due_date ASC NULLS LAST
        """, (filter,))

    tasks = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'")
    open_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tasks WHERE status = 'complete'")
    complete_count = cur.fetchone()[0]

    cur.close()
    conn.close()

    rag_colors = {"green": "#2ecc71", "amber": "#f39c12", "red": "#e74c3c"}

    projects_html = ""
    for p in projects:
        color = rag_colors.get(p[3], "#999")
        projects_html += f"""
        <div class='card'>
            <h3>{p[1]}</h3>
            <span style='background:{color};color:white;padding:4px 12px;border-radius:12px;font-size:14px;'>
                {str(p[3]).upper() if p[3] else 'NO STATUS'}
            </span>
            <p style='color:#666;margin-top:8px;'>Status: {p[2]}</p>
        </div>"""

    tasks_html = ""
    for t in tasks:
        priority_color = {"high": "#e74c3c", "medium": "#f39c12", "low": "#2ecc71"}.get(t[3], "#999")
        is_complete = t[4] == "complete"
        row_style = "background:#f9fff9;" if is_complete else ""
        action_btn = f"""
            <form method='post' action='/reopen/{t[0]}' style='display:inline'>
                <button type='submit' style='background:#95a5a6;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;'>
                    Reopen
                </button>
            </form>""" if is_complete else f"""
            <form method='post' action='/complete/{t[0]}' style='display:inline'>
                <button type='submit' style='background:#2ecc71;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;'>
                    ✓ Done
                </button>
            </form>"""

        task_text = f"<s style='color:#999'>{t[1]}</s>" if is_complete else t[1]
        tasks_html += f"""
        <tr style='{row_style}'>
            <td>{task_text}</td>
            <td>{t[2] if t[2] else '—'}</td>
            <td><span style='color:{priority_color};font-weight:bold;'>{t[3] or '—'}</span></td>
            <td>{t[5] or 'No project'}</td>
            <td>{action_btn}</td>
        </tr>"""

    active_style = "background:#1F3864;color:white;"
    all_style = ""
    complete_style = ""
    if filter == "all":
        all_style = "background:#1F3864;color:white;"
        active_style = ""
    elif filter == "complete":
        complete_style = "background:#1F3864;color:white;"
        active_style = ""

    return f"""
    <html>
    <head>
        <title>Rowan Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fa; }}
            .header {{ background: #1F3864; color: white; padding: 20px 40px; display:flex; justify-content:space-between; align-items:center; }}
            .header h1 {{ margin: 0; font-size: 24px; }}
            .header nav a {{ color: white; margin-left: 20px; text-decoration: none; font-size: 14px; opacity: 0.85; }}
            .header nav a:hover {{ opacity: 1; }}
            .header .user {{ font-size: 13px; opacity: 0.85; }}
            .container {{ padding: 30px 40px; }}
            .section-title {{ font-size: 20px; font-weight: bold; color: #1F3864; margin: 30px 0 15px; }}
            .cards {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 10px; }}
            .card {{ background: white; border-radius: 10px; padding: 20px; min-width: 200px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            .card h3 {{ margin: 0 0 10px; color: #1F3864; }}
            .stat-card {{ background: white; border-radius: 10px; padding: 20px 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align:center; }}
            .stat-card .number {{ font-size: 36px; font-weight: bold; color: #1F3864; }}
            .stat-card .label {{ color: #666; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            th {{ background: #2E5090; color: white; padding: 12px 16px; text-align: left; font-size: 14px; }}
            td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
            tr:last-child td {{ border-bottom: none; }}
            .filter-btn {{ padding: 8px 20px; border-radius: 20px; border: 2px solid #1F3864; background: white; color: #1F3864; cursor: pointer; font-size: 14px; text-decoration: none; }}
            .filters {{ display: flex; gap: 10px; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class='header'>
            <h1>🤖 Rowan — Miami Coastline Management</h1>
            <nav>
                <a href='/'>Dashboard</a>
                <a href='/chat'>Chat</a>
            </nav>
            <div class='user'>
                Signed in as {email}
                <form method='post' action='/logout' style='display:inline'>
                    <button type='submit' style='background:none;border:none;color:white;cursor:pointer;text-decoration:underline;font-size:13px;'>Sign out</button>
                </form>
            </div>
        </div>
        <div class='container'>
            <div class='section-title'>Projects</div>
            <div class='cards'>
                {projects_html}
                <div class='stat-card'>
                    <div class='number'>{open_count}</div>
                    <div class='label'>Open Tasks</div>
                </div>
                <div class='stat-card'>
                    <div class='number'>{complete_count}</div>
                    <div class='label'>Completed</div>
                </div>
            </div>
            <div class='section-title'>Tasks</div>
            <div class='filters'>
                <a href='/?filter=open' class='filter-btn' style='{active_style}'>Open</a>
                <a href='/?filter=complete' class='filter-btn' style='{complete_style}'>Completed</a>
                <a href='/?filter=all' class='filter-btn' style='{all_style}'>All</a>
            </div>
            <table>
                <tr>
                    <th>Task</th>
                    <th>Due Date</th>
                    <th>Priority</th>
                    <th>Project</th>
                    <th>Action</th>
                </tr>
                {tasks_html}
            </table>
        </div>
    </body>
    </html>"""


# ============================================================
# Exception handler so require_auth's 307 redirects actually redirect
# ============================================================

@app.exception_handler(HTTPException)
async def auth_redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and "Location" in (exc.headers or {}):
        return RedirectResponse(url=exc.headers["Location"], status_code=307)
    raise exc


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
