"""
register_telegram.py — the register on Telegram.

Commands (wired in telegram_bot.py):
  /task <text>      something you have to do      -> added at once, with Undo
  /wait <text>      something you're waiting on   -> added at once, with Undo
  /done 12 15       close tasks
  /list             master register, ranked
  /list grotto      one project's register
  /waiting          everything you're waiting on
  /review           action items waiting for your OK (sent mail, meetings)
  /minutes Grotto   next text / voice note / PDF is that meeting's minutes
  /flags            pull flagged Outlook emails in now
  /morning          send this morning's exceptions list now
  /update <words>   log progress on a task (Rowan finds the task from the words);
                    also: reply to any message showing a task #, or a voice note
                    that starts with "update"

Buttons (callback data, all short):
  rk:<id> / rx:<id>   keep / drop one proposal
  rkall / rxall       keep / drop every open proposal on that message
  tu:<id>             undo a capture
  tp:<id>:<proj>      set a captured task's project
  td:<id>             done
  tsn:<id>            push due date 3 days
  tch:<id>            draft a chase email (goes through Rowan's confirm step)
  tko:<id>            keep a task open (after a reply on its thread went out)
  upk:<task id>       which task a pending /update is for
  upx                 cancel a pending /update
  uu:<update id>      undo a logged update (and the change it made)
"""
import time
from datetime import timedelta

import requests

import register as reg

MINUTES_WINDOW_SECONDS = 900
LIST_LIMIT = 20


def _tg():
    import telegram_bot
    return telegram_bot


def _send(text, markup=None):
    return _tg().send_message(text, reply_markup=markup)


def _edit(message_id, text, markup=None):
    tg = _tg()
    payload = {"chat_id": tg.CHAT_ID, "message_id": message_id, "text": text[:4000],
               "disable_web_page_preview": True,
               "reply_markup": markup or {"inline_keyboard": []}}
    try:
        resp = requests.post(f"{tg.API}/editMessageText", json=payload, timeout=20)
        if resp.status_code >= 400 and "not modified" not in resp.text:
            print(f"[register_tg] editMessageText failed [{resp.status_code}]: {resp.text[:200]}")
    except Exception as e:
        print(f"[register_tg] editMessageText error: {e}")


# ============================================================
# CAPTURE
# ============================================================

def _capture_text(t: dict) -> str:
    who = f"Waiting on {t['waiting_on']}" if t.get("ball_in_court") == "waiting" and t.get("waiting_on") \
        else ("Waiting" if t.get("ball_in_court") == "waiting" else "You")
    lines = [f"Added #{t['id']}: {t['description']}",
             f"{t.get('project') or 'No project'} · {who} · due {reg.short_date(t.get('due_date'))}"
             + (" · blocking" if t.get("blocking") else "")]
    if not t.get("project"):
        lines.append("Which project?")
    return "\n".join(lines)


def _capture_markup(t: dict):
    rows = []
    if not t.get("project"):
        projects = reg.list_projects()[:8]
        for i in range(0, len(projects), 2):
            rows.append([{"text": p["name"][:30], "callback_data": f"tp:{t['id']}:{p['id']}"}
                         for p in projects[i:i + 2]])
    rows.append([{"text": "Undo", "callback_data": f"tu:{t['id']}"}])
    return {"inline_keyboard": rows}


def capture(text: str, waiting: bool = False) -> None:
    from capture import capture_line
    try:
        _tg()._typing()
        t = capture_line(text, waiting=waiting)
    except Exception as e:
        print(f"[register_tg] capture failed: {e}")
        _send(f"Couldn't save that: {str(e)[:200]}")
        return
    _send(_capture_text(t), _capture_markup(t))


def notify_flag_task(t: dict) -> None:
    """Confirmation for a task made from a flagged Outlook email."""
    text = "From your Outlook flag:\n" + _capture_text(t)
    _send(text, _capture_markup(t))


# ============================================================
# LISTS
# ============================================================

def _grouped(rows: list, show_project: bool = True) -> str:
    out, current = [], None
    for r in rows:
        if r["tier"] != current:
            current = r["tier"]
            out.append(f"\n{reg.TIER_LABELS[current].upper()}")
        out.append(reg.task_line(r, show_project=show_project))
    return "\n".join(out).strip()


def send_list(project: str = "") -> None:
    project = (project or "").strip()
    if project:
        pid = reg.resolve_project(project)
        if not pid:
            _send(f"No single project matches \"{project}\". Projects: "
                  + ", ".join(p["name"] for p in reg.list_projects()))
            return
        rows = reg.get_register(project=pid)
        title = f"{reg.project_name(pid)} register: {len(rows)} open"
        body = _grouped(rows[:LIST_LIMIT], show_project=False)
    else:
        rows = reg.get_register()
        title = f"Master register: {len(rows)} open"
        body = _grouped(rows[:LIST_LIMIT])
    if not rows:
        _send(title + ". Nothing open.")
        return
    more = f"\n\n+{len(rows) - LIST_LIMIT} more on the dashboard." if len(rows) > LIST_LIMIT else ""
    _send(f"{title}\n{body}{more}\n\nClose with /done <number>.")


def send_waiting() -> None:
    rows = reg.get_register(ball="waiting")
    if not rows:
        _send("You're not waiting on anyone. Add one with /wait.")
        return
    _send(f"Waiting on others: {len(rows)}\n{_grouped(rows[:LIST_LIMIT])}")


def done(rest: str) -> None:
    ids = [int(x) for x in rest.replace(",", " ").replace("#", " ").split() if x.isdigit()]
    if not ids:
        _send("Which task? e.g. /done 12 or /done 12 15")
        return
    closed, missing = [], []
    for tid in ids:
        t = reg.complete_task(tid)
        (closed if t else missing).append(f"#{tid}" + (f" {t['description']}" if t else ""))
    msg = ("Closed:\n" + "\n".join(closed)) if closed else ""
    if missing:
        msg += ("\n" if msg else "") + "Not found: " + ", ".join(missing)
    _send(msg)


# ============================================================
# PROPOSAL BATCHES
# ============================================================

def _batch_rows(message_id) -> list:
    from db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT q.*, p.name AS project FROM task_proposals q
                       LEFT JOIN projects p ON p.id = q.project_id
                       WHERE q.telegram_message_id = %s ORDER BY q.id""", (message_id,))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def _render_batch(title: str, rows: list):
    mark = {"approved": "KEPT ", "dropped": "DROPPED ", "expired": "EXPIRED "}
    lines = [title]
    for i, q in enumerate(rows, 1):
        line = f"{i}. {mark.get(q['status'], '')}{reg.proposal_line(q)}"
        if q.get("task_id"):
            line += f" (#{q['task_id']})"
        lines.append(line)
    pending = [(i, q) for i, q in enumerate(rows, 1) if q["status"] == "proposed"]
    keyboard = [[{"text": f"Keep {i}", "callback_data": f"rk:{q['id']}"},
                 {"text": f"Drop {i}", "callback_data": f"rx:{q['id']}"}] for i, q in pending]
    if len(pending) > 1:
        keyboard.append([{"text": "Keep all", "callback_data": "rkall"},
                         {"text": "Drop all", "callback_data": "rxall"}])
    if not pending:
        kept = sum(1 for q in rows if q["status"] == "approved")
        lines.append(f"\nDone: {kept} kept, {len(rows) - kept} not.")
    else:
        lines.append("\nEdit one after keeping it: just tell Rowan (\"move #123 to Friday\").")
    return "\n".join(lines), {"inline_keyboard": keyboard}


TITLES = {
    "sent_mail": "From your sent mail: {n} possible action items.",
    "meeting": "Action items from the {project} meeting: {n}.",
    None: "Action items waiting for your OK: {n}.",
}


def send_proposal_batch(source=None, ids=None, limit=reg.PROPOSAL_BATCH_SIZE, project="") -> bool:
    """Send queued proposals as one message with Keep/Drop buttons. False if none."""
    rows = reg.pending_proposals(source=source, unsent_only=(ids is None and source is not None))
    if ids is not None:
        rows = [q for q in rows if q["id"] in set(ids)]
    rows = rows[:limit]
    if not rows:
        return False
    title = TITLES.get(source, TITLES[None]).format(n=len(rows), project=project)
    text, markup = _render_batch(title, rows)
    msg_id = _send(text, markup)
    if msg_id:
        reg.mark_proposals_sent([q["id"] for q in rows], msg_id)
        # Re-render from the database so the numbering matches the stored batch.
        rows = _batch_rows(msg_id)
        _edit(msg_id, *_render_batch(title, rows))
    return bool(msg_id)


def review(rest: str = "") -> None:
    if not send_proposal_batch(limit=10):
        _send("Nothing waiting for your OK.")


def _batch_title(message: dict) -> str:
    return (message.get("text") or "Action items").split("\n", 1)[0]


def proposal_action(action: str, ref: str, message: dict) -> None:
    msg_id = message.get("message_id")
    rows = _batch_rows(msg_id)
    if action in ("rk", "rx"):
        try:
            pid = int(ref)
        except ValueError:
            return
        if action == "rk":
            reg.approve_proposal(pid)
        else:
            reg.drop_proposal(pid)
    elif action in ("rkall", "rxall"):
        for q in rows:
            if q["status"] == "proposed":
                (reg.approve_proposal if action == "rkall" else reg.drop_proposal)(q["id"])
    _edit(msg_id, *_render_batch(_batch_title(message), _batch_rows(msg_id)))


# ============================================================
# TASK BUTTONS
# ============================================================

def task_action(action: str, ref: str, message: dict) -> None:
    msg_id = message.get("message_id")
    parts = ref.split(":")
    try:
        tid = int(parts[0])
    except (ValueError, IndexError):
        return
    t = reg.get_task(tid)
    if not t:
        _edit(msg_id, (message.get("text") or "") + "\n\n(That task no longer exists.)")
        return

    if action == "tko":
        reg.touch(tid)
        _edit(msg_id, f"Kept #{tid} open: {t['description']}")
        return

    if t["status"] == "complete" and action in ("td", "tsn", "tch"):
        _send(f"#{tid} is already closed.")
        return

    if action == "tu":
        reg.delete_task(tid)
        _edit(msg_id, f"Undone. #{tid} removed.")
    elif action == "tp" and len(parts) > 1:
        reg.update_task(tid, project_id=int(parts[1]))
        t = reg.get_task(tid)
        _edit(msg_id, _capture_text(t), {"inline_keyboard": [[{"text": "Undo", "callback_data": f"tu:{tid}"}]]})
    elif action == "td":
        reg.complete_task(tid)
        _send(f"Closed #{tid}: {t['description']}")
    elif action == "tsn":
        new_due = max(reg.today(), t["due_date"] or reg.today()) + timedelta(days=3)
        reg.update_task(tid, due_date=new_due)
        _send(f"#{tid} pushed to {reg.short_date(new_due)}.")
    elif action == "tch":
        reg.touch(tid)
        who = t.get("waiting_on") or "the person this is waiting on"
        prompt = (f"Chase task #{tid} ({t.get('project') or 'no project'}): \"{t['description']}\". "
                  f"Draft a short, polite follow-up email to {who} asking for a status and a date. "
                  "If there's an earlier email thread about it, reply on that thread (search_email); "
                  "otherwise write a new email, finding the address with lookup_person or search_email. "
                  "Propose it for my confirmation.")
        _tg().run_agent(prompt)


# ============================================================
# PROGRESS UPDATES  (/update, replies to a task, "update ..." voice notes)
# ============================================================

UPDATE_PENDING_SECONDS = 900
UNDO_KEEP = 25

UPDATE_SCHEMA = """{"task_ids": [<ids of the task(s) this is about, best match first, at most 3>],
  "confident": true | false,
  "note": "<what happened, in James's words, minus the task-identifying words and any instruction>",
  "action": null | "waiting" | "mine" | "push" | "done",
  "waiting_on": "<person/firm, only when action = waiting>",
  "due_date": "YYYY-MM-DD, only when action = push and a date is stated or implied"}"""


def parse_update(text: str, tasks: list) -> dict:
    """Ask Claude which task a progress note is about, and whether it changes the task."""
    from capture import _ask_json, _context_block
    lines = "\n".join(
        f"#{t['id']} [{t.get('project') or 'No project'}] {t['description']}"
        + (f" (waiting on {t['waiting_on']})" if t.get("ball_in_court") == "waiting" and t.get("waiting_on") else "")
        for t in tasks)
    prompt = (f"{_context_block()}\n\nJames's open action items:\n{lines}\n\n"
              f"James sent this progress update about one of them:\n\"{text}\"\n\n"
              "Pick the task it is about. confident = true only when one task clearly fits; if two or more "
              "fit about equally (e.g. several door tasks), list them and set confident = false.\n"
              "action: 'waiting' when he now waits on someone (sent it to X, X will get back); 'mine' when the "
              "ball came back to him; 'push' when he moves the date; 'done' only when he says it's finished "
              "or closed. Otherwise null - most updates are just notes.\n"
              f"Return ONLY one JSON object: {UPDATE_SCHEMA}")
    try:
        out = _ask_json(prompt, max_tokens=500)
    except Exception as e:
        print(f"[register_tg] parse_update failed: {e}")
        out = None
    if not isinstance(out, dict):
        out = {}
    valid = {t["id"] for t in tasks}
    ids = []
    for i in out.get("task_ids") or []:
        try:
            i = int(str(i).lstrip("#"))
        except ValueError:
            continue
        if i in valid and i not in ids:
            ids.append(i)
    action = out.get("action") if out.get("action") in reg.UPDATE_ACTIONS else None
    return {"task_ids": ids[:3], "confident": bool(out.get("confident")) and len(ids) >= 1,
            "note": (out.get("note") or "").strip() or text.strip(), "action": action,
            "waiting_on": out.get("waiting_on"), "due_date": out.get("due_date")}


def _remember_undo(update: dict) -> None:
    t = update["task"]
    tg = _tg()
    undo = tg.state_get("update_undo") or {}
    undo[str(update["id"])] = {"task_id": t["id"], "action": update.get("action"),
                               "ball_in_court": t.get("ball_in_court"), "waiting_on": t.get("waiting_on"),
                               "due_date": t["due_date"].isoformat() if t.get("due_date") else None}
    if len(undo) > UNDO_KEEP:
        for k in sorted(undo, key=int)[:-UNDO_KEEP]:
            undo.pop(k)
    tg.state_set("update_undo", undo)


def _save_and_confirm(task_id: int, parsed: dict, source: str) -> None:
    up = reg.add_update(task_id, body=parsed.get("note") or "", source=source, action=parsed.get("action"),
                        waiting_on=parsed.get("waiting_on"), due_date=parsed.get("due_date"))
    if not up:
        _send(f"Nothing to log on #{task_id}. Tell me what happened.")
        return
    _remember_undo(up)
    t = up["task"]
    lines = [f"Logged on #{t['id']}: {t['description']} ({t.get('project') or 'No project'})"]
    if up.get("body"):
        lines.append(f"\"{up['body']}\"")
    if up.get("action") == "waiting":
        lines.append(f"{up['action_detail']}. I'll flag it if nothing moves in {reg.STALE_WAITING_DAYS} days.")
    elif up.get("action_detail"):
        lines.append(up["action_detail"] + ".")
    _send("\n".join(lines), {"inline_keyboard": [[{"text": "Undo", "callback_data": f"uu:{up['id']}"}]]})


def log_update(text: str, source: str = "telegram", candidates=None) -> None:
    """
    Log a progress note from Telegram. candidates: task ids the message is tied to
    (a reply to a message that showed them); None = search every open task.
    """
    import re
    text = (text or "").strip()
    if not text:
        _send("What happened? e.g. /update casa petrus doors, quote received, sent to Marc")
        return
    _tg()._typing()

    # "#3767 called Miguel" / "3767 called Miguel" names the task outright.
    m = re.match(r"^#?(\d{2,6})\b[\s:,-]*(.*)$", text, re.S)
    named = reg.get_task(int(m.group(1))) if m else None
    if named and (named["status"] == "open" or candidates and named["id"] in candidates):
        candidates, text = [named["id"]], m.group(2).strip() or text

    if candidates:
        tasks = [t for t in (reg.get_task(i) for i in candidates) if t]
    else:
        tasks = reg.get_register()
    if not tasks:
        _send("I couldn't find an open task for that. Check /list for the number.")
        return

    parsed = parse_update(text, tasks)
    if len(tasks) == 1:
        parsed["task_ids"], parsed["confident"] = [tasks[0]["id"]], True
    if not parsed["task_ids"]:
        _send("I couldn't tell which task that's about. Try /update #<number> <what happened>, "
              "or reply to a message that shows the task.")
        return
    if parsed["confident"] and len(parsed["task_ids"]) == 1:
        _save_and_confirm(parsed["task_ids"][0], parsed, source)
        return

    # Ambiguous: ask, and hold the note until he picks.
    _tg().state_set("update_pending", {**parsed, "source": source, "until": time.time() + UPDATE_PENDING_SECONDS})
    by_id = {t["id"]: t for t in tasks}
    rows = [[{"text": f"#{i} {by_id[i]['description']}"[:60], "callback_data": f"upk:{i}"}]
            for i in parsed["task_ids"] if i in by_id]
    rows.append([{"text": "Cancel", "callback_data": "upx"}])
    _send(f"Which task is this for?\n\"{parsed['note']}\"", {"inline_keyboard": rows})


def update_action(action: str, ref: str, message: dict) -> None:
    """Buttons: upk (pick task for a pending update), upx (cancel), uu (undo)."""
    tg = _tg()
    msg_id = message.get("message_id")
    if action == "upx":
        tg.state_set("update_pending", {})
        _edit(msg_id, "Cancelled. Nothing logged.")
        return
    if action == "upk":
        pending = tg.state_get("update_pending") or {}
        if not pending or time.time() > float(pending.get("until", 0)):
            _edit(msg_id, "That update expired. Send it again.")
            return
        tg.state_set("update_pending", {})
        _edit(msg_id, f"Logging on #{ref}...")
        _save_and_confirm(int(ref), pending, pending.get("source") or "telegram")
        return
    if action == "uu":
        undo = tg.state_get("update_undo") or {}
        prev = undo.pop(str(ref), None)
        if not reg.delete_update(int(ref)):
            _edit(msg_id, "That update was already removed.")
            return
        if prev:
            tid = prev["task_id"]
            if prev.get("action") == "done":
                from db import get_connection
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("UPDATE tasks SET status = 'open', completed_at = NULL WHERE id = %s", (tid,))
                conn.commit()
                cur.close()
                conn.close()
            elif prev.get("action") in ("waiting", "mine"):
                reg.update_task(tid, ball_in_court=prev.get("ball_in_court") or "me", waiting_on=prev.get("waiting_on"))
            elif prev.get("action") == "push" and prev.get("due_date"):
                reg.update_task(tid, due_date=prev["due_date"])
            tg.state_set("update_undo", undo)
        _edit(msg_id, "Undone. The update was removed" + (" and the task put back as it was." if prev and prev.get("action") else "."))


def task_ids_in(message: dict) -> list:
    """Task numbers (#123) shown in the message being replied to."""
    import re
    replied = message.get("reply_to_message") or {}
    text = (replied.get("text") or "") + " " + (replied.get("caption") or "")
    if not (replied.get("from") or {}).get("is_bot"):
        return []
    # Only Rowan's task lines: "#12 [Project] ...", "Added #12: ...", "Logged on #12: ...", "Closed #12: ..."
    pattern = r"(?:^|\n)\s*(?:Added |Logged on |Closed |Kept |Closed:\s*)?#(\d{2,6})(?=:| \[| )"
    ids = []
    for x in re.findall(pattern, text):
        if int(x) not in ids and reg.get_task(int(x)):
            ids.append(int(x))
    return ids


# ============================================================
# MEETING MINUTES
# ============================================================

def start_minutes(rest: str) -> None:
    tg = _tg()
    head, _, body = (rest or "").partition("\n")
    project = head.strip()
    if not project:
        _send("Which project? e.g. /minutes 71 NoBE, then send the transcript, notes, a voice note or a PDF.")
        return
    if body.strip():
        run_minutes(project, text=body.strip())
        return
    tg.state_set("minutes_mode", {"project": project, "until": time.time() + MINUTES_WINDOW_SECONDS})
    _send(f"Ready for the {project} meeting. Send the transcript or notes (paste), a voice note, "
          f"or a PDF in the next 15 minutes. Say \"cancel\" to stop.")


def minutes_target():
    """Project name if minutes mode is on, else None."""
    st = _tg().state_get("minutes_mode") or {}
    if st.get("project") and time.time() < float(st.get("until", 0)):
        return st["project"]
    return None


def run_minutes(project: str, text: str = None, pdf_bytes: bytes = None) -> None:
    from capture import process_minutes
    tg = _tg()
    tg.state_set("minutes_mode", {})
    tg._typing()
    try:
        out = process_minutes(project, text=text, pdf_bytes=pdf_bytes)
    except Exception as e:
        print(f"[register_tg] minutes failed: {e}")
        _send(f"Couldn't process those minutes: {str(e)[:200]}")
        return
    filed = "Filed as a project note." if out["project_id"] else \
        "Not filed: I couldn't match the project. Tell Rowan which project to file it under."
    _send(f"Minutes: {out['project']}\n\n{out['minutes'] or '(nothing substantive)'}\n\n{filed}")
    if out["proposal_ids"]:
        send_proposal_batch(source="meeting", ids=out["proposal_ids"], limit=12, project=out["project"])
    else:
        _send("No action items found.")


def minutes_from_file(project: str, file_id: str, file_size: int, mime: str, filename: str) -> None:
    from telegram_media import MediaError, download
    try:
        content, path = download(file_id, file_size)
    except MediaError as e:
        _send(str(e))
        return
    name = (filename or path or "").lower()
    if (mime or "").endswith("pdf") or name.endswith(".pdf"):
        run_minutes(project, pdf_bytes=content)
    elif (mime or "").startswith("text/") or name.endswith((".txt", ".vtt", ".srt", ".md")):
        run_minutes(project, text=content.decode("utf-8", errors="replace"))
    else:
        _send("I can read minutes from a PDF, a .txt/.vtt transcript, pasted text or a voice note. "
              "Save a Word file as PDF and send it again.")


def minutes_from_voice(project: str, file_id: str, file_size: int, filename: str) -> None:
    from telegram_media import MediaError, download, transcribe
    try:
        audio, path = download(file_id, file_size)
        transcript = transcribe(audio, filename or path.rsplit("/", 1)[-1] or "voice.ogg")
    except MediaError as e:
        _send(str(e))
        return
    run_minutes(project, text=transcript)


# ============================================================
# FLAGS ON DEMAND
# ============================================================

def flags_now() -> None:
    from capture import sync_flagged
    try:
        stats = sync_flagged(notify=notify_flag_task)
    except Exception as e:
        _send(f"Couldn't check Outlook flags: {str(e)[:200]}")
        return
    if not stats["added"]:
        _send(f"No new flags. {stats['flagged']} flagged emails, all already on the list"
              + (f"; closed {stats['closed']} whose flag you completed." if stats["closed"] else "."))
