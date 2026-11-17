"""
contracts.py — Rowan's contracts agent (Owner-side construction contracts).

Covers Owner-GC and Owner-Design Consultant agreements:
  review   -> risk review + clause-by-clause Owner-side redlines + cover note
  compare  -> deviation table against MCM's Owner positions
  draft    -> riders / supplementary conditions / short-form agreements
  question -> straight answers on clauses and Florida construction law

The playbook (contracts_playbook.md) is the same text as James's
"construction-contracts" Claude skill, so both give the same advice.
Keep them in step when either changes.

Every run is stored in the contract_reviews table. When a contract file is
involved, the file and a Word memo of the result are filed to OneDrive under
<Project>/Contracts.

Used by: dashboard.py (/contracts), contracts_tg.py (Telegram /contract).
"""
import base64
import io
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from db import get_connection

TZ = ZoneInfo("America/New_York")
MODEL = os.getenv("CONTRACTS_MODEL", "").strip() or "claude-sonnet-4-6"
MAX_TOKENS = int(os.getenv("CONTRACTS_MAX_TOKENS", "12000"))
CLAUDE_MAX_PDF = 30 * 1024 * 1024
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://pm-agent-production-bd91.up.railway.app").rstrip("/")

MODES = {
    "review": "Review & redline",
    "compare": "Compare to MCM standard",
    "draft": "Draft",
    "question": "Question",
}

PLAYBOOK = (Path(__file__).parent / "contracts_playbook.md").read_text(encoding="utf-8")

ROWAN_NOTES = """

## Running inside Rowan
- You are answering through Rowan (James's PM agent): a web page and Telegram.
- ALWAYS start your answer with a section headed exactly `## Bottom line`:
  at most 8 short lines, plain text or simple bullets, no tables. This is what
  goes to Telegram, so it must stand on its own (verdict + the top 3-5 issues).
- After that, the full deliverable in Markdown (tables allowed).
- Today is {today}. Project: {project}.
"""

MODE_INSTRUCTIONS = {
    "review": "Job: REVIEW & REDLINE the attached contract (job 1 in the playbook). {extra}",
    "compare": "Job: COMPARE the attached contract to the MCM Owner Positions (job 3 in the playbook). {extra}",
    "draft": ("Job: DRAFT (job 2 in the playbook). Draft what James asks for below. If a contract is "
              "attached, draft against it (e.g. a rider or supplementary conditions keyed to its articles). "
              "Request: {extra}"),
    "question": ("Job: ANSWER (job 4 in the playbook). Keep the full answer short. If a contract is "
                 "attached, answer from it with clause references. Question: {extra}"),
}

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_schema_ready = False


class ContractError(Exception):
    """A problem worth telling James about in plain words."""


# ============================================================
# STORAGE
# ============================================================

def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contract_reviews (
                id           SERIAL PRIMARY KEY,
                mode         TEXT NOT NULL,                 -- review | compare | draft | question
                project      TEXT,
                filename     TEXT,
                request      TEXT,
                perspective  TEXT NOT NULL DEFAULT 'Owner',
                status       TEXT NOT NULL DEFAULT 'running', -- running | done | error
                result       TEXT,
                error        TEXT,
                file_url     TEXT,     -- contract filed to OneDrive
                memo_url     TEXT,     -- Word memo of the result
                source       TEXT,     -- dashboard | telegram
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                finished_at  TIMESTAMPTZ
            )""")
        conn.commit()
        _schema_ready = True
    finally:
        cur.close()
        conn.close()


def create(mode: str, project: str = None, filename: str = None, request: str = "",
           perspective: str = "Owner", source: str = "dashboard") -> int:
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO contract_reviews (mode, project, filename, request, perspective, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (mode, project or None, filename or None, request or "", perspective or "Owner", source))
        rid = cur.fetchone()[0]
        conn.commit()
        return rid
    finally:
        cur.close()
        conn.close()


def _update(rid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = %s" for k in fields)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"UPDATE contract_reviews SET {cols} WHERE id = %s", (*fields.values(), rid))
        conn.commit()
    finally:
        cur.close()
        conn.close()


_COLS = ("id", "mode", "project", "filename", "request", "perspective", "status", "result",
         "error", "file_url", "memo_url", "source", "created_at", "finished_at")


def get(rid: int):
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(_COLS)} FROM contract_reviews WHERE id = %s", (rid,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    return dict(zip(_COLS, row)) if row else None


def recent(limit: int = 30) -> list:
    ensure_schema()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(_COLS)} FROM contract_reviews ORDER BY created_at DESC LIMIT %s",
                    (limit,))
        return [dict(zip(_COLS, r)) for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def page_url(rid: int) -> str:
    return f"{DASHBOARD_URL}/contracts/{rid}"


# ============================================================
# READING THE CONTRACT
# ============================================================

def _docx_text(content: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(content))
    out = []
    for p in doc.paragraphs:
        if p.text.strip():
            out.append(p.text)
    for t in doc.tables:
        for row in t.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                out.append(" | ".join(cells))
    return "\n".join(out)


def content_blocks(content: bytes, mime: str, filename: str) -> list:
    """Turn an uploaded file into Claude content blocks."""
    if not content:
        return []
    name = (filename or "").lower()
    mime = (mime or "").lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        if len(content) > CLAUDE_MAX_PDF:
            raise ContractError("That PDF is over 30 MB. Split it (agreement, general conditions, exhibits) "
                                "and send the parts.")
        return [{"type": "document", "title": filename or "contract.pdf",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(content).decode()}}]
    if name.endswith(".docx") or "wordprocessingml" in mime:
        text = _docx_text(content)
    elif name.endswith((".txt", ".md")) or mime.startswith("text/"):
        text = content.decode("utf-8", errors="replace")
    elif name.endswith(".doc"):
        raise ContractError("Old .doc files can't be read. Save it as .docx or PDF and send it again.")
    else:
        raise ContractError("I can read PDF, Word (.docx) or text files. Export it to one of those.")
    if not text.strip():
        raise ContractError("That file has no readable text (it may be scanned). Send it as a PDF.")
    return [{"type": "document", "title": filename or "contract",
             "source": {"type": "text", "media_type": "text/plain", "data": text}}]


# ============================================================
# THE AGENT
# ============================================================

def ask(mode: str, request: str = "", blocks: list = None, project: str = None,
        perspective: str = "Owner") -> str:
    """One call to Claude with the playbook. Returns Markdown."""
    if mode not in MODES:
        mode = "question"
    if mode in ("review", "compare") and not blocks:
        raise ContractError("Send the contract file for a review or comparison.")
    if mode in ("draft", "question") and not (request or "").strip():
        raise ContractError("Tell me what you want drafted or what the question is.")

    system = PLAYBOOK + ROWAN_NOTES.format(today=datetime.now(TZ).strftime("%b %d, %Y"),
                                           project=project or "not given")
    if perspective and perspective.lower() != "owner":
        system += f"\nFor this run, James is advising the {perspective}, not the Owner. Switch sides accordingly."
    extra = (request or "").strip()
    if mode in ("review", "compare"):
        extra = f"James's note: {extra}" if extra else ""
    text = MODE_INSTRUCTIONS[mode].format(extra=extra)

    try:
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": (blocks or []) + [{"type": "text", "text": text}]}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as e:
        raise ContractError(f"Claude couldn't finish that one: {str(e)[:200]}")
    out = "".join(b.text for b in msg.content if b.type == "text").strip()
    if msg.stop_reason == "max_tokens":
        out += "\n\n_(Cut off at the length limit. Ask for the rest by section.)_"
    return out


def bottom_line(result: str) -> str:
    """The '## Bottom line' section, for Telegram."""
    m = re.search(r"##\s*Bottom line\s*\n(.*?)(?=\n#{1,3}\s|\Z)", result or "", re.S | re.I)
    text = (m.group(1) if m else (result or "")[:1200]).strip()
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


# ============================================================
# WORD MEMO
# ============================================================

def _add_runs(par, text: str) -> None:
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if part:
            par.add_run(part.replace("`", "")).bold = (i % 2 == 1)


def memo_docx(title: str, subtitle: str, markdown: str) -> bytes:
    """Simple Markdown -> Word: headings, bullets, numbered lists, tables, bold."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    doc.add_heading(title, level=0)
    if subtitle:
        _add_runs(doc.add_paragraph(), subtitle)

    lines = (markdown or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                width = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=width)
                table.style = "Table Grid"
                for r, cells in enumerate(rows):
                    for c in range(width):
                        cell = table.cell(r, c)
                        cell.text = ""
                        _add_runs(cell.paragraphs[0], cells[c] if c < len(cells) else "")
                        if r == 0:
                            for run in cell.paragraphs[0].runs:
                                run.bold = True
            continue
        if not stripped or stripped in ("---", "***"):
            i += 1
            continue
        h = re.match(r"^(#{1,4})\s+(.*)", stripped)
        if h:
            doc.add_heading(h.group(2).replace("**", ""), level=min(len(h.group(1)), 3))
        elif re.match(r"^[-*]\s+", stripped):
            _add_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*]\s+", "", stripped))
        elif re.match(r"^\d+[.)]\s+", stripped):
            _add_runs(doc.add_paragraph(style="List Number"), re.sub(r"^\d+[.)]\s+", "", stripped))
        elif stripped.startswith(">"):
            _add_runs(doc.add_paragraph(style="Intense Quote"), stripped.lstrip("> "))
        else:
            _add_runs(doc.add_paragraph(), stripped)
        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ============================================================
# ONE RUN, START TO FINISH
# ============================================================

def _file_to_onedrive(content: bytes, project, filename: str, mime: str):
    from onedrive import safe_name, upload_file
    folder = f"{safe_name(project) if project else '_Unsorted'}/Contracts"
    item = upload_file(content, folder, filename, mime)
    try:
        from rowan_agent import record_project_file
        record_project_file(project, "document", item["name"], item["path"], item["web_url"],
                            description=f"Contract file: {filename}")
    except Exception as e:
        print(f"[contracts] could not record file: {e}")
    return item


def run(rid: int, content: bytes = None, mime: str = "", filename: str = None) -> dict:
    """Process a stored request. Safe to call in a background task."""
    r = get(rid)
    if not r:
        return {"ok": False, "error": "Not found"}
    try:
        blocks = content_blocks(content, mime, filename) if content else []
        result = ask(r["mode"], r["request"], blocks, r["project"], r["perspective"])
    except ContractError as e:
        _update(rid, status="error", error=str(e), finished_at=datetime.now(TZ))
        return {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"[contracts] run {rid} failed: {e}")
        _update(rid, status="error", error=f"Something went wrong: {str(e)[:200]}",
                finished_at=datetime.now(TZ))
        return {"ok": False, "error": str(e)[:200]}

    fields = {"status": "done", "result": result, "finished_at": datetime.now(TZ)}
    stamp = datetime.now(TZ).strftime("%Y-%m-%d")
    # File the contract and a Word memo (not for plain questions with no file).
    if content or r["mode"] in ("review", "compare", "draft"):
        try:
            from onedrive import safe_name
            if content and filename:
                item = _file_to_onedrive(content, r["project"], f"{stamp} {safe_name(filename, 90)}",
                                         mime or "application/octet-stream")
                fields["file_url"] = item["web_url"]
            label = MODES[r["mode"]]
            base = safe_name(filename.rsplit(".", 1)[0] if filename else (r["request"] or "contracts"), 60)
            memo = memo_docx(f"{label}: {filename or r['project'] or 'Contracts'}",
                             f"Miami Coastline Management · {r['project'] or 'No project'} · "
                             f"{datetime.now(TZ).strftime('%B %d, %Y')} · Perspective: {r['perspective']}",
                             result)
            item = _file_to_onedrive(
                memo, r["project"], f"{stamp} {label} - {base}.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            fields["memo_url"] = item["web_url"]
        except Exception as e:
            print(f"[contracts] OneDrive filing failed for {rid}: {e}")
    _update(rid, **fields)
    return {"ok": True, "result": result, "memo_url": fields.get("memo_url"),
            "file_url": fields.get("file_url")}
