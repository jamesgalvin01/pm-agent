"""
invoice_emails.py — monthly invoice emails, approved in Telegram before they go out.

The invoice generator on James's Mac (com.miamicoastline.invoices) makes the
PDFs on the 5th and writes invoices.json next to them in OneDrive:

    MCM/Accounting/Accounts Receivable/2026/11-November/
        INV-1065_farbstein_2026-11.pdf
        ...
        invoices.json   <- number, client, amount, dates, billing contacts

From the 5th to the 12th, every 30 minutes 8am-7pm, Rowan looks for that
file. Each invoice it hasn't seen gets a drafted email (to the client's
billing contacts, PDF attached) sent to James in Telegram with
Send / Edit / Skip, then one "Send all" summary. Nothing is emailed without
a tap. /invoices runs the check now (any day) and re-shows what's waiting.

Every invoice is recorded in invoice_emails, so nothing is drafted or sent twice.
"""
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from db import get_connection

TZ = ZoneInfo("America/New_York")
AR_ROOT = os.getenv("INVOICE_AR_ROOT", "MCM/Accounting/Accounts Receivable").strip("/")
MANIFEST = "invoices.json"
EDIT_WINDOW_SECONDS = 900
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

# James's standard signature and logo are added on send (outlook_mail.signed_html).

# ============================================================
# STORAGE
# ============================================================

_ready = False


def _conn():
    global _ready
    conn = get_connection()
    if not _ready:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invoice_emails (
                invoice_number INTEGER PRIMARY KEY,
                period TEXT NOT NULL,
                client_id TEXT,
                client_name TEXT,
                project_name TEXT,
                amount NUMERIC,
                due_date TEXT,
                folder TEXT,
                filename TEXT,
                to_emails JSONB DEFAULT '[]',
                cc_emails JSONB DEFAULT '[]',
                subject TEXT,
                body TEXT,
                status TEXT DEFAULT 'pending',      -- pending | sent | skipped
                notified BOOLEAN DEFAULT FALSE,
                telegram_message_id BIGINT,
                sent_at TIMESTAMPTZ,
                error TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        _ready = True
    return conn


def _rows(where="TRUE", params=()):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM invoice_emails WHERE {where} ORDER BY invoice_number", params)
    cols = [d[0] for d in cur.description]
    out = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in out:
        r["amount"] = float(r["amount"] or 0)
    return out


def _get(number):
    rows = _rows("invoice_number = %s", (int(number),))
    return rows[0] if rows else None


def _update(number, **fields):
    if not fields:
        return
    conn = _conn()
    cur = conn.cursor()
    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()]
    cur.execute(f"UPDATE invoice_emails SET {sets} WHERE invoice_number = %s", (*vals, int(number)))
    conn.commit()
    cur.close()
    conn.close()

# ============================================================
# DRAFTING
# ============================================================


def month_folder(year, month):
    return f"{AR_ROOT}/{year}/{month:02d}-{MONTHS[month - 1]}"


def _money(x):
    return f"${x:,.2f}" if x % 1 else f"${x:,.0f}"


def _due_text(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"
    except Exception:
        return iso or "the end of the month"


def draft_email(inv, period_label):
    greet = (inv.get("contact_name") or "").strip()
    project = inv.get("project_name") or inv.get("client_name")
    subject = f"Miami Coastline Management – Invoice INV-{inv['invoice_number']} – {project} – {period_label}"
    body = (
        (f"Hi {greet},\n\n" if greet else "Hello,\n\n") +
        f"Attached is Miami Coastline Management's invoice INV-{inv['invoice_number']} for "
        f"{period_label} owner's representative services on {project}, in the amount of "
        f"{_money(float(inv['amount']))}. Payment is due by {_due_text(inv.get('due_date'))}.\n\n"
        "Wire instructions are on the invoice. Please let me know if you have any questions.\n\n"
        "Thank you,"
    )
    return subject, body


def _read_manifest(year, month):
    from onedrive import download_path
    try:
        raw = download_path(f"{month_folder(year, month)}/{MANIFEST}")
    except FileNotFoundError:
        return None
    return json.loads(raw.decode("utf-8-sig"))


def collect(year, month) -> int:
    """Add any new invoices from this month's manifest. Returns how many were added."""
    data = _read_manifest(year, month)
    if not data:
        return 0
    period = f"{year}-{month:02d}"
    label = f"{MONTHS[month - 1]} {year}"
    known = {r["invoice_number"] for r in _rows("period = %s", (period,))}
    added = 0
    conn = _conn()
    cur = conn.cursor()
    for inv in data.get("invoices", []):
        n = int(inv["invoice_number"])
        if n in known:
            continue
        subject, body = draft_email(inv, label)
        cur.execute("""
            INSERT INTO invoice_emails (invoice_number, period, client_id, client_name, project_name,
                amount, due_date, folder, filename, to_emails, cc_emails, subject, body)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (n, period, inv.get("client_id"), inv.get("client_name"), inv.get("project_name"),
              inv.get("amount"), inv.get("due_date"), month_folder(year, month), inv["filename"],
              json.dumps(inv.get("to") or []), json.dumps(inv.get("cc") or []), subject, body))
        added += cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return added

# ============================================================
# TELEGRAM
# ============================================================


def _card(r):
    to = ", ".join(r["to_emails"] or []) or "— none on file —"
    lines = [f"INVOICE EMAIL — INV-{r['invoice_number']} · {r['client_name']} · {_money(r['amount'])}",
             f"To: {to}"]
    if r["cc_emails"]:
        lines.append("Cc: " + ", ".join(r["cc_emails"]))
    from outlook_mail import signed_text
    lines += [f"Subject: {r['subject']}", f"Attached: {r['filename']}", "", signed_text(r["body"])]
    if not r["to_emails"]:
        lines += ["", "No billing email for this client. Add \"to\" in clients.json on the Mac, or tap "
                      "Edit and tell me who it goes to."]
    return "\n".join(lines)


def _buttons(r):
    row = []
    if r["to_emails"]:
        row.append({"text": "Send", "callback_data": f"invsend:{r['invoice_number']}"})
    row += [{"text": "Edit", "callback_data": f"invedit:{r['invoice_number']}"},
            {"text": "Skip", "callback_data": f"invskip:{r['invoice_number']}"}]
    return {"inline_keyboard": [row]}


def _show(r):
    from telegram_bot import send_message
    mid = send_message(_card(r), reply_markup=_buttons(r))
    _update(r["invoice_number"], notified=True, telegram_message_id=mid)


def _send_all_prompt():
    from telegram_bot import send_message
    ready = [r for r in _rows("status = 'pending'") if r["to_emails"]]
    if len(ready) < 2:
        return
    total = sum(r["amount"] for r in ready)
    send_message(f"{len(ready)} invoice emails ready, {_money(total)} in total. Each one above shows "
                 "exactly what goes out.",
                 reply_markup={"inline_keyboard": [[{"text": f"Send all {len(ready)}",
                                                     "callback_data": "invall:0"}]]})


def run_check(force=False) -> dict:
    """Scheduler entry: look for this month's manifest and offer new invoice emails."""
    now = datetime.now(TZ)
    if not force and not (5 <= now.day <= 12 and 8 <= now.hour < 19):
        return {"skipped": "outside window"}
    added = collect(now.year, now.month)
    fresh = _rows("status = 'pending' AND notified = FALSE")
    for r in fresh:
        _show(r)
    if fresh:
        _send_all_prompt()
    return {"added": added, "offered": len(fresh)}


def on_demand() -> None:
    """/invoices: check now and re-show everything still waiting."""
    from telegram_bot import send_message
    try:
        now = datetime.now(TZ)
        collect(now.year, now.month)
    except Exception as e:
        print(f"[invoices] manifest check failed: {e}")
        send_message(f"Couldn't read this month's invoice list from OneDrive: {str(e)[:200]}")
    waiting = _rows("status = 'pending'")
    if not waiting:
        sent = _rows("period = %s AND status = 'sent'", (datetime.now(TZ).strftime("%Y-%m"),))
        send_message("No invoice emails waiting." + (f" {len(sent)} sent this month." if sent else
                     " This month's invoices haven't been generated yet (the Mac runs on the 5th)."))
        return
    for r in waiting:
        _show(r)
    _send_all_prompt()

# ============================================================
# ACTIONS (from the buttons)
# ============================================================


def send(number, quiet=False) -> bool:
    from onedrive import download_path
    from outlook_mail import send_new_mail
    from telegram_bot import send_message
    r = _get(number)
    if not r or r["status"] != "pending":
        if not quiet:
            send_message(f"INV-{number} was already {r['status'] if r else 'handled'}.")
        return False
    if not r["to_emails"]:
        send_message(f"INV-{number} has no recipient. Tap Edit and tell me who it goes to.")
        return False
    try:
        pdf = download_path(f"{r['folder']}/{r['filename']}")
        send_new_mail(r["to_emails"], r["subject"], r["body"], cc=r["cc_emails"] or None,
                      attachments=[(r["filename"], pdf, "application/pdf")])
    except Exception as e:
        print(f"[invoices] send INV-{number} failed: {e}")
        _update(number, error=str(e)[:500])
        send_message(f"INV-{number} did NOT send: {str(e)[:200]}. It's still waiting; try Send again.",
                     reply_markup=_buttons(r))
        return False
    _update(number, status="sent", sent_at=datetime.now(TZ), error=None)
    if not quiet:
        send_message(f"Sent INV-{number} to {', '.join(r['to_emails'])} with the PDF attached. "
                     "It's in your Sent Items.")
    return True


def send_all() -> None:
    from telegram_bot import send_message
    ready = [r for r in _rows("status = 'pending'") if r["to_emails"]]
    sent = [r for r in ready if send(r["invoice_number"], quiet=True)]
    failed = len(ready) - len(sent)
    msg = f"Sent {len(sent)} invoice email{'s' if len(sent) != 1 else ''}: " + \
          ", ".join(f"INV-{r['invoice_number']} {r['client_name']}" for r in sent) if sent else "Nothing sent."
    if failed:
        msg += f"\n{failed} didn't go out; they're still waiting (see above)."
    send_message(msg)


def skip(number) -> None:
    from telegram_bot import send_message
    r = _get(number)
    if r and r["status"] == "pending":
        _update(number, status="skipped")
        send_message(f"Skipped INV-{number}. Nothing was sent. /invoices won't offer it again.")


# ---------- editing ----------

def _edit_state():
    from telegram_bot import state_get
    s = state_get("invoice_edit") or {}
    return s if s.get("until", 0) > time.time() else None


def edit_target():
    s = _edit_state()
    return s.get("invoice_number") if s else None


def start_edit(number) -> None:
    from telegram_bot import send_message, state_set
    r = _get(number)
    if not r or r["status"] != "pending":
        send_message(f"INV-{number} was already handled.")
        return
    state_set("edit_mode", {})
    state_set("li_edit", {})
    state_set("proposal_edit", {})
    state_set("invoice_edit", {"invoice_number": int(number), "until": time.time() + EDIT_WINDOW_SECONDS})
    send_message(f"What should change on INV-{number}'s email? e.g. \"send to ap@client.com, cc Marc\", "
                 "\"mention the October credit\", \"shorter\", or paste the exact body.")


EDIT_SYSTEM = """You edit an invoice cover email James Galvin (Miami Coastline Management) is sending
a client. Apply his change and return ONLY a JSON object:
{"to": [...], "cc": [...], "subject": "...", "body": "..."}
Keep everything he didn't ask to change exactly as it is. Never change the invoice number, amount
or due date unless he says so. Only use email addresses he typed or that were already there; never
guess an address from a name (if he names someone without an address, leave the recipients as they
are). Plain text body ending with a closing word such as "Thank you," and no name or signature
(James's signature and logo are added automatically when it's sent)."""


def apply_edit(text: str) -> None:
    from telegram_bot import send_message, state_set
    number = edit_target()
    r = _get(number) if number else None
    state_set("invoice_edit", {})
    if not r or r["status"] != "pending":
        send_message("That invoice email was already handled.")
        return
    if text.strip().lower() in ("cancel", "stop", "never mind", "nevermind"):
        _show(r)
        return
    try:
        import anthropic
        from rowan_agent import MODEL
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        current = {"to": r["to_emails"], "cc": r["cc_emails"], "subject": r["subject"], "body": r["body"]}
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=EDIT_SYSTEM,
            messages=[{"role": "user", "content": f"CURRENT EMAIL:\n{json.dumps(current)}\n\nCHANGE:\n{text}"}])
        out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        new = json.loads(out[out.find("{"): out.rfind("}") + 1])
    except Exception as e:
        print(f"[invoices] edit failed: {e}")
        send_message(f"Couldn't apply that ({str(e)[:150]}). Tap Edit and try again.")
        _show(r)
        return
    email_ok = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    to = [a.strip() for a in new.get("to") or [] if email_ok.match(a.strip())]
    cc = [a.strip() for a in new.get("cc") or [] if email_ok.match(a.strip())]
    _update(number, to_emails=to, cc_emails=cc, subject=new.get("subject") or r["subject"],
            body=new.get("body") or r["body"])
    _show(_get(number))
