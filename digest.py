"""
digest.py — the pushes. Few, short, and silent when there's nothing to say.

  morning_push()     weekdays 7:00. Only exceptions: overdue, due within 48h,
                     waiting-on with no movement for 5+ days. No message at all
                     when nothing qualifies. Then the sent-mail proposal batch
                     (top 5), if any.
  monday_review()    Mondays, after the morning push. One paragraph per active
                     project: what's open, what's late, what the client owes a
                     decision on, and projects with nothing tracked.
"""
import register as reg

BUTTON_ITEMS = 8


def _tg():
    import telegram_bot
    return telegram_bot


def _exceptions_message(rows: list):
    groups = {1: "OVERDUE", 2: "DUE IN 48H", 4: f"WAITING, NO MOVEMENT {reg.STALE_WAITING_DAYS}+ DAYS"}
    lines = [f"Morning. {len(rows)} item{'s' if len(rows) != 1 else ''} need{'s' if len(rows) == 1 else ''} you."]
    for tier, label in groups.items():
        items = [r for r in rows if r["tier"] == tier]
        if items:
            lines.append(f"\n{label}")
            lines.extend(reg.task_line(r) for r in items)
    lines.append("\nFull list: /list")

    keyboard = []
    for r in rows[:BUTTON_ITEMS]:
        row = [{"text": f"Done #{r['id']}", "callback_data": f"td:{r['id']}"}]
        if r.get("ball_in_court") == "waiting":
            row.append({"text": f"Chase #{r['id']}", "callback_data": f"tch:{r['id']}"})
        row.append({"text": f"+3 days #{r['id']}", "callback_data": f"tsn:{r['id']}"})
        keyboard.append(row)
    return "\n".join(lines), {"inline_keyboard": keyboard}


def morning_push() -> dict:
    reg.ensure_register_schema()
    reg.backfill_defaults()
    tg = _tg()
    stats = {"exceptions": 0, "proposals_sent": False}
    rows = reg.exceptions()
    stats["exceptions"] = len(rows)
    if rows:
        text, markup = _exceptions_message(rows)
        tg.send_message(text, reply_markup=markup)
    from register_telegram import send_proposal_batch
    stats["proposals_sent"] = send_proposal_batch(source="sent_mail")
    return stats


def monday_review() -> bool:
    reg.ensure_register_schema()
    all_rows = reg.get_register()
    by_project = {}
    for r in all_rows:
        by_project.setdefault(r["project_id"], []).append(r)

    lines = ["Monday portfolio review"]
    for p in reg.list_projects():
        rows = by_project.pop(p["id"], [])
        if not rows:
            lines.append(f"\n{p['name']}: nothing tracked. Is that right?")
            continue
        mine = sum(1 for r in rows if r["ball_in_court"] != "waiting")
        late = sum(1 for r in rows if r["tier"] == 1)
        stale = sum(1 for r in rows if r["tier"] == 4)
        head = f"\n{p['name']}: {len(rows)} open ({mine} yours, {len(rows) - mine} waiting)"
        if late:
            head += f", {late} overdue"
        if stale:
            head += f", {stale} stale"
        lines.append(head)
        decisions = [r for r in rows if r["blocking"]]
        for r in decisions[:3]:
            lines.append("  Decision/blocking: " + reg.task_line(r, show_project=False))
        for r in [r for r in rows if not r["blocking"]][:3]:
            lines.append("  Next: " + reg.task_line(r, show_project=False))

    orphans = by_project.pop(None, [])
    leftover = [r for rs in by_project.values() for r in rs]      # inactive projects
    if orphans:
        lines.append(f"\nNo project: {len(orphans)} open. Give them a home: "
                     + ", ".join(f"#{r['id']}" for r in orphans[:8]))
    if leftover:
        lines.append(f"\nOn inactive projects: {len(leftover)} still open.")

    pending = reg.pending_proposals()
    if pending:
        lines.append(f"\n{len(pending)} action items still waiting for your OK: /review")
    return bool(_tg().send_message("\n".join(lines)))
