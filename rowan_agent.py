"""
rowan_agent.py — Rowan's brain.

Holds:
- The system prompt that defines Rowan's character and operating rules
- Tool schemas (what tools Claude can call)
- Tool implementations (Python that actually runs against Supabase)
- run_agent_turn(): the loop that takes user input, calls Claude, runs tools, returns Rowan's reply

Two write modes for the safety pattern:
- "propose": Claude must describe a write tool call as a proposal and wait for the user
  to confirm. Used for the first attempt of any write tool call in a turn.
- "execute": Claude is permitted to run write tools without further confirmation. Used when
  the user has explicitly approved a previously-proposed action.

The dispatcher enforces this. If Claude tries to execute a write tool without prior
confirmation in this conversation, the dispatcher returns a tool_result asking it to
propose the action first.
"""
import json
import os
from datetime import date, datetime
from typing import Any, Optional

import anthropic
from dotenv import load_dotenv

from db import get_connection

load_dotenv()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048
MAX_TOOL_ROUNDS = 8
CONTEXT_MESSAGE_LIMIT = 30

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ============================================================
# SYSTEM PROMPT
# ============================================================

SYSTEM_PROMPT = """You are Rowan, James Galvin's AI project manager at Miami Coastline Management (MCM).

# Who you are
- A capable, no-nonsense PM. You speak directly and skip pleasantries.
- You refer to yourself as "I". You refer to the user as "James" or "you".
- You see MCM's project data, leads, project notes and filed photos/documents, and James's Outlook calendar, through the tools listed below. You don't have access to email or the outside world unless a tool exposes it.

# How you communicate
- Be concise. One or two short paragraphs, or a tight list. Never long preambles.
- No filler ("Of course! I'd be happy to..."). Get straight to the answer.
- When you don't have enough info, ASK a single clarifying question instead of guessing. Never invent details that aren't in the data.
- If you notice something concerning that's adjacent to what was asked (overdue task, red RAG, blocked dependency), surface it briefly — but don't go off on tangents.

# How you use tools
You have read tools (safe, run them whenever useful) and write tools (change the database).

For READ tools (list_open_tasks, list_projects, get_project_details, lookup_person, list_people, list_leads, list_project_files, list_calendar_events, find_free_time):
- Just call them when they help answer the question. No need to ask permission.

For WRITE tools (mark_task_complete, reopen_task, create_task, add_risk, create_person, update_person, create_project, update_project, create_lead, add_project_note, create_calendar_event, update_calendar_event, delete_calendar_event):
- You must ALWAYS propose first, then wait for the user to confirm before executing.
- To propose, describe in plain text what you intend to do and ASK for confirmation. Be specific (task IDs, exact text, due dates).
- Do NOT call the write tool on the same turn as the proposal.
- Only call the write tool after the user has clearly agreed (e.g. "yes", "do it", "go ahead", "confirmed"). If their reply is ambiguous, ask again.
- After execution, give a one-line confirmation of what changed.

# Calendar
- All times are Eastern. Resolve "tomorrow", "Thursday", "next week" against today's date given below.
- Before proposing a new event, check the calendar for conflicts at that time and mention any.
- An event with attendees sends them real invitations. When proposing one, list every attendee email. Never guess an email address: look the person up, or ask.
- To move or cancel an event, find it with list_calendar_events first and use its event_id.

# Projects
- Before creating a project, check list_projects so you don't add a duplicate under a slightly different name.
- Use the project name exactly as James gives it. When he lists several, propose them all in one numbered proposal and create them all after one confirmation.

# Notes, leads and files
- Decisions, site observations and anything worth remembering that is not an action item go in add_project_note, not create_task.
- New prospects go in create_lead (stage 'New' unless James says otherwise).
- Photos and documents James sends are filed to OneDrive automatically; list_project_files finds them later.

# Data conventions
- Dates use ISO format (YYYY-MM-DD).
- Priority values are 'high', 'medium', 'low'.
- Task statuses are 'open' or 'complete'.
- Project RAG is 'green', 'amber', or 'red'.

# Style examples
User: "What's open this week?"
You: [call list_open_tasks with a date window, then reply]
"Five open tasks due by Sunday:
1. ...
2. ...
Heads up — #47 (Grotto plumbing inspection) is already 3 days overdue."

User: "Mark task 47 as done."
You: "I'll mark task 47 (Grotto plumbing inspection) as complete. Confirm?"
User: "yes"
You: [call mark_task_complete with task_ids=[47]] "Done — task 47 is now complete."

User: "Close the drywall submittal, the OCIP email, and the LEED task."
You: [if needed, call list_open_tasks to resolve the descriptions to IDs] "I'll mark these 3 tasks complete: 12 (drywall submittal), 15 (OCIP email), 23 (LEED plan date). Confirm?"
User: "yes"
You: [call mark_task_complete with task_ids=[12,15,23]] "Done — closed 3 tasks: 12, 15, 23."
"""


# ============================================================
# TOOL SCHEMAS (what Claude sees)
# ============================================================

TOOLS = [
    # ---------- READ TOOLS ----------
    {
        "name": "list_open_tasks",
        "description": "List open tasks across all projects. Optionally filter by project name, assignee name, or a due-date window. Returns task id, description, due date, priority, project, and assignee.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Filter to tasks on a specific project. Substring match, case-insensitive."},
                "assignee_name": {"type": "string", "description": "Filter to tasks assigned to a specific person. Substring match, case-insensitive."},
                "due_before": {"type": "string", "description": "Only tasks with due_date on or before this YYYY-MM-DD."},
                "due_after": {"type": "string", "description": "Only tasks with due_date on or after this YYYY-MM-DD."},
                "limit": {"type": "integer", "description": "Max number of tasks to return (default 50)."},
            },
        },
    },
    {
        "name": "list_projects",
        "description": "List all projects with their RAG status, status, owner, and open/complete task counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_project_details",
        "description": "Get detailed info on one project: its milestones, open tasks, risks, and metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Project name (substring match, case-insensitive). Required."},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "lookup_person",
        "description": "Find a person by name. Returns their details plus a summary of what they're assigned to (open task count).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Person's name (substring match, case-insensitive). Required."},
            },
            "required": ["name"],
        },
    },
  {
        "name": "list_people",
        "description": "List all people in the team/collaborator directory. Returns each person's id, name, email, role, and open task count. Use this when the user asks who is on the team or who can be assigned tasks.",
        "input_schema": {"type": "object", "properties": {}},
    },

    # ---------- READ TOOLS (leads, notes/files, calendar) ----------
    {
        "name": "list_leads",
        "description": "List leads in the business-development pipeline, newest activity first. Optionally filter by stage or a name/contact substring.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["New", "Contacted", "Qualified", "Proposal", "Won", "Lost"]},
                "search": {"type": "string", "description": "Substring of lead name, contact, source or notes. Case-insensitive."},
                "limit": {"type": "integer", "description": "Default 25."},
            },
        },
    },
    {
        "name": "list_project_files",
        "description": "Photos and documents James has sent Rowan, filed in OneDrive. Returns file name, project, what it shows/contains, OneDrive link and date. Optionally filter by project, kind, or a text search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Substring match."},
                "kind": {"type": "string", "enum": ["photo", "document"]},
                "search": {"type": "string", "description": "Substring of the file name or description."},
                "days": {"type": "integer", "description": "Only files from the last N days."},
                "limit": {"type": "integer", "description": "Default 20."},
            },
        },
    },
    {
        "name": "list_calendar_events",
        "description": "James's Outlook calendar events between two dates (inclusive), Eastern time. Returns event_id, subject, start, end, location, attendees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD. Required."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to start_date."},
            },
            "required": ["start_date"],
        },
    },
    {
        "name": "find_free_time",
        "description": "Open blocks on James's own calendar (weekdays, 8am-6pm Eastern unless overridden) that are at least duration_minutes long. It cannot see other people's calendars.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD. Required."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD. Defaults to start_date."},
                "duration_minutes": {"type": "integer", "description": "Default 60."},
                "day_start": {"type": "string", "description": "HH:MM, default 08:00."},
                "day_end": {"type": "string", "description": "HH:MM, default 18:00."},
            },
            "required": ["start_date"],
        },
    },

    # ---------- WRITE TOOLS ----------
{
        "name": "mark_task_complete",
        "description": "Mark one or more tasks as complete. Pass a list of task IDs — one ID to close a single task, or several to close multiple at once. REQUIRES prior user confirmation in this conversation — do not call this on the same turn you propose it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of task IDs to mark complete. For a single task, pass a list with one ID.",
                },
            },
            "required": ["task_ids"],
        },
    },
    {
        "name": "reopen_task",
        "description": "Reopen a completed task (sets status back to 'open'). REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "The task ID."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_task",
        "description": "Create a new task. REQUIRES prior user confirmation in this conversation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Task description. Required."},
                "project_name": {"type": "string", "description": "Project to attach the task to. Substring match. Optional."},
                "assignee_name": {"type": "string", "description": "Person to assign to. Substring match. Optional."},
                "due_date": {"type": "string", "description": "Due date YYYY-MM-DD. Optional."},
                "priority": {"type": "string", "enum": ["high", "medium", "low"], "description": "Default 'medium'."},
            },
            "required": ["description"],
        },
    },
    {
        "name": "add_risk",
        "description": "Log a risk against a project. REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Project name (substring match). Required."},
                "description": {"type": "string", "description": "What the risk is. Required."},
                "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
                "impact": {"type": "string", "enum": ["low", "medium", "high"]},
                "mitigation": {"type": "string", "description": "Planned mitigation (optional)."},
            },
            "required": ["project_name", "description"],
        },
    },
    {
        "name": "create_person",
        "description": "Add a new person to the team directory. REQUIRES prior user confirmation. Use this when James wants to add someone he works with so they can be assigned tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":  {"type": "string", "description": "Full name. Required."},
                "email": {"type": "string", "description": "Email address. Required (the schema enforces uniqueness)."},
                "role":  {"type": "string", "description": "Job title or role (e.g., 'Foreman', 'Project Manager'). Optional."},
            },
            "required": ["name", "email"],
        },
    },
    {
        "name": "update_person",
        "description": "Update an existing person's name, email, or role. REQUIRES prior user confirmation. Look the person up by id (preferred) or by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "person_id":  {"type": "integer", "description": "Person id. Use this if you have it."},
                "match_name": {"type": "string",  "description": "Substring of the person's name to match. Used only when person_id is not given. Must match exactly one person."},
                "name":  {"type": "string", "description": "New name. Optional."},
                "email": {"type": "string", "description": "New email. Optional."},
                "role":  {"type": "string", "description": "New role. Optional."},
            },
        },
    },
    {
        "name": "create_project",
        "description": "Add a new project. REQUIRES prior user confirmation. Check list_projects first to avoid duplicates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name exactly as James wants it shown. Required."},
                "goal": {"type": "string", "description": "One-line description of the project / MCM's role."},
                "status": {"type": "string", "description": "Default 'active'. Other values James uses: 'on hold', 'complete'."},
                "rag_status": {"type": "string", "enum": ["green", "amber", "red"], "description": "Default 'green'."},
                "owner_name": {"type": "string", "description": "Person who owns it (substring match against people). Optional."},
                "start_date": {"type": "string", "description": "YYYY-MM-DD. Optional."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD. Optional."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_project",
        "description": "Change an existing project's name, description, status, RAG, owner or dates. REQUIRES prior user confirmation. Identify it by project_id (preferred) or match_name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "match_name": {"type": "string", "description": "Substring of the current name. Must match exactly one project."},
                "name": {"type": "string", "description": "New name."},
                "goal": {"type": "string"},
                "status": {"type": "string"},
                "rag_status": {"type": "string", "enum": ["green", "amber", "red"]},
                "owner_name": {"type": "string"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "YYYY-MM-DD."},
            },
        },
    },
    {
        "name": "create_lead",
        "description": "Add a lead to the business-development pipeline. REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The opportunity: company, developer or project (e.g. 'Key Largo spec home'). Required."},
                "contact": {"type": "string", "description": "Contact person and/or email/phone."},
                "value": {"type": "number", "description": "Estimated fee value in dollars, if James gave one."},
                "status": {"type": "string", "enum": ["New", "Contacted", "Qualified", "Proposal", "Won", "Lost"], "description": "Default 'New'."},
                "source": {"type": "string", "description": "How the lead came in (referral, DemandStar, LinkedIn...)."},
                "notes": {"type": "string", "description": "Anything else worth keeping."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_project_note",
        "description": "Log a note against a project: a decision, a site observation, something someone said. Not for action items (use create_task). REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Substring match. Omit for a general note."},
                "note": {"type": "string", "description": "The note. Required."},
            },
            "required": ["note"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create an event on James's Outlook calendar. Attendees receive real invitations. REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Required."},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM Eastern. Required."},
                "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM Eastern. Or give duration_minutes."},
                "duration_minutes": {"type": "integer", "description": "Used when end is not given. Default 60."},
                "location": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Email addresses to invite. Omit for a block on James's calendar only."},
                "body": {"type": "string", "description": "Event description."},
                "online_meeting": {"type": "boolean", "description": "Add a Teams link."},
                "show_as": {"type": "string", "enum": ["busy", "tentative", "free", "oof"], "description": "Default busy."},
            },
            "required": ["subject", "start"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": "Move or rename an existing event (find its event_id with list_calendar_events). If only start is given, the event keeps its length. Attendees are notified of changes. REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Required."},
                "subject": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM Eastern."},
                "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM Eastern."},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Delete an event from James's calendar. If he organized it with attendees, they get a cancellation. REQUIRES prior user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string", "description": "Required."}},
            "required": ["event_id"],
        },
    },
]


WRITE_TOOLS = {"mark_task_complete", "reopen_task", "create_task", "add_risk", "create_person", "update_person",
               "create_project", "update_project",
               "create_lead", "add_project_note", "create_calendar_event", "update_calendar_event",
               "delete_calendar_event"}


# ============================================================
# TOOL IMPLEMENTATIONS
# ============================================================

def _row_to_dict(cur, row):
    cols = [c.name for c in cur.description]
    return dict(zip(cols, row))


def _serialize(obj):
    """Make dates/datetimes JSON-serializable."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _clean(d):
    return {k: _serialize(v) for k, v in d.items()}


def tool_list_open_tasks(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        SELECT t.id, t.description, t.due_date, t.priority, t.status,
               p.name AS project, pe.name AS assignee
          FROM tasks t
          LEFT JOIN projects p  ON t.project_id  = p.id
          LEFT JOIN people  pe ON t.assignee_id = pe.id
         WHERE t.status = 'open'
    """
    params = []
    if args.get("project_name"):
        sql += " AND LOWER(p.name) LIKE %s"
        params.append(f"%{args['project_name'].lower()}%")
    if args.get("assignee_name"):
        sql += " AND LOWER(pe.name) LIKE %s"
        params.append(f"%{args['assignee_name'].lower()}%")
    if args.get("due_before"):
        sql += " AND t.due_date <= %s"
        params.append(args["due_before"])
    if args.get("due_after"):
        sql += " AND t.due_date >= %s"
        params.append(args["due_after"])
    sql += " ORDER BY t.due_date ASC NULLS LAST, t.priority DESC"
    limit = args.get("limit") or 50
    sql += f" LIMIT {int(limit)}"

    cur.execute(sql, params)
    rows = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"count": len(rows), "tasks": rows}


def tool_list_projects(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.status, p.rag_status, p.start_date, p.end_date,
               pe.name AS owner,
               (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'open')      AS open_tasks,
               (SELECT COUNT(*) FROM tasks WHERE project_id = p.id AND status = 'complete')  AS complete_tasks
          FROM projects p
          LEFT JOIN people pe ON p.owner_id = pe.id
         ORDER BY p.name
    """)
    rows = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"count": len(rows), "projects": rows}


def tool_get_project_details(args: dict) -> dict:
    name = args["project_name"]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.goal, p.status, p.rag_status, p.start_date, p.end_date,
               pe.name AS owner
          FROM projects p
          LEFT JOIN people pe ON p.owner_id = pe.id
         WHERE LOWER(p.name) LIKE %s
         LIMIT 1
    """, (f"%{name.lower()}%",))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return {"error": f"No project matching '{name}'"}
    project = _clean(_row_to_dict(cur, row))
    pid = project["id"]

    cur.execute("SELECT id, name, due_date, status FROM milestones WHERE project_id = %s ORDER BY due_date NULLS LAST", (pid,))
    milestones = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]

    cur.execute("""
        SELECT t.id, t.description, t.due_date, t.priority, pe.name AS assignee
          FROM tasks t
          LEFT JOIN people pe ON t.assignee_id = pe.id
         WHERE t.project_id = %s AND t.status = 'open'
         ORDER BY t.due_date ASC NULLS LAST
    """, (pid,))
    open_tasks = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]

    cur.execute("SELECT id, description, likelihood, impact, mitigation, status FROM risks WHERE project_id = %s AND status = 'open'", (pid,))
    risks = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]

    ensure_extra_schema()
    cur.execute("SELECT note, created_at FROM project_notes WHERE project_id = %s ORDER BY created_at DESC LIMIT 10", (pid,))
    notes = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.execute("SELECT kind, file_name, description, web_url, created_at FROM project_files WHERE project_id = %s ORDER BY created_at DESC LIMIT 10", (pid,))
    files = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return {"project": project, "milestones": milestones, "open_tasks": open_tasks, "risks": risks,
            "recent_notes": notes, "recent_files": files}


def tool_lookup_person(args: dict) -> dict:
    name = args["name"]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, email, role FROM people WHERE LOWER(name) LIKE %s LIMIT 5",
        (f"%{name.lower()}%",),
    )
    matches = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    for p in matches:
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee_id = %s AND status = 'open'",
            (p["id"],),
        )
        p["open_task_count"] = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {"count": len(matches), "matches": matches}

def tool_list_people(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id, p.name, p.email, p.role, "
        "(SELECT COUNT(*) FROM tasks WHERE assignee_id = p.id AND status = 'open') AS open_task_count "
        "FROM people p ORDER BY p.name"
    )
    rows = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"count": len(rows), "people": rows}


def tool_mark_task_complete(args: dict) -> dict:
    task_ids = args.get("task_ids") or []
    if not task_ids:
        return {"error": "No task_ids provided."}
    conn = get_connection()
    cur = conn.cursor()
    completed = []
    not_found = []
    for tid in task_ids:
        cur.execute("UPDATE tasks SET status = 'complete' WHERE id = %s RETURNING id, description", (tid,))
        row = cur.fetchone()
        if row:
            completed.append({"task_id": row[0], "description": row[1]})
        else:
            not_found.append(tid)
    conn.commit()
    cur.close()
    conn.close()
    return {
        "ok": True,
        "completed_count": len(completed),
        "completed": completed,
        "not_found": not_found,
    }


def tool_reopen_task(args: dict) -> dict:
    task_id = args["task_id"]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'open' WHERE id = %s RETURNING id, description", (task_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return {"error": f"No task with id {task_id}"}
    return {"ok": True, "task_id": row[0], "description": row[1]}


def _resolve_project_id(cur, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    cur.execute("SELECT id FROM projects WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{name.lower()}%",))
    r = cur.fetchone()
    return r[0] if r else None


def _resolve_person_id(cur, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    cur.execute("SELECT id FROM people WHERE LOWER(name) LIKE %s LIMIT 1", (f"%{name.lower()}%",))
    r = cur.fetchone()
    return r[0] if r else None


def tool_create_task(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    project_id = _resolve_project_id(cur, args.get("project_name"))
    assignee_id = _resolve_person_id(cur, args.get("assignee_name"))
    cur.execute(
        """
        INSERT INTO tasks (description, project_id, assignee_id, due_date, priority, status, source)
        VALUES (%s, %s, %s, %s, %s, 'open', 'rowan_chat')
        RETURNING id
        """,
        (
            args["description"],
            project_id,
            assignee_id,
            args.get("due_date"),
            args.get("priority", "medium"),
        ),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {
        "ok": True,
        "task_id": new_id,
        "description": args["description"],
        "project_id": project_id,
        "assignee_id": assignee_id,
    }


def tool_add_risk(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    project_id = _resolve_project_id(cur, args["project_name"])
    if not project_id:
        cur.close()
        conn.close()
        return {"error": f"No project matching '{args['project_name']}'"}
    cur.execute(
        """
        INSERT INTO risks (project_id, description, likelihood, impact, mitigation, status)
        VALUES (%s, %s, %s, %s, %s, 'open')
        RETURNING id
        """,
        (
            project_id,
            args["description"],
            args.get("likelihood"),
            args.get("impact"),
            args.get("mitigation"),
        ),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "risk_id": new_id, "project_id": project_id}

def tool_create_person(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO people (name, email, role) VALUES (%s, %s, %s) RETURNING id",
            (args["name"], args["email"], args.get("role")),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"error": f"Could not create person: {e}"}
    cur.close()
    conn.close()
    return {"ok": True, "person_id": new_id, "name": args["name"], "email": args["email"]}


def tool_update_person(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()

    # Resolve target
    person_id = args.get("person_id")
    if not person_id:
        match_name = args.get("match_name")
        if not match_name:
            cur.close()
            conn.close()
            return {"error": "Provide either person_id or match_name."}
        cur.execute(
            "SELECT id, name FROM people WHERE LOWER(name) LIKE %s LIMIT 2",
            (f"%{match_name.lower()}%",),
        )
        rows = cur.fetchall()
        if not rows:
            cur.close()
            conn.close()
            return {"error": f"No person matching '{match_name}'"}
        if len(rows) > 1:
            cur.close()
            conn.close()
            return {"error": f"'{match_name}' matched multiple people; use person_id to disambiguate."}
        person_id = rows[0][0]

    # Build dynamic UPDATE
    fields, params = [], []
    for col in ("name", "email", "role"):
        if args.get(col) is not None:
            fields.append(f"{col} = %s")
            params.append(args[col])
    if not fields:
        cur.close()
        conn.close()
        return {"error": "Nothing to update — provide at least one of name/email/role."}
    params.append(person_id)

    try:
        cur.execute(
            f"UPDATE people SET {', '.join(fields)} WHERE id = %s RETURNING id, name, email, role",
            params,
        )
        row = cur.fetchone()
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"error": f"Could not update person: {e}"}
    cur.close()
    conn.close()
    if not row:
        return {"error": f"No person with id {person_id}"}
    return {"ok": True, "person": {"id": row[0], "name": row[1], "email": row[2], "role": row[3]}}


def _clean_date(value):
    """'' -> None, so an empty string never reaches a DATE column."""
    value = (value or "").strip() if isinstance(value, str) else value
    return value or None


def tool_create_project(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "A project name is required."}
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM projects WHERE LOWER(TRIM(name)) = LOWER(%s)", (name,))
    dup = cur.fetchone()
    if dup:
        cur.close()
        conn.close()
        return {"error": f"A project named '{dup[1]}' already exists (id {dup[0]}). Nothing created."}
    owner_id = _resolve_person_id(cur, args.get("owner_name"))
    try:
        cur.execute(
            """INSERT INTO projects (name, goal, status, rag_status, owner_id, start_date, end_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (name, args.get("goal"), args.get("status") or "active", args.get("rag_status") or "green",
             owner_id, _clean_date(args.get("start_date")), _clean_date(args.get("end_date"))),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"error": f"Could not create project: {e}"}
    cur.close()
    conn.close()
    return {"ok": True, "project_id": new_id, "name": name, "owner_id": owner_id}


def tool_update_project(args: dict) -> dict:
    conn = get_connection()
    cur = conn.cursor()
    project_id = args.get("project_id")
    if not project_id:
        match = (args.get("match_name") or "").strip()
        if not match:
            cur.close()
            conn.close()
            return {"error": "Provide project_id or match_name."}
        cur.execute("SELECT id, name FROM projects WHERE LOWER(name) LIKE %s LIMIT 2", (f"%{match.lower()}%",))
        rows = cur.fetchall()
        if len(rows) != 1:
            cur.close()
            conn.close()
            return {"error": f"'{match}' matched {len(rows)} projects; use project_id."}
        project_id = rows[0][0]

    fields, params = [], []
    for col in ("name", "goal", "status", "rag_status"):
        if args.get(col):
            fields.append(f"{col} = %s")
            params.append(args[col].strip())
    for col in ("start_date", "end_date"):
        if col in args and args[col] is not None:
            fields.append(f"{col} = %s")
            params.append(_clean_date(args[col]))
    if args.get("owner_name"):
        owner_id = _resolve_person_id(cur, args["owner_name"])
        if not owner_id:
            cur.close()
            conn.close()
            return {"error": f"No person matching '{args['owner_name']}'"}
        fields.append("owner_id = %s")
        params.append(owner_id)
    if not fields:
        cur.close()
        conn.close()
        return {"error": "Nothing to update."}
    params.append(project_id)
    try:
        cur.execute(
            f"UPDATE projects SET {', '.join(fields)} WHERE id = %s RETURNING id, name, status, rag_status",
            params,
        )
        row = cur.fetchone()
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return {"error": f"Could not update project: {e}"}
    cur.close()
    conn.close()
    if not row:
        return {"error": f"No project with id {project_id}"}
    return {"ok": True, "project": {"id": row[0], "name": row[1], "status": row[2], "rag_status": row[3]}}


# ------------------------------------------------------------
# Notes, files, leads (tables created on first use)
# ------------------------------------------------------------

_schema_ready = False


def ensure_extra_schema() -> None:
    """Tables the Telegram features use. Safe to call repeatedly."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_notes (
            id SERIAL PRIMARY KEY,
            project_id INT,
            note TEXT NOT NULL,
            source TEXT DEFAULT 'rowan_chat',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_files (
            id SERIAL PRIMARY KEY,
            project_id INT,
            kind TEXT NOT NULL,
            file_name TEXT,
            onedrive_path TEXT,
            web_url TEXT,
            caption TEXT,
            description TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT")
    conn.commit()
    cur.close()
    conn.close()
    _schema_ready = True


def tool_list_leads(args: dict) -> dict:
    ensure_extra_schema()
    conn = get_connection()
    cur = conn.cursor()
    sql = "SELECT id, name, contact, value, status, source, notes, updated_at FROM leads WHERE TRUE"
    params = []
    if args.get("status"):
        sql += " AND status = %s"
        params.append(args["status"])
    if args.get("search"):
        sql += (" AND (LOWER(name) LIKE %s OR LOWER(COALESCE(contact,'')) LIKE %s"
                " OR LOWER(COALESCE(source,'')) LIKE %s OR LOWER(COALESCE(notes,'')) LIKE %s)")
        params += [f"%{args['search'].lower()}%"] * 4
    sql += f" ORDER BY updated_at DESC NULLS LAST LIMIT {int(args.get('limit') or 25)}"
    cur.execute(sql, params)
    rows = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        if r.get("value") is not None:
            r["value"] = float(r["value"])
    return {"count": len(rows), "leads": rows}


def tool_create_lead(args: dict) -> dict:
    ensure_extra_schema()
    status = args.get("status") or "New"
    if status not in ("New", "Contacted", "Qualified", "Proposal", "Won", "Lost"):
        status = "New"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO leads (name, contact, value, status, source, notes)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (args["name"].strip(), args.get("contact"), args.get("value") or 0, status,
         args.get("source") or "Telegram", args.get("notes")),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "lead_id": new_id, "name": args["name"], "status": status}


def tool_add_project_note(args: dict) -> dict:
    ensure_extra_schema()
    conn = get_connection()
    cur = conn.cursor()
    project_id = _resolve_project_id(cur, args.get("project_name"))
    if args.get("project_name") and not project_id:
        cur.close()
        conn.close()
        return {"error": f"No project matching '{args['project_name']}'"}
    cur.execute(
        "INSERT INTO project_notes (project_id, note, source) VALUES (%s, %s, %s) RETURNING id",
        (project_id, args["note"], args.get("source") or "rowan_chat"),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "note_id": new_id, "project_id": project_id}


def tool_list_project_files(args: dict) -> dict:
    ensure_extra_schema()
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        SELECT f.id, f.kind, f.file_name, p.name AS project, f.caption, f.description,
               f.web_url, f.onedrive_path, f.created_at
          FROM project_files f
          LEFT JOIN projects p ON f.project_id = p.id
         WHERE TRUE
    """
    params = []
    if args.get("project_name"):
        sql += " AND LOWER(p.name) LIKE %s"
        params.append(f"%{args['project_name'].lower()}%")
    if args.get("kind"):
        sql += " AND f.kind = %s"
        params.append(args["kind"])
    if args.get("search"):
        sql += (" AND (LOWER(COALESCE(f.file_name,'')) LIKE %s OR LOWER(COALESCE(f.description,'')) LIKE %s"
                " OR LOWER(COALESCE(f.caption,'')) LIKE %s)")
        params += [f"%{args['search'].lower()}%"] * 3
    if args.get("days"):
        sql += " AND f.created_at >= NOW() - (%s * INTERVAL '1 day')"
        params.append(int(args["days"]))
    sql += f" ORDER BY f.created_at DESC LIMIT {int(args.get('limit') or 20)}"
    cur.execute(sql, params)
    rows = [_clean(_row_to_dict(cur, r)) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return {"count": len(rows), "files": rows}


def record_project_file(project_name, kind, file_name, onedrive_path, web_url,
                        caption=None, description=None) -> int:
    """Called by the Telegram media handler after a successful upload."""
    ensure_extra_schema()
    conn = get_connection()
    cur = conn.cursor()
    project_id = _resolve_project_id(cur, project_name)
    cur.execute(
        """INSERT INTO project_files (project_id, kind, file_name, onedrive_path, web_url, caption, description)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (project_id, kind, file_name, onedrive_path, web_url, caption, description),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def project_names() -> list:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT name FROM projects ORDER BY name")
    names = [r[0] for r in cur.fetchall() if r[0]]
    cur.close()
    conn.close()
    return names


# ------------------------------------------------------------
# Calendar
# ------------------------------------------------------------

def tool_list_calendar_events(args: dict) -> dict:
    from outlook_calendar import list_events
    events = list_events(args["start_date"], args.get("end_date"))
    return {"count": len(events), "events": events}


def tool_find_free_time(args: dict) -> dict:
    from outlook_calendar import find_free_time
    slots = find_free_time(args["start_date"], args.get("end_date"),
                           args.get("duration_minutes") or 60,
                           args.get("day_start"), args.get("day_end"))
    return {"count": len(slots), "open_blocks": slots}


def tool_create_calendar_event(args: dict) -> dict:
    from outlook_calendar import create_event
    return {"ok": True, "event": create_event(
        subject=args["subject"], start=args["start"], end=args.get("end"),
        duration_minutes=args.get("duration_minutes"), location=args.get("location"),
        attendees=args.get("attendees"), body=args.get("body"),
        online_meeting=bool(args.get("online_meeting")), show_as=args.get("show_as"),
    )}


def tool_update_calendar_event(args: dict) -> dict:
    from outlook_calendar import update_event
    return {"ok": True, "event": update_event(
        args["event_id"], subject=args.get("subject"), start=args.get("start"),
        end=args.get("end"), location=args.get("location"),
    )}


def tool_delete_calendar_event(args: dict) -> dict:
    from outlook_calendar import delete_event
    delete_event(args["event_id"])
    return {"ok": True, "deleted": args["event_id"]}


TOOL_DISPATCH = {
    "list_open_tasks":     tool_list_open_tasks,
    "list_projects":       tool_list_projects,
    "get_project_details": tool_get_project_details,
    "lookup_person":       tool_lookup_person,
    "list_people":         tool_list_people,
    "mark_task_complete":  tool_mark_task_complete,
    "reopen_task":         tool_reopen_task,
    "create_task":         tool_create_task,
    "add_risk":            tool_add_risk,
    "create_person":       tool_create_person,
    "update_person":       tool_update_person,
    "create_project":      tool_create_project,
    "update_project":      tool_update_project,
    "list_leads":          tool_list_leads,
    "create_lead":         tool_create_lead,
    "add_project_note":    tool_add_project_note,
    "list_project_files":  tool_list_project_files,
    "list_calendar_events":  tool_list_calendar_events,
    "find_free_time":        tool_find_free_time,
    "create_calendar_event": tool_create_calendar_event,
    "update_calendar_event": tool_update_calendar_event,
    "delete_calendar_event": tool_delete_calendar_event,
}


def _execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(args or {})
    except Exception as e:
        return {"error": f"Tool {name} failed: {e}"}


# ============================================================
# CONVERSATION HELPERS
# ============================================================

def _load_conversation_messages(conversation_id: int) -> list[dict]:
    """
    Load the last N messages from the DB and convert to Anthropic message format.
    Each row may be user/assistant/tool_result. tool_calls/tool_results JSONB carries the
    structured blocks; content carries the visible text.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content, tool_calls, tool_results
          FROM messages
         WHERE conversation_id = %s
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,
        (conversation_id, CONTEXT_MESSAGE_LIMIT),
    )
    rows = list(reversed(cur.fetchall()))
    cur.close()
    conn.close()

    # The window can open mid-exchange (on a tool_result, or an assistant turn).
    # The API needs the history to start on a plain user message.
    while rows and not (rows[0][0] == "user" and rows[0][1]):
        rows.pop(0)

    messages = []
    for role, content, tool_calls, tool_results in rows:
        if role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "assistant":
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            if tool_calls:
                blocks.extend(tool_calls)
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
        elif role == "tool_result":
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
    return messages


def _save_message(conversation_id: int, role: str, content: str = "",
                  tool_calls: Optional[list] = None, tool_results: Optional[list] = None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages (conversation_id, role, content, tool_calls, tool_results)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            conversation_id,
            role,
            content or "",
            json.dumps(tool_calls) if tool_calls is not None else None,
            json.dumps(tool_results) if tool_results is not None else None,
        ),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def save_exchange(conversation_id: int, user_text: str, assistant_text: str) -> int:
    """
    Record an exchange handled outside the agent loop (a photo or document
    Rowan reviewed) so follow-up questions have it in context. Returns the
    assistant message id.
    """
    _save_message(conversation_id, "user", content=user_text)
    return _save_message(conversation_id, "assistant", content=assistant_text)


# ============================================================
# CONFIRMATION GUARD
# ============================================================

AFFIRMATIVE_STARTS = (
    "yes", "y", "yep", "yeah", "yup", "confirm", "confirmed", "do it", "go ahead", "go",
    "ok", "okay", "k", "approve", "approved", "sure", "sounds good", "save", "save it",
    "please do", "correct", "proceed", "looks good", "lgtm", "send it", "book it",
    "all good", "perfect",
)


def is_affirmative(text: str) -> bool:
    t = "".join(ch for ch in (text or "").lower() if ch.isalnum() or ch.isspace()).strip()
    return any(t == w or t.startswith(w + " ") for w in AFFIRMATIVE_STARTS)


# ============================================================
# MAIN ENTRY POINT
# ============================================================

TELEGRAM_ADDENDUM = """
# Channel: Telegram
- James is on his phone. Plain text only: no markdown, no asterisks, no # headers. Keep it short.
- When you propose a write action, put the marker [CONFIRM] alone on the last line. James gets Confirm / Cancel buttons, and Confirm reaches you as "yes". Use [CONFIRM] only on proposals.
- A message starting with [Voice note] is dictation from the field, transcribed by machine, so expect misheard names. Pull out: (1) action items, proposed as tasks with project, assignee and due date where you can tell; (2) decisions and site observations, proposed as project notes. Put everything in ONE numbered proposal so one Confirm saves it all. If nothing is actionable, say so in a line. Use list_projects / list_people to match names.
- A message starting with "Quick capture" is a one-line entry James wants saved. Resolve project, person and date, then propose it in one compact line. Only ask a question if a required field is missing.
"""


def _system_prompt(channel: str) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    prompt = SYSTEM_PROMPT + (
        f"\n# Right now\nToday is {now.strftime('%A, %B %d, %Y')} ({now.date().isoformat()}); "
        f"the time is {now.strftime('%I:%M %p').lstrip('0')} Eastern.\n"
    )
    if channel == "telegram":
        prompt += TELEGRAM_ADDENDUM
    return prompt


def run_agent_turn(conversation_id: int, user_text: str, channel: str = "web") -> str:
    """
    Persist the user message, run Claude (with tool-use loop), persist all assistant
    output, and return the final visible text reply.
    """
    _save_message(conversation_id, "user", content=user_text)
    messages = _load_conversation_messages(conversation_id)
    system = _system_prompt(channel)
    # Write tools only run when James's message is a go-ahead; otherwise the
    # model gets told to propose first. This backs up the prompt rule.
    confirmed = is_affirmative(user_text)

    final_text = ""
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        text_parts = []
        tool_use_blocks = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_use_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        text_combined = "\n".join(text_parts).strip()

        if response.stop_reason == "tool_use" and tool_use_blocks:
            # Run each tool and append a tool_result block
            _save_message(
                conversation_id, "assistant",
                content=text_combined,
                tool_calls=tool_use_blocks,
            )
            messages.append({
                "role": "assistant",
                "content": ([{"type": "text", "text": text_combined}] if text_combined else []) + tool_use_blocks,
            })

            tool_result_blocks = []
            for tu in tool_use_blocks:
                if tu["name"] in WRITE_TOOLS and not confirmed:
                    result = {"error": "Not executed: James has not confirmed this yet. "
                                       "Describe exactly what you will do and ask him to confirm."}
                else:
                    result = _execute_tool(tu["name"], tu.get("input") or {})
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps(result),
                })
            _save_message(conversation_id, "tool_result", tool_results=tool_result_blocks)
            messages.append({"role": "user", "content": tool_result_blocks})
            continue  # ask Claude again with the tool results

        # Final assistant reply (no more tool use)
        final_text = text_combined or "(no response)"
        _save_message(conversation_id, "assistant", content=final_text)
        return final_text

    # Hit max rounds — save what we have
    final_text = "I got stuck in a loop calling tools. Let's try a smaller question."
    _save_message(conversation_id, "assistant", content=final_text)
    return final_text
