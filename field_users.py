"""
field_users.py — who besides James can sign in, and which projects they see.

A field user (a PM or superintendent on site) signs in with the same email
code as James, but lands on /field and only ever sees the projects assigned
to them here. Everything else in Rowan (leads, email, other projects, the
master register) stays James-only: those routes still use require_auth.

Tables (created on first use, additive only):
  field_users          one row per person: email, name, role, active
  field_user_projects  which projects each person can see
"""
from typing import Optional

from db import get_connection

_schema_ready = False


def ensure_field_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS field_users (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                name TEXT,
                role TEXT DEFAULT 'field_pm',
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS field_user_projects (
                user_id INTEGER NOT NULL REFERENCES field_users(id) ON DELETE CASCADE,
                project_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, project_id)
            )
        """)
        # Who added a task or note from the field (shown to James).
        cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_by TEXT")
        cur.execute("ALTER TABLE project_notes ADD COLUMN IF NOT EXISTS created_by TEXT")
        cur.execute("ALTER TABLE milestones ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
        conn.commit()
        _schema_ready = True
    finally:
        cur.close()
        conn.close()


def get_field_user(email: str) -> Optional[dict]:
    """The active field user for this email, with their project ids, or None."""
    email = (email or "").lower().strip()
    if not email:
        return None
    ensure_field_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, email, name, role FROM field_users WHERE email = %s AND active",
                    (email,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT project_id FROM field_user_projects WHERE user_id = %s ORDER BY project_id",
                    (row[0],))
        pids = [r[0] for r in cur.fetchall()]
        return {"id": row[0], "email": row[1], "name": row[2] or row[1], "role": row[3],
                "project_ids": pids}
    finally:
        cur.close()
        conn.close()


def is_field_user(email: str) -> bool:
    return get_field_user(email) is not None


def list_field_users() -> list:
    ensure_field_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.email, u.name, u.role, u.active,
                   COALESCE(STRING_AGG(p.name, ', ' ORDER BY p.name), '') AS projects
              FROM field_users u
              LEFT JOIN field_user_projects fp ON fp.user_id = u.id
              LEFT JOIN projects p ON p.id = fp.project_id
             GROUP BY u.id ORDER BY u.active DESC, u.name
        """)
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def add_field_user(email: str, name: str, project_id: int, role: str = "field_pm") -> int:
    """Create (or reactivate) a field user and give them one project. Returns the user id."""
    ensure_field_schema()
    email = email.lower().strip()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO field_users (email, name, role, active) VALUES (%s, %s, %s, TRUE)
            ON CONFLICT (email) DO UPDATE SET active = TRUE,
                name = COALESCE(NULLIF(EXCLUDED.name, ''), field_users.name)
            RETURNING id
        """, (email, (name or "").strip() or None, role))
        uid = cur.fetchone()[0]
        if project_id:
            cur.execute("""INSERT INTO field_user_projects (user_id, project_id) VALUES (%s, %s)
                           ON CONFLICT DO NOTHING""", (uid, project_id))
        conn.commit()
        return uid
    finally:
        cur.close()
        conn.close()


def set_field_user_active(user_id: int, active: bool) -> None:
    """Turning someone off takes effect on their next page load (their cookie stops working)."""
    ensure_field_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE field_users SET active = %s WHERE id = %s", (active, user_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
