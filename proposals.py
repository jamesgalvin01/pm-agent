"""
proposals.py — /proposal in Telegram: an owner's rep proposal from one message.

Flow
  1. /proposal <anything>  e.g.
       /proposal Soundbath for Vishal Sapra, Celebration Ventures, 3114 N Bay Rd Miami.
       410 NE 29th St. DD 4 mo @ 5k, precon 12 @ 12k, construction 14 @ 12k
     Claude turns it into a draft spec. Rowan replies with Attachment A (as
     editable text) and a summary: client, fee table with the math done,
     where it will be saved. Anything missing is listed; Build stays hidden
     until it's filled in.
  2. Edit scope    -> send the new Attachment A text (starts with ##), or say
                      what to change ("add a bullet for value engineering").
     Edit details  -> say what to change ("precon 10 months", "client is the HOA").
  3. Build         -> fills the Word template (proposal_generator.py), saves it to
                      MCM/Projects/<project>/00 MCM Contract & Fees/ as the next
                      revision, sends the .docx here, and records the lead at
                      status Proposal with the total contract value.

Nothing is emailed to the client. Build is the go-ahead for the file and
the lead; nothing is written before it.

Env
  PROPOSAL_TEMPLATE_PATH  OneDrive path of the Word template
                          (default MCM/Templates/Proposals/MCM Proposal Template.docx)
  PROPOSAL_PROJECTS_ROOT  default MCM/Projects
"""
import difflib
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import requests

import proposal_generator as gen
from db import get_connection
from onedrive import download_path, list_children, safe_name, upload_file
from rowan_agent import MODEL

TZ = ZoneInfo("America/New_York")
TEMPLATE_PATH = os.getenv("PROPOSAL_TEMPLATE_PATH",
                          "MCM/Templates/Proposals/MCM Proposal Template.docx").strip("/")
PROJECTS_ROOT = os.getenv("PROPOSAL_PROJECTS_ROOT", "MCM/Projects").strip("/")
CONTRACT_FOLDER = "00 MCM Contract & Fees"
EDIT_WINDOW_SECONDS = 900
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

REQUIRED = {
    "project_name": "project name",
    "client_lines": "client name and address",
    "salutation_name": "who the letter is addressed to (Dear ...)",
    "owner_signature_name": "who signs for the client",
    "project_address": "project address",
    "phases": "fee phases (months and monthly fee for each)",
}

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ============================================================
# STATE (reuses the Telegram bot's key/value store)
# ============================================================


def _state():
    from telegram_bot import state_get
    return state_get("proposal_draft") or {}


def _save(draft):
    from telegram_bot import state_set
    state_set("proposal_draft", draft)


def _edit_mode():
    from telegram_bot import state_get
    mode = state_get("proposal_edit") or {}
    if mode.get("until", 0) < time.time():
        return None
    return mode.get("field")


def _set_edit_mode(field=None):
    from telegram_bot import state_set
    state_set("proposal_edit", {"field": field, "until": time.time() + EDIT_WINDOW_SECONDS} if field else {})


def edit_target():
    """'scope' or 'details' if James's next message should edit the proposal draft."""
    if not _state().get("spec"):
        return None
    return _edit_mode()

# ============================================================
# CLAUDE: INTAKE AND EDITS
# ============================================================

SPEC_SHAPE = """{
  "project_name": "short name used in the file name, e.g. Soundbath",
  "client_lines": ["Company or owner name", "Street", "City, ST ZIP"],
  "salutation_name": "first name or Mr./Ms. Last, as in 'Dear ___,'",
  "owner_signature_name": "person or entity signing for the client",
  "client_role": "Owner | HOA | Developer | Lender ... (default Owner)",
  "project_address": "full street address of the project",
  "services_for": "what MCM's services cover, e.g. 'the project' or 'the Interior Common Area & FOH renovation project'",
  "re_line": "Soundbath – Owner’s Representative Services",
  "project_scope": "one sentence for 'On behalf of the Owner: ...'",
  "stages": ["optional list of project stages to show in Attachment A"],
  "phases": [{"phase": "1", "description": "Pre-Construction", "months": 4, "monthly_fee": 18000}],
  "fee_notes": ["optional lines under the fee table"],
  "folder": "optional OneDrive folder under MCM/Projects if James names one",
  "terms": {"optional overrides": "principal_rate, pm_rate, escalation_pct, retainer_months, late_interest_pct, reimbursable_markup_pct, placement_fee_pct, convenience_notice_days, liability_cap"}
}"""

INTAKE_SYSTEM = f"""You turn James Galvin's quick notes into a proposal spec for Miami Coastline
Management (MCM), his owner's representative firm in South Florida.

Return ONLY a JSON object of this shape (omit keys you have no information for;
never invent names, addresses or fees):
{SPEC_SHAPE}

Rules:
- Fees are numbers in dollars: "5k" -> 5000, "12.5k" -> 12500. Months are numbers.
- One phase per fee line, in the order given; number phases "1", "2", "3"...
  A phase description can carry a staffing note, e.g. "Construction – PM 50%".
- Use the en dash (–) in re_line and descriptions; curly apostrophes (’) in text.
- If James says the client is an HOA, condo association, lender etc., set client_role.
- Only include terms overrides James asks for."""

EDIT_DETAILS_SYSTEM = f"""You update a proposal spec for Miami Coastline Management. Apply James's
change and return ONLY the full updated JSON object (same shape as before):
{SPEC_SHAPE}
Change only what he asks; keep everything else exactly as it is. Never invent names or fees."""

EDIT_SCOPE_SYSTEM = """You edit Attachment A (Scope of Services) of an owner's representative
proposal for Miami Coastline Management. Apply James's change and return ONLY the full updated
text, in the same plain-text format:
  ## HEADING        section heading
  - item            bullet
    - sub-item      indented bullet (2 spaces)
  plain line        paragraph
  **bold**          bold text
  {role}            stays as-is (filled in later)
Change only what he asks; keep every other line word for word. Write in the same plain,
professional owner's-rep voice. Never add design, engineering or jobsite-safety
responsibilities for MCM (its terms exclude them); if asked, phrase them as coordination
or oversight of others."""


def _ask_claude(system, user, max_tokens=2500):
    resp = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _json_from(text):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Claude didn't return a spec.")
    return json.loads(text[start:end + 1])


def _clean_spec(spec):
    spec = {k: v for k, v in spec.items() if v not in (None, "", [], {})}
    if isinstance(spec.get("client_lines"), str):
        spec["client_lines"] = [l.strip() for l in re.split(r"[\n;]", spec["client_lines"]) if l.strip()]
    phases = []
    for i, ph in enumerate(spec.get("phases") or []):
        try:
            months = float(ph.get("months"))
            fee = float(ph.get("monthly_fee"))
        except (TypeError, ValueError):
            continue
        phases.append({"phase": str(ph.get("phase") or i + 1), "description": ph.get("description") or "",
                       "months": int(months) if months.is_integer() else months,
                       "monthly_fee": int(fee) if fee.is_integer() else fee})
    if phases:
        spec["phases"] = phases
    else:
        spec.pop("phases", None)
    return spec


def missing_fields(spec):
    return [label for key, label in REQUIRED.items() if not spec.get(key)]

# ============================================================
# ONEDRIVE: FOLDER, REVISION, TEMPLATE
# ============================================================


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_folder(spec):
    """Project folder under MCM/Projects: James's choice, else the closest existing match, else new."""
    if spec.get("folder"):
        return spec["folder"].strip("/")
    name = _norm(spec.get("project_name"))
    try:
        top = [c["name"] for c in list_children(PROJECTS_ROOT) if c["is_folder"]]
    except Exception as e:
        print(f"[proposals] couldn't list {PROJECTS_ROOT}: {e}")
        return safe_name(spec.get("project_name", "New Project"))
    best, score = None, 0.0
    for folder in top:
        n = _norm(folder)
        s = 1.0 if n == name else 0.9 if (name and (name in n or n in name)) else \
            difflib.SequenceMatcher(None, name, n).ratio()
        if s > score:
            best, score = folder, s
    if best and score >= 0.75:
        return best
    return safe_name(spec.get("project_name", "New Project"))


_REV_RE = re.compile(r"^Miami Coastline - (.+?) - Proposal(?: - Rev\.?\s*(\d+))?\.docx$", re.I)


def next_revision(folder, project_name):
    """0 for the first proposal in this folder, else one past the highest Rev found."""
    seen = []
    for sub in (CONTRACT_FOLDER, f"{CONTRACT_FOLDER}/Proposal History"):
        try:
            names = [c["name"] for c in list_children(f"{PROJECTS_ROOT}/{folder}/{sub}") if not c["is_folder"]]
        except Exception:
            continue
        for n in names:
            m = _REV_RE.match(n)
            if m and _norm(m.group(1)) == _norm(project_name):
                seen.append(int(m.group(2) or 0))
    return max(seen) + 1 if seen else 0

# ============================================================
# MESSAGES
# ============================================================


def _money(x):
    return f"${x:,.0f}"


def _summary_text(draft):
    spec = draft["spec"]
    lines = [f"PROPOSAL DRAFT — {spec.get('project_name', '(project?)')}", ""]
    if spec.get("client_lines"):
        lines.append("Client: " + ", ".join(spec["client_lines"]))
    if spec.get("salutation_name"):
        lines.append(f"Letter to: Dear {spec['salutation_name']},")
    if spec.get("owner_signature_name"):
        lines.append(f"Signs for {spec.get('client_role', 'Owner')}: {spec['owner_signature_name']}")
    if spec.get("project_address"):
        lines.append(f"Project: {spec['project_address']}")
    if spec.get("phases"):
        fees = gen.compute_fees(spec["phases"])
        lines += ["", "FEES"]
        for r in fees["rows"]:
            lines.append(f"{r['phase']}. {r['description']}: {r['months']:g} mo × {_money(r['monthly_fee'])}"
                         f" = {_money(r['line_total'])}")
        lines.append(f"TOTAL: {fees['total_months']:g} months, {_money(fees['total_fee'])}"
                     + (f" (blended {_money(fees['blended_monthly'])}/mo)" if fees["mixed_rates"] else ""))
    t = {**gen.TERMS_DEFAULTS, **spec.get("terms", {})}
    overrides = spec.get("terms") or {}
    lines += ["", "Terms: MCM standard" + (f" with changes: {', '.join(overrides)}" if overrides else "")
              + f" (retainer {t['retainer_months']} mo, {t['escalation_pct']:g}% annual escalation)"]
    if draft.get("folder"):
        rev = draft.get("revision") or 0
        fname = gen.proposal_filename({**spec, "revision": rev or None})
        lines += ["", f"Saves to: {PROJECTS_ROOT}/{draft['folder']}/{CONTRACT_FOLDER}/", fname]
    missing = missing_fields(spec)
    if missing:
        lines += ["", "STILL NEEDED: " + "; ".join(missing), "Tap Edit details and tell me."]
    else:
        lines += ["", "Build saves the Word file, sends it here and logs the lead at Proposal. "
                      "Nothing goes to the client."]
    return "\n".join(lines)


def _keyboard(spec):
    row1 = [{"text": "Edit scope", "callback_data": "propscope:0"},
            {"text": "Edit details", "callback_data": "propdetails:0"}]
    rows = []
    if not missing_fields(spec):
        rows.append([{"text": "Build proposal", "callback_data": "propbuild:0"}])
    rows += [row1, [{"text": "Cancel", "callback_data": "propcancel:0"}]]
    return {"inline_keyboard": rows}


def _role_text(spec):
    return spec["attachment_a"].replace("{role}", spec.get("client_role", "Owner"))


def _show(draft, scope_too=True):
    from telegram_bot import send_message
    if scope_too:
        send_message("ATTACHMENT A — SCOPE OF SERVICES\n(Edit scope to change it: send new text starting "
                     "with ##, or tell me what to change)\n\n" + _role_text(draft["spec"]))
    send_message(_summary_text(draft), reply_markup=_keyboard(draft["spec"]))


def _refresh_location(draft):
    spec = draft["spec"]
    if spec.get("project_name"):
        draft["folder"] = resolve_folder(spec)
        draft["revision"] = next_revision(draft["folder"], spec["project_name"])
    return draft

# ============================================================
# ENTRY POINTS (called from telegram_bot.py)
# ============================================================


def start(intake: str) -> None:
    from telegram_bot import send_message, _typing
    if not intake.strip():
        send_message("Tell me about the job after /proposal, e.g.\n"
                     "/proposal Soundbath for Vishal Sapra, Celebration Ventures, 3114 N Bay Rd, Miami FL. "
                     "Project at 410 NE 29th St. Due diligence 4 mo @ 5k, precon 12 @ 12k, construction 14 @ 12k")
        return
    _typing()
    try:
        spec = _clean_spec(_json_from(_ask_claude(INTAKE_SYSTEM, intake)))
    except Exception as e:
        print(f"[proposals] intake failed: {e}")
        send_message(f"I couldn't read that into a proposal ({str(e)[:150]}). Try again with the client, "
                     "address and fee phases.")
        return
    role = spec.get("client_role", "Owner")
    spec["attachment_a"] = gen.default_attachment_a(
        "{role}",
        spec.pop("project_scope", None) or "Owner’s Representative oversight of the Pre-Construction and "
                                           "Construction phases of the project.",
        spec.pop("stages", None))
    spec.setdefault("client_role", role)
    draft = _refresh_location({"spec": spec, "intake": intake, "created": time.time()})
    _set_edit_mode(None)
    _save(draft)
    _show(draft)


def start_edit(field: str) -> None:
    from telegram_bot import send_message, state_set
    if not _state().get("spec"):
        send_message("That proposal draft is gone. Start again with /proposal.")
        return
    state_set("edit_mode", {})
    state_set("li_edit", {})
    state_set("invoice_edit", {})
    state_set("onboard_edit", {})
    _set_edit_mode(field)
    if field == "scope":
        send_message("Send the new Attachment A (start with ## to replace it all), or tell me what to change: "
                     "\"add value engineering under General\", \"drop Project Stages\".")
    else:
        send_message("What should change? e.g. \"precon is 10 months\", \"add a 4th phase: closeout 2 mo at 6k\", "
                     "\"client is the HOA\", \"save under Pacific Star/3600 Design District\", \"PM rate 175\".")


def apply_edit(text: str) -> None:
    from telegram_bot import send_message, _typing
    draft = _state()
    field = _edit_mode()
    if not draft.get("spec") or not field:
        send_message("Nothing to edit. Start with /proposal.")
        return
    if text.strip().lower() in ("cancel", "stop", "never mind", "nevermind"):
        _set_edit_mode(None)
        send_message("OK, left the draft as it was.")
        _show(draft, scope_too=False)
        return
    _typing()
    spec = draft["spec"]
    try:
        if field == "scope":
            if text.lstrip().startswith("#"):
                new_a = text.strip()
            else:
                new_a = _ask_claude(EDIT_SCOPE_SYSTEM,
                                    f"CURRENT ATTACHMENT A:\n{spec['attachment_a']}\n\nCHANGE:\n{text}", 3000)
                new_a = re.sub(r"^```\w*|```$", "", new_a, flags=re.M).strip()
            gen.parse_attachment_a(new_a)        # validates it isn't empty
            spec["attachment_a"] = new_a
        else:
            current = {k: v for k, v in spec.items() if k != "attachment_a"}
            updated = _clean_spec(_json_from(_ask_claude(
                EDIT_DETAILS_SYSTEM, f"CURRENT SPEC:\n{json.dumps(current, ensure_ascii=False)}\n\nCHANGE:\n{text}")))
            updated.pop("project_scope", None)
            updated.pop("stages", None)
            spec = {**updated, "attachment_a": spec["attachment_a"]}
            draft["spec"] = spec
            draft = _refresh_location(draft)
    except Exception as e:
        print(f"[proposals] edit failed: {e}")
        send_message(f"Couldn't apply that ({str(e)[:150]}). Try saying it another way.")
        return
    _set_edit_mode(None)
    _save(draft)
    _show(draft, scope_too=(field == "scope"))


def cancel() -> None:
    from telegram_bot import send_message
    _save({})
    _set_edit_mode(None)
    send_message("Proposal draft discarded. Nothing was saved.")


def _send_document(content: bytes, filename: str, caption: str, reply_markup=None):
    from telegram_bot import API, CHAT_ID
    data = {"chat_id": CHAT_ID, "caption": caption[:1000]}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(f"{API}/sendDocument", data=data,
                         files={"document": (filename, content, DOCX_MIME)}, timeout=60)
    if resp.status_code >= 400:
        print(f"[proposals] sendDocument failed [{resp.status_code}]: {resp.text[:300]}")


def record_lead(spec, summary, filename) -> str:
    """Create or update the lead at status Proposal. Returns 'created' or 'updated'."""
    from rowan_agent import ensure_extra_schema
    ensure_extra_schema()
    name = spec["project_name"].strip()
    contact = f"{spec.get('owner_signature_name', '')} ({spec['client_lines'][0]})".strip()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    note = (f"{today}: {filename} prepared — {_money(summary['total_fee'])} over "
            f"{summary['total_months']:g} months.")
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, notes FROM leads WHERE LOWER(name) = LOWER(%s) ORDER BY updated_at DESC NULLS LAST LIMIT 1",
                (name,))
    row = cur.fetchone()
    if row:
        notes = f"{row[1]}\n{note}" if row[1] else note
        cur.execute("""UPDATE leads SET value = %s, status = 'Proposal', notes = %s,
                       contact = COALESCE(NULLIF(contact, ''), %s), updated_at = NOW() WHERE id = %s""",
                    (summary["total_fee"], notes, contact, row[0]))
        result = "updated"
    else:
        cur.execute("""INSERT INTO leads (name, contact, value, status, source, notes)
                       VALUES (%s, %s, %s, 'Proposal', 'Proposal generator', %s)""",
                    (name, contact, summary["total_fee"], note))
        result = "created"
    conn.commit()
    cur.close()
    conn.close()
    return result


def _save_built(spec, summary, item, folder) -> int:
    """Keep every built proposal so /won can set the project up from it."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id SERIAL PRIMARY KEY,
            project_name TEXT,
            folder TEXT,
            filename TEXT,
            path TEXT,
            web_url TEXT,
            total_fee NUMERIC,
            total_months NUMERIC,
            spec JSONB,
            status TEXT DEFAULT 'sent',          -- sent | signed
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""INSERT INTO proposals (project_name, folder, filename, path, web_url, total_fee,
                   total_months, spec) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (spec["project_name"], folder, item["name"], item["path"], item.get("web_url"),
                 summary["total_fee"], summary["total_months"], json.dumps(spec)))
    pid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pid


def build() -> None:
    from telegram_bot import send_message, _link_button, _typing
    draft = _state()
    spec = draft.get("spec")
    if not spec:
        send_message("That proposal draft is gone. Start again with /proposal.")
        return
    missing = missing_fields(spec)
    if missing:
        send_message("Still needed before I can build it: " + "; ".join(missing))
        return
    _typing()
    draft = _refresh_location(draft)
    rev = draft.get("revision") or 0
    full = {**spec, "revision": rev or None, "date": datetime.now(TZ).date().isoformat()}
    try:
        template = download_path(TEMPLATE_PATH)
    except Exception as e:
        send_message(f"Couldn't open the proposal template at {TEMPLATE_PATH}: {str(e)[:200]}")
        return
    try:
        content, summary = gen.build_proposal(template, full)
    except Exception as e:
        print(f"[proposals] build failed: {e}")
        send_message(f"Couldn't build the proposal: {str(e)[:250]}")
        return
    filename = summary["filename"]
    try:
        item = upload_file(content, f"{draft['folder']}/{CONTRACT_FOLDER}", filename, DOCX_MIME,
                           root=PROJECTS_ROOT)
    except Exception as e:
        print(f"[proposals] upload failed: {e}")
        _send_document(content, filename, "Built, but OneDrive upload failed — here's the file. "
                                          f"({str(e)[:150]})")
        return
    try:
        lead = record_lead(spec, summary, item["name"])
        lead_line = f"Lead {lead}: {spec['project_name']}, Proposal, {_money(summary['total_fee'])}."
    except Exception as e:
        print(f"[proposals] lead failed: {e}")
        lead_line = f"Couldn't record the lead ({str(e)[:120]}); add it with /lead."
    try:
        proposal_id = _save_built(spec, summary, item, draft["folder"])
    except Exception as e:
        print(f"[proposals] couldn't record proposal: {e}")
        proposal_id = None
    buttons = [[{"text": "Open in OneDrive", "url": item["web_url"]}]] if item.get("web_url") else []
    if proposal_id:
        buttons.append([{"text": "Mark signed → set up project", "callback_data": f"onbstart:{proposal_id}"}])
    _send_document(content, item["name"],
                   f"{item['name']}\n{_money(summary['total_fee'])} over {summary['total_months']:g} months. "
                   f"Saved to {item['path']}.\n{lead_line}\n\nWhen the client signs, tap Mark signed "
                   f"(or send /won {spec['project_name']}).",
                   reply_markup={"inline_keyboard": buttons} if buttons else None)
    draft["built"] = {"name": item["name"], "path": item["path"], "at": time.time()}
    _save(draft)
    send_message("Want changes? Tap Edit scope or Edit details on the draft above and Build again; "
                 "it saves as the next revision.")
