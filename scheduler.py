import schedule
import time
import os
from datetime import datetime, date
from reminders import run_reminders
from status_report import run_status_report, run_pipeline_report
from gmail_reader import scan_gmail_for_tasks
from outlook_reader import scan_outlook_for_tasks
from rowan_nudges import run_nudge_pass
from db import get_connection


def should_run_job(job_name):
    """Returns True and marks as run atomically, or returns False if already ran today."""
    conn = get_connection()
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
    conn.close()
    return result is not None


def run_daily_jobs():
    if not should_run_job("daily"):
        print("Daily jobs already ran today, skipping.")
        return
    print("Running daily jobs...")
    scan_gmail_for_tasks(2)
    scan_outlook_for_tasks(1)
    run_reminders()
    print("Daily jobs complete.")


def run_weekly_jobs():
    if not should_run_job("weekly"):
        print("Weekly jobs already ran today, skipping.")
        return
    print("Running weekly jobs...")
    run_status_report(1)
    run_status_report(2)
    run_pipeline_report()
    print("Weekly jobs complete.")


def run_nudges_job():
    """
    Proactive nudge pass. Runs at 12/16/20 UTC, Monday-Friday only
    (= 8am/12pm/4pm Eastern during DST, off by 1 hr in standard time).
    Internal per-finding 3-day dedup handles repetition.
    """
    # Skip weekends (UTC weekday: 0=Mon ... 6=Sun)
    if datetime.utcnow().weekday() >= 5:
        return
    print(f"[{datetime.utcnow().isoformat()}] Running Rowan nudge pass...")
    try:
        summary = run_nudge_pass()
        print(f"Nudge pass: {summary}")
    except Exception as e:
        print(f"Nudge pass failed: {e}")


# ----- Schedule -----
schedule.every().day.at("08:00").do(run_daily_jobs)
schedule.every().monday.at("08:00").do(run_weekly_jobs)

# Rowan proactive nudges — business-hours, M-F (weekend skip is inside the job)
schedule.every().day.at("08:00").do(run_nudges_job)
schedule.every().day.at("12:00").do(run_nudges_job)
schedule.every().day.at("16:00").do(run_nudges_job)

print("Scheduler started.")
print("- Gmail will be scanned every day at 8am.")
print("- Reminders will run every day at 8am.")
print("- Status reports will run every Monday at 8am.")
print("- Rowan nudges run at 08:00 / 12:00 / 16:00 Eastern, weekdays only.")
print("Press Ctrl+C to stop.")

while True:
    schedule.run_pending()
    time.sleep(60)
