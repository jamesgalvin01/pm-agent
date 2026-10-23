"""
field_agent.py — Rowan for a field PM, locked to one project.

Same engine as rowan_agent (Claude + tool loop + propose-then-confirm), but:
  - every tool is bound to ONE project id; the model can't name another one
  - no email, calendar, leads, people directory or other projects
  - anything written is stamped with who wrote it (created_by) and source 'field'
  - nothing leaves the building: Rowan can draft a message in chat, but the
    PM sends it themselves (or James does)

Conversation history reuses the messages table, in a conversation whose
status is 'field' so it never shows up in James's own chat list.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from db import get_connection
import register
from rowan_agent import (
    client, MODEL, MAX_TOKENS, MAX_TOOL_ROUNDS,
    _load_conversation_messages, _save_message, is_affirmative,
)

ET = ZoneInfo("America/New_York")


# ============================================================
# CONVERSATIONS (one per person per project)
# ============================================================

def field_conversation(project_id: int, email: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id FROM conversations
                        WHERE status = 'field'
                          AND metadata->>'email' = %s AND (metadata->>'project_id')::int = %s
                        ORDER BY id DESC LIMIT 1""", (email, project_id))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("""INSERT INTO conversations (title, status, metadata, last_message_at)
                       VALUES (%s, 'field', %s, NOW()) RETURNING id""",
                    (f"Field: {register.project_name(project_id) or project_id}",
                     json.dumps({"email": email, "project_id": project_id})))
        conv_id = cur.fetchone()[0]
        conn.commit()
        return conv_id
    finally:
        cur.close()
        conn.close()


def visible_messages(conversation_id: int, limit: int = 60) -> list:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, role, content, created_at FROM messages
                        WHERE conversation_id = %s AND role IN ('user', 'assistant')
                          AND content IS NOT NULL AND content != ''
                        ORDER BY created_at DESC, id DESC LIMIT %s""", (conversation_id, limit))
        rows = list(reversed(cur.fetchall()))
    finally:
        cur.close()
        conn.close()
    return [{"id": r[0], "role": r[1], "content": r[2],
             "created_at": r[3].isoformat() if r[3] else None} for r in rows]


# ============================================================
# DATA (all scoped by project_id)
# ============================================================

def _rows(cur) -> list:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _ser(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _clean(rows) -> list:
    return [{k: _ser(v) for k, v in r.items() if v is not None} for r in rows]


def project_overview(project_id: int) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, name, goal, status, rag_status, start_date, end_date
                         FROM projects WHERE id = %s""", (project_id,))
        proj = _rows(cur)
        cur.execute("""SELECT id, name, due_date, status FROM milestones WHERE project_id = %s
                        ORDER BY (COALESCE(status, '') IN ('complete', 'done')), due_date NULLS LAST, id""",
                    (project_id,))
        milestones = _rows(cur)
        cur.execute("""SELECT id, description, likelihood, impact, mitigation, status FROM risks
                        WHERE project_id = %s AND COALESCE(status, 'open') NOT IN ('closed', 'resolved')
                        ORDER BY id DESC LIMIT 10""", (project_id,))
        risks = _rows(cur)
    finally:
        cur.close()
        conn.close()
    return {"project": proj[0] if proj else None, "milestones": milestones, "open_risks": risks}


def recent_notes(project_id: int, limit: int = 10) -> list:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, note, source, created_at,
                              COALESCE(created_by, '') AS created_by
                         FROM project_notes WHERE project_id = %s
                        ORDER BY created_at DESC NULLS LAST, id DESC LIMIT %s""", (project_id, limit))
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def recently_completed(project_id: int, days: int = 7) -> list:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""SELECT id, description, completed_at FROM tasks
                         WHERE project_id = %s AND status = 'complete'
                           AND completed_at > NOW() - INTERVAL '{int(days)} days'
                         ORDER BY completed_at DESC""", (project_id,))
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def task_in_project(task_id, project_id: int):
    try:
        t = register.get_task(int(task_id))
    except (TypeError, ValueError):
        return None
    return t if t and t.get("project_id") == project_id else None


def add_task(project_id: int, actor: str, description: str, due_date=None, ball_in_court="me",
             waiting_on=None, blocking=False, priority="medium") -> dict:
    res = register.add_task(description, project_id=project_id, due_date=due_date,
                            ball_in_court=ball_in_court, waiting_on=waiting_on,
                            blocking=blocking, priority=priority, source="field")
    _stamp("tasks", res["id"], actor)
    return res


def add_note(project_id: int, actor: str, note: str) -> int:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO project_notes (project_id, note, source, created_by, created_at)
                       VALUES (%s, %s, 'field', %s, NOW()) RETURNING id""",
                    (project_id, note.strip(), actor))
        nid = cur.fetchone()[0]
        conn.commit()
        return nid
    finally:
        cur.close()
        conn.close()


def add_milestone(project_id: int, name: str, due_date=None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO milestones (project_id, name, due_date, status)
                       VALUES (%s, %s, %s, 'open') RETURNING id""",
                    (project_id, name.strip(), register.parse_due(due_date) if due_date else None))
        mid = cur.fetchone()[0]
        conn.commit()
        return mid
    finally:
        cur.close()
        conn.close()


def update_milestone(project_id: int, milestone_id: int, status=None, due_date=None, name=None) -> bool:
    sets, params = [], []
    if status:
        sets.append("status = %s")
        params.append(status)
    if due_date:
        sets.append("due_date = %s")
        params.append(register.parse_due(due_date))
    if name:
        sets.append("name = %s")
        params.append(name.strip())
    if not sets:
        return False
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE milestones SET {', '.join(sets)} WHERE id = %s AND project_id = %s",
                    params + [int(milestone_id), project_id])
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def _stamp(table: str, row_id: int, actor: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE {table} SET created_by = COALESCE(created_by, %s) WHERE id = %s",
                    (actor, row_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ============================================================
# TOOLS
# ============================================================

TOOLS = [
    {"name": "get_project_overview",
     "description": "The project's basics, milestones and open risks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_action_items",
     "description": "Open action items for this project, already ranked (tier 1 overdue, 2 due in 48h, "
                    "3 blocking/high, 4 waiting with no movement 5+ days, 5 the rest). "
                    "Optionally only 'me' (our side owes it) or 'waiting' (someone else owes it).",
     "input_schema": {"type": "object", "properties": {
         "ball": {"type": "string", "enum": ["me", "waiting"]},
         "include_recently_completed": {"type": "boolean"}}}},
    {"name": "list_notes",
     "description": "Recent project notes: site observations, decisions, daily log entries.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "create_action_item",
     "description": "WRITE. Add an action item to this project's register.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string"},
         "due_date": {"type": "string", "description": "YYYY-MM-DD; omit for the 7-day default"},
         "ball_in_court": {"type": "string", "enum": ["me", "waiting"]},
         "waiting_on": {"type": "string", "description": "Who owes it, when ball_in_court is waiting"},
         "blocking": {"type": "boolean"},
         "priority": {"type": "string", "enum": ["high", "medium", "low"]}},
         "required": ["description"]}},
    {"name": "update_action_item",
     "description": "WRITE. Change an action item on this project (date, wording, who owes it, blocking, priority).",
     "input_schema": {"type": "object", "properties": {
         "task_id": {"type": "integer"},
         "description": {"type": "string"},
         "due_date": {"type": "string"},
         "ball_in_court": {"type": "string", "enum": ["me", "waiting"]},
         "waiting_on": {"type": "string"},
         "blocking": {"type": "boolean"},
         "priority": {"type": "string", "enum": ["high", "medium", "low"]}},
         "required": ["task_id"]}},
    {"name": "complete_action_item",
     "description": "WRITE. Mark an action item on this project complete.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}},
                      "required": ["task_id"]}},
    {"name": "add_note",
     "description": "WRITE. Add a project note: a site observation, decision, daily log entry, or anything "
                    "worth keeping that is not an action item.",
     "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "add_milestone",
     "description": "WRITE. Add a milestone to this project.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "due_date": {"type": "string"}}, "required": ["name"]}},
    {"name": "update_milestone",
     "description": "WRITE. Change a milestone's date or name, or mark it complete (status 'complete').",
     "input_schema": {"type": "object", "properties": {
         "milestone_id": {"type": "integer"}, "status": {"type": "string", "enum": ["open", "complete"]},
         "due_date": {"type": "string"}, "name": {"type": "string"}}, "required": ["milestone_id"]}},
]

WRITE_TOOLS = {"create_action_item", "update_action_item", "complete_action_item",
               "add_note", "add_milestone", "update_milestone"}


def _run_tool(name: str, args: dict, project_id: int, actor: str) -> dict:
    a = args or {}
    if name == "get_project_overview":
        o = project_overview(project_id)
        return {"project": {k: _ser(v) for k, v in (o["project"] or {}).items() if v is not None},
                "milestones": _clean(o["milestones"]), "open_risks": _clean(o["open_risks"])}
    if name == "list_action_items":
        rows = register.get_register(project=project_id, ball=a.get("ball"))
        keep = ("id", "description", "due_date", "priority", "ball_in_court", "waiting_on",
                "blocking", "tier", "days_overdue", "idle_days")
        out = {"open": _clean([{k: r.get(k) for k in keep} for r in rows])}
        if a.get("include_recently_completed"):
            out["completed_last_7_days"] = _clean(recently_completed(project_id))
        return out
    if name == "list_notes":
        return {"notes": _clean(recent_notes(project_id, min(int(a.get("limit") or 10), 30)))}
    if name == "create_action_item":
        if not (a.get("description") or "").strip():
            return {"error": "description is required"}
        res = add_task(project_id, actor, a["description"], a.get("due_date"),
                       a.get("ball_in_court") or "me", a.get("waiting_on"),
                       bool(a.get("blocking")), a.get("priority") or "medium")
        return {"ok": True, "task_id": res["id"], "duplicate": res["duplicate"]}
    if name == "update_action_item":
        if not task_in_project(a.get("task_id"), project_id):
            return {"error": f"Task {a.get('task_id')} isn't on this project."}
        fields = {k: a[k] for k in ("description", "due_date", "ball_in_court", "waiting_on",
                                    "blocking", "priority") if k in a}
        if fields.get("ball_in_court") == "me":
            fields["waiting_on"] = None
        elif fields.get("waiting_on"):
            fields["ball_in_court"] = "waiting"
        return {"ok": register.update_task(int(a["task_id"]), **fields)}
    if name == "complete_action_item":
        if not task_in_project(a.get("task_id"), project_id):
            return {"error": f"Task {a.get('task_id')} isn't on this project."}
        register.complete_task(int(a["task_id"]))
        return {"ok": True}
    if name == "add_note":
        if not (a.get("note") or "").strip():
            return {"error": "note is required"}
        return {"ok": True, "note_id": add_note(project_id, actor, a["note"])}
    if name == "add_milestone":
        if not (a.get("name") or "").strip():
            return {"error": "name is required"}
        return {"ok": True, "milestone_id": add_milestone(project_id, a["name"], a.get("due_date"))}
    if name == "update_milestone":
        return {"ok": update_milestone(project_id, a.get("milestone_id"), a.get("status"),
                                       a.get("due_date"), a.get("name"))}
    return {"error": f"Unknown tool: {name}"}


# ============================================================
# PROMPT
# ============================================================

SYSTEM_TEMPLATE = """You are Rowan, the project assistant for Miami Coastline Management (MCM), the Owner's Representative on {project}. You are working with {user}, {role} on this project. James Galvin runs MCM and sees everything recorded here.

# Scope
- You only know about {project}. You can't see other projects, email, calendars or contacts, and you can't send anything to anyone. If asked about something outside this project, say it's outside what you can see here.
- If {user} wants a message sent (to the owner, architect, a sub), draft it in the chat for them to send, and note that anything going to the Owner on MCM's behalf goes through James.

# How you talk
- {user} is usually on site, on a phone. Be short: a few lines or a tight numbered list. No filler, no markdown headers.
- If you're missing something you need, ask one question. Never invent dates, names or figures.
- If you spot something adjacent that matters (overdue item, stale waiting-on, blocking item), mention it in one line.

# Reading
- Use get_project_overview, list_action_items and list_notes freely; no need to ask.
- Keep the register's ranking when listing items. Refer to items by #id.

# Writing (propose, then confirm)
- Every change (create_action_item, update_action_item, complete_action_item, add_note, add_milestone, update_milestone) must be proposed first: say exactly what you'll save, then ask "Save this?". Don't call the write tool in the same turn as the proposal.
- Only save after a clear yes ("yes", "go ahead", "do it"). Then confirm in one line.
- Several items from one message go in ONE numbered proposal so one yes saves them all.

# The register
- Each action item has a due date (7 days out if none given) and a ball_in_court: 'me' means our side (MCM / the field team) owes it; 'waiting' means someone else owes it, named in waiting_on ("waiting on the truss shop drawings from the engineer" -> waiting, waiting_on Engineer).
- Mark blocking only for an owner decision, or something holding up a permit, inspection, submittal, pay app or site work.
- Site observations, decisions and daily log content are notes, not action items.

# Daily logs and dictation
- When {user} describes the day ("daily log", a dictated rundown), propose: (1) one note with the log, tidied but faithful: crews/trades on site, work done, inspections, deliveries, weather if mentioned, issues; (2) any action items it implies. One numbered proposal.

# Right now
Today is {today} ({iso}); it's {time} Eastern.
"""


def _system(project_id: int, user_name: str, role: str) -> str:
    now = datetime.now(ET)
    return SYSTEM_TEMPLATE.format(
        project=register.project_name(project_id) or "this project",
        user=user_name or "the PM",
        role="the Owner's Representative (James)" if role == "admin" else "the field Project Manager",
        today=now.strftime("%A, %B %d, %Y"), iso=now.date().isoformat(),
        time=now.strftime("%I:%M %p").lstrip("0"),
    )


# ============================================================
# TURN
# ============================================================

def run_field_turn(conversation_id: int, project_id: int, actor_email: str, actor_name: str,
                   user_text: str, role: str = "field_pm") -> str:
    _save_message(conversation_id, "user", content=user_text)
    messages = _load_conversation_messages(conversation_id)
    system = _system(project_id, actor_name, role)
    confirmed = is_affirmative(user_text)

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=system,
                                          tools=TOOLS, messages=messages)
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        uses = [{"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "tool_use" and uses:
            _save_message(conversation_id, "assistant", content=text, tool_calls=uses)
            messages.append({"role": "assistant",
                             "content": ([{"type": "text", "text": text}] if text else []) + uses})
            results = []
            for tu in uses:
                if tu["name"] in WRITE_TOOLS and not confirmed:
                    result = {"error": "Not saved: not confirmed yet. Describe exactly what you'll save "
                                       "and ask to confirm."}
                else:
                    try:
                        result = _run_tool(tu["name"], tu["input"], project_id, actor_email)
                    except Exception as e:
                        result = {"error": f"{tu['name']} failed: {e}"}
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": json.dumps(result, default=str)})
            _save_message(conversation_id, "tool_result", tool_results=results)
            messages.append({"role": "user", "content": results})
            continue

        final = text or "(no response)"
        _save_message(conversation_id, "assistant", content=final)
        _touch(conversation_id)
        return final

    final = "I got stuck going back and forth with the data. Try a narrower question."
    _save_message(conversation_id, "assistant", content=final)
    return final


def _touch(conversation_id: int) -> None:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE conversations SET last_message_at = NOW() WHERE id = %s", (conversation_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
