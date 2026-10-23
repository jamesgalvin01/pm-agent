"""
field.py — the field PM's side of Rowan: one project, on a phone.

  /field                     project picker (or straight to the PM's one project)
  /field/{id}                Items: the project's action-item register
  /field/{id}/log            Log: milestones + notes / daily log
  /field/{id}/chat           Rowan, locked to this project (field_agent.py)
  /field/admin               James only: who can sign in, and to which project

Access: require_field_access lets in James (all projects) and active field
users (their assigned projects only). Every route checks the project id, and
every task / milestone action checks the row belongs to that project.
James's own pages (/, /leads, /emails, /chat) still use require_auth, so a
field user can never reach them.
"""
import html
import threading
import time
from datetime import timedelta
from urllib.parse import quote_plus

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import register
import field_agent as fa
from app_shell import APP_HEAD
from auth import require_auth, require_field_access
from field_users import add_field_user, list_field_users, set_field_user_active, ensure_field_schema

router = APIRouter()

esc = lambda v: html.escape(str(v or ""))

TIER_COLOR = {1: "#ff6b6b", 2: "#f5a623", 3: "#5b7cfa", 4: "#b48cff", 5: "#3a3f50"}


# ============================================================
# ACCESS
# ============================================================

def _allowed(user: dict, pid: int) -> bool:
    return user["project_ids"] is None or pid in user["project_ids"]


def _nope():
    # Redirects are the only HTTPException the app's handler passes through,
    # so anything out of bounds goes back to the person's own project list.
    raise HTTPException(status_code=307, headers={"Location": "/field"})


def _guard(user: dict, pid: int) -> None:
    if not _allowed(user, pid) or not register.project_name(pid):
        _nope()


def _visible_projects(user: dict) -> list:
    projects = register.list_projects(active_only=user["admin"])
    if user["admin"]:
        return projects
    return [p for p in projects if p["id"] in (user["project_ids"] or [])]


# ============================================================
# PAGE SHELL
# ============================================================

FIELD_CSS = """
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: #0a0b0f; color: #d4d7e0; -webkit-font-smoothing: antialiased;
       font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       padding-bottom: calc(72px + env(safe-area-inset-bottom)); }
a { color: inherit; }
.top { position: sticky; top: 0; z-index: 20; background: rgba(13,15,21,0.96);
       -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
       border-bottom: 1px solid #1c1f2a; padding: calc(12px + env(safe-area-inset-top)) 16px 12px;
       display: flex; align-items: center; gap: 12px; }
.top .logo { width: 34px; height: 34px; border-radius: 9px; overflow: hidden; flex-shrink: 0; }
.top .logo img { width: 100%; height: 100%; display: block; }
.top .t { flex: 1; min-width: 0; }
.top .t h1 { margin: 0; font-size: 16px; font-weight: 600; color: #f4f5f8; white-space: nowrap;
             overflow: hidden; text-overflow: ellipsis; }
.top .t .who { font-size: 12px; color: #6b7080; margin-top: 1px; }
.top .act { display: flex; gap: 6px; align-items: center; }
.top .act a, .top .act button { background: none; border: none; color: #8b90a0; font-size: 21px;
       padding: 6px; cursor: pointer; text-decoration: none; display: flex; }
.wrap { max-width: 760px; margin: 0 auto; padding: 16px; }
.h { font-size: 11px; font-weight: 600; color: #565a6b; text-transform: uppercase;
     letter-spacing: 1.4px; margin: 22px 0 10px; }
.h:first-child { margin-top: 4px; }
.stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.stat { background: #12141c; border: 1px solid #1c1f2a; border-radius: 12px; padding: 12px 10px;
        text-decoration: none; display: block; }
.stat .n { font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }
.stat .l { font-size: 10.5px; color: #6b7080; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 2px; }
.stat.on { border-color: #5b7cfa; }
.chips { display: flex; gap: 8px; overflow-x: auto; margin: 14px -16px 4px; padding: 0 16px; }
.chips::-webkit-scrollbar { display: none; }
.chip { flex-shrink: 0; font-size: 13px; padding: 8px 14px; border-radius: 20px; border: 1px solid #1c1f2a;
        color: #8b90a0; text-decoration: none; white-space: nowrap; background: none; font-family: inherit; }
.chip.on { background: #5b7cfa; border-color: #5b7cfa; color: #fff; }
.item { background: #12141c; border: 1px solid #1c1f2a; border-left: 3px solid #3a3f50;
        border-radius: 14px; padding: 14px; margin-bottom: 10px; }
.item .d { font-size: 15px; line-height: 1.4; color: #e8eaf0; font-weight: 500; }
.item .d .id { color: #565a6b; font-weight: 400; font-size: 12px; margin-right: 4px; }
.item .m { font-size: 13px; color: #8b90a0; margin-top: 6px; line-height: 1.5; }
.item .tier { font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.6px; margin-top: 8px; }
.item .by { font-size: 11.5px; color: #565a6b; margin-top: 4px; }
.row { display: flex; gap: 8px; margin-top: 12px; }
.row form { flex: 1; margin: 0; }
.b { width: 100%; min-height: 44px; border: none; border-radius: 10px; font-family: inherit; font-size: 14px;
     font-weight: 600; display: flex; align-items: center; justify-content: center; gap: 6px; cursor: pointer;
     -webkit-tap-highlight-color: transparent; }
.b-done { background: rgba(43,212,160,0.14); color: #2bd4a0; }
.b-grey { background: rgba(139,144,160,0.14); color: #b4b8c6; }
.b-blue { background: #5b7cfa; color: #fff; }
details.more { margin-top: 10px; }
details.more summary { font-size: 13px; color: #6b7080; cursor: pointer; list-style: none; padding: 6px 0; }
details.more summary::-webkit-details-marker { display: none; }
.f { background: #12141c; border: 1px solid #1c1f2a; border-radius: 14px; padding: 14px; margin-bottom: 14px; }
.f summary { font-weight: 600; color: #e8eaf0; cursor: pointer; list-style: none; display: flex;
             align-items: center; gap: 8px; font-size: 15px; }
.f summary::-webkit-details-marker { display: none; }
.f[open] summary { margin-bottom: 12px; }
label { display: block; font-size: 11px; color: #6b7080; text-transform: uppercase; letter-spacing: 0.5px;
        margin: 10px 0 5px; }
input, select, textarea { width: 100%; font-family: inherit; font-size: 16px; padding: 11px 12px;
        background: #0a0b0f; border: 1px solid #1c1f2a; border-radius: 10px; color: #d4d7e0; }
textarea { min-height: 110px; resize: vertical; line-height: 1.45; }
input:focus, select:focus, textarea:focus { outline: none; border-color: #5b7cfa; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.check { display: flex; align-items: center; gap: 8px; text-transform: none; letter-spacing: 0;
         font-size: 14px; color: #b4b8c6; margin-top: 12px; }
.check input { width: auto; }
.empty { color: #565a6b; text-align: center; padding: 26px 10px; font-size: 14px; }
.ok { background: rgba(43,212,160,0.12); color: #2bd4a0; border-radius: 10px; padding: 11px 14px;
      font-size: 14px; margin-bottom: 12px; }
.ms { display: flex; align-items: center; gap: 12px; background: #12141c; border: 1px solid #1c1f2a;
      border-radius: 12px; padding: 12px 14px; margin-bottom: 8px; }
.ms .x { flex: 1; min-width: 0; }
.ms .x .nm { font-size: 14.5px; color: #e8eaf0; font-weight: 500; }
.ms .x .dt { font-size: 12.5px; color: #8b90a0; margin-top: 2px; }
.ms.done .nm { color: #565a6b; text-decoration: line-through; }
.ms form button { background: rgba(43,212,160,0.14); color: #2bd4a0; border: none; border-radius: 9px;
      min-width: 44px; min-height: 40px; font-size: 18px; cursor: pointer; }
.note { background: #12141c; border: 1px solid #1c1f2a; border-radius: 12px; padding: 13px 14px; margin-bottom: 8px; }
.note .tx { font-size: 14px; line-height: 1.5; color: #d4d7e0; white-space: pre-wrap; }
.note .mt { font-size: 11.5px; color: #565a6b; margin-top: 8px; }
.tabs { position: fixed; left: 0; right: 0; bottom: 0; z-index: 30; display: flex;
        background: rgba(13,15,21,0.96); border-top: 1px solid #1c1f2a;
        -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
        padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); }
.tabs a { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; color: #6b7080;
          text-decoration: none; font-size: 11px; padding: 6px 0; -webkit-tap-highlight-color: transparent; }
.tabs a i { font-size: 23px; }
.tabs a.on { color: #5b7cfa; }
.pick a { display: flex; justify-content: space-between; align-items: center; background: #12141c;
          border: 1px solid #1c1f2a; border-radius: 14px; padding: 16px; margin-bottom: 10px;
          text-decoration: none; color: #e8eaf0; font-weight: 500; }
.pick a span { color: #6b7080; font-size: 13px; font-weight: 400; }
/* chat */
.msgs { display: flex; flex-direction: column; gap: 10px; padding-bottom: 150px; }
.msg { max-width: 88%; padding: 11px 14px; border-radius: 16px; font-size: 14.5px; line-height: 1.5;
       white-space: pre-wrap; word-wrap: break-word; }
.msg.user { align-self: flex-end; background: #5b7cfa; color: #fff; border-bottom-right-radius: 5px; }
.msg.assistant { align-self: flex-start; background: #12141c; border: 1px solid #1c1f2a; color: #d4d7e0;
                 border-bottom-left-radius: 5px; }
.msg.wait { color: #6b7080; font-style: italic; }
.composer { position: fixed; left: 0; right: 0; bottom: calc(62px + env(safe-area-inset-bottom)); z-index: 25;
            background: rgba(10,11,15,0.97); border-top: 1px solid #1c1f2a; padding: 10px 12px; }
.composer .in { max-width: 760px; margin: 0 auto; }
.composer .qs { display: flex; gap: 6px; overflow-x: auto; margin-bottom: 8px; }
.composer .qs::-webkit-scrollbar { display: none; }
.composer .qs button { flex-shrink: 0; font-size: 12.5px; padding: 6px 12px; border-radius: 16px;
            border: 1px solid #1c1f2a; background: none; color: #8b90a0; font-family: inherit; cursor: pointer; }
.composer .bar { display: flex; gap: 8px; align-items: flex-end; }
.composer textarea { min-height: 44px; max-height: 140px; height: 44px; padding: 11px 12px; }
.composer .send { width: 48px; height: 44px; flex-shrink: 0; border: none; border-radius: 10px;
            background: #5b7cfa; color: #fff; font-size: 20px; cursor: pointer; }
.composer .send:disabled { opacity: 0.4; }
@media (min-width: 761px) { .wrap { padding: 24px; } }
"""

FONTS = """
    <link rel='preconnect' href='https://fonts.googleapis.com'>
    <link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
    <link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>
    <link rel='stylesheet' href='https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.7.0/dist/tabler-icons.min.css'>"""


def _page(title: str, user: dict, body: str, project_id: int = None, tab: str = "",
          subtitle: str = "", extra_js: str = "") -> HTMLResponse:
    back = ""
    if user["admin"]:
        back = "<a href='/' aria-label='Back to Rowan'><i class='ti ti-home'></i></a>"
    elif len(user["project_ids"] or []) > 1 and project_id:
        back = "<a href='/field' aria-label='Projects'><i class='ti ti-layout-grid'></i></a>"
    tabs = ""
    if project_id:
        def t(key, href, icon, label):
            return (f"<a href='{href}' class='{'on' if tab == key else ''}'>"
                    f"<i class='ti {icon}'></i><span>{label}</span></a>")
        tabs = ("<nav class='tabs'>"
                + t("items", f"/field/{project_id}", "ti-list-check", "Items")
                + t("log", f"/field/{project_id}/log", "ti-notebook", "Log")
                + t("chat", f"/field/{project_id}/chat", "ti-message-circle", "Rowan")
                + "</nav>")
    who = subtitle or (f"{esc(user['name'])}" + (" &middot; admin view" if user["admin"] else ""))
    return HTMLResponse(f"""<!doctype html>
<html><head><title>{esc(title)} — Rowan</title>{APP_HEAD}{FONTS}<style>{FIELD_CSS}</style></head>
<body>
  <div class='top'>
    <div class='logo'><img src='/static/apple-touch-icon.png?v=2' alt=''></div>
    <div class='t'><h1>{esc(title)}</h1><div class='who'>{who}</div></div>
    <div class='act'>{back}
      <form method='post' action='/logout' style='margin:0'>
        <button type='submit' aria-label='Sign out'><i class='ti ti-logout'></i></button></form>
    </div>
  </div>
  <div class='wrap'>{body}</div>
  {tabs}
  {extra_js}
</body></html>""")


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


# ============================================================
# ADMIN (James only) — must be declared before /field/{project_id}
# ============================================================

@router.get("/field/admin", response_class=HTMLResponse)
def field_admin(ok: str = "", email: str = Depends(require_auth)):
    ensure_field_schema()
    user = {"email": email, "name": "James", "admin": True, "project_ids": None}
    projects = register.list_projects(active_only=True)
    opts = "".join(f"<option value='{p['id']}' {'selected' if 'farbstein' in p['name'].lower() else ''}>"
                   f"{esc(p['name'])}</option>" for p in projects)
    rows = ""
    for u in list_field_users():
        state = "Active" if u["active"] else "Off"
        rows += f"""
        <div class='item' style='border-left-color:{"#2bd4a0" if u["active"] else "#3a3f50"}'>
          <div class='d'>{esc(u['name'] or u['email'])}</div>
          <div class='m'>{esc(u['email'])}<br>{esc(u['projects'] or 'No project')} &middot; {state}</div>
          <div class='row'><form method='post' action='/field/admin/{u["id"]}/toggle'>
            <button class='b b-grey'>{'Turn off sign-in' if u['active'] else 'Turn sign-in back on'}</button>
          </form></div>
        </div>"""
    notice = f"<div class='ok'>{esc(ok)}</div>" if ok else ""
    body = f"""
      {notice}
      <div class='h'>Add a field user</div>
      <form class='f' method='post' action='/field/admin/add'>
        <label>Name</label><input name='name' required placeholder='Nelson De La Cruz'>
        <label>Email</label><input name='user_email' type='email' required autocomplete='off'>
        <label>Project</label><select name='project_id'>{opts}</select>
        <div class='row'><button class='b b-blue'>Give access</button></div>
        <div class='m' style='font-size:13px;color:#6b7080;margin-top:10px;line-height:1.5'>
          They sign in at the same Rowan address with their email and a 6-digit code, and only see
          this project: its action items, log and a project-only Rowan. No email, leads or other projects.</div>
      </form>
      <div class='h'>Field users</div>
      {rows or "<div class='empty'>Nobody yet.</div>"}"""
    return _page("Field access", user, body, subtitle="Who can sign in to project pages")


@router.post("/field/admin/add")
def field_admin_add(name: str = Form(...), user_email: str = Form(...),
                    project_id: int = Form(...), email: str = Depends(require_auth)):
    if user_email.lower().strip() == email:
        return _back("/field/admin?ok=" + quote_plus("That's your own email; you already see every project."))
    add_field_user(user_email, name, project_id)
    msg = f"{name.strip()} can now sign in to {register.project_name(project_id) or 'the project'}."
    return _back("/field/admin?ok=" + quote_plus(msg))


@router.post("/field/admin/{user_id}/toggle")
def field_admin_toggle(user_id: int, email: str = Depends(require_auth)):
    current = {u["id"]: u for u in list_field_users()}.get(user_id)
    if current:
        set_field_user_active(user_id, not current["active"])
    return _back("/field/admin")


# ============================================================
# PICKER
# ============================================================

@router.get("/field", response_class=HTMLResponse)
def field_home(user: dict = Depends(require_field_access)):
    projects = _visible_projects(user)
    if not user["admin"] and len(projects) == 1:
        return _back(f"/field/{projects[0]['id']}")
    links = "".join(f"<a href='/field/{p['id']}'>{esc(p['name'])}<span>Open &rsaquo;</span></a>"
                    for p in projects)
    admin = ""
    if user["admin"]:
        admin = ("<div class='h'>Access</div><div class='pick'><a href='/field/admin'>"
                 "Manage field users<span>&rsaquo;</span></a></div>")
    body = f"""<div class='h'>Projects</div>
      <div class='pick'>{links or "<div class='empty'>No projects assigned yet. Ask James to add you.</div>"}</div>
      {admin}"""
    return _page("Field", user, body, subtitle="Project pages for the field team")


# ============================================================
# ITEMS
# ============================================================

def _counts(rows: list) -> dict:
    return {
        "open": len(rows),
        "overdue": sum(1 for r in rows if r["tier"] == 1),
        "soon": sum(1 for r in rows if r["tier"] == 2),
        "stale": sum(1 for r in rows if r["tier"] == 4),
        "waiting": sum(1 for r in rows if r["ball_in_court"] == "waiting"),
    }


def _item_card(r: dict, pid: int, creators: dict) -> str:
    color = TIER_COLOR.get(r["tier"], "#3a3f50")
    if r["ball_in_court"] == "waiting":
        court = f"<i class='ti ti-hourglass'></i> Waiting on {esc(r['waiting_on'] or 'someone')}"
    else:
        court = "<i class='ti ti-user'></i> Ours"
    due = f"Due {register.short_date(r['due_date'])}"
    if r["tier"] == 1 and r.get("days_overdue"):
        due = f"<span style='color:#ff6b6b'>{r['days_overdue']}d overdue</span>"
    elif r["tier"] == 4:
        due += f" &middot; <span style='color:#b48cff'>no movement {r.get('idle_days')}d</span>"
    flags = " &middot; <span style='color:#f5a623'>Blocking</span>" if r["blocking"] else ""
    by = creators.get(r["id"])
    by_html = f"<div class='by'>Added by {esc(by)}</div>" if by else ""
    waiting_val = esc(r["waiting_on"]) if r["ball_in_court"] == "waiting" else ""
    return f"""
    <div class='item' style='border-left-color:{color}'>
      <div class='d'><span class='id'>#{r['id']}</span>{esc(r['description'])}</div>
      <div class='m'>{due} &middot; {court}{flags}</div>
      <div class='tier' style='color:{color}'>{register.TIER_LABELS.get(r['tier'], '')}</div>
      {by_html}
      <div class='row'>
        <form method='post' action='/field/{pid}/items/{r["id"]}/done'>
          <button class='b b-done'><i class='ti ti-check'></i>Done</button></form>
        <form method='post' action='/field/{pid}/items/{r["id"]}/push'>
          <button class='b b-grey'><i class='ti ti-calendar-plus'></i>+7 days</button></form>
      </div>
      <details class='more'><summary><i class='ti ti-adjustments'></i> Change who owes it / date</summary>
        <form method='post' action='/field/{pid}/items/{r["id"]}/edit'>
          <label>Waiting on (leave blank if it's ours)</label>
          <input name='waiting_on' value='{waiting_val}' placeholder='e.g. Architect, Pool sub'>
          <label>Due date</label>
          <input type='date' name='due_date' value='{r["due_date"] or ""}'>
          <label class='check'><input type='checkbox' name='blocking' value='1' {'checked' if r['blocking'] else ''}>
            Blocking (owner decision, permit, inspection or site work)</label>
          <div class='row'><button class='b b-blue'>Save</button></div>
        </form>
      </details>
    </div>"""


def _creators(task_ids: list) -> dict:
    if not task_ids:
        return {}
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT t.id, COALESCE(u.name, t.created_by) FROM tasks t
                        LEFT JOIN field_users u ON u.email = t.created_by
                        WHERE t.id = ANY(%s) AND t.created_by IS NOT NULL""", (task_ids,))
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


@router.get("/field/{project_id}", response_class=HTMLResponse)
def field_items(project_id: int, show: str = "all", added: int = 0,
                user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    ensure_field_schema()
    name = register.project_name(project_id)
    rows = register.get_register(project=project_id)
    c = _counts(rows)
    if show == "overdue":
        shown = [r for r in rows if r["tier"] == 1]
    elif show == "soon":
        shown = [r for r in rows if r["tier"] == 2]
    elif show == "stale":
        shown = [r for r in rows if r["tier"] == 4]
    elif show == "waiting":
        shown = [r for r in rows if r["ball_in_court"] == "waiting"]
    elif show == "ours":
        shown = [r for r in rows if r["ball_in_court"] != "waiting"]
    else:
        show, shown = "all", rows
    creators = _creators([r["id"] for r in shown])
    cards = "".join(_item_card(r, project_id, creators) for r in shown)

    def stat(key, n, label, color):
        return (f"<a class='stat {'on' if show == key else ''}' href='/field/{project_id}?show={key}'>"
                f"<div class='n' style='color:{color}'>{n}</div><div class='l'>{label}</div></a>")

    def chip(key, label):
        return f"<a class='chip {'on' if show == key else ''}' href='/field/{project_id}?show={key}'>{label}</a>"

    notice = f"<div class='ok'>Added #{added}.</div>" if added else ""
    body = f"""
      {notice}
      <div class='stats'>
        {stat('overdue', c['overdue'], 'Overdue', '#ff6b6b' if c['overdue'] else '#565a6b')}
        {stat('soon', c['soon'], 'Due 48h', '#f5a623' if c['soon'] else '#565a6b')}
        {stat('stale', c['stale'], 'Stale', '#b48cff' if c['stale'] else '#565a6b')}
        {stat('all', c['open'], 'Open', '#5b7cfa')}
      </div>
      <div class='chips'>
        {chip('all', f"All {c['open']}")}{chip('ours', 'Ours')}{chip('waiting', f"Waiting on others {c['waiting']}")}
      </div>
      <details class='f' style='margin-top:12px'>
        <summary><i class='ti ti-plus'></i> Add an action item</summary>
        <form method='post' action='/field/{project_id}/items/add'>
          <label>What needs to happen</label>
          <textarea name='description' required style='min-height:70px'></textarea>
          <label>Waiting on (leave blank if it's ours)</label>
          <input name='waiting_on' placeholder='e.g. Architect, Pool sub, Owner'>
          <div class='two'>
            <div><label>Due</label><input type='date' name='due_date'></div>
            <div><label>Priority</label><select name='priority'>
              <option value='medium'>Medium</option><option value='high'>High</option><option value='low'>Low</option>
            </select></div>
          </div>
          <label class='check'><input type='checkbox' name='blocking' value='1'>
            Blocking (owner decision, permit, inspection or site work)</label>
          <div class='row'><button class='b b-blue'>Add item</button></div>
          <div class='m' style='font-size:12.5px;color:#565a6b;margin-top:8px'>No date = 7 days from today.</div>
        </form>
      </details>
      <div class='h'>Action items</div>
      {cards or "<div class='empty'>Nothing here.</div>"}"""
    return _page(name, user, body, project_id, "items")


def _task_or_404(task_id: int, pid: int) -> dict:
    t = fa.task_in_project(task_id, pid)
    if not t:
        _nope()
    return t


@router.post("/field/{project_id}/items/add")
def field_item_add(project_id: int, description: str = Form(...), waiting_on: str = Form(""),
                   due_date: str = Form(""), priority: str = Form("medium"), blocking: str = Form(""),
                   user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    ensure_field_schema()
    waiting_on = waiting_on.strip()
    res = fa.add_task(project_id, user["email"], description, due_date or None,
                      "waiting" if waiting_on else "me", waiting_on or None, bool(blocking), priority)
    return _back(f"/field/{project_id}?added={res['id']}")


@router.post("/field/{project_id}/items/{task_id}/done")
def field_item_done(project_id: int, task_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    _task_or_404(task_id, project_id)
    register.complete_task(task_id)
    return _back(f"/field/{project_id}")


@router.post("/field/{project_id}/items/{task_id}/push")
def field_item_push(project_id: int, task_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    t = _task_or_404(task_id, project_id)
    base = max(t["due_date"] or register.today(), register.today())
    register.update_task(task_id, due_date=base + timedelta(days=7))
    return _back(f"/field/{project_id}")


@router.post("/field/{project_id}/items/{task_id}/edit")
def field_item_edit(project_id: int, task_id: int, waiting_on: str = Form(""), due_date: str = Form(""),
                    blocking: str = Form(""), user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    _task_or_404(task_id, project_id)
    waiting_on = waiting_on.strip()
    fields = {"ball_in_court": "waiting" if waiting_on else "me",
              "waiting_on": waiting_on or None, "blocking": bool(blocking)}
    if due_date:
        fields["due_date"] = due_date
    register.update_task(task_id, **fields)
    return _back(f"/field/{project_id}")


# ============================================================
# LOG: milestones + notes
# ============================================================

@router.get("/field/{project_id}/log", response_class=HTMLResponse)
def field_log(project_id: int, saved: int = 0, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    ensure_field_schema()
    name = register.project_name(project_id)
    o = fa.project_overview(project_id)
    ms_html = ""
    for m in o["milestones"]:
        done = (m.get("status") or "").lower() in ("complete", "done")
        btn = "" if done else (f"<form method='post' action='/field/{project_id}/milestones/{m['id']}/done'>"
                               f"<button aria-label='Mark complete'><i class='ti ti-check'></i></button></form>")
        ms_html += (f"<div class='ms {'done' if done else ''}'><div class='x'><div class='nm'>{esc(m['name'])}</div>"
                    f"<div class='dt'>{'Complete' if done else 'Target ' + register.short_date(m['due_date']) if m['due_date'] else 'No date'}</div>"
                    f"</div>{btn}</div>")
    names = {}
    try:
        names = {u["email"]: u["name"] for u in list_field_users()}
    except Exception:
        pass
    notes_html = ""
    for n in fa.recent_notes(project_id, 25):
        who = names.get(n["created_by"]) or ("James" if not n["created_by"] else n["created_by"])
        when = n["created_at"].astimezone(register.ET).strftime("%a %m/%d %-I:%M %p") if n["created_at"] else ""
        notes_html += (f"<div class='note'><div class='tx'>{esc(n['note'])}</div>"
                       f"<div class='mt'>{esc(who)} &middot; {when}</div></div>")
    notice = "<div class='ok'>Saved to the log.</div>" if saved else ""
    body = f"""
      {notice}
      <details class='f' open>
        <summary><i class='ti ti-pencil'></i> Daily log / note</summary>
        <form method='post' action='/field/{project_id}/notes/add'>
          <textarea name='note' required placeholder="Trades on site, work done, inspections, deliveries, issues. Tap the mic on your keyboard to dictate."></textarea>
          <div class='row'><button class='b b-blue'>Save to log</button></div>
        </form>
      </details>
      <div class='h'>Milestones</div>
      {ms_html or "<div class='empty'>No milestones yet.</div>"}
      <details class='f'>
        <summary><i class='ti ti-flag'></i> Add a milestone</summary>
        <form method='post' action='/field/{project_id}/milestones/add'>
          <label>Milestone</label><input name='name' required placeholder='e.g. Slab inspection'>
          <label>Target date</label><input type='date' name='due_date'>
          <div class='row'><button class='b b-blue'>Add milestone</button></div>
        </form>
      </details>
      <div class='h'>Log</div>
      {notes_html or "<div class='empty'>No entries yet.</div>"}"""
    return _page(name, user, body, project_id, "log")


@router.post("/field/{project_id}/notes/add")
def field_note_add(project_id: int, note: str = Form(...), user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    ensure_field_schema()
    if note.strip():
        fa.add_note(project_id, user["email"], note)
    return _back(f"/field/{project_id}/log?saved=1")


@router.post("/field/{project_id}/milestones/add")
def field_ms_add(project_id: int, name: str = Form(...), due_date: str = Form(""),
                 user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    if name.strip():
        fa.add_milestone(project_id, name, due_date or None)
    return _back(f"/field/{project_id}/log")


@router.post("/field/{project_id}/milestones/{milestone_id}/done")
def field_ms_done(project_id: int, milestone_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    fa.update_milestone(project_id, milestone_id, status="complete")
    return _back(f"/field/{project_id}/log")


# ============================================================
# ROWAN (project-only chat)
# ============================================================

_jobs: dict = {}
_jobs_lock = threading.Lock()
JOB_MAX_SECONDS = 300

CHAT_JS = """
<script>
(function(){
  const PID = %PID%;
  const box = document.getElementById('msgs');
  const ta = document.getElementById('ta');
  const btn = document.getElementById('send');
  function bubble(role, text, cls){
    const d = document.createElement('div');
    d.className = 'msg ' + role + (cls ? ' ' + cls : '');
    d.textContent = text;
    box.appendChild(d);
    return d;
  }
  function bottom(){ window.scrollTo(0, document.body.scrollHeight); }
  async function load(){
    const r = await fetch('/api/field/' + PID + '/messages');
    if (!r.ok) return;
    const rows = await r.json();
    box.innerHTML = '';
    if (!rows.length) bubble('assistant', "I'm Rowan, working on this project only. Ask what's open or overdue, dictate a daily log, or tell me what came up on site and I'll turn it into action items. I'll always show you what I'm about to save first.");
    rows.forEach(m => bubble(m.role, m.content));
    bottom();
  }
  async function poll(waitEl){
    for (let i = 0; i < 150; i++){
      await new Promise(r => setTimeout(r, 2000));
      const r = await fetch('/api/field/' + PID + '/status');
      const s = await r.json();
      if (s.state === 'done' || s.state === 'unknown'){ await load(); break; }
      if (s.state === 'error'){ waitEl.textContent = s.error || 'Something went wrong.'; break; }
    }
    btn.disabled = false;
  }
  async function send(text){
    text = (text || '').trim();
    if (!text || btn.disabled) return;
    btn.disabled = true;
    bubble('user', text);
    ta.value = ''; ta.style.height = '44px';
    const w = bubble('assistant', 'Working on it…', 'wait');
    bottom();
    const r = await fetch('/api/field/' + PID + '/send', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})});
    if (r.status === 202){ poll(w); }
    else { const j = await r.json().catch(() => ({})); w.textContent = j.detail || 'Could not send.'; btn.disabled = false; }
  }
  btn.addEventListener('click', () => send(ta.value));
  ta.addEventListener('input', () => { ta.style.height = '44px'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; });
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && window.innerWidth > 760){ e.preventDefault(); send(ta.value); } });
  document.querySelectorAll('[data-q]').forEach(b => b.addEventListener('click', () => {
    const q = b.getAttribute('data-q');
    if (q.endsWith(': ')){ ta.value = q; ta.focus(); } else { send(q); }
  }));
  load();
})();
</script>"""


@router.get("/field/{project_id}/chat", response_class=HTMLResponse)
def field_chat(project_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    name = register.project_name(project_id)
    body = f"""
      <div class='msgs' id='msgs'></div>
      <div class='composer'><div class='in'>
        <div class='qs'>
          <button data-q="What's overdue or due this week?">Overdue / this week</button>
          <button data-q="What are we waiting on from others?">Waiting on others</button>
          <button data-q="Daily log: ">Daily log</button>
          <button data-q="Give me a quick status of the project.">Status</button>
        </div>
        <div class='bar'>
          <textarea id='ta' placeholder='Message Rowan (tap the mic to dictate)'></textarea>
          <button class='send' id='send' aria-label='Send'><i class='ti ti-arrow-up'></i></button>
        </div>
      </div></div>"""
    return _page(name, user, body, project_id, "chat", extra_js=CHAT_JS.replace("%PID%", str(project_id)))


@router.get("/api/field/{project_id}/messages")
def field_chat_messages(project_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    conv = fa.field_conversation(project_id, user["email"])
    return JSONResponse(fa.visible_messages(conv))


@router.post("/api/field/{project_id}/send")
def field_chat_send(project_id: int, payload: dict = Body(...), user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    text = (payload.get("text") or "").strip()[:4000]
    if not text:
        return JSONResponse({"detail": "Type a message first."}, status_code=400)
    ensure_field_schema()
    conv = fa.field_conversation(project_id, user["email"])
    with _jobs_lock:
        job = _jobs.get(conv)
        if job and job["state"] == "running" and time.time() - job["started"] < JOB_MAX_SECONDS:
            return JSONResponse({"detail": "Rowan is still working on your last message."}, status_code=409)
        _jobs[conv] = {"state": "running", "error": "", "started": time.time()}

    role = "admin" if user["admin"] else "field_pm"

    def work():
        try:
            fa.run_field_turn(conv, project_id, user["email"], user["name"], text, role=role)
            state, err = "done", ""
        except Exception as e:
            print(f"[field] turn failed: {e}", flush=True)
            state, err = "error", "Rowan hit an error. Try again in a moment."
        with _jobs_lock:
            _jobs[conv] = {"state": state, "error": err, "started": time.time()}

    threading.Thread(target=work, name=f"field-turn-{conv}", daemon=True).start()
    return JSONResponse({"status": "started"}, status_code=202)


@router.get("/api/field/{project_id}/status")
def field_chat_status(project_id: int, user: dict = Depends(require_field_access)):
    _guard(user, project_id)
    conv = fa.field_conversation(project_id, user["email"])
    with _jobs_lock:
        job = dict(_jobs.get(conv) or {})
    if not job:
        return JSONResponse({"state": "unknown"})
    if job["state"] == "running" and time.time() - job["started"] > JOB_MAX_SECONDS:
        return JSONResponse({"state": "error", "error": "Rowan took too long. Refresh to see what was done."})
    return JSONResponse({"state": job["state"], "error": job.get("error", "")})
