"""
capture.py — how action items get into the register.

Strongest signal first:

  1. Telegram /task and /wait   James types it      -> straight into the register
  2. Outlook flag               James flags an email -> straight into the register
                                (one task per flagged email; closing the task
                                 marks the flag complete, and marking the flag
                                 complete in Outlook closes the task)
  3. Sent mail                  what James promised or asked for in mail he sent
                                -> a *proposal*, reviewed in the morning batch
  4. Meeting minutes            transcript / voice note / PDF after a meeting
                                -> minutes filed as a project note, action items
                                   sent as a proposal batch right away

The inbox itself is no longer scanned for tasks. That was the noise.

Every path dedups on the email thread (conversationId): one thread never
produces more than one open task.
"""
import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from dotenv import load_dotenv

import register as reg

load_dotenv()

MODEL = "claude-sonnet-4-6"
OWNER_EMAIL = "james@miami-coastline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SENT_LOOKBACK_HOURS = 72          # covers weekends; the seen-log stops repeats
MAX_SENT_PER_PASS = 40

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

QUOTE_MARKERS = r"\n\s*(From:|Sent from|On .{5,80} wrote:|-{3,}\s*Original Message|_{5,})"
SKIP_SUBJECT = re.compile(r"^(accepted|declined|tentative|canceled|cancelled|automatic reply|out of office)\b", re.I)


# ============================================================
# CLAUDE HELPERS
# ============================================================

def _ask_json(prompt: str, content=None, max_tokens: int = 1500):
    """One Claude call that must answer with JSON. Returns parsed JSON or None."""
    blocks = list(content or []) + [{"type": "text", "text": prompt}]
    resp = client.messages.create(model=MODEL, max_tokens=max_tokens,
                                  messages=[{"role": "user", "content": blocks}])
    raw = "".join(b.text for b in resp.content if b.type == "text")
    raw = raw.replace("```json", "").replace("```", "").strip()
    start = min([i for i in (raw.find("{"), raw.find("[")) if i != -1], default=-1)
    if start == -1:
        return None
    end = max(raw.rfind("}"), raw.rfind("]")) + 1
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError:
        print(f"[capture] could not parse Claude JSON: {raw[:200]}")
        return None


def _context_block() -> str:
    today = reg.today()
    names = ", ".join(p["name"] for p in reg.list_projects()) or "(none)"
    return (f"Today is {today.strftime('%A')} {today.isoformat()} (Eastern).\n"
            f"MCM projects (use these exact names, or null if none fits): {names}\n"
            f"If no due date is stated or clearly implied, use null (the system defaults it to "
            f"{reg.DEFAULT_DUE_DAYS} days out). Resolve 'Friday', 'next week', 'EOW' against today.")


ITEM_SCHEMA = """{"description": "<imperative, under 12 words, specific deliverable>",
  "project": "<exact project name or null>",
  "ball_in_court": "me" | "waiting",
  "waiting_on": "<person or firm James is waiting on, or null>",
  "due_date": "YYYY-MM-DD or null",
  "blocking": true | false}"""

BLOCKING_RULE = ("blocking = true only for a client/owner decision, or something holding up a permit, "
                 "a submittal, a pay application or work on site.")


# ============================================================
# 1. TELEGRAM ONE-LINERS  (/task, /wait)
# ============================================================

def parse_capture(text: str, waiting: bool = False) -> dict:
    """Turn James's one line into a task dict. Falls back to the raw text if Claude fails."""
    kind = ("James is WAITING on someone else for this (ball_in_court = waiting; "
            "waiting_on = who he's waiting on)." if waiting else
            "This is something James himself has to do (ball_in_court = me), "
            "unless the wording clearly says he's waiting on someone.")
    prompt = (f"{_context_block()}\n\n{kind}\n{BLOCKING_RULE}\n\n"
              f"James typed this into his task list:\n\"{text}\"\n\n"
              f"Return ONLY one JSON object: {ITEM_SCHEMA}\n"
              f"Keep his wording; strip the date and project from the description once captured.")
    try:
        item = _ask_json(prompt, max_tokens=400)
    except Exception as e:
        print(f"[capture] parse_capture failed: {e}")
        item = None
    if not isinstance(item, dict) or not item.get("description"):
        item = {"description": text, "project": None, "due_date": None, "blocking": False,
                "ball_in_court": "waiting" if waiting else "me", "waiting_on": None}
    if waiting:
        item["ball_in_court"] = "waiting"
    return item


def capture_line(text: str, waiting: bool = False) -> dict:
    """Parse and insert. Returns the new task row."""
    item = parse_capture(text, waiting=waiting)
    res = reg.add_task(
        item["description"],
        project_id=reg.resolve_project(item.get("project")),
        due_date=item.get("due_date"),
        ball_in_court=item.get("ball_in_court") or "me",
        waiting_on=item.get("waiting_on"),
        blocking=bool(item.get("blocking")),
        source="telegram_wait" if waiting else "telegram_task",
    )
    return reg.get_task(res["id"])


# ============================================================
# GRAPH
# ============================================================

def _token():
    from outlook_mail import get_access_token
    return get_access_token()


def _get(token, path, params=None, text_body=False, all_pages=False, max_pages=10):
    """GET a Graph collection. all_pages follows @odata.nextLink (raises if any page fails)."""
    headers = {"Authorization": f"Bearer {token}"}
    if text_body:
        headers["Prefer"] = 'outlook.body-content-type="text"'
    url, out = f"{GRAPH}{path}", []
    for _ in range(max_pages if all_pages else 1):
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Graph GET {path} failed [{resp.status_code}]: {resp.text[:300]}")
        data = resp.json()
        out.extend(data.get("value", []))
        url, params = data.get("@odata.nextLink"), None
        if not url:
            break
    return out


def _flag_status(token, message_id: str):
    """'flagged' / 'complete' / 'notFlagged', or None if the email is gone or unreadable."""
    resp = requests.get(f"{GRAPH}/me/messages/{message_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"$select": "flag"}, timeout=30)
    if resp.status_code >= 400:
        return None
    return ((resp.json().get("flag") or {}).get("flagStatus"))


def new_content(body: str) -> str:
    return re.split(QUOTE_MARKERS, body or "")[0].strip()


def _addr(r) -> str:
    e = (r or {}).get("emailAddress") or {}
    return e.get("name") or e.get("address") or ""


def clear_outlook_flag(message_id: str) -> None:
    """Mark a flagged email's flag complete (called when its task is closed)."""
    token = _token()
    resp = requests.patch(f"{GRAPH}/me/messages/{message_id}",
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                          json={"flag": {"flagStatus": "complete"}}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"clear flag failed [{resp.status_code}]: {resp.text[:200]}")


# ============================================================
# 2. OUTLOOK FLAGS
# ============================================================

FLAG_PROMPT = """James flagged this email in Outlook. A flag means: "this is an action item". Turn it into exactly ONE task for his register.

{context}
{blocking_rule}

Decide whose court the ball is in:
- "me" if James has to do something (reply with an answer, review, approve, send, decide, pay).
- "waiting" if the email shows James already asked for something and is now waiting on the sender or someone else.

Email
From: {sender}
To: {to}
Subject: {subject}
Received: {received}

{body}

Return ONLY one JSON object: {schema}"""


def sync_flagged(notify=None) -> dict:
    """
    New flags -> tasks. Flag cleared in Outlook -> task closed.
    notify(task_row) is called for each new task (Telegram confirmation).
    """
    reg.ensure_register_schema()
    token = _token()
    stats = {"flagged": 0, "added": 0, "existing": 0, "closed": 0}

    flagged = _get(token, "/me/messages", params={
        "$filter": "flag/flagStatus eq 'flagged'",
        "$select": "id,conversationId,subject,from,toRecipients,receivedDateTime,webLink",
        "$top": 50,
    }, all_pages=True)
    stats["flagged"] = len(flagged)
    flagged_ids = {m["id"] for m in flagged}

    for m in flagged:
        if reg.already_seen("flag", m["id"]):
            continue
        thread_ref = f"thread:{m.get('conversationId') or m['id']}"
        existing = _open_task_for_ref(thread_ref)
        if existing:
            # Already tracked from this thread: link the flag to it, don't duplicate.
            _link_flag(existing, m["id"])
            reg.touch(existing)
            reg.mark_seen("flag", m["id"])
            stats["existing"] += 1
            continue

        body = ""
        try:
            full = requests.get(f"{GRAPH}/me/messages/{m['id']}",
                                headers={"Authorization": f"Bearer {token}",
                                         "Prefer": 'outlook.body-content-type="text"'},
                                params={"$select": "body"}, timeout=30).json()
            body = new_content((full.get("body") or {}).get("content", ""))[:5000]
        except Exception as e:
            print(f"[capture] flagged body fetch failed: {e}")

        item = _ask_json(FLAG_PROMPT.format(
            context=_context_block(), blocking_rule=BLOCKING_RULE, schema=ITEM_SCHEMA,
            sender=_addr(m.get("from")), to=", ".join(_addr(r) for r in m.get("toRecipients") or []),
            subject=m.get("subject") or "", received=(m.get("receivedDateTime") or "")[:10],
            body=body or "(no body)"), max_tokens=400)
        if not isinstance(item, dict) or not item.get("description"):
            item = {"description": f"Action: {m.get('subject') or 'flagged email'}",
                    "ball_in_court": "me"}

        res = reg.add_task(
            item["description"], project_id=reg.resolve_project(item.get("project")),
            due_date=item.get("due_date"), ball_in_court=item.get("ball_in_court") or "me",
            waiting_on=item.get("waiting_on"), blocking=bool(item.get("blocking")),
            source="outlook_flag", source_ref=thread_ref, source_url=m.get("webLink"),
            source_message_id=m["id"], flag_message_id=m["id"],
        )
        reg.mark_seen("flag", m["id"])
        if not res["duplicate"]:
            stats["added"] += 1
            if notify:
                try:
                    notify(reg.get_task(res["id"]))
                except Exception as e:
                    print(f"[capture] flag notify failed: {e}")

    # A flag marked COMPLETE in Outlook closes its task. A flag that was just
    # cleared (not completed), or an email that was deleted, leaves the task
    # open: only an explicit "done" closes anything.
    for t in _open_flag_tasks():
        if t["flag_message_id"] in flagged_ids:
            continue
        if _flag_status(token, t["flag_message_id"]) == "complete":
            reg.complete_task(t["id"], sync_flag=False)
            stats["closed"] += 1
    return stats


def _open_task_for_ref(ref: str):
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM tasks WHERE source_ref = %s AND status = 'open' LIMIT 1", (ref,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()


def _link_flag(task_id: int, message_id: str) -> None:
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE tasks SET flag_message_id = %s WHERE id = %s", (message_id, task_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def _open_flag_tasks() -> list:
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, flag_message_id FROM tasks
                       WHERE status = 'open' AND flag_message_id IS NOT NULL""")
        return [{"id": r[0], "flag_message_id": r[1]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


# ============================================================
# MOVEMENT ON WAITING-ON THREADS
# ============================================================

def touch_replied_threads(hours: int = 24) -> int:
    """
    A reply arriving on a thread James is waiting on counts as movement:
    it resets the 5-day stale clock. Nothing is closed automatically.
    """
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, source_ref FROM tasks WHERE status = 'open'
                       AND ball_in_court = 'waiting' AND source_ref LIKE 'thread:%%'""")
        waiting = {r[1].split(":", 1)[1]: r[0] for r in cur.fetchall()}
    finally:
        cur.close()
        conn.close()
    if not waiting:
        return 0
    token = _token()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    msgs = _get(token, "/me/mailFolders/inbox/messages", params={
        "$filter": f"receivedDateTime ge {since}",
        "$select": "conversationId", "$top": 100})
    touched = set()
    for m in msgs:
        tid = waiting.get(m.get("conversationId"))
        if tid and tid not in touched:
            reg.touch(tid)
            touched.add(tid)
    return len(touched)


# ============================================================
# 3. SENT MAIL  ->  proposals
# ============================================================

SENT_PROMPT = """You review emails James Galvin SENT (he is an owner's representative on construction projects). Find real commitments only. Most sent emails contain none, and that is the right answer for them.

{context}
{blocking_rule}

Two kinds count:
A. James PROMISED a specific deliverable or action ("I'll send the revised budget Friday", "I will review the pay app and revert by Tuesday") -> ball_in_court "me".
B. James ASKED someone for a specific deliverable, ideally with a date ("Please send the survey by the 20th", "Can you get me the updated schedule this week?") -> ball_in_court "waiting", waiting_on = that person or firm.

Do NOT count: thanks, acknowledgements, "let me know", "happy to discuss", "keep me posted", FYIs, opinions, meeting logistics that are already a calendar invite, questions answerable in a reply with no deliverable, things already done in the email itself (an attachment he's sending now), vague intentions with no deliverable.

Return at most ONE item per email: the single most important commitment. If none qualifies, return {{"items": []}}.

Emails (each starts with ### and its index):
{emails}

Return ONLY: {{"items": [{{"email_index": <int>,
  "evidence": "<the exact sentence, under 20 words>",
  "description": "<imperative, under 12 words, specific deliverable>",
  "project": "<exact project name or null>",
  "ball_in_court": "me" | "waiting",
  "waiting_on": "<person or firm, or null>",
  "due_date": "YYYY-MM-DD or null",
  "blocking": true | false}}]}}"""


def scan_sent_mail(hours: int = SENT_LOOKBACK_HOURS) -> dict:
    """Queue proposals from mail James sent. Nothing goes in the register until he approves."""
    reg.ensure_register_schema()
    token = _token()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    msgs = _get(token, "/me/mailFolders/sentitems/messages", text_body=True, params={
        "$filter": f"sentDateTime ge {since}",
        "$orderby": "sentDateTime desc",
        "$select": "id,conversationId,subject,toRecipients,ccRecipients,sentDateTime,webLink,body",
        "$top": MAX_SENT_PER_PASS,
    })
    stats = {"sent": len(msgs), "considered": 0, "proposed": 0}

    batch, seen_threads = [], set()
    for m in msgs:
        if reg.already_seen("sent", m["id"]):
            continue
        reg.mark_seen("sent", m["id"])
        subject = m.get("subject") or ""
        body = new_content((m.get("body") or {}).get("content", ""))
        conv = m.get("conversationId") or m["id"]
        if SKIP_SUBJECT.match(subject) or len(body) < 25 or conv in seen_threads:
            continue
        seen_threads.add(conv)
        batch.append((m, body[:2500]))
    stats["considered"] = len(batch)
    if not batch:
        return stats

    for start in range(0, len(batch), 10):
        chunk = batch[start:start + 10]
        emails = "\n\n".join(
            f"### {i}\nTo: {', '.join(_addr(r) for r in m.get('toRecipients') or [])}\n"
            f"Cc: {', '.join(_addr(r) for r in m.get('ccRecipients') or []) or '-'}\n"
            f"Subject: {m.get('subject') or ''}\nSent: {(m.get('sentDateTime') or '')[:10]}\n\n{body}"
            for i, (m, body) in enumerate(chunk))
        out = _ask_json(SENT_PROMPT.format(context=_context_block(), blocking_rule=BLOCKING_RULE,
                                           emails=emails), max_tokens=2000)
        items = (out or {}).get("items") if isinstance(out, dict) else None
        used = set()
        for it in items or []:
            try:
                idx = int(it.get("email_index"))
                m = chunk[idx][0]
            except (TypeError, ValueError, IndexError):
                continue
            if idx in used or not it.get("description"):
                continue
            used.add(idx)
            pid = reg.add_proposal(
                it["description"], project_id=reg.resolve_project(it.get("project")),
                due_date=it.get("due_date"), ball_in_court=it.get("ball_in_court") or "me",
                waiting_on=it.get("waiting_on"), blocking=bool(it.get("blocking")),
                source="sent_mail", source_ref=f"thread:{m.get('conversationId') or m['id']}",
                source_url=m.get("webLink"), source_message_id=m["id"], evidence=it.get("evidence"))
            if pid:
                stats["proposed"] += 1
    return stats


# ============================================================
# 4. MEETING MINUTES
# ============================================================

MINUTES_PROMPT = """You are the owner's representative's PM assistant. Below is {what} from a meeting on the {project} project. Produce minutes and action items.

{context}
{blocking_rule}

Minutes: plain text (no markdown), under 250 words, sections DECISIONS, DISCUSSION, NEXT MEETING (skip empty ones). One line per point.

Action items: only real ones with a clear owner. ball_in_court "me" when James (or MCM) owns it; otherwise "waiting" with waiting_on = the owner (person or firm). Use "{project}" as the project unless an item clearly belongs elsewhere. At most 12.

Return ONLY: {{"minutes": "<text>", "items": [{schema}]}}

{material}"""


def process_minutes(project_hint: str, text: str = None, pdf_bytes: bytes = None) -> dict:
    """Minutes -> project note; action items -> proposal batch (source 'meeting')."""
    reg.ensure_register_schema()
    project_id = reg.resolve_project(project_hint)
    pname = reg.project_name(project_id) or project_hint or "unspecified"
    content, what, material = None, "a transcript or notes", f"Material:\n{(text or '')[:60000]}"
    if pdf_bytes:
        content = [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                   "data": base64.b64encode(pdf_bytes).decode()}}]
        what, material = "the attached document (minutes, transcript or notes)", ""
    out = _ask_json(MINUTES_PROMPT.format(what=what, project=pname, context=_context_block(),
                                          blocking_rule=BLOCKING_RULE, schema=ITEM_SCHEMA,
                                          material=material), content=content, max_tokens=3000)
    if not isinstance(out, dict):
        raise RuntimeError("Couldn't read that meeting material.")

    minutes = (out.get("minutes") or "").strip()
    if minutes and project_id:
        from rowan_agent import ensure_extra_schema
        from db import get_connection
        ensure_extra_schema()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO project_notes (project_id, note, source) VALUES (%s, %s, 'meeting')",
                    (project_id, f"Meeting minutes {reg.today().isoformat()}\n{minutes}"))
        conn.commit()
        cur.close()
        conn.close()

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    ids = []
    for i, it in enumerate((out.get("items") or [])[:12]):
        if not it.get("description"):
            continue
        pid = reg.add_proposal(
            it["description"],
            project_id=reg.resolve_project(it.get("project")) or project_id,
            due_date=it.get("due_date"), ball_in_court=it.get("ball_in_court") or "me",
            waiting_on=it.get("waiting_on"), blocking=bool(it.get("blocking")),
            source="meeting", source_ref=f"meeting:{project_id}:{stamp}:{i}")
        if pid:
            ids.append(pid)
    return {"project": pname, "project_id": project_id, "minutes": minutes, "proposal_ids": ids}


# ============================================================
# 5. "SEND + TASK"  (a reply James just approved)
# ============================================================

REPLY_PROMPT = """James Galvin just sent this email reply{to_part}. He tapped "Send + task", which means the reply commits him (or someone else) to something he wants on his action-item list.

{context}
{blocking_rule}

Pick the ONE most important commitment in the reply:
- something James promised to do -> ball_in_court "me"
- something James asked them to deliver -> ball_in_court "waiting", waiting_on = them
Ignore pleasantries and anything already done in the reply itself.

Subject: {subject}

Reply:
{reply}

Return ONLY one JSON object: {schema}
If the reply truly contains no commitment, return {{"description": null}}."""


def commitment_from_reply(reply: str, to: str = None, subject: str = "", conversation_id: str = None):
    """Add the commitment in a just-sent reply to the register. Returns the task row, or None."""
    reg.ensure_register_schema()
    item = _ask_json(REPLY_PROMPT.format(
        to_part=f" to {to}" if to else "", context=_context_block(), blocking_rule=BLOCKING_RULE,
        subject=subject or "", reply=(reply or "")[:4000], schema=ITEM_SCHEMA), max_tokens=400)
    if not isinstance(item, dict) or not item.get("description"):
        return None
    res = reg.add_task(
        item["description"], project_id=reg.resolve_project(item.get("project")),
        due_date=item.get("due_date"), ball_in_court=item.get("ball_in_court") or "me",
        waiting_on=item.get("waiting_on"), blocking=bool(item.get("blocking")),
        source="email_reply",
        source_ref=f"thread:{conversation_id}" if conversation_id else None)
    t = reg.get_task(res["id"])
    if res["duplicate"]:
        # The thread already has an open task: freshen it rather than duplicate.
        reg.touch(res["id"])
        t["already_tracked"] = True
    return t
