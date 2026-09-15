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


def run_daily_jobs():
    if not should_run_job("daily"):
        log("Daily jobs already ran today, skipping.")
        return
    log("Running daily jobs...")
    from gmail_reader import scan_gmail_for_tasks
    from outlook_reader import scan_outlook_for_tasks
    from reminders import run_reminders
    run_step("gmail scan", lambda: scan_gmail_for_tasks(2))
    run_step("outlook scan", lambda: scan_outlook_for_tasks(1))
    run_step("reminders", run_reminders)
    log("Daily jobs complete.")


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
    log("LinkedIn draft sent.")


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


# ----- Schedule -----
schedule.every().day.at("08:00").do(safe("daily jobs", run_daily_jobs))
schedule.every().monday.at("08:00").do(safe("weekly jobs", run_weekly_jobs))
schedule.every().day.at("09:00").do(safe("LinkedIn draft", run_linkedin_draft_job))

# Rowan proactive nudges — DISABLED (James, Sep 2026: noon nudge was too much).
# The job is still defined above; re-enable by uncommenting the line below.
# schedule.every().day.at("12:00").do(safe("nudge pass", run_nudges_job))

log("Scheduler started.")
log("- Gmail and Outlook scanned every day at 8am.")
log("- Reminders run every day at 8am.")
log("- Status reports run every Monday at 8am.")
log("- LinkedIn draft email sent every day at 9am.")
log("- Rowan nudges: disabled.")
check_database()

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
