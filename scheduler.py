import schedule
import time
import os
from datetime import datetime, date
from reminders import run_reminders
from status_report import run_status_report
from gmail_reader import scan_gmail_for_tasks
from outlook_reader import scan_outlook_for_tasks
from db import get_connection

def get_last_run(job_name):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS job_runs (
            job_name TEXT PRIMARY KEY,
            last_run DATE
        )
    """)
    conn.commit()
    cur.execute("SELECT last_run FROM job_runs WHERE job_name = %s", (job_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def set_last_run(job_name):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO job_runs (job_name, last_run)
        VALUES (%s, %s)
        ON CONFLICT (job_name) DO UPDATE SET last_run = EXCLUDED.last_run
    """, (job_name, date.today()))
    conn.commit()
    cur.close()
    conn.close()

def run_daily_jobs():
    today = date.today()
    if get_last_run("daily") == today:
        print("Daily jobs already ran today, skipping.")
        return
    set_last_run("daily")
    print("Running daily jobs...")
    scan_gmail_for_tasks(2)
    scan_outlook_for_tasks(1)
    run_reminders()
    print("Daily jobs complete.")

def run_weekly_jobs():
    today = date.today()
    if get_last_run("weekly") == today:
        print("Weekly jobs already ran today, skipping.")
        return
    set_last_run("weekly")
    print("Running weekly jobs...")
    run_status_report(1)
    run_status_report(2)
    print("Weekly jobs complete.")

schedule.every().day.at("08:00").do(run_daily_jobs)
schedule.every().monday.at("08:00").do(run_weekly_jobs)

print("Scheduler started.")
print("- Gmail will be scanned every day at 8am.")
print("- Reminders will run every day at 8am.")
print("- Status reports will run every Monday at 8am.")
print("Press Ctrl+C to stop.")

while True:
    schedule.run_pending()
    time.sleep(60)