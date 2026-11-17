"""
contracts_tg.py — Telegram side of the contracts agent.

  /contract                      -> next PDF/Word file James sends gets a review
  /contract compare              -> next file gets compared to MCM's standard
  /contract review 71 NoBE       -> same, filed under the project
  /contract draft <what>         -> draft a rider/clause/short form
  /contract <question>           -> answer a contracts / FL law question
  file with caption "/contract ..." -> review (or compare/question) right away

Replies with the bottom line, plus buttons for the full write-up on the
dashboard and the Word memo in OneDrive.
"""
import time

import contracts

WINDOW_SECONDS = 15 * 60
STATE_KEY = "contract_mode"


def _tg():
    import telegram_bot
    return telegram_bot


def _match_project(text: str):
    """Pull a known project name off the front of the text, if there is one."""
    try:
        import register as reg
        names = [p["name"] for p in reg.list_projects()]
    except Exception:
        return None, text
    low = text.lower()
    for n in sorted(names, key=len, reverse=True):
        if low.startswith(n.lower()):
            return n, text[len(n):].lstrip(" :-,")
    return None, text


def parse(rest: str) -> dict:
    """'/contract <rest>' -> {mode, project, request}."""
    rest = (rest or "").strip()
    first, _, tail = rest.partition(" ")
    word = first.lower()
    if word in ("review", "redline", "compare", "draft", "ask", "question", "q"):
        mode = {"redline": "review", "ask": "question", "q": "question"}.get(word, word)
        rest = tail.strip()
    else:
        mode = None
    project, rest = _match_project(rest)
    return {"mode": mode, "project": project, "request": rest}


def wants_file() -> dict:
    """Pending '/contract' state if James is about to send a file, else None."""
    st = _tg().state_get(STATE_KEY) or {}
    if st.get("mode") and time.time() < float(st.get("until", 0)):
        return st
    return None


def clear() -> None:
    _tg().state_set(STATE_KEY, {})


def _buttons(rid: int, memo_url: str = None):
    row = [{"text": "Full write-up", "url": contracts.page_url(rid)}]
    if memo_url:
        row.append({"text": "Word memo", "url": memo_url})
    return {"inline_keyboard": [row]}


def command(rest: str) -> None:
    """Handle '/contract ...' typed as text."""
    tg = _tg()
    p = parse(rest)
    mode, project, request = p["mode"], p["project"], p["request"]

    if mode in (None, "review", "compare") and not (mode is None and request):
        tg.state_set(STATE_KEY, {"mode": mode or "review", "project": project, "request": request,
                                 "until": time.time() + WINDOW_SECONDS})
        what = "compare it to MCM's standard" if mode == "compare" else "review it Owner-side"
        tg.send_message(f"Send the contract (PDF or Word) in the next 15 minutes and I'll {what}"
                        f"{' for ' + project if project else ''}. Say \"cancel\" to stop.")
        return

    mode = mode or "question"
    if mode == "draft" and not request:
        tg.send_message("What should I draft? e.g. /contract draft Owner-side rider to A201 for "
                        "Farbstein: 10% retainage, $1,500/day LDs, 558 opt-out")
        return
    tg._typing()
    tg.send_message("Drafting..." if mode == "draft" else "Looking into it...")
    rid = contracts.create(mode, project, None, request, source="telegram")
    res = contracts.run(rid)
    if not res["ok"]:
        tg.send_message(res["error"])
        return
    body = res["result"] if mode == "question" else contracts.bottom_line(res["result"])
    if mode == "question":
        body = body.replace("## Bottom line", "").strip()
    tg.send_message(body, reply_markup=_buttons(rid, res.get("memo_url")))


def handle_file(file_id: str, file_size: int, mime: str, filename: str, caption: str,
                pending: dict = None) -> None:
    """A contract file arrived (caption '/contract ...' or after '/contract')."""
    tg = _tg()
    from telegram_media import MediaError, download
    if pending:
        mode, project, request = pending.get("mode"), pending.get("project"), pending.get("request") or ""
        if caption:
            request = f"{request} {caption}".strip()
        clear()
    else:
        cap = caption or ""
        cap = cap.split(None, 1)[1] if cap.lower().startswith("/contract") and " " in cap else ""
        p = parse(cap)
        mode, project, request = p["mode"], p["project"], p["request"]
        if not mode:
            mode = "question" if request.endswith("?") else "review"
    if project is None:
        project, request = _match_project(request)

    tg._typing()
    label = contracts.MODES.get(mode, "Review").lower()
    tg.send_message(f"Got {filename or 'the contract'}. Working on the {label} "
                    f"(a full review takes a few minutes)...")
    try:
        content, path = download(file_id, file_size)
    except MediaError as e:
        tg.send_message(str(e))
        return
    filename = filename or path.rsplit("/", 1)[-1]
    rid = contracts.create(mode, project, filename, request, source="telegram")
    res = contracts.run(rid, content, mime, filename)
    if not res["ok"]:
        tg.send_message(res["error"])
        return
    head = f"{contracts.MODES[mode]}: {filename}" + (f" ({project})" if project else "")
    tg.send_message(f"{head}\n\n{contracts.bottom_line(res['result'])}",
                    reply_markup=_buttons(rid, res.get("memo_url")))
    try:
        from rowan_agent import save_exchange
        conv_id = tg._conversation_id()
        save_exchange(conv_id, f"[Contract {mode}: {filename}] {request}".strip(),
                      contracts.bottom_line(res["result"]))
        tg._touch_conversation(conv_id)
    except Exception as e:
        print(f"[contracts_tg] could not save exchange: {e}")
