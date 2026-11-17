"""
meetings.py — live meeting notes on the dashboard (/meetings).

A meeting moves through four states:

  live        James types notes and/or records audio. Audio goes up in
              ~1-minute chunks, each one transcribed by Whisper and appended
              to the transcript, so a long meeting never hits a size limit.
              A Zoom / Teams / Google Meet transcript can be pasted or
              uploaded (.vtt .srt .txt .docx .pdf) at any point.
  processing  "End meeting": Claude reads notes + transcript and writes
              minutes, decisions, open questions and action items.
  review      Action items sit in task_proposals (source 'meeting'), so they
              can be approved on the meeting page OR with Keep/Drop in
              Telegram: one approval state, two places. Nothing reaches the
              register until James approves it.
  closed      Minutes are filed as a project note.

Shorthand in the notes (all optional; Claude also finds untagged items):
  AI:  action item for someone else     "AI: Jonny - pool shop drawings Fri"
  ME:  action item for James            "ME: send revised budget to owner"
  D:   decision                         "D: owner approved tile upgrade"
  ?:   open question (carried forward)  "?: who pays for the survey"

Recurring meetings (same title, numbers and dates ignored) form a series.
A new meeting in a series opens with the series' still-open action items and
last time's open questions as its agenda, and Claude flags carried items the
notes say are now done.
"""
import io
import json
import os
import re
from datetime import datetime, timedelta

import register as reg
from db import get_connection

AUDIO_MAX_BYTES = 24 * 1024 * 1024        # Whisper's limit is 25 MB per file
MATERIAL_CAP = 150_000                    # chars of notes + transcript sent to Claude
MAX_ITEMS = 20

PLATFORMS = ("Zoom", "Teams", "Google Meet", "In person", "Phone")

_schema_ready = False


# ============================================================
# SCHEMA
# ============================================================

def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    reg.ensure_register_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id            SERIAL PRIMARY KEY,
                title         TEXT NOT NULL,
                project_id    INTEGER,
                event_id      TEXT,
                platform      TEXT,
                attendees     TEXT,
                series_key    TEXT,
                status        TEXT NOT NULL DEFAULT 'live',   -- live | processing | review | closed
                notes         TEXT NOT NULL DEFAULT '',
                transcript    TEXT NOT NULL DEFAULT '',
                minutes       TEXT,
                decisions     TEXT,      -- JSON list of strings
                questions     TEXT,      -- JSON list of strings
                carried       TEXT,      -- JSON list of {task_id, status, note} suggestions
                run           INTEGER NOT NULL DEFAULT 0,
                error         TEXT,
                note_filed    BOOLEAN NOT NULL DEFAULT FALSE,
                started_at    TIMESTAMPTZ DEFAULT NOW(),
                ended_at      TIMESTAMPTZ,
                updated_at    TIMESTAMPTZ DEFAULT NOW()
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS meetings_series ON meetings (series_key, id)")
        conn.commit()
        _schema_ready = True
    finally:
        cur.close()
        conn.close()


def _rows(cur) -> list:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


# ============================================================
# SERIES + PROJECT GUESSING
# ============================================================

_SERIES_NOISE = re.compile(
    r"\b(\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?|\d+(st|nd|rd|th)?|#\s*\d+|no\.?\s*\d+|"
    r"jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sept?(ember)?|"
    r"oct(ober)?|nov(ember)?|dec(ember)?|mon(day)?|tue(sday)?|wed(nesday)?|thu(rsday)?|fri(day)?|"
    r"weekly|bi-?weekly|recurring|meeting|mtg|call)\b", re.I)


def series_key(title: str) -> str:
    """'Farbstein OAC #14 - 11/17' and 'Farbstein OAC Meeting #15' -> 'farbstein oac'."""
    s = _SERIES_NOISE.sub(" ", (title or "").lower())
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip() or (title or "").strip().lower()


_GENERIC = {"the", "and", "residence", "project", "beach", "miami", "golden", "house", "home", "tower"}


def guess_project(*texts):
    """Project id when a project name (or a distinctive word of one) appears in the title/location."""
    hay = " ".join(t for t in texts if t).lower()
    if not hay:
        return None
    projects = reg.list_projects()
    for p in projects:
        if p["name"].lower() in hay:
            return p["id"]
    hits = []
    for p in projects:
        words = [w for w in re.findall(r"[a-z0-9]+", p["name"].lower()) if len(w) >= 4 and w not in _GENERIC]
        if any(re.search(rf"\b{re.escape(w)}\b", hay) for w in words):
            hits.append(p["id"])
    return hits[0] if len(hits) == 1 else None


def guess_platform(ev: dict) -> str:
    blob = " ".join(str(ev.get(k) or "") for k in ("location", "subject", "join_url", "provider")).lower()
    if "zoom" in blob:
        return "Zoom"
    if "meet.google" in blob or "google meet" in blob:
        return "Google Meet"
    if "teams" in blob:
        return "Teams"
    if ev.get("online_meeting"):
        return "Teams"            # Outlook's built-in online meeting is Teams
    return "In person" if ev.get("location") else ""


# ============================================================
# CALENDAR (what's on now / today)
# ============================================================

def calendar_today() -> list:
    """Today's Outlook events that haven't ended more than 30 minutes ago, 'now' ones first."""
    try:
        import outlook_calendar
        events = outlook_calendar.list_events(reg.today().isoformat())
    except Exception as e:
        print(f"[meetings] calendar unavailable: {e}")
        return []
    now = datetime.now(reg.ET).replace(tzinfo=None)
    out = []
    for ev in events:
        if ev.get("all_day"):
            continue
        try:
            start = datetime.strptime(ev["start"], "%Y-%m-%d %H:%M")
            end = datetime.strptime(ev["end"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if end < now - timedelta(minutes=30):
            continue
        ev = dict(ev)
        ev["now"] = start - timedelta(minutes=15) <= now <= end
        ev["time"] = start.strftime("%-I:%M %p")
        ev["platform"] = guess_platform(ev)
        ev["project_id"] = guess_project(ev.get("subject"), ev.get("location"))
        out.append(ev)
    out.sort(key=lambda e: (not e["now"], e["start"]))
    return out[:8]


# ============================================================
# MEETINGS
# ============================================================

def start(title: str, project_id=None, platform: str = "", attendees: str = "", event_id: str = None) -> int:
    ensure_schema()
    title = (title or "").strip() or f"Meeting {datetime.now(reg.ET).strftime('%-m/%-d %-I:%M %p')}"
    if not project_id:
        project_id = guess_project(title)
    conn = get_connection()
    cur = conn.cursor()
    try:
        if not project_id:     # same series as an earlier meeting -> same project
            cur.execute("""SELECT project_id FROM meetings WHERE series_key = %s AND project_id IS NOT NULL
                            ORDER BY id DESC LIMIT 1""", (series_key(title),))
            row = cur.fetchone()
            project_id = row[0] if row else None
        if event_id:     # tapping the same calendar event twice reopens the live meeting
            cur.execute("SELECT id FROM meetings WHERE event_id = %s AND status = 'live' ORDER BY id DESC LIMIT 1",
                        (event_id,))
            row = cur.fetchone()
            if row:
                return row[0]
        cur.execute("""INSERT INTO meetings (title, project_id, platform, attendees, event_id, series_key)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (title[:200], project_id or None, (platform or "")[:40] or None,
                     (attendees or "")[:1000] or None, event_id, series_key(title)))
        mid = cur.fetchone()[0]
        conn.commit()
        return mid
    finally:
        cur.close()
        conn.close()


def get(meeting_id: int):
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT m.*, p.name AS project FROM meetings m
                       LEFT JOIN projects p ON p.id = m.project_id WHERE m.id = %s""", (int(meeting_id),))
        rows = _rows(cur)
    finally:
        cur.close()
        conn.close()
    if not rows:
        return None
    m = rows[0]
    m["decisions"] = _json(m["decisions"], [])
    m["questions"] = _json(m["questions"], [])
    m["carried"] = _json(m["carried"], [])
    return m


def recent(limit: int = 30) -> list:
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id, m.title, m.status, m.platform, m.started_at, m.ended_at, p.name AS project,
                   (SELECT COUNT(*) FROM task_proposals q
                     WHERE q.source_ref LIKE 'mtg:' || m.id || ':%%' AND q.status = 'proposed') AS pending,
                   (SELECT COUNT(*) FROM task_proposals q
                     WHERE q.source_ref LIKE 'mtg:' || m.id || ':%%' AND q.status = 'approved') AS approved
              FROM meetings m LEFT JOIN projects p ON p.id = m.project_id
             ORDER BY (m.status IN ('live', 'processing', 'review')) DESC, m.started_at DESC
             LIMIT %s""", (limit,))
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def update_meta(meeting_id: int, title=None, project_id=None, platform=None, attendees=None) -> None:
    sets, vals = [], []
    if title is not None and title.strip():
        sets += ["title = %s", "series_key = %s"]
        vals += [title.strip()[:200], series_key(title)]
    if project_id is not None:
        sets.append("project_id = %s")
        vals.append(int(project_id) or None)
    if platform is not None:
        sets.append("platform = %s")
        vals.append(platform[:40] or None)
    if attendees is not None:
        sets.append("attendees = %s")
        vals.append(attendees[:1000] or None)
    if not sets:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE meetings SET {', '.join(sets)}, updated_at = NOW() WHERE id = %s",
                    (*vals, int(meeting_id)))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def save_notes(meeting_id: int, notes: str) -> bool:
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE meetings SET notes = %s, updated_at = NOW() WHERE id = %s",
                    ((notes or "")[:200_000], int(meeting_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def append_transcript(meeting_id: int, text: str, label: str = "") -> str:
    """Add a block to the transcript. Returns the block as stored."""
    text = (text or "").strip()
    if not text:
        return ""
    stamp = datetime.now(reg.ET).strftime("%-I:%M %p")
    block = f"[{stamp}{' · ' + label if label else ''}] {text}"
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE meetings
                          SET transcript = CASE WHEN transcript = '' THEN %s ELSE transcript || E'\\n\\n' || %s END,
                              updated_at = NOW()
                        WHERE id = %s""", (block, block, int(meeting_id)))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return block


def delete(meeting_id: int) -> None:
    """Remove a meeting and any of its proposals not yet approved (approved tasks stay)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE task_proposals SET status = 'dropped', decided_at = NOW()
                        WHERE source_ref LIKE %s AND status = 'proposed'""", (f"mtg:{int(meeting_id)}:%",))
        cur.execute("DELETE FROM meetings WHERE id = %s", (int(meeting_id),))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ============================================================
# AUDIO + TRANSCRIPT FILES
# ============================================================

def transcribe_chunk(meeting_id: int, audio: bytes, filename: str) -> str:
    """One recorded chunk -> Whisper -> appended to the transcript. '' for silence."""
    from telegram_media import MediaError, transcribe
    if not audio or len(audio) < 2000:
        return ""
    if len(audio) > AUDIO_MAX_BYTES:
        raise ValueError("That recording chunk is too large to transcribe.")
    try:
        text = transcribe(audio, filename or "chunk.webm")
    except MediaError as e:
        if "couldn't make out" in str(e).lower():
            return ""          # a quiet minute, not an error
        raise
    if _whisper_noise(text):
        return ""
    return append_transcript(meeting_id, text)


def _whisper_noise(text: str) -> bool:
    """Whisper invents a stock phrase on silence; don't fill the transcript with it."""
    t = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
    return t in ("", "you", "thank you", "thanks for watching", "thank you for watching", "bye",
                 "construction project notes for miami coastline management")


def _vtt_to_text(raw: str) -> str:
    """WebVTT / SRT -> 'Speaker: words' lines, timestamps and cue numbers dropped, repeats merged."""
    lines, last = [], None
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s == "WEBVTT" or s.isdigit() or "-->" in s or s.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        s = re.sub(r"<v\s+([^>]+)>", r"\1: ", s)
        s = re.sub(r"</?[^>]+>", "", s).strip()
        if s and s != last:
            lines.append(s)
            last = s
    return "\n".join(lines)


def text_from_file(filename: str, content: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith((".vtt", ".srt")):
        return _vtt_to_text(content.decode("utf-8", errors="replace"))
    if name.endswith((".txt", ".md", ".csv")):
        return content.decode("utf-8", errors="replace")
    if name.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    raise ValueError("Upload a .vtt, .srt, .txt, .docx or .pdf transcript, or paste the text.")


def add_transcript_text(meeting_id: int, text: str, label: str = "Transcript") -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("That transcript was empty.")
    append_transcript(meeting_id, text[:400_000], label)
    return len(text)


# ============================================================
# CARRY-FORWARD (recurring meetings)
# ============================================================

def previous_in_series(m: dict):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id, title, started_at, questions FROM meetings
                        WHERE series_key = %s AND id < %s AND status IN ('review', 'closed')
                          AND (project_id IS NULL OR %s IS NULL OR project_id = %s)
                        ORDER BY id DESC LIMIT 1""",
                    (m["series_key"], m["id"], m["project_id"], m["project_id"]))
        rows = _rows(cur)
        return rows[0] if rows else None
    finally:
        cur.close()
        conn.close()


def carried_items(m: dict) -> dict:
    """{'previous': meeting or None, 'tasks': open tasks from earlier meetings in the series, 'questions': [...]}"""
    prev = previous_in_series(m)
    if not prev:
        return {"previous": None, "tasks": [], "questions": []}
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT id FROM meetings WHERE series_key = %s AND id < %s
                          AND (project_id IS NULL OR %s IS NULL OR project_id = %s)""",
                    (m["series_key"], m["id"], m["project_id"], m["project_id"]))
        ids = [r[0] for r in cur.fetchall()]
        refs = [f"mtg:{i}:%" for i in ids]
        cur.execute("SELECT id FROM tasks WHERE status = 'open' AND source_ref LIKE ANY(%s) ORDER BY due_date, id",
                    (refs,))
        task_ids = [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    tasks = [t for t in (reg.get_task(i) for i in task_ids) if t]
    return {"previous": prev, "tasks": tasks, "questions": _json(prev.get("questions"), [])}


# ============================================================
# END MEETING -> minutes + proposals
# ============================================================

PROMPT = """You are Rowan, the PM assistant for James Galvin, owner's representative at Miami Coastline Management (South Florida and the Keys). Turn this meeting into minutes and action items.

Meeting: {title}
Project: {project}
Platform: {platform}
Attendees: {attendees}

{context}
{blocking_rule}

James's notes may use shorthand. Honor it:
  AI: <owner> - <item> <date>   action item for someone else (ball_in_court "waiting", waiting_on = owner)
  ME: <item>                    action item for James (ball_in_court "me")
  D: <text>                     a decision
  ?: <text>                     an open question
Also find untagged action items in the notes and transcript. James's notes outrank the transcript when they disagree. "I", "me", "James", "MCM" or "owner's rep" taking something on = ball_in_court "me". Owners are people or firms (GC, architect, engineer, sub, client). Skip vague talk with no deliverable; merge duplicates. At most {max_items} items.

{carried_block}

Return ONLY one JSON object:
{{"minutes": "<plain text, no markdown, under 300 words, sections DISCUSSION and NEXT MEETING (skip empty); one line per point>",
  "decisions": ["<one line each>"],
  "questions": ["<open questions still unanswered at the end>"],
  "items": [{schema_with_evidence}],
  "carried": [{{"task_id": <id from the carried list>, "status": "done" | "progress", "note": "<what was said, one line>"}}]}}

NOTES (typed by James):
{notes}

TRANSCRIPT:
{transcript}"""


def _carried_block(tasks: list, questions: list) -> str:
    if not tasks and not questions:
        return "There are no carried items from earlier meetings; return \"carried\": []."
    lines = ["Open items carried from earlier meetings in this series. Report in \"carried\" only those this meeting discussed: status done if finished, progress if there was news."]
    for t in tasks:
        who = f" (waiting on {t['waiting_on']})" if t.get("ball_in_court") == "waiting" and t.get("waiting_on") else ""
        lines.append(f"  task_id {t['id']}: {t['description']}{who}")
    if questions:
        lines.append("Questions open from last time (drop them from \"questions\" if answered): " + "; ".join(questions))
    return "\n".join(lines)


def _set(meeting_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE meetings SET {sets}, updated_at = NOW() WHERE id = %s", (*fields.values(), int(meeting_id)))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def end(meeting_id: int) -> bool:
    """Mark the meeting as processing. Returns False if there's nothing to process."""
    m = get(meeting_id)
    if not m or not ((m["notes"] or "").strip() or (m["transcript"] or "").strip()):
        return False
    _set(meeting_id, status="processing", error=None, ended_at=m["ended_at"] or datetime.now(reg.ET))
    return True


def process(meeting_id: int) -> None:
    """Background job: Claude -> minutes, decisions, questions, carried suggestions; items -> proposals."""
    from capture import BLOCKING_RULE, ITEM_SCHEMA, _ask_json, _context_block
    m = get(meeting_id)
    if not m:
        return
    try:
        carry = carried_items(m)
        notes = (m["notes"] or "").strip() or "(none)"
        transcript = (m["transcript"] or "").strip() or "(none)"
        room = MATERIAL_CAP - len(notes)
        if len(transcript) > room:
            transcript = transcript[:max(room, 20_000)] + "\n[transcript truncated]"
        schema = ITEM_SCHEMA.rstrip().rstrip("}") + ',\n  "evidence": "<the line from the notes/transcript it came from, under 20 words>"}'
        out = _ask_json(PROMPT.format(
            title=m["title"], project=m.get("project") or "not set", platform=m.get("platform") or "unknown",
            attendees=m.get("attendees") or "not listed", context=_context_block(),
            blocking_rule=BLOCKING_RULE, max_items=MAX_ITEMS,
            carried_block=_carried_block(carry["tasks"], carry["questions"]),
            schema_with_evidence=schema, notes=notes[:MATERIAL_CAP], transcript=transcript), max_tokens=6000)
        if not isinstance(out, dict):
            raise RuntimeError("Claude's answer couldn't be read.")

        run = (m["run"] or 0) + 1
        conn = get_connection()
        cur = conn.cursor()
        try:   # re-processing replaces proposals James hasn't decided on yet
            cur.execute("""UPDATE task_proposals SET status = 'dropped', decided_at = NOW()
                            WHERE source_ref LIKE %s AND status = 'proposed'""", (f"mtg:{m['id']}:%",))
            conn.commit()
        finally:
            cur.close()
            conn.close()

        ids = []
        for i, it in enumerate((out.get("items") or [])[:MAX_ITEMS]):
            if not isinstance(it, dict) or not (it.get("description") or "").strip():
                continue
            pid = reg.add_proposal(
                it["description"],
                project_id=reg.resolve_project(it.get("project")) or m["project_id"],
                due_date=it.get("due_date"), ball_in_court=it.get("ball_in_court") or "me",
                waiting_on=it.get("waiting_on"), blocking=bool(it.get("blocking")),
                source="meeting", source_ref=f"mtg:{m['id']}:r{run}:{i}",
                source_url=f"/meetings/{m['id']}", evidence=it.get("evidence"))
            if pid:
                ids.append(pid)
        valid = {t["id"] for t in carry["tasks"]}
        carried = [c for c in (out.get("carried") or [])
                   if isinstance(c, dict) and c.get("task_id") in valid and c.get("status") in ("done", "progress")]
        _set(m["id"], status="review", run=run, error=None,
             minutes=(out.get("minutes") or "").strip(),
             decisions=json.dumps([str(d) for d in (out.get("decisions") or []) if d][:30]),
             questions=json.dumps([str(q) for q in (out.get("questions") or []) if q][:30]),
             carried=json.dumps(carried))
        _notify(m, ids)
    except Exception as e:
        print(f"[meetings] processing {meeting_id} failed: {e}")
        _set(meeting_id, status="live", error=f"Couldn't write the minutes: {str(e)[:200]}")


def _notify(m: dict, proposal_ids: list) -> None:
    """Telegram: a heads-up with the link, then the items with Keep/Drop buttons."""
    try:
        import register_telegram as regtg
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        n = len(proposal_ids)
        regtg._send(f"Minutes ready: {m['title']}\n{n} action item{'s' if n != 1 else ''} to review."
                    + (f"\n{base}/meetings/{m['id']}" if base else ""))
        if proposal_ids:
            regtg.send_proposal_batch(source="meeting", ids=proposal_ids, limit=MAX_ITEMS,
                                      project=m.get("project") or m["title"])
    except Exception as e:
        print(f"[meetings] telegram notice failed: {e}")


# ============================================================
# REVIEW
# ============================================================

def proposals(meeting_id: int) -> list:
    """This meeting's items from its latest run (pending, approved and dropped)."""
    m = get(meeting_id)
    if not m:
        return []
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT q.*, p.name AS project, t.status AS task_status
                         FROM task_proposals q
                         LEFT JOIN projects p ON p.id = q.project_id
                         LEFT JOIN tasks t ON t.id = q.task_id
                        WHERE q.source_ref LIKE %s
                        ORDER BY q.id""", (f"mtg:{int(meeting_id)}:r{m['run']}:%",))
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def _owns(meeting_id: int, proposal_id: int):
    q = reg.get_proposal(int(proposal_id))
    if not q or not (q.get("source_ref") or "").startswith(f"mtg:{int(meeting_id)}:"):
        return None
    return q


def edit_proposal(meeting_id: int, proposal_id: int, description=None, project_id=None, due_date=None,
                  owner: str = None) -> bool:
    """owner '' or 'me' -> James; anything else -> waiting on that person/firm."""
    q = _owns(meeting_id, proposal_id)
    if not q or q["status"] != "proposed":
        return False
    sets, vals = [], []
    if description is not None and description.strip():
        sets.append("description = %s")
        vals.append(description.strip()[:500])
    if project_id is not None:
        sets.append("project_id = %s")
        vals.append(int(project_id) or None)
    if due_date is not None:
        sets.append("due_date = %s")
        vals.append(reg.parse_due(due_date))
    if owner is not None:
        who = owner.strip()
        mine = who.lower() in ("", "me", "james", "mcm")
        sets += ["ball_in_court = %s", "waiting_on = %s"]
        vals += ["me" if mine else "waiting", None if mine else who[:120]]
    if not sets:
        return True
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE task_proposals SET {', '.join(sets)} WHERE id = %s", (*vals, int(proposal_id)))
        conn.commit()
        return True
    finally:
        cur.close()
        conn.close()


def approve(meeting_id: int, proposal_id: int):
    if not _owns(meeting_id, proposal_id):
        return None
    return reg.approve_proposal(int(proposal_id))


def drop(meeting_id: int, proposal_id: int) -> bool:
    if not _owns(meeting_id, proposal_id):
        return False
    return reg.drop_proposal(int(proposal_id))


def approve_all(meeting_id: int) -> int:
    n = 0
    for q in proposals(meeting_id):
        if q["status"] == "proposed" and reg.approve_proposal(q["id"]):
            n += 1
    return n


def apply_carried(meeting_id: int, task_id: int, action: str, note: str = "") -> bool:
    """Close a carried task ('done') or log the meeting's news on it ('note')."""
    m = get(meeting_id)
    if not m:
        return False
    t = reg.get_task(int(task_id))
    if not t:
        return False
    label = f"{m['title']} ({reg.short_date(m['started_at'].astimezone(reg.ET).date())})"
    body = f"{label}: {note}".strip() if note else f"Discussed at {label}"
    reg.add_update(int(task_id), body=body, source="meeting", action="done" if action == "done" else None)
    # Forget the suggestion so it doesn't show again.
    rest = [c for c in m["carried"] if c.get("task_id") != int(task_id)]
    _set(meeting_id, carried=json.dumps(rest))
    return True


def close(meeting_id: int) -> bool:
    """Finish the review: file the minutes as a project note (once)."""
    m = get(meeting_id)
    if not m:
        return False
    if not m["project_id"]:
        picked = {q["project_id"] for q in proposals(meeting_id) if q["status"] == "approved" and q["project_id"]}
        if len(picked) == 1:
            m["project_id"] = picked.pop()
            update_meta(meeting_id, project_id=m["project_id"])
    if m["project_id"] and not m["note_filed"] and (m["minutes"] or m["decisions"]):
        from rowan_agent import ensure_extra_schema
        ensure_extra_schema()
        parts = [f"Meeting minutes: {m['title']} ({reg.short_date(m['started_at'].astimezone(reg.ET).date())})"]
        if m["decisions"]:
            parts.append("DECISIONS\n" + "\n".join(f"- {d}" for d in m["decisions"]))
        if m["minutes"]:
            parts.append(m["minutes"])
        if m["questions"]:
            parts.append("OPEN QUESTIONS\n" + "\n".join(f"- {q}" for q in m["questions"]))
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO project_notes (project_id, note, source) VALUES (%s, %s, 'meeting')",
                        (m["project_id"], "\n\n".join(parts)))
            conn.commit()
        finally:
            cur.close()
            conn.close()
    _set(meeting_id, status="closed", note_filed=bool(m["project_id"]) or m["note_filed"])
    return True


def reopen(meeting_id: int) -> None:
    """Back to note-taking (e.g. to add a transcript that arrived later). End again to re-run."""
    _set(meeting_id, status="live")
