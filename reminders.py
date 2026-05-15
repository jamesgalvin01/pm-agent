import os
import psycopg2
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv
from datetime import date

load_dotenv()

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

def send_reminder(task):
    project_name = task[4] if task[4] else "No Project"
    message = Mail(
    from_email="james@miami-coastline.com",
    to_emails="james@miami-coastline.com",
    subject=f"[{project_name}] Task Reminder: {task[1]}",
    plain_text_content=f"Project: {project_name}\nTask: {task[1]}\nDue: {task[2]}\nPriority: {task[3]}"
)
    sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
    sg.send(message)
    print(f"Reminder sent for: {task[1]}")

def run_reminders():
    tasks = get_due_tasks()
    if not tasks:
        print("No tasks due soon.")
        return
    print(f"Found {len(tasks)} tasks due soon.")
    for task in tasks:
        send_reminder(task)

run_reminders()