"""
register.py — the action-item register.

One table (`tasks`), two views:
  - project register: open tasks for one project
  - master register:  every open task, ranked by fixed rules

Ranking (tier 1 is most urgent), then by due date:
  1  overdue
  2  due within the next 2 days
  3  blocking (client decision, permit or construction path) or priority 'high'
  4  waiting on someone else, with no movement for STALE_WAITING_DAYS+
  5  everything else

Rules that keep the list trustworthy:
  - every task has a due date; none given means today + DEFAULT_DUE_DAYS
  - every task says whose court the ball is in: 'me' or 'waiting' (+ who)
  - tasks extracted by Claude (sent mail, meetings) are *proposals* until
    James approves them; tasks James enters himself go straight in
  - one source item (email thread, flagged message) never makes more than
    one task

All schema changes are additive and idempotent (ADD COLUMN IF NOT EXISTS),
so this is safe to run on the live database at every start.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db import get_connection

ET = ZoneInfo("America/New_York")

DEFAULT_DUE_DAYS = 7
STALE_WAITING_DAYS = 5
DUE_SOON_DAYS = 2
PROPOSAL_BATCH_SIZE = 5
PROPOSAL_EXPIRY_DAYS = 7

TIER_LABELS = {
    1: "Overdue",
    2: "Due in 48h",
    3: "Blocking / high",
    4: "Waiting - stale",
    5: "Open",
}

_schema_ready = False


# ============================================================
# SCHEMA
# ============================================================

def ensure_register_schema() -> None:
    """Add the register columns and tables. Safe to call repeatedly."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        for ddl in (
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS ball_in_court TEXT DEFAULT 'me'",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS waiting_on TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS blocking BOOLEAN DEFAULT FALSE",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_ref TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_url TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS source_message_id TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS flag_message_id TEXT",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_touched_at TIMESTAMPTZ DEFAULT NOW()",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
        ):
            cur.execute(ddl)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS task_proposals (
                id SERIAL PRIMARY KEY,
                description TEXT NOT NULL,
                project_id INTEGER,
                ball_in_court TEXT DEFAULT 'me',
                waiting_on TEXT,
                due_date DATE,
                blocking BOOLEAN DEFAULT FALSE,
                source TEXT,
                source_ref TEXT,
                source_url TEXT,
                source_message_id TEXT,
                evidence TEXT,
                status TEXT DEFAULT 'proposed',
                task_id INTEGER,
                telegram_message_id BIGINT,
                sent_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                decided_at TIMESTAMPTZ
            )
        """)
        # What each capture path has already looked at, so nothing is
        # processed twice (a flagged email, a sent-mail thread, a meeting).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS capture_seen (
                source TEXT NOT NULL,
                ref TEXT NOT NULL,
                seen_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (source, ref)
            )
        """)
        conn.commit()
        _schema_ready = True
    finally:
        cur.close()
        conn.close()
    backfill_defaults()


def backfill_defaults() -> None:
    """
    Tasks that arrive by older paths (dashboard, SMS, lead alerts) may have no
    due date or court. Give them the defaults so nothing floats undated.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            UPDATE tasks SET due_date = (COALESCE(created_at, NOW()) AT TIME ZONE 'America/New_York')::date
                                        + {DEFAULT_DUE_DAYS}
             WHERE status = 'open' AND due_date IS NULL
        """)
        cur.execute("UPDATE tasks SET ball_in_court = 'me' WHERE ball_in_court IS NULL")
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ============================================================
# DATES
# ============================================================

def today() -> date:
    return datetime.now(ET).date()


def default_due() -> date:
    return today() + timedelta(days=DEFAULT_DUE_DAYS)


def parse_due(value):
    """YYYY-MM-DD string / date / None -> date (default when missing or unreadable)."""
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            pass
    return default_due()


def short_date(d) -> str:
    if not d:
        return "-"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return d.strftime("%a %m/%d")


# ============================================================
# RANKING
# ============================================================

def tier_sql(alias: str = "t") -> str:
    """SQL expression giving each open task its tier (1-5)."""
    a = alias
    return f"""
        CASE
          WHEN {a}.due_date < CURRENT_DATE THEN 1
          WHEN {a}.due_date <= CURRENT_DATE + INTERVAL '{DUE_SOON_DAYS} days' THEN 2
          WHEN COALESCE({a}.blocking, FALSE) OR {a}.priority = 'high' THEN 3
          WHEN {a}.ball_in_court = 'waiting'
               AND COALESCE({a}.last_touched_at, {a}.created_at, NOW())
                   < NOW() - INTERVAL '{STALE_WAITING_DAYS} days' THEN 4
          ELSE 5
        END"""


REGISTER_COLUMNS = f"""
    t.id, t.description, t.due_date, t.priority, t.status,
    p.name AS project, t.project_id,
    COALESCE(t.ball_in_court, 'me') AS ball_in_court, t.waiting_on,
    COALESCE(t.blocking, FALSE) AS blocking, t.source, t.source_url,
    t.last_touched_at, t.created_at,
    {tier_sql('t')} AS tier,
    (CURRENT_DATE - t.due_date) AS days_overdue,
    EXTRACT(DAY FROM NOW() - COALESCE(t.last_touched_at, t.created_at, NOW()))::INT AS idle_days
"""


def _rows(cur) -> list:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_register(project=None, ball=None, max_tier=None, limit=None) -> list:
    """
    Open tasks in priority order.
      project: project id (int) or name substring (str); None = master register
      ball:    'me' or 'waiting' to filter
      max_tier: only tiers <= this (e.g. 4 = the exceptions list)
    """
    ensure_register_schema()
    sql = f"""
        SELECT {REGISTER_COLUMNS}
          FROM tasks t
          LEFT JOIN projects p ON p.id = t.project_id
         WHERE t.status = 'open'
    """
    params = []
    if isinstance(project, int):
        sql += " AND t.project_id = %s"
        params.append(project)
    elif project:
        sql += " AND LOWER(p.name) LIKE %s"
        params.append(f"%{project.lower()}%")
    if ball:
        sql += " AND COALESCE(t.ball_in_court, 'me') = %s"
        params.append(ball)
    sql = f"SELECT * FROM ({sql}) r"
    if max_tier:
        sql += " WHERE r.tier <= %s"
        params.append(max_tier)
    sql += " ORDER BY r.tier, r.due_date NULLS LAST, r.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def exceptions() -> list:
    """Only what needs attention: overdue, due in 48h, stale waiting-on (tiers 1, 2, 4)."""
    return [r for r in get_register(max_tier=4) if r["tier"] in (1, 2, 4)]


# ============================================================
# PROJECTS
# ============================================================

def list_projects(active_only: bool = True) -> list:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name, status FROM projects ORDER BY name")
        rows = [{"id": r[0], "name": r[1], "status": r[2]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    if active_only:
        rows = [r for r in rows if (r["status"] or "").lower() not in
                ("complete", "completed", "closed", "cancelled", "canceled", "archived",
                 "on hold", "hold", "paused", "dormant")]
    return rows


def resolve_project(name):
    """Project id for a name / alias substring, or None if no single match."""
    if not name:
        return None
    needle = str(name).strip().lower()
    projects = list_projects(active_only=False)
    exact = [p for p in projects if p["name"].lower() == needle]
    if exact:
        return exact[0]["id"]
    hits = [p for p in projects if needle in p["name"].lower() or p["name"].lower() in needle]
    return hits[0]["id"] if len(hits) == 1 else None


def project_name(project_id):
    if not project_id:
        return None
    for p in list_projects(active_only=False):
        if p["id"] == project_id:
            return p["name"]
    return None


# ============================================================
# WRITES
# ============================================================

def add_task(description: str, project_id=None, due_date=None, ball_in_court: str = "me",
             waiting_on: str = None, blocking: bool = False, priority: str = "medium",
             source: str = "manual", source_ref: str = None, source_url: str = None,
             source_message_id: str = None, flag_message_id: str = None,
             assignee_id=None) -> dict:
    """
    Insert one task. Returns {"id", "duplicate": bool}.
    A task with the same source_ref that's still open is not added twice.
    """
    ensure_register_schema()
    ball = "waiting" if (ball_in_court == "waiting" or waiting_on) else "me"
    conn = get_connection()
    cur = conn.cursor()
    try:
        if source_ref:
            cur.execute("SELECT id FROM tasks WHERE source_ref = %s AND status = 'open' LIMIT 1",
                        (source_ref,))
            row = cur.fetchone()
            if row:
                return {"id": row[0], "duplicate": True}
        cur.execute("""
            INSERT INTO tasks (description, project_id, assignee_id, due_date, priority, status,
                               source, ball_in_court, waiting_on, blocking, source_ref, source_url,
                               source_message_id, flag_message_id, created_at, last_touched_at)
            VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING id
        """, (description.strip(), project_id, assignee_id, parse_due(due_date),
              priority if priority in ("high", "medium", "low") else "medium",
              source, ball, (waiting_on or None) if ball == "waiting" else None,
              bool(blocking), source_ref, source_url, source_message_id, flag_message_id))
        new_id = cur.fetchone()[0]
        conn.commit()
        return {"id": new_id, "duplicate": False}
    finally:
        cur.close()
        conn.close()


def get_task(task_id: int):
    ensure_register_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT {REGISTER_COLUMNS}, t.source_ref, t.source_message_id, t.flag_message_id
              FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
             WHERE t.id = %s
        """, (task_id,))
        rows = _rows(cur)
        return rows[0] if rows else None
    finally:
        cur.close()
        conn.close()


def update_task(task_id: int, **fields) -> bool:
    """Change any of: description, project_id, due_date, ball_in_court, waiting_on, blocking, priority."""
    allowed = {"description", "project_id", "due_date", "ball_in_court", "waiting_on", "blocking", "priority"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(parse_due(v) if k == "due_date" else v)
    if not sets:
        return False
    sets.append("last_touched_at = NOW()")
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = %s", params + [task_id])
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def touch(task_id: int) -> None:
    """Record movement on a task (resets the stale-waiting clock)."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE tasks SET last_touched_at = NOW() WHERE id = %s", (task_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def complete_task(task_id: int, sync_flag: bool = True):
    """
    Mark complete. Returns the task row (before completion) or None.
    sync_flag: also mark the source email's Outlook flag complete (off when
    the flag was cleared in Outlook and that is why we're closing it).
    """
    ensure_register_schema()
    task = get_task(task_id)
    if not task:
        return None
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE tasks SET status = 'complete', completed_at = NOW(), last_touched_at = NOW()
                        WHERE id = %s""", (task_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    # A task that came from a flagged email clears the flag in Outlook too.
    if task.get("flag_message_id"):
        # Forget the flag so re-flagging the same email later makes a new task.
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM capture_seen WHERE source = 'flag' AND ref = %s",
                        (task["flag_message_id"],))
            conn.commit()
        finally:
            cur.close()
            conn.close()
        if sync_flag:
            try:
                from capture import clear_outlook_flag
                clear_outlook_flag(task["flag_message_id"])
            except Exception as e:
                print(f"[register] could not clear Outlook flag for task {task_id}: {e}")
    return task


def delete_task(task_id: int) -> bool:
    """Used only by Undo right after a capture."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


# ============================================================
# SEEN-LOG (dedup for capture paths)
# ============================================================

def already_seen(source: str, ref: str) -> bool:
    ensure_register_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM capture_seen WHERE source = %s AND ref = %s", (source, ref))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def mark_seen(source: str, ref: str) -> None:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO capture_seen (source, ref) VALUES (%s, %s)
                       ON CONFLICT DO NOTHING""", (source, ref))
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ============================================================
# PROPOSALS (Claude-extracted items waiting for James)
# ============================================================

def add_proposal(description: str, project_id=None, due_date=None, ball_in_court="me",
                 waiting_on=None, blocking=False, source="", source_ref=None,
                 source_url=None, source_message_id=None, evidence=None):
    """Queue an extracted item. Skips one already proposed/approved for the same source_ref."""
    ensure_register_schema()
    ball = "waiting" if (ball_in_court == "waiting" or waiting_on) else "me"
    conn = get_connection()
    cur = conn.cursor()
    try:
        if source_ref:
            cur.execute("""SELECT 1 FROM task_proposals WHERE source_ref = %s
                           AND status IN ('proposed', 'approved')""", (source_ref,))
            if cur.fetchone():
                return None
            cur.execute("SELECT 1 FROM tasks WHERE source_ref = %s AND status = 'open'", (source_ref,))
            if cur.fetchone():
                return None
        cur.execute("""
            INSERT INTO task_proposals (description, project_id, ball_in_court, waiting_on,
                                        due_date, blocking, source, source_ref, source_url,
                                        source_message_id, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (description.strip(), project_id, ball, waiting_on if ball == "waiting" else None,
              parse_due(due_date), bool(blocking), source, source_ref, source_url,
              source_message_id, (evidence or "")[:300] or None))
        pid = cur.fetchone()[0]
        conn.commit()
        return pid
    finally:
        cur.close()
        conn.close()


def get_proposal(proposal_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""SELECT q.*, p.name AS project FROM task_proposals q
                       LEFT JOIN projects p ON p.id = q.project_id WHERE q.id = %s""", (proposal_id,))
        rows = _rows(cur)
        return rows[0] if rows else None
    finally:
        cur.close()
        conn.close()


def pending_proposals(source=None, unsent_only=False, limit=None) -> list:
    """Open proposals, most important first (blocking, then soonest due)."""
    ensure_register_schema()
    expire_old_proposals()
    sql = """SELECT q.*, p.name AS project FROM task_proposals q
             LEFT JOIN projects p ON p.id = q.project_id
             WHERE q.status = 'proposed'"""
    params = []
    if source:
        sql += " AND q.source = %s"
        params.append(source)
    if unsent_only:
        sql += " AND q.sent_at IS NULL"
    sql += " ORDER BY q.blocking DESC, q.due_date NULLS LAST, q.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return _rows(cur)
    finally:
        cur.close()
        conn.close()


def mark_proposals_sent(ids, telegram_message_id=None) -> None:
    if not ids:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE task_proposals SET sent_at = NOW(), telegram_message_id = %s
                       WHERE id = ANY(%s)""", (telegram_message_id, list(ids)))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def approve_proposal(proposal_id: int):
    """Turn a proposal into a task. Returns the new task id, or None if it was already handled."""
    q = get_proposal(proposal_id)
    if not q or q["status"] != "proposed":
        return None
    res = add_task(q["description"], project_id=q["project_id"], due_date=q["due_date"],
                   ball_in_court=q["ball_in_court"], waiting_on=q["waiting_on"],
                   blocking=q["blocking"], source=q["source"] or "proposal",
                   source_ref=q["source_ref"], source_url=q["source_url"],
                   source_message_id=q.get("source_message_id"))
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE task_proposals SET status = 'approved', task_id = %s, decided_at = NOW()
                       WHERE id = %s""", (res["id"], proposal_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return res["id"]


def drop_proposal(proposal_id: int) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""UPDATE task_proposals SET status = 'dropped', decided_at = NOW()
                       WHERE id = %s AND status = 'proposed'""", (proposal_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def expire_old_proposals() -> None:
    """Proposals nobody answered in a week go away, so the queue never piles up."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""UPDATE task_proposals SET status = 'expired', decided_at = NOW()
                        WHERE status = 'proposed'
                          AND created_at < NOW() - INTERVAL '{PROPOSAL_EXPIRY_DAYS} days'""")
        conn.commit()
    finally:
        cur.close()
        conn.close()


# ============================================================
# FORMATTING (plain text, for Telegram)
# ============================================================

def task_line(t: dict, show_project: bool = True) -> str:
    """'#123 [71 NoBE] Send revised budget - due Fri 10/23' with waiting/overdue context."""
    parts = [f"#{t['id']}"]
    if show_project:
        parts.append(f"[{t.get('project') or 'No project'}]")
    desc = t["description"]
    if (t.get("ball_in_court") == "waiting") and t.get("waiting_on"):
        desc = f"WAITING ON {t['waiting_on']}: {desc}"
    parts.append(desc)
    tail = f"due {short_date(t.get('due_date'))}"
    if t.get("tier") == 1 and t.get("days_overdue"):
        tail = f"{t['days_overdue']}d overdue"
    elif t.get("tier") == 4:
        tail = f"no movement {t.get('idle_days')}d"
    return " ".join(parts) + f" - {tail}"


def proposal_line(q: dict) -> str:
    who = f"WAITING ON {q['waiting_on']}: " if q.get("ball_in_court") == "waiting" and q.get("waiting_on") else ""
    proj = q.get("project") or "No project"
    flag = " (blocking)" if q.get("blocking") else ""
    return f"[{proj}] {who}{q['description']} - due {short_date(q.get('due_date'))}{flag}"
