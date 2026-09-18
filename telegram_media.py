"""
telegram_media.py — what Rowan does with voice notes, photos and documents
James sends on Telegram.

  voice / audio  -> Whisper transcript (OpenAI), handed back to the bot
  photo          -> Claude looks at it, picks the project, files it to OneDrive
  PDF            -> Claude reviews it as James's owner's rep, files it to OneDrive
  anything else  -> filed to OneDrive, not read

Nothing here writes to the task list or calendar; follow-ups go through the
agent, which asks James to confirm.
"""
import base64
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
import requests

from onedrive import safe_name, upload_file
from rowan_agent import MODEL, project_names, record_project_file

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1").strip()

TZ = ZoneInfo("America/New_York")
TELEGRAM_MAX_DOWNLOAD = 20 * 1024 * 1024        # Bot API getFile limit
CLAUDE_MAX_IMAGE = int(3.7 * 1024 * 1024)       # stays under 5 MB once base64'd
VISION_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


class MediaError(Exception):
    """A problem worth telling James about in plain words."""


# ============================================================
# TELEGRAM DOWNLOAD
# ============================================================

def download(file_id: str, file_size: int = 0) -> tuple:
    """Returns (bytes, telegram file_path)."""
    if file_size and file_size > TELEGRAM_MAX_DOWNLOAD:
        raise MediaError("That file is over Telegram's 20 MB limit for bots. "
                         "Save it to OneDrive yourself and tell me where.")
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    resp = requests.get(f"{api}/getFile", params={"file_id": file_id}, timeout=20)
    data = resp.json() if resp.content else {}
    if not data.get("ok"):
        raise MediaError(f"Telegram wouldn't hand over the file: {data.get('description', resp.status_code)}")
    path = data["result"]["file_path"]
    file_resp = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=120)
    if file_resp.status_code >= 400:
        raise MediaError(f"Download from Telegram failed [{file_resp.status_code}].")
    return file_resp.content, path


def _stamp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H%M")


# ============================================================
# VOICE
# ============================================================

def _vocabulary_hint() -> str:
    """Project and people names help Whisper spell them right."""
    words = []
    try:
        words += project_names()
        from db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM people ORDER BY name LIMIT 60")
        words += [r[0] for r in cur.fetchall() if r[0]]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[telegram_media] vocabulary hint unavailable: {e}")
    hint = "Construction project notes for Miami Coastline Management. " + ", ".join(words)
    return hint[:800]


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    if not OPENAI_API_KEY:
        raise MediaError("Voice notes need OPENAI_API_KEY set on the pm-agent service in Railway.")
    resp = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        files={"file": (filename, audio)},
        data={"model": TRANSCRIBE_MODEL, "prompt": _vocabulary_hint(), "language": "en"},
        timeout=180,
    )
    if resp.status_code >= 400:
        raise MediaError(f"Transcription failed [{resp.status_code}]: {resp.text[:200]}")
    text = (resp.json().get("text") or "").strip()
    if not text:
        raise MediaError("I couldn't make out any words in that voice note.")
    return text


# ============================================================
# CLAUDE: structured look at a photo or document
# ============================================================

FILE_TOOL = {
    "name": "file_it",
    "description": "Record where this belongs and what to tell James.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": ["string", "null"],
                        "description": "Exact project name from the list, or null if you can't tell."},
            "title": {"type": "string",
                      "description": "3-8 word description for the file name, e.g. 'pool shell pour north side'."},
            "description": {"type": "string",
                            "description": "1-3 factual sentences on what it shows/contains, for search later."},
            "reply": {"type": "string",
                      "description": "What to send James on Telegram. Plain text, no markdown."},
        },
        "required": ["project", "title", "description", "reply"],
    },
}

PHOTO_PROMPT = """You are Rowan, the AI project manager for James Galvin, owner's representative at Miami Coastline Management (South Florida and the Keys). James just sent a jobsite photo from his phone.

Projects: {projects}
James's caption: {caption}

Work out which project it belongs to (the caption is the strongest signal; don't guess without one unless the photo makes it obvious). Describe what it shows the way a construction PM would: trade, work in place, stage, anything readable (delivery tickets, inspection cards, labels, dimensions).

Your reply to James: at most 4 short lines. Say what you see, and flag anything an owner's rep should care about (quality or safety concerns, a failed or missing inspection, material that doesn't look like spec, weather exposure). If there's an obvious follow-up, ask whether he wants a task for it. Don't pad."""

DOC_PROMPT = """You are Rowan, the AI project manager for James Galvin, owner's representative at Miami Coastline Management. James forwarded a document and wants it reviewed from the owner's side.

Projects: {projects}
File name: {filename}
James's note: {caption}

Identify what it is (change order, pay application, proposal/bid, invoice, contract or amendment, submittal, RFI, schedule, report, other) and which project it belongs to.

Then write the review for James. Plain text, no markdown, phone-readable, under ~250 words:
- First line: document type, who it's from, the date, and the headline number if there is one.
- 2-4 lines summarizing what it asks for or says.
- "Flags:" then the issues an owner's rep should push on. Be specific and cite the page or line. Depending on type: markup and fee math, overhead or profit stacked on subs, contingency or allowance use, scope gaps and exclusions, duplicated or already-paid scope, time extensions buried in pricing, retainage math, stored materials without backup, missing lien waivers or signatures, math errors, schedule impacts, unfavorable terms (indemnity, waiver of consequential damages, pay-when-paid, notice deadlines).
- If you checked the math, say whether it ties.
- If there's nothing to flag, say so plainly. Never invent problems.
If James's note asks a specific question, answer that first."""


def _ask_claude(content_blocks: list, prompt: str) -> dict:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        tools=[FILE_TOOL],
        tool_choice={"type": "tool", "name": "file_it"},
        messages=[{"role": "user", "content": content_blocks + [{"type": "text", "text": prompt}]}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise MediaError("I couldn't read that one.")


def _match_project(name, projects):
    if not name:
        return None
    for p in projects:
        if p.lower() == str(name).lower():
            return p
    for p in projects:
        if str(name).lower() in p.lower() or p.lower() in str(name).lower():
            return p
    return None


def _file(content: bytes, project, subfolder: str, filename: str, mime: str,
          kind: str, caption: str, description: str) -> dict:
    """Upload and record. Returns {"path", "web_url"} or {"error"}."""
    folder = f"{safe_name(project) if project else '_Unsorted'}/{subfolder}"
    try:
        item = upload_file(content, folder, filename, mime)
    except Exception as e:
        print(f"[telegram_media] OneDrive upload failed: {e}")
        return {"error": str(e)}
    try:
        record_project_file(project, kind, item["name"], item["path"], item["web_url"],
                            caption=caption or None, description=description)
    except Exception as e:
        print(f"[telegram_media] could not record file: {e}")
    return {"path": item["path"], "web_url": item["web_url"]}


def _filed_line(result: dict) -> str:
    if result.get("error"):
        return f"\n\nCouldn't save it to OneDrive: {result['error'][:160]}"
    return f"\n\nFiled: {result['path']}"


# ============================================================
# PHOTO
# ============================================================

def handle_photo(content: bytes, mime: str, caption: str, original_name: str = None) -> dict:
    """Returns {"reply", "web_url", "summary"}; summary is what goes in the chat history."""
    projects = project_names()
    ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".jpg")
    if original_name and "." in original_name:
        ext = "." + original_name.rsplit(".", 1)[-1].lower()

    info = None
    if mime in VISION_TYPES and len(content) <= CLAUDE_MAX_IMAGE:
        info = _ask_claude(
            [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                          "data": base64.b64encode(content).decode()}}],
            PHOTO_PROMPT.format(projects=", ".join(projects) or "(none)", caption=caption or "(none)"),
        )
        project = _match_project(info.get("project"), projects)
        title, description, reply = info["title"], info["description"], info["reply"].strip()
    else:
        project = _match_project(caption, projects) if caption else None
        title = caption or (original_name or "photo")
        description = caption or "Photo (not viewed: format or size Rowan can't read)"
        reply = ("I can't view this format or size, so I filed it without looking. "
                 "Send it as a regular photo (not a file) if you want me to read it.")

    filename = f"{_stamp()} {safe_name(title, 60)}{ext}"
    result = _file(content, project, "Photos", filename, mime, "photo", caption, description)
    reply = f"{reply}\n\nProject: {project or 'not sure, left in _Unsorted'}{_filed_line(result)}"
    summary = f"Photo for {project or 'unknown project'}: {description}"
    return {"reply": reply, "web_url": result.get("web_url"), "summary": summary}


# ============================================================
# DOCUMENT
# ============================================================

def handle_document(content: bytes, mime: str, filename: str, caption: str) -> dict:
    projects = project_names()
    filename = filename or "document"
    lower = filename.lower()

    if mime in VISION_TYPES or lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return handle_photo(content, mime if mime in VISION_TYPES else "image/heic", caption, filename)

    if mime == "application/pdf" or lower.endswith(".pdf"):
        try:
            info = _ask_claude(
                [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                 "data": base64.b64encode(content).decode()}}],
                DOC_PROMPT.format(projects=", ".join(projects) or "(none)",
                                  filename=filename, caption=caption or "(none)"),
            )
        except anthropic.APIError as e:
            print(f"[telegram_media] PDF review failed: {e}")
            info = None
        if info:
            project = _match_project(info.get("project"), projects)
            reply, description = info["reply"].strip(), info["description"]
        else:
            project = _match_project(caption, projects) if caption else None
            reply = "I couldn't read that PDF (it may be scanned, locked or over 100 pages). Filed it anyway."
            description = caption or filename
    else:
        project = _match_project(caption, projects) if caption else None
        reply = ("I can only read PDFs and photos so far, so I filed this without reading it. "
                 "Export it to PDF if you want a review.")
        description = caption or filename

    stored_name = f"{_stamp()} {safe_name(filename, 90)}"
    result = _file(content, project, "Documents", stored_name, mime or "application/octet-stream",
                   "document", caption, description)
    reply = f"{reply}\n\nProject: {project or 'not sure, left in _Unsorted'}{_filed_line(result)}"
    summary = f"Document '{filename}' for {project or 'unknown project'}: {description}"
    return {"reply": reply, "web_url": result.get("web_url"), "summary": summary}
