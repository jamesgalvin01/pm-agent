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
- You only have visibility into MCM's project data via the tools listed below — you don't have access to email, calendars, or the outside world unless a tool exposes it.

# How you communicate
- Be concise. One or two short paragraphs, or a tight list. Never long preambles.
- No filler ("Of course! I'd be happy to..."). Get straight to the answer.
- When you don't have enough info, ASK a single clarifying question instead of guessing. Never invent details that aren't in the data.
- If you notice something concerning that's adjacent to what was asked (overdue task, red RAG, blocked dependency), surface it briefly — but don't go off on tangents.

# How you use tools
You have read tools (safe, run them whenever useful) and write tools (change the database).

For READ tools (list_open_tasks, list_projects, get_project_details, lookup_person):
- Just call them when they help answer the question. No need to ask permission.

For WRITE tools (mark_task_complete, reopen_task, create_task, add_risk):
- You must ALWAYS propose first, then wait for the user to confirm before executing.
- To propose, describe in plain text what you intend to do and ASK for confirmation. Be specific (task IDs, exact text, due dates).
- Do NOT call the write tool on the same turn as the proposal.
- Only call the write tool after the user has clearly agreed (e.g. "yes", "do it", "go ahead", "confirmed"). If their reply is ambiguous, ask again.
- After execution, give a one-line confirmation of what changed.

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
You: [call mark_task_complete] "Done — task 47 is now complete."
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

    # ---------- WRITE TOOLS ----------
    {
        "name": "mark_task_complete",
        "description": "Mark a single task as complete. REQUIRES prior user confirmation in this conversation — do not call this on the same turn you propose it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "The task ID."},
            },
            "required": ["task_id"],
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
]


WRITE_TOOLS = {"mark_task_complete", "reopen_task", "create_task", "add_risk", "create_person", "update_person"}


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

    cur.close()
    conn.close()
    return {"project": project, "milestones": milestones, "open_tasks": open_tasks, "risks": risks}


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
    task_id = args["task_id"]
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status = 'complete' WHERE id = %s RETURNING id, description", (task_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return {"error": f"No task with id {task_id}"}
    return {"ok": True, "task_id": row[0], "description": row[1]}


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


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def run_agent_turn(conversation_id: int, user_text: str) -> str:
    """
    Persist the user message, run Claude (with tool-use loop), persist all assistant
    output, and return the final visible text reply.
    """
    _save_message(conversation_id, "user", content=user_text)
    messages = _load_conversation_messages(conversation_id)

    final_text = ""
    for _ in range(MAX_TOOL_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
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
