"""
meetings_page.py — HTML for /meetings (list + start) and /meetings/{id} (live, review, closed).

Routes are in dashboard.py; the logic is in meetings.py.
"""
import html
import json

import register as reg

esc = html.escape

STATUS = {"live": ("Live", "st-live"), "processing": ("Writing minutes", "st-proc"),
          "review": ("Review", "st-review"), "closed": ("Done", "st-closed")}


def _project_options(projects: list, selected=None, blank: str = "No project") -> str:
    opts = [f"<option value='0'>{esc(blank)}</option>"]
    for p in projects:
        sel = " selected" if selected and p["id"] == selected else ""
        opts.append(f"<option value='{p['id']}'{sel}>{esc(p['name'])}</option>")
    return "".join(opts)


def _platform_options(selected: str = "") -> str:
    from meetings import PLATFORMS
    opts = ["<option value=''>Platform</option>"]
    for p in PLATFORMS:
        opts.append(f"<option{' selected' if p == selected else ''}>{esc(p)}</option>")
    return "".join(opts)


def _place(loc) -> str:
    """Hide raw join links ('https://zoom.us/j/...'); keep real places."""
    loc = (loc or "").strip()
    return "" if loc.lower().startswith(("http", "microsoft teams", "zoom")) else loc


def _when(ts) -> str:
    return reg.when(ts) if ts else ""


# ============================================================
# /meetings
# ============================================================

def list_page(events: list, meetings: list, projects: list, err: str = "") -> str:
    ev_html = ""
    for e in events:
        attendees = ", ".join(a for a in (e.get("attendees") or []) if a)
        badge = "<span class='ev-now'>Now</span>" if e.get("now") else ""
        plat = f"<span class='ev-plat'>{esc(e['platform'])}</span>" if e.get("platform") else ""
        ev_html += f"""
        <form method='post' action='/meetings/start' class='ev'>
            <input type='hidden' name='title' value='{esc(e["subject"])}'>
            <input type='hidden' name='event_id' value='{esc(e.get("event_id") or "")}'>
            <input type='hidden' name='platform' value='{esc(e.get("platform") or "")}'>
            <input type='hidden' name='attendees' value='{esc(attendees)}'>
            <input type='hidden' name='project_id' value='{e.get("project_id") or 0}'>
            <button type='submit'>
                <span class='ev-time'>{esc(e["time"])}</span>
                <span class='ev-body'><span class='ev-t'>{esc(e["subject"])}</span>
                    <span class='ev-m'>{plat}{esc(_place(e.get("location")))}</span></span>
                {badge}<i class='ti ti-player-play'></i>
            </button>
        </form>"""
    if not ev_html:
        ev_html = "<div class='empty-sm'>Nothing else on your Outlook calendar today.</div>"

    rows = ""
    for m in meetings:
        label, cls = STATUS.get(m["status"], (m["status"], ""))
        bits = [b for b in (m.get("project"), m.get("platform"), _when(m["started_at"])) if b]
        counts = ""
        if m.get("pending"):
            counts = f"<span class='mc-pend'>{m['pending']} to review</span>"
        elif m.get("approved"):
            counts = f"<span class='mc-ok'>{m['approved']} task{'s' if m['approved'] != 1 else ''}</span>"
        rows += f"""
        <a class='mrow' href='/meetings/{m["id"]}'>
            <span class='st {cls}'>{esc(label)}</span>
            <span class='mrow-b'><span class='mrow-t'>{esc(m["title"])}</span>
                <span class='mrow-m'>{esc(" · ".join(bits))}</span></span>
            {counts}<i class='ti ti-chevron-right'></i>
        </a>"""
    if not rows:
        rows = "<div class='empty-sm'>No meetings yet. Start one above.</div>"

    banner = f"<div class='tp-err'>{esc(err)}</div>" if err else ""
    return f"""
    <div class='mt'>
        {banner}
        <div class='mt-h'><h2>Meetings</h2></div>
        <div class='panel'>
            <div class='sec'>Start from your calendar</div>
            <div class='evs'>{ev_html}</div>
            <details class='adhoc'>
                <summary><i class='ti ti-plus'></i> Start a meeting that isn't on the calendar</summary>
                <form method='post' action='/meetings/start' class='adhoc-f'>
                    <input name='title' placeholder='Title, e.g. Farbstein site walk' required>
                    <select name='project_id'>{_project_options(projects)}</select>
                    <select name='platform'>{_platform_options("In person")}</select>
                    <input name='attendees' placeholder='Who is there (optional)'>
                    <button type='submit'><i class='ti ti-player-play'></i> Start</button>
                </form>
            </details>
        </div>
        <div class='sec' style='margin-top:22px'>Recent</div>
        <div class='mrows'>{rows}</div>
    </div>"""


# ============================================================
# /meetings/{id}
# ============================================================

def _carry_panel(m: dict, carry: dict, editable: bool) -> str:
    tasks, questions, prev = carry["tasks"], carry["questions"], carry["previous"]
    if not prev or (not tasks and not questions):
        return ""
    items = ""
    for t in tasks:
        who = f"Waiting on {t['waiting_on']}" if t.get("ball_in_court") == "waiting" and t.get("waiting_on") else "You"
        due = f"due {reg.short_date(t['due_date'])}" if t.get("due_date") else ""
        btns = (f"<button class='cb' data-carry='{t['id']}' data-act='done' title='Mark done'><i class='ti ti-check'></i></button>"
                if editable else "")
        items += (f"<div class='ci' id='carry-{t['id']}'><div class='ci-b'><a href='/task/{t['id']}?back=/meetings/{m['id']}'>"
                  f"{esc(t['description'])}</a><span class='ci-m'>{esc(who)}{' · ' + esc(due) if due else ''}</span></div>{btns}</div>")
    qs = "".join(f"<div class='ci q'><i class='ti ti-help-circle'></i><div class='ci-b'>{esc(q)}</div></div>" for q in questions)
    when = reg.short_date(prev["started_at"].astimezone(reg.ET).date()) if prev.get("started_at") else ""
    return f"""
        <details class='panel carry' open>
            <summary><i class='ti ti-history'></i> Open from <a href='/meetings/{prev["id"]}'>last meeting</a> ({esc(when)})
                <span class='cnt'>{len(tasks) + len(questions)}</span></summary>
            {items}{qs}
        </details>"""


def _transcript_tools(m: dict) -> str:
    return f"""
        <details class='addtr'>
            <summary><i class='ti ti-file-upload'></i> Add a Zoom / Teams / Meet transcript</summary>
            <form id='trForm' class='addtr-f'>
                <textarea name='text' rows='4' placeholder='Paste the transcript or recap here'></textarea>
                <div class='addtr-r'>
                    <input type='file' name='file' accept='.vtt,.srt,.txt,.docx,.pdf,.md'>
                    <button type='submit'>Add</button>
                </div>
                <div class='hint'>Zoom: Recordings → Audio transcript (.vtt). Teams: meeting chat → Recap → Transcript → Download (.docx or .vtt). Meet: the transcript Google Doc in Drive (download as .docx) or paste it.</div>
            </form>
        </details>"""


def _details_form(m: dict, projects: list) -> str:
    return f"""
        <details class='meta-edit'>
            <summary><i class='ti ti-pencil'></i> Details</summary>
            <form id='metaForm' class='adhoc-f'>
                <input name='title' value='{esc(m["title"])}' placeholder='Title'>
                <select name='project_id'>{_project_options(projects, m["project_id"])}</select>
                <select name='platform'>{_platform_options(m.get("platform") or "")}</select>
                <input name='attendees' value='{esc(m.get("attendees") or "")}' placeholder='Attendees'>
                <button type='submit'>Save</button>
            </form>
        </details>"""


def _head(m: dict, projects: list, editable: bool) -> str:
    label, cls = STATUS.get(m["status"], (m["status"], ""))
    bits = [b for b in (m.get("project") or "No project", m.get("platform"),
                        _when(m["started_at"]), m.get("attendees")) if b]
    return f"""
        <a class='tp-back' href='/meetings'><i class='ti ti-arrow-left'></i> Meetings</a>
        <div class='mh'>
            <div><h2>{esc(m["title"])}</h2><div class='mh-m'>{esc(" · ".join(bits))}</div></div>
            <span class='st {cls}'>{esc(label)}</span>
        </div>
        {_details_form(m, projects) if editable else ""}"""


def _live(m: dict, carry: dict, projects: list) -> str:
    err = f"<div class='tp-err'>{esc(m['error'])}</div>" if m.get("error") else ""
    transcript = esc(m["transcript"] or "")
    return f"""
        {err}
        {_carry_panel(m, carry, True)}
        <div class='panel notes'>
            <div class='notes-h'>
                <div class='chips'>
                    <button type='button' class='chip-i' data-ins='Act: ' title='Action item for someone else'>Act</button>
                    <button type='button' class='chip-i' data-ins='ME: ' title='Action item for you'>ME</button>
                    <button type='button' class='chip-i' data-ins='D: ' title='Decision'>D</button>
                    <button type='button' class='chip-i' data-ins='?: ' title='Open question'>?</button>
                </div>
                <span id='saveState' class='save-st'>Saved</span>
            </div>
            <textarea id='notes' placeholder='Type as the meeting goes.&#10;Act: Jonny - pool shop drawings Fri&#10;ME: send revised budget to owner&#10;D: owner approved tile upgrade&#10;?: who pays for the survey'>{esc(m["notes"] or "")}</textarea>
        </div>

        <div class='panel rec'>
            <div class='rec-row'>
                <button type='button' id='recBtn' class='rec-btn'><i class='ti ti-microphone'></i><span>Record</span></button>
                <div class='rec-info'><span id='recTime'>00:00</span> <span id='recState' class='rec-st'>Not recording</span></div>
            </div>
            <div class='hint'>Transcribed about every minute. Keep this screen open while recording. If others are being recorded, get their OK first (Florida requires everyone's consent).</div>
            <div id='transcript' class='tr {"" if transcript else "empty"}'>{transcript or "Transcript will appear here."}</div>
            {_transcript_tools(m)}
        </div>

        <div class='endbar'>
            <button type='button' id='endBtn' class='end-btn'><i class='ti ti-player-stop'></i> End meeting &amp; draft tasks</button>
            <button type='button' id='delBtn' class='del-btn' title='Delete this meeting'><i class='ti ti-trash'></i></button>
        </div>"""


def _item_card(m: dict, q: dict, projects: list) -> str:
    if q["status"] != "proposed":
        if q["status"] == "approved" and q.get("task_id"):
            return (f"<div class='it done'><i class='ti ti-circle-check'></i><div class='it-b'>"
                    f"<a href='/task/{q['task_id']}?back=/meetings/{m['id']}'>{esc(q['description'])}</a>"
                    f"<span class='ci-m'>In the register · #{q['task_id']}</span></div></div>")
        return f"<div class='it dropped'><i class='ti ti-x'></i><div class='it-b'>{esc(q['description'])}</div></div>"
    owner = q["waiting_on"] if q["ball_in_court"] == "waiting" else ""
    due = q["due_date"].isoformat() if q.get("due_date") else ""
    ev = f"<div class='it-ev'>“{esc(q['evidence'])}”</div>" if q.get("evidence") else ""
    return f"""
        <div class='it' data-pid='{q["id"]}'>
            <input class='it-d' name='description' value='{esc(q["description"])}'>
            {ev}
            <div class='it-f'>
                <label><span>Owner</span><input name='owner' value='{esc(owner or "")}' placeholder='Me'></label>
                <label><span>Due</span><input type='date' name='due_date' value='{due}'></label>
                <label><span>Project</span><select name='project_id'>{_project_options(projects, q.get("project_id"))}</select></label>
            </div>
            <div class='it-a'>
                <button type='button' class='ok' data-act='approve'><i class='ti ti-check'></i> Add to register</button>
                <button type='button' class='no' data-act='drop'><i class='ti ti-x'></i> Drop</button>
            </div>
        </div>"""


def _bullets(title: str, items: list, icon: str) -> str:
    if not items:
        return ""
    lis = "".join(f"<li>{esc(i)}</li>" for i in items)
    return f"<div class='mn-s'><div class='sec'><i class='ti {icon}'></i> {esc(title)}</div><ul>{lis}</ul></div>"


def _record(m: dict) -> str:
    """Notes and transcript, folded away, for review/closed."""
    out = ""
    if (m["notes"] or "").strip():
        out += f"<details class='raw'><summary>Your notes</summary><pre>{esc(m['notes'])}</pre></details>"
    if (m["transcript"] or "").strip():
        out += f"<details class='raw'><summary>Transcript</summary><pre>{esc(m['transcript'])}</pre></details>"
    return out


def _review(m: dict, items: list, carry: dict, projects: list) -> str:
    pending = [q for q in items if q["status"] == "proposed"]
    cards = "".join(_item_card(m, q, projects) for q in items) or "<div class='empty-sm'>No action items found.</div>"
    by_id = {t["id"]: t for t in carry["tasks"]}
    sugg = ""
    for c in m["carried"]:
        t = by_id.get(c["task_id"])
        if not t:
            continue
        act = ("<button type='button' class='ok' data-carry='{0}' data-act='done' data-note='{1}'><i class='ti ti-check'></i> Close task</button>"
               if c["status"] == "done" else
               "<button type='button' class='ok' data-carry='{0}' data-act='note' data-note='{1}'><i class='ti ti-message-2'></i> Log note</button>"
               ).format(t["id"], esc(c.get("note") or ""))
        sugg += (f"<div class='it' id='carry-{t['id']}'><div class='it-b'><b>{esc(t['description'])}</b>"
                 f"<span class='ci-m'>{'Sounds done' if c['status'] == 'done' else 'Update'}: {esc(c.get('note') or '')}</span></div>"
                 f"<div class='it-a'>{act}<button type='button' class='no' data-carry='{t['id']}' data-act='skip'>Skip</button></div></div>")
    sugg_html = f"<div class='panel'><div class='sec'><i class='ti ti-history'></i> Carried items from earlier meetings</div>{sugg}</div>" if sugg else ""

    approve_all = (f"<button type='button' id='approveAll' class='ok'><i class='ti ti-checks'></i> Add all {len(pending)}</button>"
                   if len(pending) > 1 else "")
    closing = ("<button type='button' id='closeBtn' class='end-btn'><i class='ti ti-file-check'></i> Finish &amp; file minutes</button>"
               if m["status"] == "review" else
               "<span class='filed'><i class='ti ti-circle-check'></i> Minutes filed"
               + (" to the project notes" if m["project_id"] else "") + "</span>")
    return f"""
        <div class='panel'>
            <div class='sec-row'><div class='sec'><i class='ti ti-list-check'></i> Action items</div>{approve_all}</div>
            <div class='its'>{cards}</div>
        </div>
        {sugg_html}
        <div class='panel mn'>
            {_bullets("Decisions", m["decisions"], "ti-gavel")}
            {f"<div class='mn-s'><div class='sec'><i class='ti ti-notes'></i> Minutes</div><pre class='mn-t'>{esc(m['minutes'])}</pre></div>" if m.get("minutes") else ""}
            {_bullets("Open questions (carried to next meeting)", m["questions"], "ti-help-circle")}
        </div>
        {_record(m)}
        <div class='endbar'>
            {closing}
            <button type='button' id='reopenBtn' class='ghost'><i class='ti ti-pencil'></i> Back to notes</button>
        </div>"""


def meeting_page(m: dict, items: list, carry: dict, projects: list) -> str:
    status = m["status"]
    if status == "live":
        body = _live(m, carry, projects)
    elif status == "processing":
        body = ("<div class='panel proc'><div class='spin'></div><div>Rowan is writing the minutes and drafting tasks. "
                "This takes about 20–40 seconds; the page will refresh itself. You'll also get it in Telegram.</div></div>")
    else:
        body = _review(m, items, carry, projects)
    cfg = json.dumps({"id": m["id"], "status": status})
    return f"""
    <div class='mt'>
        {_head(m, projects, status in ("live", "review"))}
        {body}
    </div>
    <script>window.MTG = {cfg};</script>
    <script>{JS}</script>"""


# ============================================================
# STYLES + SCRIPT
# ============================================================

CSS = """
    .mt { max-width: 820px; margin: 0 auto; }
    .mt .panel { margin-bottom: 14px; }
    .mt-h h2, .mh h2 { margin: 0; font-size: 20px; font-weight: 600; color: #FFFFFF; }
    .mt-h { margin-bottom: 14px; }
    .sec { font-size: 12px; text-transform: uppercase; letter-spacing: .7px; color: #9993D0; font-weight: 600;
           margin-bottom: 10px; display: flex; align-items: center; gap: 6px; }
    .sec-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 4px; }
    .hint { font-size: 12px; color: #847FC0; margin-top: 8px; line-height: 1.45; }
    .empty-sm { color: #847FC0; font-size: 14px; padding: 6px 2px; }
    .tp-back { display: inline-flex; align-items: center; gap: 6px; color: #ABA6DD; text-decoration: none; font-size: 13px; margin-bottom: 12px; }
    .tp-err { border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; background: rgba(255,92,138,0.14); color: #FF8FC4; }

    .evs { display: flex; flex-direction: column; gap: 8px; }
    .ev { margin: 0; }
    .ev button { width: 100%; display: flex; align-items: center; gap: 12px; text-align: left; cursor: pointer;
                 background: #1B1747; border: 1px solid #352F78; border-radius: 12px; padding: 12px 14px; color: #E6E3FF; font-family: inherit; }
    .ev button:hover { border-color: #B45CF0; }
    .ev-time { font-size: 13px; color: #ABA6DD; width: 66px; flex-shrink: 0; }
    .ev-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
    .ev-t { font-size: 15px; font-weight: 500; color: #FFFFFF; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ev-m { font-size: 12px; color: #847FC0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ev-plat { color: #E0B0FF; margin-right: 6px; }
    .ev-now { font-size: 11px; font-weight: 600; color: #FFFFFF; background: linear-gradient(90deg, #FF4F8B, #9B4DEB);
              border-radius: 8px; padding: 2px 8px; }
    .ev .ti-player-play { color: #FF8FC4; font-size: 18px; }

    details > summary { cursor: pointer; list-style: none; }
    details > summary::-webkit-details-marker { display: none; }
    .adhoc, .meta-edit, .addtr { margin-top: 12px; }
    .adhoc summary, .meta-edit summary, .addtr summary { font-size: 13px; color: #CBC7F0; display: inline-flex; align-items: center; gap: 6px; }
    .meta-edit { margin: 0 0 12px; }
    .adhoc-f { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 8px; margin-top: 10px; }
    .adhoc-f input[name=attendees] { grid-column: 1 / 3; }
    .adhoc-f input, .adhoc-f select, .it input, .it select, .addtr textarea, .addtr input[type=file] {
        font-family: inherit; background: #14113A; border: 1px solid #352F78; border-radius: 8px; color: #F3F1FF;
        padding: 9px 10px; font-size: 14px; color-scheme: dark; min-width: 0; }
    .adhoc-f button, .addtr button { font-family: inherit; border: none; border-radius: 8px; padding: 9px 14px; cursor: pointer;
        color: #FFFFFF; font-weight: 500; background: linear-gradient(90deg, #FF4F8B, #9B4DEB); }

    .mrows { display: flex; flex-direction: column; gap: 8px; }
    .mrow { display: flex; align-items: center; gap: 12px; text-decoration: none; color: #E6E3FF;
            background: rgba(34,29,88,0.72); border: 1px solid #352F78; border-radius: 12px; padding: 12px 14px; }
    .mrow:hover { border-color: #B45CF0; }
    .mrow-b { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
    .mrow-t { font-size: 15px; color: #FFFFFF; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .mrow-m { font-size: 12px; color: #847FC0; }
    .mrow .ti-chevron-right { color: #847FC0; }
    .mc-pend { font-size: 12px; color: #FF8FC4; white-space: nowrap; }
    .mc-ok { font-size: 12px; color: #2bd4a0; white-space: nowrap; }
    .st { font-size: 11px; font-weight: 600; border-radius: 8px; padding: 3px 8px; white-space: nowrap; }
    .st-live { background: rgba(255,79,139,0.2); color: #FF8FC4; }
    .st-proc { background: rgba(245,166,35,0.18); color: #f5a623; }
    .st-review { background: rgba(180,92,240,0.2); color: #E0B0FF; }
    .st-closed { background: rgba(43,212,160,0.14); color: #2bd4a0; }

    .mh { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 8px; }
    .mh-m { font-size: 13px; color: #9993D0; margin-top: 4px; }

    .carry summary { font-size: 14px; color: #E6E3FF; display: flex; align-items: center; gap: 6px; }
    .carry summary a { color: #FF8FC4; }
    .cnt { margin-left: auto; background: #352F78; border-radius: 10px; padding: 1px 8px; font-size: 12px; color: #CBC7F0; }
    .ci { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-top: 1px solid #2A2468; }
    .ci:first-of-type { margin-top: 10px; }
    .ci-b { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; font-size: 14px; }
    .ci-b a { color: #F3F1FF; text-decoration: none; }
    .ci-m { font-size: 12px; color: #847FC0; }
    .ci.q .ti { color: #f5a623; }
    .cb { background: rgba(43,212,160,0.14); color: #2bd4a0; border: none; border-radius: 8px; width: 36px; height: 36px; cursor: pointer; font-size: 16px; }

    .notes-h { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .chips { display: flex; gap: 6px; }
    .chip-i { font-family: inherit; font-size: 13px; font-weight: 600; min-width: 42px; height: 34px; border-radius: 8px; cursor: pointer;
              border: 1px solid #4A4396; background: #1B1747; color: #E0B0FF; }
    .chip-i:active { background: rgba(180,92,240,0.3); }
    .save-st { font-size: 12px; color: #847FC0; }
    .save-st.bad { color: #FF8FC4; }
    #notes { width: 100%; min-height: 300px; resize: vertical; background: #14113A; border: 1px solid #352F78; border-radius: 10px;
             color: #F3F1FF; font-family: inherit; font-size: 15px; line-height: 1.55; padding: 12px; outline: none; box-sizing: border-box; }
    #notes:focus { border-color: #B45CF0; }

    .rec-row { display: flex; align-items: center; gap: 14px; }
    .rec-btn { font-family: inherit; display: inline-flex; align-items: center; gap: 8px; border: none; border-radius: 24px;
               padding: 11px 20px; font-size: 15px; font-weight: 600; cursor: pointer; color: #FFFFFF;
               background: linear-gradient(90deg, #FF4F8B, #9B4DEB); }
    .rec-btn.on { background: #FF5C8A; animation: pulse 1.6s infinite; }
    @keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(255,92,138,.5); } 50% { box-shadow: 0 0 0 8px rgba(255,92,138,0); } }
    .rec-info { font-size: 14px; color: #E6E3FF; font-variant-numeric: tabular-nums; }
    .rec-st { color: #847FC0; font-size: 12px; margin-left: 6px; }
    .tr { margin-top: 12px; max-height: 260px; overflow-y: auto; background: #14113A; border: 1px solid #2A2468; border-radius: 10px;
          padding: 10px 12px; font-size: 13px; line-height: 1.55; color: #CBC7F0; white-space: pre-wrap; }
    .tr.empty { color: #847FC0; }
    .addtr-f { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
    .addtr textarea { width: 100%; box-sizing: border-box; resize: vertical; }
    .addtr-r { display: flex; gap: 8px; align-items: center; }
    .addtr-r input[type=file] { flex: 1; padding: 7px; }

    .endbar { display: flex; gap: 10px; align-items: center; margin: 18px 0 30px; }
    .end-btn { flex: 1; font-family: inherit; border: none; border-radius: 12px; padding: 14px; font-size: 15px; font-weight: 600;
               cursor: pointer; color: #FFFFFF; background: linear-gradient(90deg, #FF4F8B, #9B4DEB);
               display: inline-flex; align-items: center; justify-content: center; gap: 8px; }
    .end-btn:disabled { opacity: .5; cursor: default; }
    .del-btn, .ghost { font-family: inherit; background: none; border: 1px solid #352F78; border-radius: 12px; color: #ABA6DD;
                       padding: 13px 15px; cursor: pointer; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }
    .filed { flex: 1; color: #2bd4a0; font-size: 14px; display: inline-flex; align-items: center; gap: 6px; }

    .proc { display: flex; align-items: center; gap: 14px; color: #E6E3FF; font-size: 14px; line-height: 1.5; }
    .spin { width: 26px; height: 26px; border-radius: 50%; border: 3px solid #352F78; border-top-color: #FF4F8B;
            animation: spin 0.9s linear infinite; flex-shrink: 0; }
    @keyframes spin { to { transform: rotate(360deg); } }

    .its { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
    .it { background: #1B1747; border: 1px solid #352F78; border-radius: 12px; padding: 12px; }
    .it.done, .it.dropped { display: flex; gap: 10px; align-items: flex-start; padding: 10px 12px; }
    .it.done .ti { color: #2bd4a0; font-size: 18px; } .it.done a { color: #F3F1FF; text-decoration: none; }
    .it.dropped { opacity: .5; text-decoration: line-through; }
    .it-b { flex: 1; display: flex; flex-direction: column; gap: 3px; font-size: 14px; color: #F3F1FF; }
    .it .it-d { width: 100%; box-sizing: border-box; font-size: 15px; font-weight: 500; }
    .it-ev { font-size: 12px; color: #847FC0; margin-top: 6px; font-style: italic; }
    .it-f { display: grid; grid-template-columns: 1.3fr 1fr 1.3fr; gap: 8px; margin-top: 10px; }
    .it-f label { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
    .it-f label span { font-size: 11px; color: #847FC0; text-transform: uppercase; letter-spacing: .5px; }
    .it-a { display: flex; gap: 8px; margin-top: 10px; }
    .it-a button, #approveAll { font-family: inherit; font-size: 13px; font-weight: 600; border: none; border-radius: 9px; padding: 9px 13px;
                  cursor: pointer; display: inline-flex; align-items: center; gap: 5px; }
    .ok { background: rgba(43,212,160,0.16); color: #2bd4a0; }
    .no { background: rgba(255,92,138,0.12); color: #FF8FC4; }
    .it-a .ok { flex: 1; justify-content: center; }

    .mn-s { margin-bottom: 14px; } .mn-s:last-child { margin-bottom: 0; }
    .mn ul { margin: 0; padding-left: 20px; color: #E6E3FF; font-size: 14px; line-height: 1.6; }
    .mn-t, .raw pre { white-space: pre-wrap; font-family: inherit; font-size: 14px; line-height: 1.55; color: #E6E3FF; margin: 0; }
    .raw { margin-bottom: 10px; background: rgba(34,29,88,0.5); border: 1px solid #2A2468; border-radius: 12px; padding: 12px 14px; }
    .raw summary { font-size: 13px; color: #CBC7F0; }
    .raw pre { margin-top: 10px; max-height: 400px; overflow-y: auto; color: #CBC7F0; font-size: 13px; }

    @media (max-width: 760px) {
        .adhoc-f { grid-template-columns: 1fr; }
        .adhoc-f input[name=attendees] { grid-column: auto; }
        .it-f { grid-template-columns: 1fr 1fr; }
        .it-f label:last-child { grid-column: 1 / 3; }
        #notes { min-height: 42vh; }
        .ev-time { width: 58px; }
        .endbar { position: sticky; bottom: calc(68px + env(safe-area-inset-bottom)); z-index: 10; margin: 14px -14px 0;
                  padding: 10px 14px; background: rgba(20,17,58,0.94); -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); }
    }
"""

JS = r"""
(function () {
  const M = window.MTG, id = M.id;
  const $ = (s) => document.querySelector(s);
  async function post(url, data, isForm) {
    const opts = { method: 'POST', credentials: 'same-origin' };
    if (isForm) opts.body = data;
    else { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(data || {}); }
    const r = await fetch(url, opts);
    if (r.redirected || !(r.headers.get('content-type') || '').includes('json'))
      throw new Error('You were signed out. Reload the page and sign in (your notes are kept on this device).');
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok || j.ok === false) throw new Error(j.error || ('Request failed (' + r.status + ')'));
    return j;
  }

  // ---------- details form ----------
  const meta = $('#metaForm');
  if (meta) meta.addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await post('/meetings/' + id + '/meta', Object.fromEntries(new FormData(meta))); location.reload(); }
    catch (err) { alert(err.message); }
  });

  // ---------- processing: wait, then reload ----------
  if (M.status === 'processing') {
    const tick = async () => {
      try {
        const r = await fetch('/meetings/' + id + '/status.json', { credentials: 'same-origin' });
        const j = await r.json();
        if (j.status !== 'processing') { location.reload(); return; }
      } catch (e) {}
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
    return;
  }

  // ---------- carried items (live + review) ----------
  document.querySelectorAll('[data-carry]').forEach((b) => b.addEventListener('click', async () => {
    const act = b.dataset.act, tid = b.dataset.carry;
    const row = document.getElementById('carry-' + tid);
    try {
      if (act !== 'skip') await post('/meetings/' + id + '/carried/' + tid, { action: act, note: b.dataset.note || '' });
      else await post('/meetings/' + id + '/carried/' + tid, { action: 'skip' });
      if (row) row.remove();
    } catch (err) { alert(err.message); }
  }));

  // ---------- review ----------
  async function decide(card, act) {
    const pid = card.dataset.pid;
    const f = {};
    card.querySelectorAll('input, select').forEach((el) => { f[el.name] = el.value; });
    card.querySelectorAll('button').forEach((b) => b.disabled = true);
    try {
      await post('/meetings/' + id + '/items/' + pid, Object.assign({ action: act }, f));
      return true;
    } catch (err) {
      alert(err.message);
      card.querySelectorAll('button').forEach((b) => b.disabled = false);
      return false;
    }
  }
  document.querySelectorAll('.it[data-pid] [data-act]').forEach((b) => b.addEventListener('click', async () => {
    if (await decide(b.closest('.it'), b.dataset.act)) location.reload();
  }));
  const all = $('#approveAll');
  if (all) all.addEventListener('click', async () => {
    all.disabled = true;
    for (const card of document.querySelectorAll('.it[data-pid]')) {
      if (!(await decide(card, 'approve'))) break;
    }
    location.reload();
  });
  const closeBtn = $('#closeBtn');
  if (closeBtn) closeBtn.addEventListener('click', async () => {
    const left = document.querySelectorAll('.it[data-pid]').length;
    if (left && !confirm(left + ' action item' + (left > 1 ? 's are' : ' is') + ' still waiting. They stay in /review on Telegram. Finish anyway?')) return;
    try { await post('/meetings/' + id + '/close'); location.reload(); } catch (err) { alert(err.message); }
  });
  const reopenBtn = $('#reopenBtn');
  if (reopenBtn) reopenBtn.addEventListener('click', async () => {
    if (!confirm('Go back to the notes? Ending the meeting again redrafts the items you haven\'t decided on yet.')) return;
    try { await post('/meetings/' + id + '/reopen'); location.reload(); } catch (err) { alert(err.message); }
  });

  if (M.status !== 'live') return;

  // ---------- notes: autosave ----------
  const notes = $('#notes'), saveSt = $('#saveState');
  const KEY = 'mtg-notes-' + id;
  let timer = null, dirty = false, saving = null;
  try {
    const local = localStorage.getItem(KEY);
    if (local && local.length > notes.value.length) { notes.value = local; dirty = true; schedule(); }
  } catch (e) {}
  function setSt(t, bad) { saveSt.textContent = t; saveSt.classList.toggle('bad', !!bad); }
  function schedule() { clearTimeout(timer); timer = setTimeout(save, 1200); }
  async function save() {
    clearTimeout(timer);
    if (!dirty) return;
    dirty = false;
    setSt('Saving…');
    const value = notes.value;
    saving = post('/meetings/' + id + '/notes', { notes: value }).then(() => {
      setSt('Saved');
      try { localStorage.removeItem(KEY); } catch (e) {}
    }).catch(() => { dirty = true; setSt('Not saved, retrying', true); timer = setTimeout(save, 5000); });
    return saving;
  }
  notes.addEventListener('input', () => {
    dirty = true; setSt('Editing…');
    try { localStorage.setItem(KEY, notes.value); } catch (e) {}
    schedule();
  });
  document.querySelectorAll('.chip-i').forEach((b) => b.addEventListener('click', () => {
    const ins = b.dataset.ins, s = notes.selectionStart, v = notes.value;
    const lineStart = v.lastIndexOf('\n', s - 1) + 1;
    const lineHasText = v.slice(lineStart, s).trim().length > 0;
    const add = (lineHasText ? '\n' : '') + ins;
    notes.value = v.slice(0, s) + add + v.slice(notes.selectionEnd);
    const pos = s + add.length;
    notes.focus(); notes.setSelectionRange(pos, pos);
    notes.dispatchEvent(new Event('input'));
  }));

  // ---------- recorder: ~60 s chunks, sent one at a time ----------
  const CHUNK_MS = 60000;
  const recBtn = $('#recBtn'), recTime = $('#recTime'), recState = $('#recState'), trBox = $('#transcript');
  let stream = null, rec = null, recording = false, chunkTimer = null, clock = null, startedAt = 0, elapsed = 0;
  let queue = [], sending = false, wakeLock = null, mime = '';
  function pickMime() {
    if (!window.MediaRecorder) return null;
    for (const t of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']) {
      try { if (MediaRecorder.isTypeSupported(t)) return t; } catch (e) {}
    }
    return '';
  }
  function ext(type) { return /mp4|aac|m4a/.test(type) ? 'm4a' : /ogg/.test(type) ? 'ogg' : 'webm'; }
  function fmt(ms) { const s = Math.floor(ms / 1000); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }
  function status() {
    const q = queue.length + (sending ? 1 : 0);
    recState.textContent = recording ? ('Recording' + (q ? ' · transcribing ' + q : '')) : (q ? 'Transcribing ' + q + '…' : 'Not recording');
  }
  function addBlock(text) {
    if (!text) return;
    if (trBox.classList.contains('empty')) { trBox.textContent = ''; trBox.classList.remove('empty'); }
    trBox.textContent += (trBox.textContent ? '\n\n' : '') + text;
    trBox.scrollTop = trBox.scrollHeight;
  }
  async function pump() {
    if (sending) return;
    sending = true;
    while (queue.length) {
      const blob = queue[0];
      status();
      let ok = false;
      for (let attempt = 0; attempt < 3 && !ok; attempt++) {
        try {
          const fd = new FormData();
          fd.append('file', blob, 'chunk.' + ext(blob.type || mime));
          const j = await post('/meetings/' + id + '/audio', fd, true);
          addBlock(j.block);
          ok = true;
        } catch (e) {
          await new Promise((r) => setTimeout(r, 3000 * (attempt + 1)));
        }
      }
      if (!ok) addBlock('[a minute of audio could not be transcribed]');
      queue.shift();
    }
    sending = false;
    status();
  }
  function startChunk() {
    const chunks = [];
    const r = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    r.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    r.onstop = () => {
      if (chunks.length) { queue.push(new Blob(chunks, { type: r.mimeType || mime })); pump(); }
      if (recording) startChunk();
    };
    r.start();
    rec = r;
    chunkTimer = setTimeout(() => { if (r.state === 'recording') r.stop(); }, CHUNK_MS);
  }
  async function startRec() {
    mime = pickMime();
    if (mime === null || !navigator.mediaDevices) { alert('This browser can\'t record audio. Try Safari or Chrome.'); return; }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (e) { alert('Microphone access was blocked. Allow it for this site and try again.'); return; }
    recording = true;
    startedAt = Date.now();
    startChunk();
    recBtn.classList.add('on'); recBtn.querySelector('span').textContent = 'Stop';
    clock = setInterval(() => { recTime.textContent = fmt(elapsed + Date.now() - startedAt); }, 500);
    try { if (navigator.wakeLock) wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
    status();
  }
  function stopRec() {
    recording = false;
    clearTimeout(chunkTimer);
    clearInterval(clock);
    elapsed += Date.now() - startedAt;
    if (rec && rec.state === 'recording') rec.stop();
    if (stream) stream.getTracks().forEach((t) => t.stop());
    recBtn.classList.remove('on'); recBtn.querySelector('span').textContent = 'Record';
    try { if (wakeLock) wakeLock.release(); } catch (e) {}
    wakeLock = null;
    status();
  }
  recBtn.addEventListener('click', () => (recording ? stopRec() : startRec()));
  document.addEventListener('visibilitychange', async () => {
    if (recording && document.visibilityState === 'visible' && navigator.wakeLock) {
      try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
    }
  });
  window.addEventListener('beforeunload', (e) => {
    if (recording || queue.length || sending || dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  // ---------- transcript paste / upload ----------
  const trForm = $('#trForm');
  if (trForm) trForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = trForm.querySelector('button');
    btn.disabled = true; btn.textContent = 'Adding…';
    try {
      const j = await post('/meetings/' + id + '/transcript', new FormData(trForm), true);
      location.reload();
    } catch (err) { alert(err.message); btn.disabled = false; btn.textContent = 'Add'; }
  });

  // ---------- end / delete ----------
  const endBtn = $('#endBtn');
  endBtn.addEventListener('click', async () => {
    if (recording) stopRec();
    endBtn.disabled = true;
    endBtn.textContent = 'Finishing…';
    dirty = true;
    await save();
    try { await saving; } catch (e) {}
    while (queue.length || sending) { status(); await new Promise((r) => setTimeout(r, 800)); }
    try { await post('/meetings/' + id + '/end'); location.reload(); }
    catch (err) { alert(err.message); endBtn.disabled = false; endBtn.innerHTML = '<i class="ti ti-player-stop"></i> End meeting &amp; draft tasks'; }
  });
  $('#delBtn').addEventListener('click', async () => {
    if (!confirm('Delete this meeting and its notes? This can\'t be undone.')) return;
    stopRec && recording && stopRec();
    dirty = false;
    try { await post('/meetings/' + id + '/delete'); try { localStorage.removeItem(KEY); } catch (e) {} location.href = '/meetings'; }
    catch (err) { alert(err.message); }
  });
})();
"""
