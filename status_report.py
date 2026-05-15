import anthropic
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from db import get_connection
from health_analyzer import analyze_health

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def get_project_data(project_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name, goal, status FROM projects WHERE id = %s", (project_id,))
    project = cur.fetchone()
    cur.execute("""
        SELECT description, due_date, priority, status
        FROM tasks WHERE project_id = %s
    """, (project_id,))
    tasks = cur.fetchall()
    cur.close()
    conn.close()
    task_list = [{"task": t[0], "due_date": str(t[1]), "priority": t[2], "status": t[3]} for t in tasks]
    return {"name": project[0], "goal": project[1], "status": project[2]}, task_list

def generate_report(project_name, rag_status, tasks, actions):
    overdue = [t for t in tasks if t["status"] == "overdue"]
    open_tasks = [t for t in tasks if t["status"] == "open"]
    done_tasks = [t for t in tasks if t["status"] == "complete"]

    prompt = f"""Write a professional weekly project status report.

Project: {project_name}
RAG Status: {rag_status.upper()}
Open tasks: {[t["task"] for t in open_tasks]}
Completed tasks: {[t["task"] for t in done_tasks]}
Overdue tasks: {[t["task"] for t in overdue]}
Recommended actions: {actions}

Write exactly 3 sections:
1. Executive Summary (2-3 sentences, non-technical)
2. Progress This Week (bullet points)
3. Actions Required (what needs a decision or escalation)

Keep it professional and concise."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

def send_report_email(project_name, report, rag_status):
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = "james@miami-coastline.com"
    msg["Subject"] = f"Weekly Status Report: {project_name} [{rag_status.upper()}]"
    msg.attach(MIMEText(report, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.sendmail(gmail_user, "james@miami-coastline.com", msg.as_string())

    print(f"Status report emailed for: {project_name}")

def run_status_report(project_id):
    project, tasks = get_project_data(project_id)
    health = analyze_health(project["name"], tasks)
    report = generate_report(
        project["name"],
        health["rag_status"],
        tasks,
        health["recommended_actions"]
    )
    print("\n--- STATUS REPORT ---")
    print(report)
    print("---------------------\n")
    send_report_email(project["name"], report, health["rag_status"])

run_status_report(1)
