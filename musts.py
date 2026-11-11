"""
musts.py — "Must accomplish": the short list of things that have to get done today.

Each morning at 6:00 ET the list resets:
  1. anything unfinished from the last list rolls over to the top, keeping the
     day it was first added (3+ days of rolling gets flagged)
  2. the rest of the 10 slots fill from the register, in the register's own
     ranking (overdue, due in 48h, blocking/high, stale waiting, the rest).
     Waiting-on items only qualify once they're stale, as "Chase ..."

James can swap any item (it goes back to being an ordinary register task),
remove it, promote a register task (★ on the dashboard, /must 3772 in
Telegram) or add a one-off line that isn't a register task at all
(/must call Marc re budget). Checking off a must that came from a task closes
that task in the register too.

The "active day" is today once 6:00 ET has passed, otherwise the last list
that was filled. The dashboard and Telegram fill today's list themselves if
the scheduler hasn't yet, so nothing depends on the worker's timing.
"""
from datetime import datetime, timedelta

import register as reg
from db import get_connection

MAX_MUSTS = 10
RESET_HOUR = 6          # ET
ROLL_FLAG_DAYS = 3      # flag an item that has rolled over this many days

_schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_musts (
                id          SERIAL PRIMARY KEY,
                day         DATE NOT NULL,
                task_id     INTEGER,
                title       TEXT,
                rank        INTEGER NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'open',   -- open | done | removed
                source      TEXT NOT NULL DEFAULT 'auto',   -- auto | rollover | dashboard | telegram | chat
                first_day   DATE NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                done_at     TIMESTAMPTZ
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS daily_musts_day ON daily_musts (day)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_must_days (
                day        DATE PRIMARY KEY,
                filled_at  TIMESTAMPTZ DEFAULT NOW()
            )""")
        conn.commit()
        _schema_ready = True
    finally:
        cur.close()
        conn.close()


# ============================================================
# DAYS
# ============================================================

def _now():
    return datetime.now(reg.ET)


def active_day():
    """Today after 6:00 ET (filling it if needed); before that, the last list filled."""
    ensure_schema()
    now = _now()
    today = now.date()
    if now.hour >= RESET_HOUR:
        if not _is_filled(today):
            fill_day(today)
        return today
    if _is_filled(today):
        return today
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT MAX(day) FROM daily_must_days WHERE day < %s", (today,))
        last = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()
    if last:
        return last
    fill_day(today)       # very first run, before 6am
    return today


def _is_filled(day) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM daily_must_days WHERE day = %s", (day,))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


# ============================================================
# READ
# ============================================================

def _sync_completed(day) -> None:
    """A must whose register task was closed elsewhere counts as done."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE daily_musts m SET status = 'done', done_at = COALESCE(t.completed_at, NOW())
              FROM tasks t
             WHERE m.task_id = t.id AND m.day = %s AND m.status = 'open' AND t.status <> 'open'
        """, (day,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_list(day=None) -> list:
    """The day's musts in order (open and done; removed ones are left out)."""
    ensure_schema()
    day = day or active_day()
    _sync_completed(day)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT m.id, m.day, m.task_id, m.status, m.source, m.first_day, m.rank, m.done_at,
                   COALESCE(m.title, t.description) AS title,
                   t.project_id, p.name AS project, t.due_date, t.status AS task_status,
                   COALESCE(t.ball_in_court, 'me') AS ball_in_court, t.waiting_on,
                   (m.day - m.first_day) AS rolled_days
              FROM daily_musts m
              LEFT JOIN tasks t ON t.id = m.task_id
              LEFT JOIN projects p ON p.id = t.project_id
             WHERE m.day = %s AND m.status <> 'removed'
             ORDER BY m.rank, m.id
        """, (day,))
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    for r in rows:
        if r["ball_in_court"] == "waiting" and r["waiting_on"] and r["task_id"] and r["title"] and not r["title"].startswith("Chase"):
            r["title"] = f"Chase {r['waiting_on']}: {r['title']}"
        r["flagged"] = (r["rolled_days"] or 0) >= ROLL_FLAG_DAYS
        r["late"] = (day - r["due_date"]).days if r.get("due_date") else None
    return rows


def summary(rows=None) -> dict:
    rows = rows if rows is not None else get_list()
    done = sum(1 for r in rows if r["status"] == "done")
    return {"done": done, "total": len(rows), "open": len(rows) - done,
            "topped_out": bool(rows) and done == len(rows)}


def for_page() -> dict:
    """What the Home page needs, JSON-ready."""
    day = active_day()
    rows = get_list(day)
    items = [{
        "id": r["id"], "title": r["title"], "task_id": r["task_id"], "status": r["status"],
        "source": r["source"], "project_id": r["project_id"], "project": r["project"],
        "due": r["due_date"].isoformat() if r["due_date"] else None, "late": r["late"],
        "rolled": r["rolled_days"] or 0, "flagged": r["flagged"],
    } for r in rows]
    return {"day": day.isoformat(), "max": MAX_MUSTS, "items": items, **summary(rows)}


# ============================================================
# FILL + ROLLOVER
# ============================================================

def _candidates(exclude_task_ids: set) -> list:
    """Register tasks in ranking order that are worth a must slot."""
    out = []
    for t in reg.get_register():
        if t["id"] in exclude_task_ids:
            continue
        if t["ball_in_court"] == "waiting" and t["tier"] != 4:
            continue          # only stale waiting items earn a slot (as a chase)
        out.append(t)
    return out


def fill_day(day=None, force: bool = False) -> dict:
    """Build the day's list: rollovers first, then the top register tasks. Idempotent per day."""
    ensure_schema()
    day = day or _now().date()
    conn = get_connection()
    cur = conn.cursor()
    try:
        # Claim the day; only one caller fills it.
        cur.execute("""INSERT INTO daily_must_days (day) VALUES (%s)
                       ON CONFLICT (day) DO NOTHING RETURNING day""", (day,))
        claimed = cur.fetchone() is not None
        conn.commit()
        if not claimed and not force:
            return {"day": day.isoformat(), "filled": False}

        # 1. roll over what's still open from the last list
        cur.execute("SELECT MAX(day) FROM daily_must_days WHERE day < %s", (day,))
        prev = cur.fetchone()[0]
        rolled = []
        if prev:
            cur.execute("""
                SELECT m.task_id, m.title, m.first_day, m.source
                  FROM daily_musts m LEFT JOIN tasks t ON t.id = m.task_id
                 WHERE m.day = %s AND m.status = 'open'
                   AND (m.task_id IS NULL OR t.status = 'open')
                 ORDER BY m.rank, m.id""", (prev,))
            rolled = cur.fetchall()
        cur.execute("SELECT task_id FROM daily_musts WHERE day = %s AND task_id IS NOT NULL", (day,))
        present = {r[0] for r in cur.fetchall()}
        rank = 0
        for task_id, title, first_day, _src in rolled:
            if task_id and task_id in present:
                continue
            rank += 1
            cur.execute("""INSERT INTO daily_musts (day, task_id, title, rank, source, first_day)
                           VALUES (%s, %s, %s, %s, 'rollover', %s)""",
                        (day, task_id, None if task_id else title, rank, first_day))
            if task_id:
                present.add(task_id)
        conn.commit()

        # 2. fill the remaining slots from the register
        cur.execute("SELECT COUNT(*) FROM daily_musts WHERE day = %s AND status <> 'removed'", (day,))
        have = cur.fetchone()[0]
        added = 0
        for t in _candidates(present):
            if have + added >= MAX_MUSTS:
                break
            added += 1
            cur.execute("""INSERT INTO daily_musts (day, task_id, rank, source, first_day)
                           VALUES (%s, %s, %s, 'auto', %s)""", (day, t["id"], rank + added, day))
        conn.commit()
        return {"day": day.isoformat(), "filled": True, "rolled": len(rolled), "added": added}
    finally:
        cur.close()
        conn.close()


# ============================================================
# CHANGES
# ============================================================

def _next_rank(cur, day) -> int:
    cur.execute("SELECT COALESCE(MAX(rank), 0) + 1 FROM daily_musts WHERE day = %s", (day,))
    return cur.fetchone()[0]


def _make_room(cur, day):
    """List full? Bump the lowest-ranked auto-picked open item back to the register. Returns its title."""
    cur.execute("SELECT COUNT(*) FROM daily_musts WHERE day = %s AND status <> 'removed'", (day,))
    if cur.fetchone()[0] < MAX_MUSTS:
        return None
    cur.execute("""
        SELECT m.id, COALESCE(m.title, t.description)
          FROM daily_musts m LEFT JOIN tasks t ON t.id = m.task_id
         WHERE m.day = %s AND m.status = 'open' AND m.source = 'auto'
         ORDER BY m.rank DESC, m.id DESC LIMIT 1""", (day,))
    row = cur.fetchone()
    if not row:
        return None           # everything is hand-picked or rolled over: go over 10 rather than drop one
    cur.execute("UPDATE daily_musts SET status = 'removed' WHERE id = %s", (row[0],))
    return row[1]


def add(title: str = None, task_id: int = None, source: str = "dashboard") -> dict:
    """Add a one-off line or promote a register task. Returns {ok, id, bumped, duplicate, error}."""
    ensure_schema()
    day = active_day()
    title = (title or "").strip() or None
    if not title and not task_id:
        return {"ok": False, "error": "Nothing to add."}
    if task_id:
        t = reg.get_task(int(task_id))
        if not t:
            return {"ok": False, "error": f"There's no task #{task_id}."}
        if t["status"] != "open":
            return {"ok": False, "error": f"#{task_id} is already closed."}
    conn = get_connection()
    cur = conn.cursor()
    try:
        if task_id:
            cur.execute("SELECT id, status FROM daily_musts WHERE day = %s AND task_id = %s ORDER BY id DESC LIMIT 1",
                        (day, task_id))
            row = cur.fetchone()
            if row and row[1] != "removed":
                return {"ok": True, "id": row[0], "duplicate": True, "bumped": None}
            if row:   # it was swapped out earlier today; bring it back
                bumped = _make_room(cur, day)
                cur.execute("UPDATE daily_musts SET status = 'open', source = %s, rank = %s WHERE id = %s",
                            (source, _next_rank(cur, day), row[0]))
                conn.commit()
                return {"ok": True, "id": row[0], "duplicate": False, "bumped": bumped}
        bumped = _make_room(cur, day)
        cur.execute("""INSERT INTO daily_musts (day, task_id, title, rank, source, first_day)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (day, task_id, None if task_id else title, _next_rank(cur, day), source, day))
        new_id = cur.fetchone()[0]
        conn.commit()
        if task_id:
            reg.touch(int(task_id))
        return {"ok": True, "id": new_id, "duplicate": False, "bumped": bumped}
    finally:
        cur.close()
        conn.close()


def get(must_id: int):
    for r in get_list(_day_of(must_id)):
        if r["id"] == int(must_id):
            return r
    return None


def _day_of(must_id: int):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT day FROM daily_musts WHERE id = %s", (int(must_id),))
        row = cur.fetchone()
        return row[0] if row else active_day()
    finally:
        cur.close()
        conn.close()


def _set(must_id: int, status: str, done: bool = False) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE daily_musts SET status = %s, done_at = {'NOW()' if done else 'NULL'} WHERE id = %s",
                    (status, int(must_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        cur.close()
        conn.close()


def complete(must_id: int) -> dict:
    """Check it off; a must that is a register task closes that task too."""
    ensure_schema()
    m = get(must_id)
    if not m:
        return {"ok": False, "error": "That item isn't on the list any more."}
    _set(must_id, "done", done=True)
    if m["task_id"] and m["task_status"] == "open":
        reg.complete_task(m["task_id"])
    return {"ok": True, "title": m["title"], **summary()}


def undo(must_id: int) -> dict:
    """Un-check it (and reopen its register task if checking it off had closed it)."""
    ensure_schema()
    m = get(must_id)
    if not m:
        return {"ok": False, "error": "That item isn't on the list any more."}
    _set(must_id, "open")
    if m["task_id"] and m["task_status"] != "open":
        conn = get_connection()
        cur = conn.cursor()
        try:
            cur.execute("""UPDATE tasks SET status = 'open', completed_at = NULL, last_touched_at = NOW()
                            WHERE id = %s""", (m["task_id"],))
            conn.commit()
        finally:
            cur.close()
            conn.close()
    return {"ok": True, "title": m["title"]}


def remove(must_id: int) -> dict:
    """Take it off today's list. A register task stays in the register as it was."""
    ensure_schema()
    m = get(must_id)
    if not m:
        return {"ok": False, "error": "That item isn't on the list any more."}
    _set(must_id, "removed")
    return {"ok": True, "title": m["title"]}


def swap(must_id: int) -> dict:
    """Replace it with the next register task that isn't already on today's list."""
    ensure_schema()
    m = get(must_id)
    if not m:
        return {"ok": False, "error": "That item isn't on the list any more."}
    day = m["day"]
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE daily_musts SET status = 'removed' WHERE id = %s", (int(must_id),))
        cur.execute("SELECT task_id FROM daily_musts WHERE day = %s AND task_id IS NOT NULL", (day,))
        present = {r[0] for r in cur.fetchall()}
        nxt = next(iter(_candidates(present)), None)
        new_title = None
        if nxt:
            cur.execute("""INSERT INTO daily_musts (day, task_id, rank, source, first_day)
                           VALUES (%s, %s, %s, 'auto', %s)""", (day, nxt["id"], m["rank"], day))
            new_title = nxt["description"]
        conn.commit()
        return {"ok": True, "title": m["title"], "replacement": new_title}
    finally:
        cur.close()
        conn.close()


# ============================================================
# TEXT (Telegram)
# ============================================================

def line(r: dict, n: int) -> str:
    mark = "✅" if r["status"] == "done" else f"{n}."
    bits = []
    if r.get("project"):
        bits.append(r["project"])
    if r["status"] != "done":
        if r.get("late") is not None and r["late"] > 0:
            bits.append(f"{r['late']}d overdue")
        elif r.get("late") == 0:
            bits.append("due today")
        if r.get("rolled_days"):
            bits.append(f"rolled {r['rolled_days']}d" + (" ⚠" if r.get("flagged") else ""))
    tail = f"  ({' · '.join(bits)})" if bits else ""
    return f"{mark} {r['title']}{tail}"
