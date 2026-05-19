import os
import resend
import psycopg2
from dotenv import load_dotenv
from datetime import date

load_dotenv()

resend.api_key = os.getenv("RESEND_API_KEY")

def get_connection():
    return psycopg2.connect(
        host="aws-1-us-east-1.pooler.supabase.com",
        database="postgres",
        user="postgres.fywwujzmxdnhophhgaey",
        password=os.getenv("SUPABASE_PASSWORD"),
        port=5432,
        sslmode="require"
    )

def get_due_tasks():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.id, t.description, t.due_date, t.priority, p.name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'open'
        AND t.due_date IS NOT NULL
        AND t.due_date <= CURRENT_DATE + INTERVAL '2 days'
    """)
    tasks = cur.fetchall()
    cur.close()
    conn.close()
    return tasks

def get_todays_tasks():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.description, t.due_date, t.priority, t.source, p.name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        WHERE t.status = 'open'
        AND DATE(t.created_at) = CURRENT_DATE
        ORDER BY t.priority DESC
    """)
    tasks = cur.fetchall()
    cur.close()
    conn.close()
    return tasks

def send_daily_summary(due_tasks, new_tasks):
    today = date.today().strftime("%B %d, %Y")

    body = f"Rowan Daily Summary — {today}\n"
    body += "=" * 40 + "\n\n"

    body += f"NEW TASKS SCANNED TODAY ({len(new_tasks)})\n"
    body += "-" * 30 + "\n"
    if new_tasks:
        for t in new_tasks:
            source = t[3] or "unknown"
            project = t[4] or "No Project"
            due = f" | Due: {t[1]}" if t[1] else ""
            body += f"• [{project}] {t[0]} (via {source}){due}\n"
    else:
        body += "No new tasks scanned today.\n"

    body += f"\nTASKS DUE SOON ({len(due_tasks)})\n"
    body += "-" * 30 + "\n"
    if due_tasks:
        for t in due_tasks:
            project = t[4] or "No Project"
            body += f"• [{project}] {t[1]} | Due: {t[2]} | Priority: {t[3]}\n"
    else:
        body += "No tasks due in the next 2 days.\n"

    resend.Emails.send({
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"Rowan Daily Summary — {today}",
        "text": body
    })
    print("Daily summary email sent.")

def send_reminder(task):
    project_name = task[4] if task[4] else "No Project"
    resend.Emails.send({
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"[{project_name}] Task Reminder: {task[1]}",
        "text": f"Project: {project_name}\nTask: {task[1]}\nDue: {task[2]}\nPriority: {task[3]}"
    })
    print(f"Reminder sent for: {task[1]}")

def run_reminders():
    due_tasks = get_due_tasks()
    new_tasks = get_todays_tasks()

    if not due_tasks:
        print("No tasks due soon.")
    else:
        print(f"Found {len(due_tasks)} tasks due soon.")
        for task in due_tasks:
            send_reminder(task)

    send_daily_summary(due_tasks, new_tasks)