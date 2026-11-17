"""
contracts_page.py — HTML for /contracts (new request + history) and
/contracts/{id} (the write-up). Routes are in dashboard.py; logic in contracts.py.
Reuses the meetings page CSS so it matches the command-center look.
"""
import html
import re

import markdown as _md

import register as reg
from contracts import MODES

esc = html.escape

STATUS = {"running": ("Working", "st-proc"), "done": ("Done", "st-closed"), "error": ("Error", "st-live")}

CSS = """
    .ct-f { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
    .ct-f .full { grid-column: 1 / -1; }
    .ct-f input, .ct-f select, .ct-f textarea {
        font-family: inherit; background: #14113A; border: 1px solid #352F78; border-radius: 8px; color: #F3F1FF;
        padding: 9px 10px; font-size: 14px; color-scheme: dark; min-width: 0; }
    .ct-f textarea { min-height: 84px; resize: vertical; }
    .ct-f button { font-family: inherit; border: none; border-radius: 8px; padding: 10px 14px; cursor: pointer;
        color: #FFFFFF; font-weight: 500; background: linear-gradient(90deg, #FF4F8B, #9B4DEB); }
    .ct-links { display: flex; gap: 8px; flex-wrap: wrap; margin: 4px 0 14px; }
    .ct-links a { font-size: 13px; color: #E0B0FF; text-decoration: none; border: 1px solid #352F78;
        border-radius: 8px; padding: 6px 10px; display: inline-flex; gap: 6px; align-items: center; }
    .ct-links a:hover { border-color: #B45CF0; }
    .ct-body { color: #E6E3FF; font-size: 14.5px; line-height: 1.6; }
    .ct-body h1, .ct-body h2, .ct-body h3 { color: #FFFFFF; margin: 22px 0 8px; }
    .ct-body h2 { font-size: 18px; } .ct-body h3 { font-size: 15.5px; }
    .ct-body table { border-collapse: collapse; width: 100%; font-size: 13px; margin: 10px 0 16px; display: block; overflow-x: auto; }
    .ct-body th, .ct-body td { border: 1px solid #352F78; padding: 7px 9px; vertical-align: top; text-align: left; }
    .ct-body th { background: #1B1747; color: #FFFFFF; }
    .ct-body code, .ct-body pre { background: #14113A; border-radius: 6px; padding: 2px 5px; white-space: pre-wrap; }
    .ct-body pre { padding: 10px 12px; }
    .ct-body blockquote { border-left: 3px solid #B45CF0; margin: 10px 0; padding: 2px 12px; color: #CBC7F0; }
    .ct-wait { display: flex; align-items: center; gap: 10px; color: #CBC7F0; padding: 18px 4px; }
    @media (max-width: 700px) { .ct-f { grid-template-columns: 1fr; } }
"""


def _project_options(projects: list) -> str:
    opts = ["<option value=''>No project</option>"]
    opts += [f"<option>{esc(p['name'])}</option>" for p in projects]
    return "".join(opts)


def _mode_options() -> str:
    return "".join(f"<option value='{k}'>{esc(v)}</option>" for k, v in MODES.items())


def list_page(rows: list, projects: list, err: str = "") -> str:
    items = ""
    for r in rows:
        label, cls = STATUS.get(r["status"], (r["status"], ""))
        title = r["filename"] or (r["request"] or "")[:90] or MODES.get(r["mode"], r["mode"])
        bits = [MODES.get(r["mode"], r["mode"]), r.get("project"), reg.when(r["created_at"]) if r["created_at"] else ""]
        items += f"""
        <a class='mrow' href='/contracts/{r["id"]}'>
            <span class='st {cls}'>{esc(label)}</span>
            <span class='mrow-b'><span class='mrow-t'>{esc(title)}</span>
                <span class='mrow-m'>{esc(" · ".join(b for b in bits if b))}</span></span>
            <i class='ti ti-chevron-right'></i>
        </a>"""
    if not items:
        items = "<div class='empty-sm'>Nothing yet. Upload a contract above or ask a question.</div>"
    banner = f"<div class='tp-err'>{esc(err)}</div>" if err else ""
    return f"""
    <div class='mt'>
        {banner}
        <div class='mt-h'><h2>Contracts</h2></div>
        <div class='panel'>
            <div class='sec'><i class='ti ti-file-certificate'></i> Owner–GC and Owner–Design Consultant</div>
            <form method='post' action='/contracts/new' enctype='multipart/form-data' class='ct-f'>
                <select name='mode'>{_mode_options()}</select>
                <select name='project'>{_project_options(projects)}</select>
                <select name='perspective'>
                    <option>Owner</option><option>Contractor</option><option>Design Consultant</option>
                </select>
                <input class='full' type='file' name='file' accept='.pdf,.docx,.txt,.md'>
                <textarea class='full' name='request' placeholder='Optional for a review. For Draft or Question, say what you need, e.g. "Owner rider to A201: 10% retainage to SC, $1,500/day LDs, 558 opt-out" or "Can the GC claim delay damages under 8.3.1?"'></textarea>
                <button class='full' type='submit'><i class='ti ti-sparkles'></i> Run</button>
            </form>
            <div class='hint'>A full review takes a few minutes. The contract and a Word memo are filed to OneDrive under the project's Contracts folder. Not legal advice; have counsel sign off before execution.</div>
        </div>
        <div class='panel'>
            <div class='sec'>History</div>
            <div class='mrows'>{items}</div>
        </div>
    </div>"""


def view_page(r: dict) -> str:
    label, cls = STATUS.get(r["status"], (r["status"], ""))
    title = r["filename"] or MODES.get(r["mode"], r["mode"])
    meta = " · ".join(b for b in (MODES.get(r["mode"]), r.get("project"), f"{r['perspective']} side",
                                  reg.when(r["created_at"]) if r["created_at"] else "") if b)
    links = ""
    if r.get("memo_url"):
        links += f"<a href='{esc(r['memo_url'])}' target='_blank'><i class='ti ti-file-type-docx'></i> Word memo</a>"
    if r.get("file_url"):
        links += f"<a href='{esc(r['file_url'])}' target='_blank'><i class='ti ti-file'></i> Contract in OneDrive</a>"
    request = f"<div class='hint'>Request: {esc(r['request'])}</div>" if r.get("request") else ""

    if r["status"] == "running":
        body = f"""<div class='ct-wait'><i class='ti ti-loader-2'></i> Working on it. This page refreshes when it's done.</div>
        <script>
        (function tick() {{
          setTimeout(async () => {{
            try {{
              const j = await (await fetch('/contracts/{r["id"]}/status.json', {{credentials: 'same-origin'}})).json();
              if (j.status !== 'running') {{ location.reload(); return; }}
            }} catch (e) {{}}
            tick();
          }}, 4000);
        }})();
        </script>"""
    elif r["status"] == "error":
        body = f"<div class='tp-err'>{esc(r.get('error') or 'Something went wrong.')}</div>"
    else:
        body = "<div class='ct-body'>" + _md.markdown(
            re.sub(r"(?m)^(\s*)&gt;", r"\1>", html.escape(r.get("result") or "", quote=False)),
            extensions=["tables", "sane_lists", "fenced_code"]) + "</div>"

    return f"""
    <div class='mt'>
        <a class='tp-back' href='/contracts'><i class='ti ti-arrow-left'></i> Contracts</a>
        <div class='mh'><h2>{esc(title)}</h2><span class='st {cls}'>{esc(label)}</span></div>
        <div class='hint'>{esc(meta)}</div>
        {request}
        <div class='ct-links'>{links}</div>
        <div class='panel'>{body}</div>
    </div>"""
