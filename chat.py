"""
chat.py — Rowan's interactive chat interface.

Stage 3: read-only.
- GET  /chat                              -> renders the chat page (auth required)
- GET  /api/chat/conversations            -> list user's conversations (JSON)
- GET  /api/chat/messages?conversation_id -> messages in a conversation (JSON)
- POST /api/chat/conversations            -> create a new conversation

Stage 4 will add POST /api/chat/send (calls Claude, persists assistant turn).
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from db import get_connection
from auth import require_auth

router = APIRouter()


# ============================================================
# Helpers
# ============================================================

def _ensure_default_conversation() -> int:
    """
    Ensure at least one conversation exists. Returns the id of the most
    recent conversation, creating a seeded 'General' thread if none exist.
    """
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM conversations ORDER BY last_message_at DESC NULLS LAST, id DESC LIMIT 1")
    row = cur.fetchone()
    if row:
        conv_id = row[0]
        cur.close()
        conn.close()
        return conv_id

    # No conversations yet — create one and seed a welcome message
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
            "Hi James — I'm Rowan. This is where we'll talk things through. "
            "Right now I can show you what I know, but I can't reply yet. "
            "We'll wire that up in the next step.",
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
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, role, content, created_at
          FROM messages
         WHERE conversation_id = %s
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


# ============================================================
# Chat page
# ============================================================

@router.get("/chat", response_class=HTMLResponse)
def chat_page(email: str = Depends(require_auth)):
    # Make sure there's something to look at
    initial_conv_id = _ensure_default_conversation()

    return f"""
    <html>
    <head>
        <title>Rowan — Chat</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: -apple-system, Arial, sans-serif; margin: 0; background: #f5f7fa; height: 100vh; display: flex; flex-direction: column; }}
            .header {{ background: #1F3864; color: white; padding: 14px 24px; display:flex; justify-content:space-between; align-items:center; flex-shrink: 0; }}
            .header h1 {{ margin: 0; font-size: 18px; }}
            .header nav a {{ color: white; margin-left: 20px; text-decoration: none; font-size: 14px; opacity: 0.85; }}
            .header nav a:hover {{ opacity: 1; }}
            .header .user {{ font-size: 13px; opacity: 0.85; }}
            .header .user form {{ display: inline; }}
            .header .user button {{ background: none; border: none; color: white; cursor: pointer; text-decoration: underline; font-size: 13px; margin-left: 12px; }}

            .layout {{ flex: 1; display: flex; min-height: 0; }}

            .sidebar {{ width: 280px; background: white; border-right: 1px solid #e6e8ec; display: flex; flex-direction: column; }}
            .sidebar-header {{ padding: 16px; border-bottom: 1px solid #f0f1f4; display: flex; align-items: center; justify-content: space-between; }}
            .sidebar-header h2 {{ margin: 0; font-size: 14px; color: #1F3864; text-transform: uppercase; letter-spacing: 0.5px; }}
            .new-btn {{ background: #1F3864; color: white; border: none; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; }}
            .convo-list {{ overflow-y: auto; flex: 1; }}
            .convo {{ padding: 14px 16px; border-bottom: 1px solid #f5f6f8; cursor: pointer; }}
            .convo:hover {{ background: #f9fafc; }}
            .convo.active {{ background: #eef2f9; border-left: 3px solid #1F3864; padding-left: 13px; }}
            .convo .title {{ font-size: 14px; color: #1a1a1a; margin-bottom: 4px; font-weight: 500; }}
            .convo .meta {{ font-size: 11px; color: #999; }}

            .main {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            .messages {{ flex: 1; overflow-y: auto; padding: 24px; }}
            .empty {{ color: #999; text-align: center; padding: 40px; font-size: 14px; }}
            .msg {{ margin-bottom: 18px; display: flex; }}
            .msg.user {{ justify-content: flex-end; }}
            .msg .bubble {{ max-width: 70%; padding: 12px 16px; border-radius: 14px; font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word; }}
            .msg.assistant .bubble {{ background: white; color: #1a1a1a; border: 1px solid #e6e8ec; }}
            .msg.user .bubble {{ background: #1F3864; color: white; }}
            .msg .meta {{ font-size: 11px; color: #999; margin-top: 4px; }}

            .composer {{ border-top: 1px solid #e6e8ec; padding: 16px 24px; background: white; flex-shrink: 0; }}
            .composer form {{ display: flex; gap: 10px; }}
            .composer textarea {{ flex: 1; padding: 12px 14px; border: 1px solid #ccc; border-radius: 10px; font-size: 14px; resize: none; font-family: inherit; min-height: 44px; max-height: 120px; }}
            .composer textarea:disabled {{ background: #f5f6f8; color: #999; cursor: not-allowed; }}
            .composer button {{ background: #1F3864; color: white; border: none; padding: 0 22px; border-radius: 10px; font-size: 14px; cursor: pointer; font-weight: 500; }}
            .composer button:disabled {{ background: #c4c9d1; cursor: not-allowed; }}
            .composer .note {{ font-size: 11px; color: #999; margin-top: 6px; }}
        </style>
    </head>
    <body>
        <div class='header'>
            <h1>🤖 Rowan</h1>
            <nav>
                <a href='/'>Dashboard</a>
                <a href='/chat'>Chat</a>
            </nav>
            <div class='user'>
                {email}
                <form method='post' action='/logout'>
                    <button type='submit'>Sign out</button>
                </form>
            </div>
        </div>

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
                    <form onsubmit='return false;'>
                        <textarea
                            id='composer-input'
                            placeholder='Sending will be enabled in the next step...'
                            disabled
                        ></textarea>
                        <button type='submit' disabled>Send</button>
                    </form>
                    <div class='note'>Stage 3: read-only. Sending coming in Stage 4.</div>
                </div>
            </main>
        </div>

        <script>
            let activeConvId = {initial_conv_id};

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

            async function loadMessages(convId) {{
                const res = await fetch('/api/chat/messages?conversation_id=' + convId);
                const list = await res.json();
                const el = document.getElementById('messages');
                if (!list.length) {{
                    el.innerHTML = "<div class='empty'>No messages in this conversation yet.</div>";
                    return;
                }}
                el.innerHTML = list.map(m => {{
                    const when = m.created_at
                        ? new Date(m.created_at).toLocaleString(undefined, {{hour:'numeric', minute:'2-digit'}})
                        : '';
                    return `<div class='msg ${{m.role}}'>
                                <div>
                                    <div class='bubble'>${{escapeHtml(m.content)}}</div>
                                    <div class='meta'>${{when}}</div>
                                </div>
                            </div>`;
                }}).join('');
                el.scrollTop = el.scrollHeight;
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

            function escapeHtml(s) {{
                return (s || '').replace(/[&<>"']/g, c => (
                    {{'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}}[c]
                ));
            }}

            // Initial load
            loadConversations();
            loadMessages(activeConvId);
        </script>
    </body>
    </html>"""
