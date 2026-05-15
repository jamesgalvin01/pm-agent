import schedule
import time
from reminders import run_reminders
from status_report import run_status_report
from gmail_reader import scan_gmail_for_tasks
from outlook_reader import scan_outlook_for_tasks

def run_daily_jobs():
    print("Running daily jobs...")
    scan_gmail_for_tasks(2)
    scan_outlook_for_tasks(1)
    run_reminders()
    print("Daily jobs complete.")

def run_weekly_jobs():
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