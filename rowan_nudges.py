"""
rowan_nudges.py — Rowan reaches out to James proactively.

Runs from scheduler.py on a cron-style schedule (8am/12pm/4pm Eastern, M-F).

Four detectors:
- overdue_task: open tasks past their due_date
- high_priority_due_soon: high-priority open tasks due in the next 36 hours
- conversation_awaiting_reply: Rowan's last message in a thread was 24+ hrs ago
                                and was a question (ends with '?')
- stale_amber_red_project: amber/red project with no activity in 7+ days

Dedup: each (trigger_type, target_kind, target_id) is nudged at most once per 3 days.

For each new finding:
  1. Post a message into the appropriate conversation (existing or fresh)
  2. Insert a row in the nudges table
  3. Send a Resend notification email
"""
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import resend
from dotenv import load_dotenv

from db import get_connection

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")
EMAIL_FROM      = os.getenv("EMAIL_FROM",      "Rowan <rowan@miami-coastline.com>")
EMAIL_TO        = os.getenv("ALLOWED_EMAIL",   "james@miami-coastline.com")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

DEDUP_WINDOW_DAYS = 3


# ============================================================
# Dedup
# ============================================================

def _was_recently_nudged(cur, trigger_type: str, target_kind: str, target_id: int) -> bool:
    cur.execute(
        """
        SELECT 1 FROM nudges
         WHERE trigger_type = %s
           AND target_kind  = %s
           AND target_id    = %s
           AND created_at   > NOW() - INTERVAL '%s days'
         LIMIT 1
        """,
        (trigger_type, target_kind, target_id, DEDUP_WINDOW_DAYS),
    )
    return cur.fetchone() is not None


# ============================================================
# Conversation routing
# ============================================================

def _get_or_create_nudge_conversation(cur, title: str) -> int:
    """
    Use the General conversation if one exists, otherwise create a new
    conversation with the given title. Nudges land in the most natural place.
    """
    cur.execute(
        "SELECT id FROM conversations WHERE title = %s ORDER BY id ASC LIMIT 1",
        ("General",),
    )
    row = cur.fetchone()
    if row:
        return row[0]

    cur.execute(
        "INSERT INTO conversations (title, status) VALUES (%s, 'active') RETURNING id",
        (title,),
    )
    return cur.fetchone()[0]


def _post_message(cur, conversation_id: int, content: str) -> int:
    cur.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'assistant', %s) RETURNING id",
        (conversation_id, content),
    )
    return cur.fetchone()[0]


def _record_nudge(cur, trigger_type, target_kind, target_id, conversation_id, message_id, email_sent):
    cur.execute(
        """
        INSERT INTO nudges (trigger_type, target_kind, target_id, conversation_id, message_id, email_sent)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (trigger_type, target_kind, target_id, conversation_id, message_id, email_sent),
    )


# ============================================================
# Email
# ============================================================

def _send_email(subject: str, body_plain: str) -> bool:
    """Send a single notification email. Returns True on success."""
    chat_url = f"{PUBLIC_BASE_URL}/chat" if PUBLIC_BASE_URL else "/chat"
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
    <h2 style="color:#1F3864;margin-top:0;">{subject}</h2>
    <div style="white-space: pre-wrap; line-height: 1.55; font-size: 14px;">{body_plain}</div>
    <p style="margin: 28px 0;">
      <a href="{chat_url}"
         style="background:#1F3864;color:white;padding:12px 20px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block;">
        Open chat with Rowan
      </a>
    </p>
    <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px;">
    <p style="font-size:12px;color:#999;">From Rowan, your project manager. Reply in chat.</p>
  </body>
</html>
"""
    try:
        resend.Emails.send({
            "from":    EMAIL_FROM,
            "to":      EMAIL_TO,
            "subject": f"[Rowan] {subject}",
            "html":    html,
        })
        return True
    except Exception as e:
        print(f"[nudges] email send failed: {e}")
        return False


# ============================================================
# Detectors
# ============================================================

def _detect_overdue_tasks(cur) -> list[dict]:
    cur.execute(
        """
        SELECT t.id, t.description, t.due_date, t.priority, p.name AS project, pe.name AS assignee
          FROM tasks t
          LEFT JOIN projects p  ON t.project_id  = p.id
          LEFT JOIN people   pe ON t.assignee_id = pe.id
         WHERE t.status = 'open'
           AND t.due_date IS NOT NULL
           AND t.due_date < CURRENT_DATE
         ORDER BY t.due_date ASC
         LIMIT 25
        """
    )
    return [
        {
            "trigger": "overdue_task",
            "kind": "task",
            "id": r[0],
            "summary": f"Task #{r[0]} is overdue (due {r[2]}): {r[1]}"
                       + (f" [project: {r[4]}]" if r[4] else "")
                       + (f" [assignee: {r[5]}]" if r[5] else "")
                       + f" [priority: {r[3] or '—'}]",
        }
        for r in cur.fetchall()
    ]


def _detect_high_priority_due_soon(cur) -> list[dict]:
    cur.execute(
        """
        SELECT t.id, t.description, t.due_date, p.name AS project, pe.name AS assignee
          FROM tasks t
          LEFT JOIN projects p  ON t.project_id  = p.id
          LEFT JOIN people   pe ON t.assignee_id = pe.id
         WHERE t.status = 'open'
           AND t.priority = 'high'
           AND t.due_date IS NOT NULL
           AND t.due_date >= CURRENT_DATE
           AND t.due_date <= CURRENT_DATE + INTERVAL '1 day'
         ORDER BY t.due_date ASC
         LIMIT 25
        """
    )
    return [
        {
            "trigger": "high_priority_due_soon",
            "kind": "task",
            "id": r[0],
            "summary": f"High-priority task #{r[0]} due {r[2]}: {r[1]}"
                       + (f" [project: {r[3]}]" if r[3] else "")
                       + (f" [assignee: {r[4]}]" if r[4] else ""),
        }
        for r in cur.fetchall()
    ]


def _detect_conversations_awaiting_reply(cur) -> list[dict]:
    """
    A conversation is awaiting reply if:
      - The most recent message is from Rowan (role='assistant')
      - It's older than 24 hours
      - Its content ends with '?' (Rowan asked a question)
    """
    cur.execute(
        """
        WITH last_msg AS (
          SELECT DISTINCT ON (conversation_id)
                 conversation_id, role, content, created_at
            FROM messages
           ORDER BY conversation_id, created_at DESC, id DESC
        )
        SELECT c.id, c.title, lm.content, lm.created_at
          FROM conversations c
          JOIN last_msg lm ON c.id = lm.conversation_id
         WHERE c.status != 'archived'
           AND lm.role = 'assistant'
           AND lm.created_at < NOW() - INTERVAL '24 hours'
           AND TRIM(lm.content) LIKE '%?'
         ORDER BY lm.created_at ASC
         LIMIT 10
        """
    )
    out = []
    for r in cur.fetchall():
        snippet = (r[2] or "").strip()
        if len(snippet) > 140:
            snippet = snippet[:137] + "..."
        out.append({
            "trigger": "conversation_awaiting_reply",
            "kind": "conversation",
            "id": r[0],
            "summary": f"Still waiting on a reply in \"{r[1] or 'Untitled'}\" — I asked: \"{snippet}\"",
            "conversation_id_override": r[0],  # nudge belongs in that same thread
        })
    return out


def _detect_stale_amber_red_projects(cur) -> list[dict]:
    """
    Amber or red projects where:
      - No task on the project has been updated/created in 7 days
      - And no status_report has been generated in 7 days
    """
    cur.execute(
        """
        SELECT p.id, p.name, p.rag_status
          FROM projects p
         WHERE p.rag_status IN ('amber', 'red')
           AND NOT EXISTS (
             SELECT 1 FROM tasks t
              WHERE t.project_id = p.id
                AND t.created_at > NOW() - INTERVAL '7 days'
           )
           AND NOT EXISTS (
             SELECT 1 FROM status_reports sr
              WHERE sr.project_id = p.id
                AND sr.generated_at > NOW() - INTERVAL '7 days'
           )
         LIMIT 10
        """
    )
    return [
        {
            "trigger": "stale_amber_red_project",
            "kind": "project",
            "id": r[0],
            "summary": f"Project \"{r[1]}\" is {r[2].upper()} and has had no task or status activity in 7+ days.",
        }
        for r in cur.fetchall()
    ]


DETECTORS = [
    _detect_overdue_tasks,
    _detect_high_priority_due_soon,
    _detect_conversations_awaiting_reply,
    _detect_stale_amber_red_projects,
]


# ============================================================
# Main entry point
# ============================================================

def run_nudge_pass() -> dict:
    """
    Run all detectors, post nudges for new findings, send one email per new nudge.
    Returns a summary dict with counts.
    """
    conn = get_connection()
    cur = conn.cursor()

    findings = []
    for detector in DETECTORS:
        try:
            findings.extend(detector(cur))
        except Exception as e:
            print(f"[nudges] detector {detector.__name__} failed: {e}")

    posted = 0
    emails_sent = 0
    skipped_dedup = 0

    for f in findings:
        if _was_recently_nudged(cur, f["trigger"], f["kind"], f["id"]):
            skipped_dedup += 1
            continue

        # Where does the nudge message land?
        if f.get("conversation_id_override"):
            conv_id = f["conversation_id_override"]
        else:
            conv_id = _get_or_create_nudge_conversation(cur, "General")

        # Frame the message in Rowan's voice
        if f["trigger"] == "conversation_awaiting_reply":
            body = "Bumping this — " + f["summary"].replace(
                "Still waiting on a reply in", "still waiting on your reply about"
            )
        else:
            body = "Heads up: " + f["summary"]

        msg_id = _post_message(cur, conv_id, body)

        # Try email
        email_ok = _send_email(
            subject=_subject_for(f["trigger"]),
            body_plain=body,
        )
        if email_ok:
            emails_sent += 1

        _record_nudge(
            cur, f["trigger"], f["kind"], f["id"],
            conv_id, msg_id, email_ok,
        )
        posted += 1

    conn.commit()
    cur.close()
    conn.close()

    summary = {
        "findings": len(findings),
        "posted": posted,
        "emails_sent": emails_sent,
        "skipped_dedup": skipped_dedup,
    }
    print(f"[nudges] pass complete: {summary}")
    return summary


def _subject_for(trigger: str) -> str:
    return {
        "overdue_task":                "Task is overdue",
        "high_priority_due_soon":      "High-priority task due soon",
        "conversation_awaiting_reply": "Still waiting on your reply",
        "stale_amber_red_project":     "Project needs attention",
    }.get(trigger, "Heads up")


if __name__ == "__main__":
    # Manual run for testing
    print(run_nudge_pass())
