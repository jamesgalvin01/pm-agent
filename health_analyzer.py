import anthropic
import json
import os
from dotenv import load_dotenv
from db import get_connection

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def get_project_tasks(project_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT description, due_date, priority, status
        FROM tasks
        WHERE project_id = %s
    """, (project_id,))
    tasks = cur.fetchall()
    cur.close()
    conn.close()
    return [{"task": t[0], "due_date": str(t[1]), "priority": t[2], "status": t[3]} for t in tasks]

def analyze_health(project_name, tasks):
    prompt = f"""You are a senior project manager. Analyze these tasks for the project '{project_name}' and return ONLY a JSON object with:
- rag_status: red, amber, or green
- justification: one sentence explanation
- recommended_actions: list of 2 actions to take this week

Tasks: {json.dumps(tasks)}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.content[0].text
    clean = raw.replace("```json", "").replace("```", "").strip()
    start = clean.find('{')
    end = clean.rfind('}') + 1
    return json.loads(clean[start:end])

def run_health_check(project_id, project_name):
    tasks = get_project_tasks(project_id)
    if not tasks:
        print(f"No tasks found for project: {project_name}")
        return
    result = analyze_health(project_name, tasks)
    print(f"\nProject: {project_name}")
    print(f"RAG Status: {result['rag_status'].upper()}")
    print(f"Justification: {result['justification']}")
    print(f"Recommended Actions: {result['recommended_actions']}")
    return result

run_health_check(1, "Test Project")