from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from db import get_connection
import uvicorn

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def home():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, name, status, rag_status FROM projects")
    projects = cur.fetchall()

    cur.execute("""
        SELECT t.id, t.description, t.due_date, t.priority, t.status, p.name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        ORDER BY t.due_date ASC NULLS LAST
    """)
    tasks = cur.fetchall()

    cur.close()
    conn.close()

    rag_colors = {"green": "#2ecc71", "amber": "#f39c12", "red": "#e74c3c"}

    projects_html = ""
    for p in projects:
        color = rag_colors.get(p[3], "#999")
        projects_html += f"""
        <div class='card'>
            <h3>{p[1]}</h3>
            <span style='background:{color};color:white;padding:4px 12px;border-radius:12px;font-size:14px;'>
                {str(p[3]).upper() if p[3] else 'NO STATUS'}
            </span>
            <p style='color:#666;margin-top:8px;'>Status: {p[2]}</p>
        </div>"""

    tasks_html = ""
    for t in tasks:
        priority_color = {"high": "#e74c3c", "medium": "#f39c12", "low": "#2ecc71"}.get(t[3], "#999")
        status_color = {"open": "#3498db", "complete": "#2ecc71", "overdue": "#e74c3c"}.get(t[4], "#999")
        tasks_html += f"""
        <tr>
            <td>{t[1]}</td>
            <td>{t[2] if t[2] else 'No date'}</td>
            <td><span style='color:{priority_color};font-weight:bold;'>{t[3] or '-'}</span></td>
            <td><span style='color:{status_color};font-weight:bold;'>{t[4] or '-'}</span></td>
            <td>{t[5] or 'No project'}</td>
        </tr>"""

    return f"""
    <html>
    <head>
        <title>PM Agent Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fa; }}
            .header {{ background: #1F3864; color: white; padding: 20px 40px; }}
            .header h1 {{ margin: 0; font-size: 24px; }}
            .container {{ padding: 30px 40px; }}
            .section-title {{ font-size: 20px; font-weight: bold; color: #1F3864; margin: 30px 0 15px; }}
            .cards {{ display: flex; gap: 20px; flex-wrap: wrap; }}
            .card {{ background: white; border-radius: 10px; padding: 20px; min-width: 200px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            .card h3 {{ margin: 0 0 10px; color: #1F3864; }}
            table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            th {{ background: #2E5090; color: white; padding: 12px 16px; text-align: left; font-size: 14px; }}
            td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: #f9f9f9; }}
        </style>
    </head>
    <body>
        <div class='header'>
            <h1>PM Agent Dashboard — Miami Coastline Management</h1>
        </div>
        <div class='container'>
            <div class='section-title'>Projects</div>
            <div class='cards'>{projects_html}</div>
            <div class='section-title'>All Tasks</div>
            <table>
                <tr>
                    <th>Task</th>
                    <th>Due Date</th>
                    <th>Priority</th>
                    <th>Status</th>
                    <th>Project</th>
                </tr>
                {tasks_html}
            </table>
        </div>
    </body>
    </html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)