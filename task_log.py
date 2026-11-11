"""
task_log.py — the progress log on the dashboard.

  latest_line(u)          the grey "latest update" line under a task in the register
  page(task, updates)     the task's own page: history + the add-update form
  CSS                     styles for both (dropped into the dashboard's <style>)

Routes live in dashboard.py: GET /task/{id}, POST /task/{id}/update,
POST /task/{id}/update/{update_id}/delete.
"""
import html

import register as reg

esc = html.escape


def latest_line(u: dict, task_id: int) -> str:
    """One-line preview of the newest update, linking to the task page. '' when there are none."""
    if not u:
        return ""
    text = u.get("body") or u.get("action_detail") or ""
    count = u.get("count") or 1
    badge = f"<span class='ul-n'>{count} update{'s' if count != 1 else ''}</span>"
    return (f"<a class='ulast' href='/task/{task_id}'><i class='ti ti-message-2'></i>"
            f"<span class='ul-t'><span class='ul-w'>{esc(reg.when(u.get('created_at')))}:</span> "
            f"{esc(text)}</span>{badge}</a>")


def _entry(u: dict, first: bool, task_id: int) -> str:
    src = reg.UPDATE_SOURCES.get(u.get("source") or "", (u.get("source") or "").title())
    tag = f"<div class='le-tag'>{esc(u['action_detail'])}</div>" if u.get("action_detail") else ""
    body = f"<div class='le-b'>{esc(u['body'])}</div>" if u.get("body") else ""
    remove = (f"<form method='post' action='/task/{task_id}/update/{u['id']}/delete' class='le-del'>"
              f"<button title='Remove this note' aria-label='Remove this note'><i class='ti ti-x'></i></button></form>")
    return f"""
        <div class='le{" first" if first else ""}'>
            <span class='le-dot'></span>
            <div class='le-h'>{esc(reg.when(u.get("created_at")))} · {esc(src)}{remove}</div>
            {body}{tag}
        </div>"""


def page(t: dict, updates: list, back: str = "/tasks#register", error: str = "", saved: bool = False) -> str:
    is_open = t["status"] == "open"
    waiting = t.get("ball_in_court") == "waiting"
    meta = [esc(t.get("project") or "No project")]
    if waiting:
        meta.append("Waiting on " + esc(t.get("waiting_on") or "others"))
    if t.get("blocking"):
        meta.append("<span style='color:#c792ea;'>blocking</span>")
    if t.get("source"):
        meta.append(f"added {esc(reg.short_date(t['created_at'].date()) if t.get('created_at') else '')} from {esc(t['source'].replace('_', ' '))}")
    due = f"due {reg.short_date(t['due_date'])}" if t.get("due_date") else "no due date"
    tier = t.get("tier")
    if tier == 1 and t.get("days_overdue"):
        due = f"<span style='color:#FF8FC4;'>{t['days_overdue']}d overdue</span>"
    status = "" if is_open else "<span class='tp-closed'><i class='ti ti-check'></i> Closed</span>"
    email = (f" <a href='{esc(t['source_url'])}' target='_blank' class='tp-mail' title='Open the email'>"
             f"<i class='ti ti-mail'></i></a>") if t.get("source_url") else ""

    log = "".join(_entry(u, i == 0, t["id"]) for i, u in enumerate(updates))
    if not log:
        log = "<div class='le-empty'>No updates yet. Log what's happened so far below.</div>"

    banner = ""
    if error:
        banner = f"<div class='tp-err'>{esc(error)}</div>"
    elif saved:
        banner = "<div class='tp-ok'>Update saved.</div>"

    if is_open:
        back_chip = ("<input type='radio' name='action' value='mine' id='qa-mine' class='qa-r'>"
                     "<label for='qa-mine' class='qa-chip'><i class='ti ti-arrow-back-up'></i> Back with me</label>"
                     if waiting else
                     "<input type='radio' name='action' value='waiting' id='qa-waiting' class='qa-r'>"
                     "<label for='qa-waiting' class='qa-chip'><i class='ti ti-hourglass'></i> Waiting on…</label>")
        form = f"""
        <form method='post' action='/task/{t["id"]}/update' class='upd'>
            <input type='hidden' name='back' value='{esc(back)}'>
            <textarea name='body' rows='3' placeholder='What happened? e.g. Called Miguel, quote coming Thursday'></textarea>
            <div class='qa'>
                <input type='radio' name='action' value='' id='qa-none' class='qa-r' checked>
                <label for='qa-none' class='qa-chip'><i class='ti ti-notes'></i> Just a note</label>
                {back_chip}
                <input type='radio' name='action' value='push' id='qa-push' class='qa-r'>
                <label for='qa-push' class='qa-chip'><i class='ti ti-calendar'></i> Push date</label>
                <input type='radio' name='action' value='done' id='qa-done' class='qa-r'>
                <label for='qa-done' class='qa-chip'><i class='ti ti-check'></i> Done</label>
                <div class='qa-x x-waiting'><input name='waiting_on' placeholder='Waiting on who? e.g. Marc'
                     value='{esc(t.get("waiting_on") or "")}'></div>
                <div class='qa-x x-push'><label>New due date</label><input type='date' name='due_date'></div>
                <button type='submit' class='qa-save'>Save update</button>
            </div>
        </form>"""
    else:
        form = f"""
        <form method='post' action='/task/{t["id"]}/update' class='upd'>
            <input type='hidden' name='back' value='{esc(back)}'>
            <textarea name='body' rows='2' placeholder='Add a note to this closed task'></textarea>
            <div class='qa'><button type='submit' class='qa-save'>Save note</button></div>
        </form>"""

    return f"""
    <div class='tp'>
        <a class='tp-back' href='{esc(back)}'><i class='ti ti-arrow-left'></i> Register</a>
        {banner}
        <div class='panel tp-card'>
            <div class='tp-top'>
                <h2>{esc(t["description"] or "")}{email}</h2>
                <div class='tp-due'>{status or due}</div>
            </div>
            <div class='tp-meta'>#{t["id"]} · {" · ".join(meta)}</div>
            <div class='tp-log'>{log}</div>
            {form}
        </div>
    </div>"""


CSS = """
        .ulast { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-size: 12px; color: #CBC7F0;
                 text-decoration: none; min-width: 0; }
        .ulast:hover .ul-t { color: #FFFFFF; }
        .ulast .ti { color: #FF8FC4; font-size: 14px; flex-shrink: 0; }
        .ul-t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
        .ul-w { color: #847FC0; }
        .ul-n { margin-left: auto; flex-shrink: 0; background: #352F78; border-radius: 10px; padding: 1px 8px;
                font-size: 11px; color: #CBC7F0; }
        a.task-link { color: inherit; text-decoration: none; }
        a.task-link:hover { color: #FFFFFF; text-decoration: underline; text-decoration-color: #B45CF0;
                            text-underline-offset: 3px; }
        .btn-log { background: rgba(180,92,240,0.16); color: #E0B0FF; text-decoration: none; }
        .t-log { background: rgba(180,92,240,0.16); color: #E0B0FF; text-decoration: none; }

        .tp { max-width: 760px; margin: 0 auto; }
        .tp-back { display: inline-flex; align-items: center; gap: 6px; color: #ABA6DD; text-decoration: none;
                   font-size: 13px; margin-bottom: 14px; }
        .tp-back:hover { color: #FFFFFF; }
        .tp-card { padding: 20px 22px; border-color: #B45CF0; }
        .tp-top { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; }
        .tp-top h2 { margin: 0; font-size: 19px; font-weight: 600; color: #FFFFFF; line-height: 1.35; }
        .tp-mail { color: #FF8FC4; font-size: 17px; }
        .tp-due { font-size: 13px; color: #ABA6DD; white-space: nowrap; padding-top: 3px; }
        .tp-closed { color: #2bd4a0; }
        .tp-meta { font-size: 13px; color: #9993D0; margin: 4px 0 20px; }
        .tp-ok, .tp-err { border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; }
        .tp-ok { background: rgba(43,212,160,0.12); color: #2bd4a0; }
        .tp-err { background: rgba(255,92,138,0.14); color: #FF8FC4; }

        .tp-log { border-left: 2px solid #352F78; margin: 0 0 20px 6px; padding-left: 18px; }
        .le { position: relative; margin-bottom: 16px; }
        .le:last-child { margin-bottom: 0; }
        .le-dot { position: absolute; left: -25px; top: 5px; width: 12px; height: 12px; border-radius: 50%;
                  background: #4A4396; border: 2px solid #221D58; }
        .le.first .le-dot { background: linear-gradient(135deg, #FF4F8B, #9B4DEB); }
        .le-h { font-size: 12px; color: #847FC0; display: flex; align-items: center; gap: 6px; }
        .le-b { font-size: 14px; color: #E6E3FF; margin-top: 3px; line-height: 1.5; white-space: pre-wrap; }
        .le.first .le-b { color: #FFFFFF; }
        .le-tag { display: inline-block; margin-top: 6px; font-size: 12px; color: #E0B0FF;
                  background: rgba(180,92,240,0.18); border-radius: 8px; padding: 2px 9px; }
        .le-del { margin: 0 0 0 auto; opacity: 0; transition: opacity .15s; }
        .le:hover .le-del { opacity: 1; }
        .le-del button { background: none; border: none; color: #847FC0; cursor: pointer; font-size: 14px; padding: 0 2px; }
        .le-del button:hover { color: #FF8FC4; }
        .le-empty { font-size: 13px; color: #847FC0; padding: 2px 0; }

        .upd { background: #1B1747; border: 1px solid #352F78; border-radius: 12px; padding: 12px; }
        .upd textarea { width: 100%; background: transparent; border: none; color: #F3F1FF; font-family: inherit;
                        font-size: 14px; resize: vertical; outline: none; padding: 4px 2px 10px; }
        .upd textarea::placeholder { color: #847FC0; }
        .qa { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .qa-r { position: absolute; opacity: 0; pointer-events: none; }
        .qa-chip { font-size: 12px; border: 1px solid #4A4396; border-radius: 8px; padding: 6px 10px; color: #CBC7F0;
                   cursor: pointer; display: inline-flex; align-items: center; gap: 5px; user-select: none; }
        .qa-chip:hover { border-color: #B45CF0; }
        .qa-r:checked + .qa-chip { background: rgba(180,92,240,0.22); border-color: #B45CF0; color: #FFFFFF; }
        .qa-r:focus-visible + .qa-chip { box-shadow: 0 0 0 2px #FF8FC4; }
        .qa-x { display: none; width: 100%; align-items: center; gap: 8px; order: 5; }
        .qa-x label { font-size: 12px; color: #847FC0; }
        .qa-x input { font-family: inherit; background: #14113A; border: 1px solid #352F78; border-radius: 8px;
                      color: #F3F1FF; padding: 8px 10px; font-size: 14px; color-scheme: dark; }
        .qa-x input:focus { outline: none; border-color: #B45CF0; }
        .x-waiting input { flex: 1; max-width: 320px; }
        #qa-waiting:checked ~ .x-waiting, #qa-push:checked ~ .x-push { display: flex; }
        .qa-save { margin-left: auto; font-family: inherit; font-size: 13px; font-weight: 500; color: #FFFFFF;
                   border: none; border-radius: 8px; padding: 8px 16px; cursor: pointer;
                   background: linear-gradient(90deg, #FF4F8B, #9B4DEB); order: 4; }
        .qa-save:hover { opacity: .9; }
        @media (max-width: 760px) {
            .tp-card { padding: 16px; }
            .tp-top { flex-direction: column; gap: 4px; }
            .tp-top h2 { font-size: 17px; }
            .qa-save { width: 100%; margin-left: 0; padding: 11px; order: 6; }
            .qa-chip { padding: 8px 11px; }
            .le-del { opacity: 1; }
        }
"""
