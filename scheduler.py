import schedule
import time
import os
import sys
import traceback
from datetime import datetime, date

HEARTBEAT_SECONDS = 3600


def log(msg):
    """Timestamped, unbuffered — Railway drops anything that sits in the buffer."""
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def report_failure(label):
    log(f"!! {label} FAILED — scheduler is still running")
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()


def safe(label, fn):
    """Wrap a scheduled job so nothing it raises can kill the loop."""
    def wrapper():
        try:
            fn()
        except Exception:
            report_failure(label)
    wrapper.__name__ = f"safe_{label.replace(' ', '_')}"
    return wrapper


def check_database():
    """Log whether the database is reachable at boot. Never fatal."""
    try:
        from db import get_connection
        conn = get_connection()
        conn.close()
        log("Database connection OK.")
        return True
    except Exception:
        report_failure("database connection check")
        return False


def should_run_job(job_name):
    """Returns True and marks as run atomically, or False if it already ran today."""
    from db import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                job_name TEXT PRIMARY KEY,
                last_run DATE
            )
        """)
        conn.commit()
        cur.execute("""
            INSERT INTO job_runs (job_name, last_run)
            VALUES (%s, CURRENT_DATE)
            ON CONFLICT (job_name) DO UPDATE
            SET last_run = CURRENT_DATE
            WHERE job_runs.last_run < CURRENT_DATE
            RETURNING job_name
        """, (job_name,))
        result = cur.fetchone()
        conn.commit()
        cur.close()
        return result is not None
    finally:
        conn.close()


def run_step(label, fn):
    """Run one step of a multi-step job; a failure here doesn't abort the rest."""
    try:
        fn()
    except Exception:
        report_failure(label)


# The old 8am inbox scan (gmail_reader / outlook_reader -> every action item in
# every email) and the daily summary email (reminders.py) are retired: they were
# the noise. Action items now come in through register capture (capture.py)
# and the only daily push is the 7am exceptions list (digest.py).


def run_sent_mail_scan():
    """Weekdays 6:40 — proposals from mail James sent. Nothing enters the register without his OK."""
    if datetime.now().weekday() >= 5:
        return
    if not should_run_job("sent_mail_scan"):
        return
    from capture import scan_sent_mail
    log(f"Sent-mail scan: {scan_sent_mail()}")


def run_morning_push():
    """Weekdays 7:00 — exceptions only (silent if none), then the proposal batch. Mondays add the review."""
    if datetime.now().weekday() >= 5:
        return
    if not should_run_job("morning_push"):
        log("Morning push already ran today, skipping.")
        return
    from digest import morning_push, monday_review
    run_step("morning push", lambda: log(f"Morning push: {morning_push()}"))
    if datetime.now().weekday() == 0:
        run_step("monday review", lambda: log(f"Monday review sent: {monday_review()}"))

    # Overnight email: draft anything not yet triaged, then start the queue.
    def overnight_email():
        from email_replies import run_reply_draft_pass
        from approvals import notify_next_email_draft
        log(f"Overnight email pass: {run_reply_draft_pass(hours=36)}")
        notify_next_email_draft(respect_hours=True)
    run_step("overnight email", overnight_email)


def run_flag_sync():
    """Every 20 min, 6am-9pm: flagged Outlook emails -> tasks; replies on waiting threads reset the stale clock."""
    if not 6 <= datetime.now().hour < 21:
        return
    from capture import sync_flagged, touch_replied_threads
    from register_telegram import notify_flag_task
    stats = sync_flagged(notify=notify_flag_task)
    if stats["added"] or stats["closed"]:
        log(f"Flag sync: {stats}")
    run_step("waiting-thread touch", lambda: touch_replied_threads(hours=12))


def run_weekly_jobs():
    if not should_run_job("weekly"):
        log("Weekly jobs already ran today, skipping.")
        return
    log("Running weekly jobs...")
    from status_report import run_status_report, run_pipeline_report
    run_step("status report (1)", lambda: run_status_report(1))
    run_step("status report (2)", lambda: run_status_report(2))
    run_step("pipeline report", run_pipeline_report)
    log("Weekly jobs complete.")


def run_linkedin_draft_job():
    if not should_run_job("linkedin_draft"):
        log("LinkedIn draft already ran today, skipping.")
        return
    log("Running LinkedIn draft job...")
    from linkedin_drafter import run_linkedin_draft
    run_linkedin_draft()
    log("LinkedIn draft delivered.")


def run_nudges_job():
    """
    Proactive nudge pass. Runs once daily at 12:00 Eastern, Monday-Friday only.
    Internal per-finding 3-day dedup handles repetition.
    """
    if datetime.now().weekday() >= 5:
        return
    log("Running Rowan nudge pass...")
    from rowan_nudges import run_nudge_pass
    summary = run_nudge_pass()
    log(f"Nudge pass: {summary}")


def run_email_reply_job():
    """
    Backup to real-time email (graph_push.py): every 15 minutes, draft replies
    for anything in the inbox that hasn't been triaged yet. Drafts are offered
    to James only 7am-7pm weekdays; outside that they wait for the 7am push.
    Nothing is sent here. Idempotent: emails already in email_drafts are skipped.
    """
    from email_replies import run_reply_draft_pass
    stats = run_reply_draft_pass(hours=24)
    if stats.get("drafted") or stats.get("errors"):
        log(f"Email backup pass: {stats}")
    try:
        from approvals import notify_next_email_draft
        if notify_next_email_draft(respect_hours=True):
            log("Offered a draft for approval.")
    except Exception:
        report_failure("email draft approval notify")


def run_invoice_email_job():
    """
    5th-12th, 8am-7pm Eastern: pick up the invoices the Mac generated
    (invoices.json in the month's AR folder) and offer each cover email in
    Telegram for Send / Edit / Skip. Nothing is sent here.
    """
    from invoice_emails import run_check
    stats = run_check()
    if stats.get("added") or stats.get("offered"):
        log(f"Invoice emails: {stats}")


# ----- Schedule -----
schedule.every().monday.at("08:00").do(safe("weekly jobs", run_weekly_jobs))

# Action-item register (register.py / capture.py / digest.py)
schedule.every().day.at("06:40").do(safe("sent mail scan", run_sent_mail_scan))
schedule.every().day.at("07:00").do(safe("morning push", run_morning_push))
schedule.every(20).minutes.do(safe("flag sync", run_flag_sync))
schedule.every().day.at("09:00").do(safe("LinkedIn draft", run_linkedin_draft_job))

# Email reply drafts: real time via graph_push.py (web service); this is the
# 15-minute backup. Drafts reach James 7am-7pm weekdays only.
schedule.every(15).minutes.do(safe("email backup pass", run_email_reply_job))

# Invoice cover emails: offered for approval once the Mac's 5th-of-month run lands.
schedule.every(30).minutes.do(safe("invoice emails", run_invoice_email_job))

# Rowan proactive nudges — DISABLED (James, Sep 2026: noon nudge was too much).
# The job is still defined above; re-enable by uncommenting the line below.
# schedule.every().day.at("12:00").do(safe("nudge pass", run_nudges_job))

log("Scheduler started.")
log("- Register: flagged Outlook emails synced every 20 min (6am-9pm).")
log("- Register: sent-mail proposals at 6:40, morning exceptions push at 7:00, weekdays (Mondays + portfolio review).")
log("- Inbox task scan and daily summary email: retired.")
log("- Status reports run every Monday at 8am.")
log("- LinkedIn draft sent every day at 9am (Telegram, or email if Telegram is off).")
log("- Email reply drafts: real time as mail arrives, 15-min backup; offered 7am-7pm weekdays.")
log("- Invoice emails: checked every 30 min, 5th-12th 8am-7pm; each sent only after a Telegram tap.")
log("- Rowan nudges: disabled.")
check_database()
try:
    from register import ensure_register_schema
    ensure_register_schema()
    log("Register schema OK.")
except Exception:
    report_failure("register schema")

last_heartbeat = time.monotonic()

while True:
    try:
        schedule.run_pending()
    except Exception:
        report_failure("scheduler loop")
    if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
        log(f"heartbeat — alive, {len(schedule.jobs)} jobs scheduled, next run {schedule.next_run()}")
        last_heartbeat = time.monotonic()
    time.sleep(60)
