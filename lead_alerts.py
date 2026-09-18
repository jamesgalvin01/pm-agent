"""
lead_alerts.py — new-opportunity alerts with Pursue / Pass buttons.

Anything that finds a possible job (the procurement-portal search in
lead_search.py once it's built, a referral forwarded in, a manual test)
calls propose_lead(). James gets it on Telegram:

    Pursue  -> added to the leads pipeline (stage 'New'); if there's a
               submission deadline, a high-priority task due that day
    Pass    -> recorded as passed, so the same opportunity never alerts twice
    Details -> the full summary and link

Test: send /testlead to the bot.
"""
import sys
from datetime import date

from db import get_connection


def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lead_candidates (
            id SERIAL PRIMARY KEY,
            source TEXT,
            external_id TEXT,
            title TEXT NOT NULL,
            agency TEXT,
            location TEXT,
            due_date DATE,
            url TEXT,
            est_value NUMERIC,
            summary TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            lead_id INT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            decided_at TIMESTAMPTZ,
            UNIQUE (source, external_id)
        )
    """)


def _one(sql, params=()):
    conn = get_connection()
    cur = conn.cursor()
    _ensure_table(cur)
    cur.execute(sql, params)
    row = cur.fetchone() if cur.description else None
    conn.commit()
    cur.close()
    conn.close()
    return row


def _get(cid):
    row = _one("""SELECT id, source, title, agency, location, due_date, url, est_value, summary, status
                    FROM lead_candidates WHERE id = %s""", (cid,))
    if not row:
        return None
    keys = ("id", "source", "title", "agency", "location", "due_date", "url", "est_value", "summary", "status")
    return dict(zip(keys, row))


def _card(c: dict, full: bool = False) -> str:
    lines = [f"New opportunity ({c['source'] or 'unknown source'}):", c["title"]]
    who = ", ".join(x for x in (c.get("agency"), c.get("location")) if x)
    if who:
        lines.append(who)
    if c.get("due_date"):
        days = (c["due_date"] - date.today()).days
        lines.append(f"Due: {c['due_date'].strftime('%a %b %d')} ({days} days)")
    if c.get("est_value"):
        lines.append(f"Est. value: ${float(c['est_value']):,.0f}")
    summary = (c.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append(summary if full or len(summary) <= 400 else summary[:400].rstrip() + "...")
    if full and c.get("url"):
        lines.append(f"\n{c['url']}")
    return "\n".join(lines)


def _keyboard(cid, with_details=True):
    row = [{"text": "Pursue", "callback_data": f"ldgo:{cid}"},
           {"text": "Pass", "callback_data": f"ldno:{cid}"}]
    if with_details:
        row.append({"text": "Details", "callback_data": f"ldinfo:{cid}"})
    return {"inline_keyboard": [row]}


def propose_lead(title: str, source: str, external_id: str = None, agency: str = None,
                 location: str = None, due_date=None, url: str = None, est_value=None,
                 summary: str = None):
    """
    Record an opportunity and send it to James. Returns the candidate id, or
    None if this one was already seen (same source + external_id).
    """
    row = _one("""
        INSERT INTO lead_candidates (source, external_id, title, agency, location, due_date, url, est_value, summary)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source, external_id) DO NOTHING
        RETURNING id
    """, (source, external_id or url or title, title, agency, location, due_date, url, est_value, summary))
    if not row:
        return None
    from telegram_bot import send_message
    send_message(_card(_get(row[0])), reply_markup=_keyboard(row[0]))
    return row[0]


def handle(action: str, cid: int) -> None:
    """Button taps from telegram_bot."""
    from telegram_bot import send_message
    c = _get(cid)
    if not c:
        send_message("That opportunity is gone.")
        return
    if action == "ldinfo":
        markup = _keyboard(cid, with_details=False) if c["status"] == "pending" else None
        send_message(_card(c, full=True), reply_markup=markup)
        return
    if c["status"] != "pending":
        send_message(f"Already marked {c['status']}.")
        return

    if action == "ldno":
        _one("UPDATE lead_candidates SET status = 'passed', decided_at = NOW() WHERE id = %s", (cid,))
        send_message(f"Passed on: {c['title']}")
        return

    if c.get("source") == "test":
        _one("UPDATE lead_candidates SET status = 'pursuing', decided_at = NOW() WHERE id = %s", (cid,))
        send_message("Test worked. A real alert would now be in /leads, with a task to submit by "
                     f"{c['due_date'].strftime('%a %b %d')}. Nothing was added for this test.")
        return

    # Pursue: add to the pipeline, plus a deadline task if there is one.
    notes = "\n".join(x for x in (
        c.get("agency"), c.get("location"),
        f"Due {c['due_date'].isoformat()}" if c.get("due_date") else None,
        c.get("url"), c.get("summary"),
    ) if x)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT")
    cur.execute("""INSERT INTO leads (name, contact, value, status, source, notes)
                   VALUES (%s, %s, %s, 'New', %s, %s) RETURNING id""",
                (c["title"][:200], c.get("agency"), c.get("est_value") or 0, c.get("source"), notes))
    lead_id = cur.fetchone()[0]
    task_line = ""
    if c.get("due_date"):
        cur.execute("""INSERT INTO tasks (description, due_date, priority, status, source)
                       VALUES (%s, %s, 'high', 'open', 'lead_alert')""",
                    (f"Submit response: {c['title'][:150]}", c["due_date"]))
        task_line = f"\nTask added: submit by {c['due_date'].strftime('%a %b %d')}."
    cur.execute("UPDATE lead_candidates SET status = 'pursuing', lead_id = %s, decided_at = NOW() WHERE id = %s",
                (lead_id, cid))
    conn.commit()
    cur.close()
    conn.close()
    send_message(f"Added to the pipeline: {c['title']}{task_line}")


def send_test_alert():
    """Sample alert for trying the buttons (Telegram: /testlead)."""
    from datetime import datetime, timedelta
    return propose_lead(
            title="TEST - Owner's Representative Services, New Fire Station",
            source="test",
            external_id=f"test-{datetime.now().isoformat(timespec='seconds')}",
            agency="Monroe County BOCC",
            location="Key Largo, FL",
            due_date=date.today() + timedelta(days=21),
            url="https://example.com/rfq",
            summary="Sample alert so you can try the buttons. Tap Pass to clear it.",
        )


if __name__ == "__main__":
    if "--test" in sys.argv:
        print("Sent test alert." if send_test_alert() else "Not sent.")
    else:
        print(__doc__)
