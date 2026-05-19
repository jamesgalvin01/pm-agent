from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from db import get_connection
import uvicorn

app = FastAPI()

@app.post("/complete/{task_id}")
def complete_task(task_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'complete' WHERE id = %s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/reopen/{task_id}")
def reopen_task(task_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'open' WHERE id = %s", (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return RedirectResponse(url="/", status_code=303)

@app.get("/", response_class=HTMLResponse)
def home(filter: str = "open"):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, name, status, rag_status FROM projects")
    projects = cur.fetchall()

    if filter == "all":
        cur.execute("""
            SELECT t.id, t.description, t.due_date, t.priority, t.status, p.name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            ORDER BY t.status ASC, t.due_date ASC NULLS LAST
        """)
    else:
        cur.execute("""
            SELECT t.id, t.description, t.due_date, t.priority, t.status, p.name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = %s
            ORDER BY t.due_date ASC NULLS LAST
        """, (filter,))

    tasks = cur.fetchall()

    cur.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'")
    open_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM tasks WHERE status = 'complete'")
    complete_count = cur.fetchone()[0]

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
        is_complete = t[4] == "complete"
        row_style = "background:#f9fff9;" if is_complete else ""
        action_btn = f"""
            <form method='post' action='/reopen/{t[0]}' style='display:inline'>
                <button type='submit' style='background:#95a5a6;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;'>
                    Reopen
                </button>
            </form>""" if is_complete else f"""
            <form method='post' action='/complete/{t[0]}' style='display:inline'>
                <button type='submit' style='background:#2ecc71;color:white;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;'>
                    ✓ Done
                </button>
            </form>"""

        task_text = f"<s style='color:#999'>{t[1]}</s>" if is_complete else t[1]
        tasks_html += f"""
        <tr style='{row_style}'>
            <td>{task_text}</td>
            <td>{t[2] if t[2] else '—'}</td>
            <td><span style='color:{priority_color};font-weight:bold;'>{t[3] or '—'}</span></td>
            <td>{t[5] or 'No project'}</td>
            <td>{action_btn}</td>
        </tr>"""

    active_style = "background:#1F3864;color:white;"
    all_style = ""
    complete_style = ""
    if filter == "all":
        all_style = "background:#1F3864;color:white;"
        active_style = ""
    elif filter == "complete":
        complete_style = "background:#1F3864;color:white;"
        active_style = ""

    return f"""
    <html>
    <head>
        <title>Rowan Dashboard</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; background: #f5f7fa; }}
            .header {{ background: #1F3864; color: white; padding: 20px 40px; display:flex; justify-content:space-between; align-items:center; }}
            .header h1 {{ margin: 0; font-size: 24px; }}
            .container {{ padding: 30px 40px; }}
            .section-title {{ font-size: 20px; font-weight: bold; color: #1F3864; margin: 30px 0 15px; }}
            .cards {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 10px; }}
            .card {{ background: white; border-radius: 10px; padding: 20px; min-width: 200px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            .card h3 {{ margin: 0 0 10px; color: #1F3864; }}
            .stat-card {{ background: white; border-radius: 10px; padding: 20px 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); text-align:center; }}
            .stat-card .number {{ font-size: 36px; font-weight: bold; color: #1F3864; }}
            .stat-card .label {{ color: #666; font-size: 14px; }}
            table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
            th {{ background: #2E5090; color: white; padding: 12px 16px; text-align: left; font-size: 14px; }}
            td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
            tr:last-child td {{ border-bottom: none; }}
            .filter-btn {{ padding: 8px 20px; border-radius: 20px; border: 2px solid #1F3864; background: white; color: #1F3864; cursor: pointer; font-size: 14px; text-decoration: none; }}
            .filters {{ display: flex; gap: 10px; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class='header'>
            <h1>🤖 Rowan — Miami Coastline Management</h1>
        </div>
        <div class='container'>
            <div class='section-title'>Projects</div>
            <div class='cards'>
                {projects_html}
                <div class='stat-card'>
                    <div class='number'>{open_count}</div>
                    <div class='label'>Open Tasks</div>
                </div>
                <div class='stat-card'>
                    <div class='number'>{complete_count}</div>
                    <div class='label'>Completed</div>
                </div>
            </div>
            <div class='section-title'>Tasks</div>
            <div class='filters'>
                <a href='/?filter=open' class='filter-btn' style='{active_style}'>Open</a>
                <a href='/?filter=complete' class='filter-btn' style='{complete_style}'>Completed</a>
                <a href='/?filter=all' class='filter-btn' style='{all_style}'>All</a>
            </div>
            <table>
                <tr>
                    <th>Task</th>
                    <th>Due Date</th>
                    <th>Priority</th>
                    <th>Project</th>
                    <th>Action</th>
                </tr>
                {tasks_html}
            </table>
        </div>
    </body>
    </html>"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)