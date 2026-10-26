"""
onboarding.py — set up a new project when a proposal is signed.

Start it from the "Mark signed" button on a proposal Rowan built, or with
/won <project>. Rowan loads that proposal and shows one card with everything
it will do; nothing happens until James taps Set up:

  1. OneDrive   MCM/Projects/<project>/ with the standard folders
                (00 MCM Contract & Fees ... 09 Closeout, _Archive); existing
                folders are left alone
  2. Rowan      the project, so it has an action-item register
  3. Kickoff    starter action items in that register (contract & money,
                team & meetings, documents & baseline), deduplicated
  4. Billing    the client, with the monthly fee for each phase from the
                billing start month, dropped into
                MCM/Accounting/Invoicing/Inbox/ for the invoice generator on
                the Mac to pick up on its next run (5th of the month)
  5. Leads      the lead marked Won; the proposal marked signed

QuickBooks isn't connected to Rowan yet, so the QuickBooks customer is a
kickoff task carrying the details.
"""
import json
import math
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from db import get_connection

TZ = ZoneInfo("America/New_York")
PROJECTS_ROOT = "MCM/Projects"
BILLING_INBOX_ROOT = "MCM/Accounting"
BILLING_INBOX = "Invoicing/Inbox"
EDIT_WINDOW_SECONDS = 900

STANDARD_FOLDERS = [
    "00 MCM Contract & Fees", "01 Legal & Agreements", "02 Meetings & Minutes",
    "03 Trackers & Schedule", "04 Consultants", "05 Design & Plans", "06 Zoning & Permitting",
    "07 Budget & Financials", "08 Construction", "09 Closeout", "_Archive",
]

# ============================================================
# HELPERS
# ============================================================


def _money(x):
    return f"${x:,.0f}"


def _month_add(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m0 = (y * 12 + m - 1) + n
    return f"{m0 // 12}-{m0 % 12 + 1:02d}"


def _month_label(ym: str) -> str:
    return datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%b %Y")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())[:24] or "client"


def billing_schedule(phases, start_month):
    """[{start, end, amount, description}] — phases back to back from start_month."""
    out, cursor = [], start_month
    for ph in phases:
        months = max(1, math.ceil(float(ph["months"])))
        end = _month_add(cursor, months - 1)
        desc = "Owners Rep. Services"
        if ph.get("description"):
            desc += f" – {ph['description']}"
        out.append({"start": cursor, "end": end, "amount": ph["monthly_fee"], "description": desc})
        cursor = _month_add(end, 1)
    return out


def _state():
    from telegram_bot import state_get
    return state_get("onboard_draft") or {}


def _save(d):
    from telegram_bot import state_set
    state_set("onboard_draft", d)


def edit_target():
    from telegram_bot import state_get
    mode = state_get("onboard_edit") or {}
    return bool(mode.get("until", 0) > time.time() and _state().get("proposal_id"))


def _set_edit(on: bool):
    from telegram_bot import state_set
    state_set("onboard_edit", {"until": time.time() + EDIT_WINDOW_SECONDS} if on else {})

# ============================================================
# LOAD A PROPOSAL
# ============================================================


def _proposal(proposal_id=None, name=None):
    conn = get_connection()
    cur = conn.cursor()
    try:
        if proposal_id:
            cur.execute("SELECT id, project_name, folder, filename, spec, status FROM proposals WHERE id = %s",
                        (int(proposal_id),))
        else:
            cur.execute("""SELECT id, project_name, folder, filename, spec, status FROM proposals
                            WHERE LOWER(project_name) LIKE %s ORDER BY created_at DESC LIMIT 1""",
                        (f"%{(name or '').strip().lower()}%",))
        row = cur.fetchone()
    except Exception:
        conn.rollback()
        row = None
    finally:
        cur.close()
        conn.close()
    if not row:
        return None
    spec = row[4] if isinstance(row[4], dict) else json.loads(row[4])
    return {"id": row[0], "project_name": row[1], "folder": row[2], "filename": row[3],
            "spec": spec, "status": row[5]}


def _draft_from(p):
    spec = p["spec"]
    today = datetime.now(TZ).date()
    start = _month_add(today.strftime("%Y-%m"), 1)          # first invoice next month by default
    lines = spec.get("client_lines") or []
    return {
        "proposal_id": p["id"],
        "proposal_file": p["filename"],
        "project_name": spec["project_name"],
        "folder": p.get("folder") or spec["project_name"],
        "client_name": lines[0] if lines else spec.get("owner_signature_name", ""),
        "address_lines": lines[1:],
        "client_role": spec.get("client_role", "Owner"),
        "billing_start": start,
        "billing_contact": spec.get("salutation_name", ""),
        "billing_emails": [],
        "cc_emails": [],
        "phases": spec.get("phases", []),
        "retainer_months": (spec.get("terms") or {}).get("retainer_months", 1),
    }

# ============================================================
# CARD
# ============================================================


def _card(d):
    sched = billing_schedule(d["phases"], d["billing_start"])
    total = sum(s["amount"] * (_months_between(s["start"], s["end"])) for s in sched)
    retainer = d["phases"][0]["monthly_fee"] * d["retainer_months"] if d["phases"] else 0
    lines = [f"SET UP PROJECT — {d['project_name']}", f"From: {d['proposal_file']}", "",
             f"1. OneDrive: {PROJECTS_ROOT}/{d['folder']}/ with folders 00–09 + _Archive (existing ones kept)",
             f"2. Rowan: project \"{d['project_name']}\" with its action-item register",
             "3. Kickoff items: contract & money, team & meetings, documents & baseline (11 items)",
             f"4. Billing: {d['client_name']}" + (f", {', '.join(d['address_lines'])}" if d["address_lines"] else "")]
    for s in sched:
        span = _month_label(s["start"]) + ("" if s["start"] == s["end"] else f" – {_month_label(s['end'])}")
        lines.append(f"     {span}: {_money(s['amount'])}/mo  ({s['description'].split(' – ', 1)[-1]})")
    lines.append(f"     Total {_money(total)}; first invoice {_month_label(d['billing_start'])} 5th")
    to = ", ".join(d["billing_emails"]) or "— none yet —"
    lines.append(f"     Invoice email to: {to}" + (f" (cc {', '.join(d['cc_emails'])})" if d["cc_emails"] else "")
                 + (f", \"Hi {d['billing_contact']},\"" if d["billing_contact"] else ""))
    lines += [f"5. Lead → Won; proposal → signed",
              f"Plus tasks to invoice the {_money(retainer)} retainer and create the QuickBooks customer.", ""]
    if not d["billing_emails"]:
        lines.append("Missing: who gets the invoices. Tap Edit and send the billing email(s).")
    else:
        lines.append("Tap Set up to do all of it, or Edit to change anything "
                     "(billing start, emails, folder, client name).")
    return "\n".join(lines)


def _months_between(a, b):
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7])) + 1


def _buttons():
    return {"inline_keyboard": [[{"text": "Set up", "callback_data": "onbgo:0"},
                                 {"text": "Edit", "callback_data": "onbedit:0"},
                                 {"text": "Cancel", "callback_data": "onbcancel:0"}]]}


def _show(d):
    from telegram_bot import send_message
    send_message(_card(d), reply_markup=_buttons())

# ============================================================
# ENTRY POINTS
# ============================================================


def start(proposal_id=None, name=None) -> None:
    from telegram_bot import send_message
    if not proposal_id and not (name or "").strip():
        send_message("Which project? e.g. /won Soundbath")
        return
    p = _proposal(proposal_id, name)
    if not p:
        send_message(f"I don't have a proposal for \"{name}\". Onboarding uses the proposal's client, fees and "
                     "phases, so build it first with /proposal (you can mark it signed right after).")
        return
    if p["status"] == "signed":
        send_message(f"Heads up: {p['filename']} is already marked signed. Setting up again only adds what's "
                     "missing; nothing is duplicated.")
    d = _draft_from(p)
    _set_edit(False)
    _save(d)
    _show(d)


def start_edit() -> None:
    from telegram_bot import send_message, state_set
    if not _state().get("proposal_id"):
        send_message("That setup was already done or cancelled. Start again with /won <project>.")
        return
    for key in ("edit_mode", "li_edit", "proposal_edit", "invoice_edit"):
        state_set(key, {})
    _set_edit(True)
    send_message("What should change? e.g. \"bill to ap@acme.com, cc Jane jane@acme.com\", \"billing starts "
                 "January\", \"greet Saul\", \"folder Pacific Star/Hallandale\", \"client is Acme Holdings LLC\".")


EDIT_SYSTEM = """You update a new-project setup for Miami Coastline Management. Apply James's change and
return ONLY the full updated JSON object, same keys as given. Rules:
- billing_start is "YYYY-MM"; "January" means the next January from today ({today}).
- billing_emails and cc_emails are lists of addresses James typed; never guess an address from a name.
- billing_contact is the name used in "Hi ___," on invoice emails.
- Don't change phases, proposal_id or proposal_file unless he explicitly asks.
Change only what he asks."""


def apply_edit(text: str) -> None:
    from telegram_bot import send_message
    d = _state()
    _set_edit(False)
    if not d.get("proposal_id"):
        send_message("That setup was already done or cancelled.")
        return
    if text.strip().lower() in ("cancel", "stop", "never mind", "nevermind"):
        _show(d)
        return
    try:
        import anthropic
        import os
        from rowan_agent import MODEL
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=1500,
            system=EDIT_SYSTEM.replace("{today}", datetime.now(TZ).date().isoformat()),
            messages=[{"role": "user", "content": f"CURRENT:\n{json.dumps(d)}\n\nCHANGE:\n{text}"}])
        out = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        new = json.loads(out[out.find("{"): out.rfind("}") + 1])
    except Exception as e:
        print(f"[onboarding] edit failed: {e}")
        send_message(f"Couldn't apply that ({str(e)[:150]}). Tap Edit and try again.")
        _show(d)
        return
    email_ok = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    for key in ("billing_emails", "cc_emails"):
        new[key] = [a.strip() for a in (new.get(key) or []) if email_ok.match(str(a).strip())]
    if not re.match(r"^\d{4}-\d{2}$", str(new.get("billing_start", ""))):
        new["billing_start"] = d["billing_start"]
    for key in ("proposal_id", "proposal_file", "phases"):
        new.setdefault(key, d[key])
    _save(new)
    _show(new)


def cancel() -> None:
    from telegram_bot import send_message
    _save({})
    _set_edit(False)
    send_message("Project setup cancelled. Nothing was changed.")

# ============================================================
# DO IT
# ============================================================


def kickoff_items(d, retainer):
    c = d["client_name"] or d["client_role"]
    sched = billing_schedule(d["phases"], d["billing_start"])
    sched_txt = "; ".join(f"{_month_label(s['start'])}–{_month_label(s['end'])} {_money(s['amount'])}/mo"
                          for s in sched)
    return [
        # (key, description, days from today, waiting_on, priority)
        ("agreement", f"File the countersigned agreement in 00 MCM Contract & Fees", 2, None, "high"),
        ("retainer-invoice", f"Invoice the retainer: {_money(retainer)} (one month's fee, applied to the final "
                             "invoice)", 3, None, "high"),
        ("retainer-paid", f"Retainer payment of {_money(retainer)}", 14, c, "high"),
        ("coi", f"Send certificate of insurance naming {c} as additional insured (CGL)", 5, None, "medium"),
        ("quickbooks", f"Create QuickBooks customer {c}" +
                       (f" ({', '.join(d['address_lines'])})" if d["address_lines"] else "") +
                       f" and monthly invoices to mirror the Mac: {sched_txt}", 3, None, "medium"),
        ("directory", "Build the project directory: owner, design team, GC and consultants", 7, None, "medium"),
        ("kickoff", f"Schedule the kickoff meeting with {c}", 5, None, "high"),
        ("cadence", "Set the weekly meeting cadence and minutes distribution list", 10, None, "medium"),
        ("documents", "Existing drawings, survey and lease / agreements", 10, c, "medium"),
        ("budget", "Baseline budget", 21, None, "medium"),
        ("schedule", "Baseline schedule", 21, None, "medium"),
    ]


def _project_id(d):
    from register import resolve_project
    pid = resolve_project(d["project_name"])
    if pid:
        return pid, False
    from rowan_agent import tool_create_project
    res = tool_create_project({"name": d["project_name"], "status": "active", "rag_status": "green",
                               "start_date": datetime.now(TZ).date().isoformat(),
                               "goal": f"Owner's rep for {d['client_name']} (from {d['proposal_file']})"})
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res["project_id"], True


def billing_entry(d):
    sched = billing_schedule(d["phases"], d["billing_start"])
    return {
        "id": _slug(d["project_name"]),
        "name": d["client_name"],
        "address_lines": d["address_lines"],
        "project_name": d["project_name"],
        "billing_contact": d["billing_contact"],
        "billing_emails": d["billing_emails"],
        "cc_emails": d["cc_emails"],
        "start_month": sched[0]["start"] if sched else d["billing_start"],
        "end_month": sched[-1]["end"] if sched else d["billing_start"],
        "schedule": sched,
        "line_items": [{"description": "Owners Rep. Services",
                        "amount": sched[0]["amount"] if sched else 0}],
        "_added_by": f"Rowan onboarding from {d['proposal_file']} on {datetime.now(TZ).date().isoformat()}",
    }


def setup() -> None:
    from telegram_bot import send_message, _typing
    d = _state()
    if not d.get("proposal_id"):
        send_message("That setup was already done or cancelled. Start again with /won <project>.")
        return
    _typing()
    done, problems = [], []

    # 1. OneDrive folders
    try:
        from onedrive import ensure_folder
        made = sum(1 for f in STANDARD_FOLDERS if ensure_folder(f"{PROJECTS_ROOT}/{d['folder']}/{f}"))
        done.append(f"OneDrive: {PROJECTS_ROOT}/{d['folder']}/ ready ({made} folder{'s' if made != 1 else ''} "
                    "created, the rest already there)")
    except Exception as e:
        problems.append(f"OneDrive folders: {str(e)[:150]}")

    # 2. Rowan project
    pid = None
    try:
        pid, new = _project_id(d)
        done.append(f"Rowan: project {d['project_name']} " + ("created" if new else "already existed"))
    except Exception as e:
        problems.append(f"Rowan project: {str(e)[:150]}")

    # 3. Kickoff items
    if pid:
        try:
            from register import add_task
            retainer = d["phases"][0]["monthly_fee"] * d.get("retainer_months", 1) if d["phases"] else 0
            today = datetime.now(TZ).date()
            added = 0
            for key, desc, days, waiting, prio in kickoff_items(d, retainer):
                r = add_task(desc, project_id=pid, due_date=today + timedelta(days=days),
                             ball_in_court="waiting" if waiting else "me", waiting_on=waiting,
                             priority=prio, source="onboarding", source_ref=f"onboard:{pid}:{key}")
                added += 0 if r.get("duplicate") else 1
            done.append(f"Kickoff: {added} action items added to the {d['project_name']} register "
                        "(/list " + d["project_name"].split()[0].lower() + " to see them)")
        except Exception as e:
            problems.append(f"Kickoff items: {str(e)[:150]}")

    # 4. Billing → invoice generator inbox
    try:
        from onedrive import upload_file
        entry = billing_entry(d)
        item = upload_file(json.dumps(entry, indent=2, ensure_ascii=False).encode(), BILLING_INBOX,
                           f"{entry['id']}.json", "application/json", root=BILLING_INBOX_ROOT)
        done.append(f"Billing: {entry['name']} queued for the invoice generator, first invoice "
                    f"{_month_label(entry['start_month'])} 5th ({item['path']})")
    except Exception as e:
        problems.append(f"Billing: {str(e)[:150]}")

    # 5. Lead + proposal
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE leads SET status = 'Won', updated_at = NOW() WHERE LOWER(name) = LOWER(%s)",
                    (d["project_name"],))
        lead_rows = cur.rowcount
        cur.execute("UPDATE proposals SET status = 'signed' WHERE id = %s", (d["proposal_id"],))
        conn.commit()
        cur.close()
        conn.close()
        done.append("Lead marked Won" if lead_rows else "Proposal marked signed (no matching lead to mark Won)")
    except Exception as e:
        problems.append(f"Lead/proposal: {str(e)[:150]}")

    msg = f"{d['project_name']} is set up.\n\n" + "\n".join(f"✓ {x}" for x in done)
    if problems:
        msg += "\n\nDidn't work:\n" + "\n".join(f"✗ {x}" for x in problems) + \
               "\nTap Set up again once fixed; what's done won't be repeated."
    else:
        _save({})
    if not d["billing_emails"]:
        msg += "\n\nNo billing email yet: the invoice email will wait for you to add a recipient."
    send_message(msg, reply_markup=_buttons() if problems else None)
