"""
musts_tg.py — Must accomplish in Telegram.

  6:00 AM   today's list, each item with ✅ Done and ⇄ Swap buttons
  6:00 PM   recap: how many got done, what rolls to tomorrow
  /musts    the list now (same buttons)
  /must call Marc re budget   add a one-off item
  /must 3772  (or /must 3772 3768)   promote register tasks

Buttons edit the list message in place so it always shows the current state.
Callback data: md:<id> done, mu:<id> undo, ms:<id> swap, mr:<id> remove, mref:0 refresh.
"""
import re

import musts


def _tg():
    import telegram_bot
    return telegram_bot


def _render(header: str = None):
    rows = musts.get_list()
    s = musts.summary(rows)
    head = header or "Must accomplish today"
    lines = [f"{head} · {s['done']}/{s['total']} done"]
    if s["topped_out"]:
        lines[0] += " 🏗 Topped out!"
    if not rows:
        lines.append("\nNothing on the list. Add one: /must call Marc re budget")
    for n, r in enumerate(rows, 1):
        lines.append(musts.line(r, n))
    flagged = [r for r in rows if r["status"] != "done" and r["flagged"]]
    if flagged:
        lines.append(f"\n⚠ {len(flagged)} item{'s have' if len(flagged) != 1 else ' has'} rolled over "
                     f"{musts.ROLL_FLAG_DAYS}+ days. Do it, swap it, or drop it.")
    lines.append("\nAdd: /must <what>  or  /must <task #>")
    keyboard = []
    for n, r in enumerate(rows, 1):
        if r["status"] == "done":
            keyboard.append([{"text": f"↩ Undo {n}", "callback_data": f"mu:{r['id']}"}])
        else:
            keyboard.append([{"text": f"✅ {n}", "callback_data": f"md:{r['id']}"},
                             {"text": f"⇄ Swap {n}", "callback_data": f"ms:{r['id']}"},
                             {"text": "✕", "callback_data": f"mr:{r['id']}"}])
    keyboard.append([{"text": "Refresh", "callback_data": "mref:0"}])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def send_list(header: str = None) -> None:
    text, markup = _render(header)
    _tg().send_message(text, reply_markup=markup)


def morning() -> dict:
    """6:00 AM: reset the list and send it."""
    musts.ensure_schema()
    reg_today = musts._now().date()
    result = musts.fill_day(reg_today)
    send_list("Good morning. Must accomplish today")
    return result


def recap() -> dict:
    """6:00 PM: what got done and what rolls to tomorrow."""
    rows = musts.get_list()
    s = musts.summary(rows)
    if not rows:
        return s
    if s["topped_out"]:
        text = f"Topped out 🏗 All {s['total']} musts done today. Nice work."
    else:
        left = [r for r in rows if r["status"] != "done"]
        text = (f"Today: {s['done']} of {s['total']} musts done.\n\n"
                f"Rolling to tomorrow ({len(left)}):\n" +
                "\n".join(f"• {r['title']}" + (f"  ⚠ rolled {r['rolled_days'] + 1}d"
                                                 if (r['rolled_days'] or 0) + 1 >= musts.ROLL_FLAG_DAYS else "")
                          for r in left) +
                "\n\nFinish any tonight with /musts, or swap them out before 6 AM.")
    _tg().send_message(text)
    return s


def command(rest: str) -> None:
    """/must <text> adds a line; /must 3772 [3768 ...] promotes register tasks."""
    tg = _tg()
    rest = (rest or "").strip()
    if not rest:
        tg.send_message("Add what, e.g. /must call Marc re budget, or /must 3772 for a task in the register.")
        return
    ids = re.findall(r"#?(\d+)", rest)
    only_ids = re.fullmatch(r"[#\d,\s]+", rest) is not None
    msgs = []
    if only_ids:
        for i in ids:
            r = musts.add(task_id=int(i), source="telegram")
            if not r["ok"]:
                msgs.append(r["error"])
            elif r.get("duplicate"):
                msgs.append(f"#{i} is already on today's list.")
            else:
                msgs.append(f"Added #{i}." + (f" Bumped to make room: {r['bumped']}" if r.get("bumped") else ""))
    else:
        r = musts.add(title=rest, source="telegram")
        msgs.append("Added." + (f" Bumped to make room: {r['bumped']}" if r.get("bumped") else "") if r["ok"] else r["error"])
    text, markup = _render(" ".join(msgs).strip() + "\n\nMust accomplish today")
    tg.send_message(text, reply_markup=markup)


def action(action: str, ref: str, message: dict) -> None:
    from register_telegram import _edit
    msg_id = message.get("message_id")
    note = ""
    try:
        mid = int(ref)
    except ValueError:
        mid = 0
    if action == "md":
        r = musts.complete(mid)
        note = f"Done: {r['title']}" if r["ok"] else r["error"]
    elif action == "mu":
        r = musts.undo(mid)
        note = f"Reopened: {r['title']}" if r["ok"] else r["error"]
    elif action == "ms":
        r = musts.swap(mid)
        note = (f"Swapped out: {r['title']}" + (f"\nIn: {r['replacement']}" if r.get("replacement") else "")) if r["ok"] else r["error"]
    elif action == "mr":
        r = musts.remove(mid)
        note = f"Removed: {r['title']}" if r["ok"] else r["error"]
    text, markup = _render()
    _edit(msg_id, (note + "\n\n" if note else "") + text, markup)
