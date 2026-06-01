import anthropic
import os
import resend
from dotenv import load_dotenv
from db import get_connection
from health_analyzer import analyze_health

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
resend.api_key = os.getenv("RESEND_API_KEY")

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

def get_pipeline_data():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*),
            COALESCE(SUM(value) FILTER (WHERE status NOT IN ('Won','Lost')), 0),
            COALESCE(SUM(value) FILTER (WHERE status = 'Won'), 0),
            COUNT(*) FILTER (WHERE status = 'Won'),
            COUNT(*) FILTER (WHERE status IN ('Won','Lost'))
        FROM leads
    """)
    total, open_val, won_val, won_count, closed_count = cur.fetchone()

    cur.execute("""
        SELECT status, COUNT(*), COALESCE(SUM(value), 0)
        FROM leads
        WHERE status NOT IN ('Won','Lost')
        GROUP BY status
    """)
    by_stage = cur.fetchall()
    cur.close()
    conn.close()

    win_rate = round(won_count / closed_count * 100) if closed_count else 0
    return {
        "total": total,
        "open_value": float(open_val),
        "won_value": float(won_val),
        "win_rate": win_rate,
        "by_stage": [{"stage": s[0], "count": s[1], "value": float(s[2])} for s in by_stage],
    }

def generate_pipeline_summary(pipeline):
    if pipeline["total"] == 0:
        return "New Business Pipeline\n\nNo active leads in the pipeline this week."

    stage_lines = ", ".join(
        f"{s['count']} in {s['stage']} (${s['value']:,.0f})" for s in pipeline["by_stage"]
    ) or "none"

    prompt = f"""Write a brief "New Business Pipeline" section for a weekly report.
Total leads: {pipeline['total']}
Open pipeline value: ${pipeline['open_value']:,.0f}
Won value (lifetime): ${pipeline['won_value']:,.0f}
Win rate: {pipeline['win_rate']}%
Open leads by stage: {stage_lines}
Write 2-3 sentences summarizing pipeline health and where attention is needed.
Start with the heading "New Business Pipeline" on its own line. Keep it concise and professional."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

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
    params = {
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"Weekly Status Report: {project_name} [{rag_status.upper()}]",
        "text": report
    }
    resend.Emails.send(params)
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

def run_pipeline_report():
    pipeline = get_pipeline_data()
    summary = generate_pipeline_summary(pipeline)

    print("\n--- PIPELINE REPORT ---")
    print(summary)
    print("-----------------------\n")

    params = {
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"Weekly Pipeline Summary [{pipeline['total']} leads, ${pipeline['open_value']:,.0f} open]",
        "text": summary,
    }
    resend.Emails.send(params)
    print("Pipeline report emailed.")

