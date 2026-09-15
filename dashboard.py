import os
import html as _html
from urllib.parse import quote_plus
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
from email_replies import run_reply_draft_pass, ensure_schema as ensure_email_schema
from outlook_mail import send_reply
from chat import router as chat_router
from sms_webhook import router as sms_router

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

app = FastAPI()

# Mount chat routes (/chat page + /api/chat/* JSON endpoints)
app.include_router(chat_router)
app.include_router(sms_router)


# ============================================================
# SHARED STYLES — modern dark theme
# ============================================================
# One stylesheet shared across dashboard, leads, and login pages.
# Self-hosted, so fixed hex values (no CSS-variable theming) and a
# Google Fonts link for the Inter typeface + Tabler icon font.

FONT_LINKS = """
    <link rel='preconnect' href='https://fonts.googleapis.com'>
    <link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
    <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>
    <link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.7.0/dist/tabler-icons.min.css'>"""

BASE_CSS = """
        * { box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0;
            background: #0a0b0f;
            color: #d4d7e0;
            -webkit-font-smoothing: antialiased;
        }
        a { color: inherit; }

        .header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 18px 32px;
            background: #0d0f15;
            border-bottom: 1px solid #1c1f2a;
        }
        .header .brand { display: flex; align-items: center; gap: 12px; }
        .header .logo {
            width: 34px; height: 34px; border-radius: 9px;
            background: linear-gradient(135deg, #5b7cfa, #3d56c4);
            display: flex; align-items: center; justify-content: center;
            color: white; font-size: 19px;
            box-shadow: 0 0 20px rgba(91,124,250,0.35);
        }
        .header h1 {
            margin: 0; font-size: 16px; font-weight: 600;
            color: #f4f5f8; letter-spacing: -0.2px;
        }
        .header .sub { font-size: 13px; color: #565a6b; font-weight: 400; }
        .header nav { display: flex; align-items: center; gap: 4px; }
        .header nav a {
            color: #6b7080; text-decoration: none; font-size: 13px;
            padding: 7px 14px; border-radius: 8px; transition: all 0.15s ease;
        }
        .header nav a:hover { color: #d4d7e0; background: #181b25; }
        .header nav a.active { color: #f4f5f8; background: #181b25; }
        .header .user { font-size: 13px; color: #565a6b; display: flex; align-items: center; gap: 12px; }
        .header .user form button {
            background: none; border: none; color: #6b7080;
            cursor: pointer; font-size: 13px; font-family: inherit;
            padding: 0; transition: color 0.15s ease;
        }
        .header .user form button:hover { color: #d4d7e0; }

        .container { padding: 28px 32px; max-width: 1200px; }

        .section-title {
            font-size: 11px; font-weight: 600; color: #565a6b;
            text-transform: uppercase; letter-spacing: 1.5px;
            margin: 32px 0 14px;
        }
        .section-title:first-child { margin-top: 0; }

        .cards {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 14px; margin-bottom: 8px;
        }
        .card {
            background: #12141c; border: 1px solid #1c1f2a; border-radius: 14px;
            padding: 18px; position: relative; overflow: hidden;
        }
        .card .rag-bar { position: absolute; top: 0; left: 0; width: 3px; height: 100%; }
        .card h3 { margin: 0 0 12px; font-size: 14px; font-weight: 600; color: #e8eaf0; }
        .card .rag-pill {
            display: inline-block; font-size: 10px; font-weight: 600;
            letter-spacing: 0.5px; padding: 4px 11px; border-radius: 20px;
        }
        .card .meta { font-size: 12px; color: #565a6b; margin: 14px 0 0; }

        .stat-card {
            background: #12141c; border: 1px solid #1c1f2a; border-radius: 14px;
            padding: 18px; display: flex; flex-direction: column; justify-content: center;
        }
        .stat-card .number { font-size: 30px; font-weight: 700; letter-spacing: -1px; color: #5b7cfa; }
        .stat-card .number.green { color: #2bd4a0; }
        .stat-card .label {
            font-size: 11px; color: #6b7080; margin-top: 6px;
            text-transform: uppercase; letter-spacing: 0.8px;
        }

        .filters { display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; }
        .filter-btn {
            font-size: 13px; padding: 7px 18px; border-radius: 8px;
            border: 1px solid #1c1f2a; background: transparent; color: #8b90a0;
            cursor: pointer; text-decoration: none; transition: all 0.15s ease;
        }
        .filter-btn:hover { color: #d4d7e0; border-color: #2a2e3c; }
        .filter-btn.active { background: #5b7cfa; color: white; border-color: #5b7cfa; }

        table {
            width: 100%; border-collapse: collapse;
            background: #12141c; border: 1px solid #1c1f2a;
            border-radius: 14px; overflow: hidden;
        }
        th {
            background: transparent; color: #565a6b; padding: 13px 20px;
            text-align: left; font-size: 10px; font-weight: 600;
            text-transform: uppercase; letter-spacing: 1px;
            border-bottom: 1px solid #1c1f2a;
        }
        td { padding: 16px 20px; border-bottom: 1px solid #15171f; font-size: 13px; color: #d4d7e0; }
        tr:last-child td { border-bottom: none; }
        tr.complete td { color: #565a6b; }
        td s { color: #565a6b; }

        .pill {
            display: inline-flex; align-items: center; gap: 5px;
            font-size: 12px; font-weight: 500; padding: 6px 12px; border-radius: 8px;
        }
        .btn {
            font-family: inherit; font-size: 12px; font-weight: 500;
            border: none; padding: 6px 13px; border-radius: 8px; cursor: pointer;
            display: inline-flex; align-items: center; gap: 5px; transition: opacity 0.15s ease;
        }
        .btn:hover { opacity: 0.85; }
        .btn-done { background: rgba(43,212,160,0.12); color: #2bd4a0; }
        .btn-reopen { background: rgba(139,144,160,0.12); color: #8b90a0; }
        .btn-delete { background: rgba(255,107,107,0.12); color: #ff6b6b; }

        .prio { font-weight: 600; }

        .add-form {
            background: #12141c; border: 1px solid #1c1f2a; border-radius: 14px;
            padding: 20px; margin-bottom: 20px;
            display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end;
        }
        .add-form label { display: block; font-size: 11px; color: #565a6b; margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.5px; }
        .add-form input, .add-form select {
            font-family: inherit; padding: 9px 12px; background: #0a0b0f;
            border: 1px solid #1c1f2a; border-radius: 8px; font-size: 14px; color: #d4d7e0;
        }
        .add-form input:focus, .add-form select:focus { outline: none; border-color: #5b7cfa; }
        .add-form button {
            font-family: inherit; background: #5b7cfa; color: white; border: none;
            padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500;
            transition: opacity 0.15s ease;
        }
        .add-form button:hover { opacity: 0.9; }
        .empty { color: #565a6b; text-align: center; padding: 28px; }"""


def page_head(title: str) -> str:
    return f"""
    <head>
        <title>{title}</title>
        {FONT_LINKS}
        <style>{BASE_CSS}</style>
    </head>"""


def header_html(email: str, active: str) -> str:
    def cls(name):
        return "active" if name == active else ""
    return f"""
        <div class='header'>
            <div class='brand'>
                <div class='logo'><i class='ti ti-sparkles'></i></div>
                <h1>Rowan</h1>
                <span class='sub'>Miami Coastline</span>
            </div>
            <nav>
                <a href='/' class='{cls("dashboard")}'>Dashboard</a>
                <a href='/leads' class='{cls("leads")}'>Leads</a>
                <a href='/emails' class='{cls("emails")}'>Email</a>
                <a href='/chat' class='{cls("chat")}'>Chat</a>
            </nav>
            <div class='user'>
                <span>{email}</span>
                <form method='post' action='/logout' style='display:inline'>
                    <button type='submit'>Sign out</button>
                </form>
            </div>
        </div>"""


# ============================================================
# AUTH ROUTES
# ============================================================

@app.get("/login", response_class=HTMLResponse)
def login_get(sent: int = 0):
    msg = ""
    if sent:
        msg = """
        <div style='background:rgba(43,212,160,0.12);color:#2bd4a0;padding:14px 18px;border-radius:10px;margin-bottom:18px;font-size:14px;'>
            Check your email for a sign-in link. It's valid for 15 minutes.
        </div>"""
    return f"""
    <html>
    <head>
        <title>Sign in — Rowan</title>
        {FONT_LINKS}
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                background: #0a0b0f; margin: 0; color: #d4d7e0;
                -webkit-font-smoothing: antialiased;
                display: flex; align-items: center; justify-content: center; min-height: 100vh;
            }}
            .wrap {{
                width: 100%; max-width: 400px; padding: 36px;
                background: #12141c; border: 1px solid #1c1f2a; border-radius: 18px;
            }}
            .logo {{
                width: 44px; height: 44px; border-radius: 12px;
                background: linear-gradient(135deg, #5b7cfa, #3d56c4);
                display: flex; align-items: center; justify-content: center;
                color: white; font-size: 24px; margin-bottom: 20px;
                box-shadow: 0 0 24px rgba(91,124,250,0.4);
            }}
            h1 {{ color: #f4f5f8; margin: 0 0 8px; font-size: 22px; font-weight: 600; letter-spacing: -0.3px; }}
            p {{ color: #6b7080; margin: 0 0 24px; font-size: 14px; }}
            label {{ display: block; font-size: 11px; color: #565a6b; margin-bottom: 7px; text-transform: uppercase; letter-spacing: 0.5px; }}
            input[type=email] {{
                width: 100%; padding: 12px 14px; background: #0a0b0f;
                border: 1px solid #1c1f2a; border-radius: 10px; font-size: 14px;
                color: #d4d7e0; font-family: inherit;
            }}
            input[type=email]:focus {{ outline: none; border-color: #5b7cfa; }}
            button {{
                width: 100%; background: #5b7cfa; color: white; border: none;
                padding: 13px; border-radius: 10px; font-size: 15px; font-weight: 500;
                cursor: pointer; margin-top: 18px; font-family: inherit; transition: opacity 0.15s ease;
            }}
            button:hover {{ opacity: 0.9; }}
        </style>
    </head>
    <body>
        <div class='wrap'>
            <div class='logo'><i class='ti ti-sparkles'></i></div>
            <h1>Rowan</h1>
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
            "<html><body style='font-family:Inter,Arial,sans-serif;padding:40px;background:#0a0b0f;color:#d4d7e0;'>"
            "<h2>Link invalid or expired</h2>"
            "<p>Magic links are valid for 15 minutes and can only be used once.</p>"
            "<p><a href='/login' style='color:#5b7cfa;'>Request a new link</a></p>"
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


# ============================================================
# LEADS ROUTES
# ============================================================

LEAD_STAGES = ["New", "Contacted", "Qualified", "Proposal", "Won", "Lost"]


@app.post("/leads/add")
def add_lead(
    name: str = Form(...),
    contact: str = Form(""),
    value: float = Form(0),
    status: str = Form("New"),
    source: str = Form(""),
    email: str = Depends(require_auth),
):
    if status not in LEAD_STAGES:
        status = "New"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO leads (name, contact, value, status, source)
           VALUES (%s, %s, %s, %s, %s)""",
        (name.strip(), contact.strip() or None, value or 0, status, source.strip() or None),
    )
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/leads", status_code=303)


@app.post("/leads/{lead_id}/status")
def update_lead_status(
    lead_id: int,
    status: str = Form(...),
    email: str = Depends(require_auth),
):
    if status not in LEAD_STAGES:
        raise HTTPException(status_code=400, detail="Invalid status")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE leads SET status = %s WHERE id = %s", (status, lead_id))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/leads", status_code=303)


@app.post("/leads/{lead_id}/delete")
def delete_lead(lead_id: int, email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM leads WHERE id = %s", (lead_id,))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/leads", status_code=303)


@app.get("/leads", response_class=HTMLResponse)
def leads_page(filter: str = "all", email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()

    if filter in LEAD_STAGES:
        cur.execute(
            """SELECT id, name, contact, value, status, source
               FROM leads WHERE status = %s
               ORDER BY updated_at DESC""",
            (filter,),
        )
    else:
        cur.execute(
            """SELECT id, name, contact, value, status, source
               FROM leads
               ORDER BY updated_at DESC"""
        )
    leads = cur.fetchall()

    # Pipeline metrics
    cur.execute("""
        SELECT
            COUNT(*),
            COALESCE(SUM(value) FILTER (WHERE status NOT IN ('Won','Lost')), 0),
            COALESCE(SUM(value) FILTER (WHERE status = 'Won'), 0),
            COUNT(*) FILTER (WHERE status = 'Won'),
            COUNT(*) FILTER (WHERE status IN ('Won','Lost'))
        FROM leads
    """)
    total, open_val, won_val, won_count, closed_count = cur.fetchone()
    win_rate = round(won_count / closed_count * 100) if closed_count else 0

    cur.close()
    conn.close()

    # Stage accent colors (dark-theme tuned)
    stage_colors = {
        "New": "#8b90a0", "Contacted": "#5b7cfa", "Qualified": "#a974f5",
        "Proposal": "#f5a623", "Won": "#2bd4a0", "Lost": "#ff6b6b",
    }

    # Stage filter buttons
    filter_btns = "<a href='/leads' class='filter-btn {}'>All</a>".format(
        "active" if filter == "all" else ""
    )
    for s in LEAD_STAGES:
        active = "active" if filter == s else ""
        filter_btns += f"<a href='/leads?filter={s}' class='filter-btn {active}'>{s}</a>"

    rows_html = ""
    for l in leads:
        lid, lname, lcontact, lvalue, lstatus, lsource = l
        color = stage_colors.get(lstatus, "#8b90a0")
        # status dropdown that submits on change
        opts = "".join(
            f"<option value='{s}'{' selected' if s == lstatus else ''}>{s}</option>"
            for s in LEAD_STAGES
        )
        rows_html += f"""
        <tr>
            <td><strong style='color:#e8eaf0;'>{lname}</strong></td>
            <td style='color:#8b90a0;'>{lcontact or '—'}</td>
            <td style='color:#8b90a0;'>{lsource or '—'}</td>
            <td style='color:#e8eaf0;'>${float(lvalue or 0):,.0f}</td>
            <td>
                <form method='post' action='/leads/{lid}/status' style='display:inline'>
                    <select name='status' onchange='this.form.submit()'
                        style='font-family:inherit;border:none;background:{color}1f;color:{color};padding:6px 12px;border-radius:8px;font-size:12px;font-weight:500;cursor:pointer;'>
                        {opts}
                    </select>
                </form>
            </td>
            <td>
                <form method='post' action='/leads/{lid}/delete' style='display:inline'
                    onsubmit='return confirm("Delete this lead?");'>
                    <button type='submit' class='btn btn-delete'>
                        <i class='ti ti-trash'></i> Delete
                    </button>
                </form>
            </td>
        </tr>"""

    if not rows_html:
        rows_html = "<tr><td colspan='6' class='empty'>No leads yet — add one above.</td></tr>"

    add_options = "".join(f"<option value='{s}'>{s}</option>" for s in LEAD_STAGES)

    return f"""
    <html>
    {page_head("Leads — Rowan")}
    <body>
        {header_html(email, "leads")}
        <div class='container'>
            <div class='section-title'>Pipeline</div>
            <div class='cards'>
                <div class='stat-card'><div class='number'>{total}</div><div class='label'>Total Leads</div></div>
                <div class='stat-card'><div class='number'>${float(open_val):,.0f}</div><div class='label'>Open Pipeline</div></div>
                <div class='stat-card'><div class='number green'>${float(won_val):,.0f}</div><div class='label'>Won Value</div></div>
                <div class='stat-card'><div class='number'>{win_rate}%</div><div class='label'>Win Rate</div></div>
            </div>

            <div class='section-title'>Add a lead</div>
            <form method='post' action='/leads/add' class='add-form'>
                <div><label>Company / name</label><input type='text' name='name' required></div>
                <div><label>Contact</label><input type='text' name='contact'></div>
                <div><label>Value ($)</label><input type='number' name='value' min='0' step='100' value='0'></div>
                <div><label>Stage</label><select name='status'>{add_options}</select></div>
                <div><label>Source</label><input type='text' name='source'></div>
                <button type='submit'>+ Add lead</button>
            </form>

            <div class='section-title'>All leads</div>
            <div class='filters'>{filter_btns}</div>
            <table>
                <tr>
                    <th>Name</th><th>Contact</th><th>Source</th><th>Value</th><th>Stage</th><th></th>
                </tr>
                {rows_html}
            </table>
        </div>
    </body>
    </html>"""


# ============================================================
# EMAIL REPLY APPROVAL ROUTES
# ============================================================
# Rowan drafts replies to inbox mail; nothing is sent until James
# clicks Approve & Send on this page.

URGENCY_COLORS = {"high": "#ff6b6b", "normal": "#5b7cfa", "low": "#8b90a0"}


def _fetch_drafts(status: str, limit: int = 50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, from_name, from_email, subject, received_at, body_preview,
                  draft_body, rationale, urgency, status, decided_at, error
             FROM email_drafts
            WHERE status = %s
            ORDER BY received_at DESC NULLS LAST
            LIMIT %s""",
        (status, limit),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


@app.post("/emails/scan")
def emails_scan(email: str = Depends(require_auth)):
    try:
        run_reply_draft_pass()
    except Exception as e:
        print(f"[dashboard] Inbox scan failed: {e}")
        return RedirectResponse(url=f"/emails?err={quote_plus(str(e)[:200])}", status_code=303)
    return RedirectResponse(url="/emails", status_code=303)


@app.post("/emails/{draft_id}/send")
def emails_send(
    draft_id: int,
    body: str = Form(...),
    reply_all: str = Form(""),
    email: str = Depends(require_auth),
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT graph_message_id, status FROM email_drafts WHERE id = %s",
        (draft_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Draft not found")

    graph_message_id, status = row
    if status != "pending":
        cur.close()
        conn.close()
        return RedirectResponse(url="/emails", status_code=303)

    body = (body or "").strip()
    if not body:
        cur.close()
        conn.close()
        return RedirectResponse(
            url="/emails?err=" + quote_plus("Draft was empty — nothing sent."),
            status_code=303,
        )

    try:
        sent_id = send_reply(graph_message_id, body, reply_all=bool(reply_all))
    except Exception as e:
        cur.execute(
            "UPDATE email_drafts SET draft_body = %s, error = %s WHERE id = %s",
            (body, str(e)[:500], draft_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return RedirectResponse(
            url="/emails?err=" + quote_plus(f"Send failed: {str(e)[:180]}"),
            status_code=303,
        )

    cur.execute(
        """UPDATE email_drafts
              SET status = 'sent', draft_body = %s, sent_message_id = %s,
                  decided_at = NOW(), error = NULL
            WHERE id = %s""",
        (body, sent_id, draft_id),
    )
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/emails?ok=1", status_code=303)


@app.post("/emails/{draft_id}/discard")
def emails_discard(draft_id: int, email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE email_drafts SET status = 'discarded', decided_at = NOW() WHERE id = %s",
        (draft_id,),
    )
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/emails", status_code=303)


@app.get("/emails", response_class=HTMLResponse)
def emails_page(ok: int = 0, err: str = "", email: str = Depends(require_auth)):
    try:
        ensure_email_schema()
    except Exception as e:
        print(f"[dashboard] email_drafts schema check failed: {e}")

    pending = _fetch_drafts("pending")
    recent_sent = _fetch_drafts("sent", limit=10)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'pending'),
            COUNT(*) FILTER (WHERE status = 'sent' AND decided_at::date = CURRENT_DATE),
            COUNT(*) FILTER (WHERE status = 'skipped' AND created_at::date = CURRENT_DATE)
        FROM email_drafts
    """)
    n_pending, n_sent_today, n_skipped_today = cur.fetchone()
    cur.close()
    conn.close()

    banner = ""
    if err:
        banner = (
            "<div style='background:rgba(255,107,107,0.12);color:#ff6b6b;padding:14px 18px;"
            "border-radius:10px;margin-bottom:18px;font-size:14px;'>"
            f"{_html.escape(err)}</div>"
        )
    elif ok:
        banner = (
            "<div style='background:rgba(43,212,160,0.12);color:#2bd4a0;padding:14px 18px;"
            "border-radius:10px;margin-bottom:18px;font-size:14px;'>"
            "Reply sent — it's in your Outlook Sent Items.</div>"
        )

    cards = ""
    for d in pending:
        (did, fname, femail, subj, received, preview, draft_body,
         rationale, urgency, _status, _decided, err_note) = d
        color = URGENCY_COLORS.get(urgency or "normal", "#5b7cfa")
        received_s = received.strftime("%b %d, %-I:%M %p") if received else "—"
        retry_note = ""
        if err_note:
            retry_note = (
                "<div style='color:#ff6b6b;font-size:12px;margin-bottom:10px;'>"
                f"Last send attempt failed: {_html.escape(err_note[:200])}</div>"
            )
        cards += f"""
        <div style='background:#12141c;border:1px solid #1c1f2a;border-radius:14px;padding:20px;margin-bottom:16px;'>
            <div style='display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:6px;'>
                <div>
                    <div style='font-size:15px;font-weight:600;color:#e8eaf0;'>{_html.escape(subj or "(no subject)")}</div>
                    <div style='font-size:13px;color:#8b90a0;margin-top:4px;'>
                        {_html.escape(fname or "")} &lt;{_html.escape(femail or "")}&gt; · {received_s}
                    </div>
                </div>
                <span class='pill' style='background:{color}1f;color:{color};white-space:nowrap;'>{_html.escape((urgency or "normal").title())}</span>
            </div>

            <div style='font-size:12px;color:#565a6b;margin:12px 0 14px;font-style:italic;'>
                {_html.escape(rationale or "")}
            </div>

            <details style='margin-bottom:14px;'>
                <summary style='cursor:pointer;font-size:12px;color:#6b7080;'>Original message</summary>
                <div style='margin-top:10px;padding:14px;background:#0a0b0f;border:1px solid #1c1f2a;border-radius:10px;font-size:13px;color:#8b90a0;white-space:pre-wrap;line-height:1.55;'>{_html.escape(preview or "")}</div>
            </details>

            {retry_note}

            <form method='post' action='/emails/{did}/send'>
                <label style='display:block;font-size:11px;color:#565a6b;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;'>Rowan's draft — edit before sending</label>
                <textarea name='body' rows='9' style="width:100%;font-family:'Inter',sans-serif;font-size:14px;line-height:1.6;padding:14px;background:#0a0b0f;border:1px solid #1c1f2a;border-radius:10px;color:#d4d7e0;resize:vertical;">{_html.escape(draft_body or "")}</textarea>
                <div style='display:flex;align-items:center;gap:14px;margin-top:12px;flex-wrap:wrap;'>
                    <button type='submit' class='btn btn-done' style='padding:9px 16px;font-size:13px;'>
                        <i class='ti ti-send'></i> Approve &amp; send
                    </button>
                    <label style='font-size:12px;color:#8b90a0;display:flex;align-items:center;gap:6px;cursor:pointer;'>
                        <input type='checkbox' name='reply_all' value='1'> Reply all
                    </label>
                </div>
            </form>

            <form method='post' action='/emails/{did}/discard' style='margin-top:10px;'
                  onsubmit='return confirm("Discard this draft?");'>
                <button type='submit' class='btn btn-reopen' style='padding:9px 16px;font-size:13px;'>
                    <i class='ti ti-x'></i> Discard
                </button>
            </form>
        </div>"""

    if not cards:
        cards = (
            "<div style='background:#12141c;border:1px solid #1c1f2a;border-radius:14px;'>"
            "<div class='empty'>Nothing waiting on you. Scan the inbox to look for new mail.</div></div>"
        )

    sent_rows = ""
    for s in recent_sent:
        (_sid, sfname, sfemail, ssubj, _sreceived, _sprev,
         sbody, _srat, _surg, _sstatus, sdecided, _serr) = s
        when = sdecided.strftime("%b %d, %-I:%M %p") if sdecided else "—"
        sent_rows += f"""
        <tr>
            <td style='color:#8b90a0;'>{when}</td>
            <td><strong style='color:#e8eaf0;'>{_html.escape(sfname or sfemail or "")}</strong></td>
            <td style='color:#8b90a0;'>{_html.escape(ssubj or "")}</td>
            <td style='color:#565a6b;'>{_html.escape((sbody or "")[:90])}{"…" if len(sbody or "") > 90 else ""}</td>
        </tr>"""
    if not sent_rows:
        sent_rows = "<tr><td colspan='4' class='empty'>No replies sent yet.</td></tr>"

    return f"""
    <html>
    {page_head("Email — Rowan")}
    <body>
        {header_html(email, "emails")}
        <div class='container'>
            {banner}
            <div class='section-title'>Inbox</div>
            <div class='cards'>
                <div class='stat-card'><div class='number'>{n_pending}</div><div class='label'>Awaiting You</div></div>
                <div class='stat-card'><div class='number green'>{n_sent_today}</div><div class='label'>Sent Today</div></div>
                <div class='stat-card'><div class='number'>{n_skipped_today}</div><div class='label'>Filtered Today</div></div>
            </div>

            <form method='post' action='/emails/scan' style='margin:20px 0 4px;'>
                <button type='submit' style="font-family:inherit;background:#5b7cfa;color:white;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500;">
                    <i class='ti ti-refresh'></i> Scan inbox now
                </button>
                <span style='font-size:12px;color:#565a6b;margin-left:12px;'>Runs automatically at 8:15am and 1pm on weekdays. Only mail addressed directly to you.</span>
            </form>

            <div class='section-title'>Drafts awaiting approval</div>
            {cards}

            <div class='section-title'>Recently sent</div>
            <table>
                <tr><th>When</th><th>To</th><th>Subject</th><th>Reply</th></tr>
                {sent_rows}
            </table>
        </div>
    </body>
    </html>"""


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

    # RAG colors (dark-theme tuned)
    rag_colors = {"green": "#2bd4a0", "amber": "#f5a623", "red": "#ff6b6b"}

    projects_html = ""
    for p in projects:
        color = rag_colors.get(p[3], "#8b90a0")
        rag_label = str(p[3]).upper() if p[3] else "NO STATUS"
        projects_html += f"""
        <div class='card'>
            <div class='rag-bar' style='background:{color};'></div>
            <h3>{p[1]}</h3>
            <span class='rag-pill' style='color:{color};background:{color}1f;'>● {rag_label}</span>
            <p class='meta'>{p[2]}</p>
        </div>"""

    tasks_html = ""
    for t in tasks:
        priority_color = {"high": "#ff6b6b", "medium": "#f5a623", "low": "#2bd4a0"}.get(t[3], "#8b90a0")
        is_complete = t[4] == "complete"
        row_class = "complete" if is_complete else ""
        action_btn = f"""
            <form method='post' action='/reopen/{t[0]}' style='display:inline'>
                <button type='submit' class='btn btn-reopen'>
                    <i class='ti ti-rotate'></i> Reopen
                </button>
            </form>""" if is_complete else f"""
            <form method='post' action='/complete/{t[0]}' style='display:inline'>
                <button type='submit' class='btn btn-done'>
                    <i class='ti ti-check'></i> Done
                </button>
            </form>"""

        task_text = f"<s>{t[1]}</s>" if is_complete else t[1]
        prio_text = t[3] or "—"
        prio_html = f"<span class='prio' style='color:{priority_color};'>● {prio_text}</span>" if t[3] else "<span style='color:#565a6b;'>—</span>"
        tasks_html += f"""
        <tr class='{row_class}'>
            <td>{task_text}</td>
            <td style='color:#6b7080;'>{t[2] if t[2] else '—'}</td>
            <td>{prio_html}</td>
            <td style='color:#6b7080;'>{t[5] or 'No project'}</td>
            <td>{action_btn}</td>
        </tr>"""

    open_cls = "active" if filter == "open" else ""
    complete_cls = "active" if filter == "complete" else ""
    all_cls = "active" if filter == "all" else ""

    return f"""
    <html>
    {page_head("Rowan Dashboard")}
    <body>
        {header_html(email, "dashboard")}
        <div class='container'>
            <div class='section-title'>Projects</div>
            <div class='cards'>
                {projects_html}
                <div class='stat-card'>
                    <div class='number'>{open_count}</div>
                    <div class='label'>Open Tasks</div>
                </div>
                <div class='stat-card'>
                    <div class='number green'>{complete_count}</div>
                    <div class='label'>Completed</div>
                </div>
            </div>
            <div class='section-title'>Tasks</div>
            <div class='filters'>
                <a href='/?filter=open' class='filter-btn {open_cls}'>Open</a>
                <a href='/?filter=complete' class='filter-btn {complete_cls}'>Completed</a>
                <a href='/?filter=all' class='filter-btn {all_cls}'>All</a>
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
