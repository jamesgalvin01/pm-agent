"""
chat.py — Rowan's interactive chat interface.

Routes:
- GET  /chat                              -> renders the chat page (auth required)
- GET  /api/chat/conversations            -> list user's conversations (JSON)
- GET  /api/chat/messages?conversation_id -> messages in a conversation (JSON)
- POST /api/chat/conversations            -> create a new conversation
- POST /api/chat/send                     -> starts Rowan's reply in the background, returns at once
- GET  /api/chat/status?conversation_id   -> is that reply still running / done / failed

Why background (Oct 2026): a long turn (e.g. closing many tasks) used to hold
the browser's request open until done. Railway's edge, or a phone pausing the
page, could cut it ("upstream error") even though the work finished. Now the
page starts the turn, then checks every couple of seconds.
"""
import threading
import time

from app_shell import APP_HEAD, MOBILE_CSS, tab_bar
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Body, Query
from fastapi.responses import HTMLResponse, JSONResponse

from db import get_connection
from auth import require_auth
from rowan_agent import run_agent_turn

router = APIRouter()

# conversation_id -> {"state": "running" | "done" | "error", "error": str, "started": float}
_jobs: dict = {}
_jobs_lock = threading.Lock()
JOB_MAX_SECONDS = 600


# ============================================================
# Helpers
# ============================================================

def _ensure_default_conversation() -> int:
    """Ensure at least one conversation exists. Returns its id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM conversations ORDER BY last_message_at DESC NULLS LAST, id DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        conv_id = row[0]
        cur.close()
        conn.close()
        return conv_id

    cur.execute(
        "INSERT INTO conversations (title, status) VALUES (%s, %s) RETURNING id",
        ("General", "active"),
    )
    conv_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
        (
            conv_id,
            "assistant",
            "Hi James — I'm Rowan. Ask me about projects, tasks, people, or risks. "
            "I can also create or modify tasks, but I'll confirm with you before making any changes.",
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return conv_id


# ============================================================
# JSON APIs
# ============================================================

@router.get("/api/chat/conversations")
def list_conversations(email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, status, last_message_at, created_at
          FROM conversations
         WHERE status != 'archived'
         ORDER BY last_message_at DESC NULLS LAST, id DESC
         LIMIT 100
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return JSONResponse([
        {
            "id": r[0],
            "title": r[1] or "Untitled",
            "status": r[2],
            "last_message_at": r[3].isoformat() if r[3] else None,
            "created_at": r[4].isoformat() if r[4] else None,
        }
        for r in rows
    ])


@router.get("/api/chat/messages")
def list_messages(
    conversation_id: int = Query(...),
    email: str = Depends(require_auth),
):
    """Return user-visible messages only (skip tool_result rows, hide raw tool_calls)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, role, content, created_at
          FROM messages
         WHERE conversation_id = %s
           AND role IN ('user', 'assistant')
           AND content IS NOT NULL
           AND content != ''
         ORDER BY created_at ASC, id ASC
        """,
        (conversation_id,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return JSONResponse([
        {
            "id": r[0],
            "role": r[1],
            "content": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
        }
        for r in rows
    ])


@router.post("/api/chat/conversations")
def create_conversation(email: str = Depends(require_auth)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (title, status) VALUES (%s, %s) RETURNING id",
        ("New conversation", "active"),
    )
    conv_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({"id": conv_id})


@router.post("/api/chat/send")
def send_message(
    payload: dict = Body(...),
    email: str = Depends(require_auth),
):
    conversation_id = payload.get("conversation_id")
    text = (payload.get("text") or "").strip()
    if not conversation_id or not text:
        raise HTTPException(status_code=400, detail="conversation_id and text are required")
    conv = int(conversation_id)

    with _jobs_lock:
        job = _jobs.get(conv)
        if job and job["state"] == "running" and time.time() - job["started"] < JOB_MAX_SECONDS:
            # Returned, not raised: the dashboard's HTTPException handler only passes redirects through.
            return JSONResponse({"detail": "Rowan is still working on your last message. Give it a moment."},
                                status_code=409)
        _jobs[conv] = {"state": "running", "error": "", "started": time.time()}

    def work():
        try:
            run_agent_turn(conv, text)
            state, err = "done", ""
        except Exception as e:
            print(f"[send] agent turn failed: {e}", flush=True)
            state, err = "error", f"Agent error: {e}"
        with _jobs_lock:
            _jobs[conv] = {"state": state, "error": err, "started": _jobs.get(conv, {}).get("started", time.time())}

    threading.Thread(target=work, name=f"chat-turn-{conv}", daemon=True).start()
    return JSONResponse({"status": "started"}, status_code=202)


@router.get("/api/chat/status")
def chat_status(conversation_id: int = Query(...), email: str = Depends(require_auth)):
    with _jobs_lock:
        job = dict(_jobs.get(conversation_id) or {})
    if not job:
        # Unknown: never started here, or Rowan restarted. The page reloads messages.
        return JSONResponse({"state": "unknown"})
    if job["state"] == "running" and time.time() - job["started"] > JOB_MAX_SECONDS:
        return JSONResponse({"state": "error", "error": "Rowan took too long. Refresh to see what was done."})
    return JSONResponse({"state": job["state"], "error": job.get("error", "")})


# ============================================================
# Chat page
# ============================================================

@router.get("/chat", response_class=HTMLResponse)
def chat_page(email: str = Depends(require_auth)):
    initial_conv_id = _ensure_default_conversation()

    return f"""
    <html>
    <head>
        <title>Rowan — Chat</title>
        {APP_HEAD}
        <link rel='preconnect' href='https://fonts.googleapis.com'>
        <link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
        <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>
        <link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.7.0/dist/tabler-icons.min.css'>
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                margin: 0; background: #0a0b0f; color: #d4d7e0; height: 100vh;
                display: flex; flex-direction: column; -webkit-font-smoothing: antialiased;
            }}
            .header {{
                display: flex; justify-content: space-between; align-items: center;
                padding: 16px 32px; background: #0d0f15; border-bottom: 1px solid #1c1f2a; flex-shrink: 0;
            }}
            .header .brand {{ display: flex; align-items: center; gap: 12px; }}
            .header .logo {{
                width: 34px; height: 34px; border-radius: 9px;
                background: linear-gradient(135deg, #5b7cfa, #3d56c4);
                display: flex; align-items: center; justify-content: center;
                color: white; font-size: 19px; box-shadow: 0 0 20px rgba(91,124,250,0.35);
            }}
            .header h1 {{ margin: 0; font-size: 16px; font-weight: 600; color: #f4f5f8; letter-spacing: -0.2px; }}
            .header .sub {{ font-size: 13px; color: #565a6b; }}
            .header nav {{ display: flex; align-items: center; gap: 4px; }}
            .header nav a {{ color: #6b7080; text-decoration: none; font-size: 13px; padding: 7px 14px; border-radius: 8px; transition: all 0.15s ease; }}
            .header nav a:hover {{ color: #d4d7e0; background: #181b25; }}
            .header nav a.active {{ color: #f4f5f8; background: #181b25; }}
            .header .user {{ font-size: 13px; color: #565a6b; display: flex; align-items: center; gap: 12px; }}
            .header .user form {{ display: inline; }}
            .header .user button {{ background: none; border: none; color: #6b7080; cursor: pointer; font-size: 13px; font-family: inherit; transition: color 0.15s ease; }}
            .header .user button:hover {{ color: #d4d7e0; }}

            .layout {{ flex: 1; display: flex; min-height: 0; }}

            .sidebar {{ width: 280px; background: #0d0f15; border-right: 1px solid #1c1f2a; display: flex; flex-direction: column; }}
            .sidebar-header {{ padding: 18px 20px; border-bottom: 1px solid #1c1f2a; display: flex; align-items: center; justify-content: space-between; }}
            .sidebar-header h2 {{ margin: 0; font-size: 11px; color: #565a6b; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600; }}
            .new-btn {{ background: #5b7cfa; color: white; border: none; padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 12px; font-family: inherit; font-weight: 500; transition: opacity 0.15s ease; }}
            .new-btn:hover {{ opacity: 0.9; }}
            .convo-list {{ overflow-y: auto; flex: 1; padding: 8px; }}
            .convo {{ padding: 12px 14px; border-radius: 10px; cursor: pointer; margin-bottom: 4px; transition: background 0.15s ease; }}
            .convo:hover {{ background: #12141c; }}
            .convo.active {{ background: #181b25; }}
            .convo .title {{ font-size: 14px; color: #e8eaf0; margin-bottom: 4px; font-weight: 500; }}
            .convo .meta {{ font-size: 11px; color: #565a6b; }}

            .main {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            .messages {{ flex: 1; overflow-y: auto; padding: 28px; }}
            .empty {{ color: #565a6b; text-align: center; padding: 40px; font-size: 14px; }}
            .msg {{ margin-bottom: 18px; display: flex; }}
            .msg.user {{ justify-content: flex-end; }}
            .msg .bubble {{ max-width: 70%; padding: 13px 17px; border-radius: 16px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }}
            .msg.assistant .bubble {{ background: #12141c; color: #d4d7e0; border: 1px solid #1c1f2a; }}
            .msg.user .bubble {{ background: #5b7cfa; color: white; }}
            .msg .meta {{ font-size: 11px; color: #565a6b; margin-top: 5px; }}
            .thinking {{ color: #565a6b; font-style: italic; padding: 13px 17px; }}
            .thinking .dot {{ animation: blink 1.4s infinite both; }}
            .thinking .dot:nth-child(2) {{ animation-delay: 0.2s; }}
            .thinking .dot:nth-child(3) {{ animation-delay: 0.4s; }}
            @keyframes blink {{ 0% {{opacity:.2;}} 20% {{opacity:1;}} 100% {{opacity:.2;}} }}

            .composer {{ border-top: 1px solid #1c1f2a; padding: 18px 28px; background: #0d0f15; flex-shrink: 0; }}
            .composer form {{ display: flex; gap: 10px; }}
            .composer textarea {{
                flex: 1; padding: 13px 16px; background: #12141c; border: 1px solid #1c1f2a;
                border-radius: 12px; font-size: 14px; resize: none; font-family: inherit;
                min-height: 46px; max-height: 120px; color: #d4d7e0;
            }}
            .composer textarea::placeholder {{ color: #565a6b; }}
            .composer textarea:focus {{ outline: none; border-color: #5b7cfa; }}
            .composer button {{ background: #5b7cfa; color: white; border: none; padding: 0 24px; border-radius: 12px; font-size: 14px; cursor: pointer; font-weight: 500; font-family: inherit; transition: opacity 0.15s ease; }}
            .composer button:hover {{ opacity: 0.9; }}
            .composer button:disabled {{ background: #2a2e3c; color: #565a6b; cursor: not-allowed; }}
            body {{ height: 100dvh; }}
            .m-new {{ display: none; }}
        </style>
        <style>{MOBILE_CSS}</style>
        <style>
            @media (max-width: 760px) {{
                body {{ padding-bottom: 0; }}
                .sidebar {{ display: none; }}
                .m-new {{ display: inline-flex; background: none; border: none; color: #8ea4ff; font-size: 22px; padding: 0 6px; }}
                .messages {{ padding: 16px 14px; }}
                .msg .bubble {{ max-width: 88%; font-size: 15px; }}
                .composer {{ padding: 10px 12px; margin-bottom: calc(64px + env(safe-area-inset-bottom)); }}
                .composer textarea {{ font-size: 16px; }}
                .composer button {{ padding: 0 16px; }}
            }}
        </style>
    </head>
    <body>
        <div class='header'>
            <div class='brand'>
                <div class='logo brand-logo'><img src='/static/apple-touch-icon.png?v=2' alt='Rowan'></div>
                <h1>Rowan</h1>
                <span class='sub'>Chat</span>
            </div>
            <nav>
                <a href='/'>Dashboard</a>
                <a href='/leads'>Leads</a>
                <a href='/emails'>Email</a>
                <a href='/chat' class='active'>Chat</a>
            </nav>
            <div class='user'>
                <button class='m-new' onclick='newConversation()' aria-label='New conversation'><i class='ti ti-edit'></i></button>
                <span>{email}</span>
                <form method='post' action='/logout'>
                    <button type='submit' aria-label='Sign out'><span class='signout-text'>Sign out</span><i class='ti ti-logout signout-icon'></i></button>
                </form>
            </div>
        </div>
        {tab_bar("chat")}

        <div class='layout'>
            <aside class='sidebar'>
                <div class='sidebar-header'>
                    <h2>Conversations</h2>
                    <button class='new-btn' onclick='newConversation()'>+ New</button>
                </div>
                <div id='convo-list' class='convo-list'></div>
            </aside>

            <main class='main'>
                <div id='messages' class='messages'>
                    <div class='empty'>Loading...</div>
                </div>
                <div class='composer'>
                    <form id='composer-form'>
                        <textarea
                            id='composer-input'
                            placeholder='Ask Rowan anything... (Enter to send, Shift+Enter for newline)'
                            rows='1'
                        ></textarea>
                        <button id='send-btn' type='submit'>Send</button>
                    </form>
                </div>
            </main>
        </div>

        <script>
            let activeConvId = {initial_conv_id};
            let sending = false;

            async function loadConversations() {{
                const res = await fetch('/api/chat/conversations');
                const list = await res.json();
                const el = document.getElementById('convo-list');
                if (!list.length) {{
                    el.innerHTML = "<div class='empty'>No conversations yet.</div>";
                    return;
                }}
                el.innerHTML = list.map(c => {{
                    const when = c.last_message_at
                        ? new Date(c.last_message_at).toLocaleString(undefined, {{month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}})
                        : '—';
                    const cls = c.id === activeConvId ? 'convo active' : 'convo';
                    return `<div class="${{cls}}" onclick="selectConversation(${{c.id}})">
                                <div class='title'>${{escapeHtml(c.title)}}</div>
                                <div class='meta'>${{when}}</div>
                            </div>`;
                }}).join('');
            }}

            async function waitForReply(convId) {{
                const started = Date.now();
                let misses = 0;
                while (Date.now() - started < 10 * 60 * 1000) {{
                    await new Promise(r => setTimeout(r, 1500));
                    let st;
                    try {{
                        const r = await fetch('/api/chat/status?conversation_id=' + convId);
                        st = await r.json();
                    }} catch (_) {{
                        // network blip (phone asleep, page paused): keep waiting
                        if (++misses > 40) throw new Error('lost connection - refresh to see the reply');
                        continue;
                    }}
                    if (st.state === 'done' || st.state === 'unknown') return;
                    if (st.state === 'error') throw new Error(st.error || 'Rowan hit an error');
                }}
                throw new Error('still working - refresh in a minute to see the reply');
            }}

            async function loadMessages(convId) {{
                const res = await fetch('/api/chat/messages?conversation_id=' + convId);
                const list = await res.json();
                const el = document.getElementById('messages');
                if (!list.length) {{
                    el.innerHTML = "<div class='empty'>No messages in this conversation yet.</div>";
                    return;
                }}
                el.innerHTML = list.map(renderMessage).join('');
                el.scrollTop = el.scrollHeight;
            }}

            function renderMessage(m) {{
                const when = m.created_at
                    ? new Date(m.created_at).toLocaleString(undefined, {{hour:'numeric', minute:'2-digit'}})
                    : '';
                return `<div class='msg ${{m.role}}'>
                            <div>
                                <div class='bubble'>${{escapeHtml(m.content)}}</div>
                                <div class='meta'>${{when}}</div>
                            </div>
                        </div>`;
            }}

            function selectConversation(convId) {{
                activeConvId = convId;
                loadConversations();
                loadMessages(convId);
            }}

            async function newConversation() {{
                const res = await fetch('/api/chat/conversations', {{method: 'POST'}});
                const data = await res.json();
                activeConvId = data.id;
                await loadConversations();
                await loadMessages(activeConvId);
            }}

            async function sendMessage(text) {{
                if (sending || !text.trim()) return;
                sending = true;
                const sendBtn = document.getElementById('send-btn');
                const input = document.getElementById('composer-input');
                sendBtn.disabled = true;
                input.disabled = true;

                // Optimistically render the user message
                const msgsEl = document.getElementById('messages');
                msgsEl.insertAdjacentHTML('beforeend', renderMessage({{
                    role: 'user', content: text, created_at: new Date().toISOString()
                }}));
                // Thinking indicator
                msgsEl.insertAdjacentHTML('beforeend',
                    `<div id='thinking' class='msg assistant'><div><div class='bubble thinking'>
                        <span class='dot'>●</span> <span class='dot'>●</span> <span class='dot'>●</span>
                     </div></div></div>`);
                msgsEl.scrollTop = msgsEl.scrollHeight;

                try {{
                    const convId = activeConvId;
                    const res = await fetch('/api/chat/send', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{conversation_id: convId, text: text}})
                    }});
                    if (!res.ok) {{
                        let err = await res.text();
                        try {{ err = JSON.parse(err).detail || err; }} catch (_) {{}}
                        throw new Error(err || 'request failed');
                    }}
                    // Rowan works in the background; check back until the reply is saved.
                    await waitForReply(convId);
                    document.getElementById('thinking')?.remove();
                    if (convId === activeConvId) await loadMessages(convId);
                    await loadConversations();
                }} catch (e) {{
                    document.getElementById('thinking')?.remove();
                    msgsEl.insertAdjacentHTML('beforeend',
                        `<div class='msg assistant'><div><div class='bubble' style='border-color:#ff6b6b;color:#ff6b6b;'>
                            Something went wrong: ${{escapeHtml(String(e))}}
                         </div></div></div>`);
                }} finally {{
                    sending = false;
                    sendBtn.disabled = false;
                    input.disabled = false;
                    input.value = '';
                    input.focus();
                }}
            }}

            function escapeHtml(s) {{
                return (s || '').replace(/[&<>"']/g, c => (
                    {{'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}}[c]
                ));
            }}

            // Composer wiring
            const form = document.getElementById('composer-form');
            const input = document.getElementById('composer-input');
            form.addEventListener('submit', (e) => {{
                e.preventDefault();
                sendMessage(input.value);
            }});
            input.addEventListener('keydown', (e) => {{
                if (e.key === 'Enter' && !e.shiftKey) {{
                    e.preventDefault();
                    sendMessage(input.value);
                }}
            }});

            // Initial load
            loadConversations();
            loadMessages(activeConvId);
            input.focus();
        </script>
    </body>
    </html>"""
