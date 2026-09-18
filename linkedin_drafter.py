import anthropic
import os
import resend
from datetime import date
from dotenv import load_dotenv
from db import get_connection

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
resend.api_key = os.getenv("RESEND_API_KEY")

# Each angle carries its own voice guidance and a real example of James's writing.
ANGLES = [
    {
        "name": "thought-leadership insight",
        "guidance": (
            "Measured and advisory. Open with a clear thesis. Build the argument in "
            "calm, full sentences. Use 'we' / 'at Miami Coastline Management' framing. "
            "Frame insight from the owner's-rep value perspective. Close with a short "
            "rule-of-three or a clean directive. Do NOT name any specific project."
        ),
        "example": (
            "In real estate development, success is rarely determined at groundbreaking"
            "—it's decided long before. At Miami Coastline Management, we've seen time and "
            "again that strong pre-construction planning is what separates smooth projects "
            "from costly delays. Pre-construction is where vision meets strategy. It's where "
            "budgets are validated, risks are identified, and timelines refined—with clarity, "
            "not under pressure. As an Owner's Representative, our role is to bring structure, "
            "accountability, and expertise to this process. Build smart. Plan early. Choose "
            "the right team."
        ),
    },
    {
        "name": "market-pulse / business development",
        "guidance": (
            "Timely and outward-facing. Tie to the current moment or season in South "
            "Florida construction. Read the market, position Miami Coastline as in-the-know, "
            "and close with a soft 'let's connect' invitation. Do NOT name specific projects."
        ),
        "example": (
            "Gearing up for South Florida's fall building season. At Miami Coastline "
            "Management, we've been deep into preparations for what's shaping up to be a "
            "high-volume season. With several major projects slated to break ground, this is "
            "the time for developers to finalize strategy, align teams, and ensure pre-"
            "development milestones are in place—from permitting and financing to contractor "
            "coordination. If you're preparing to launch this season and need experienced "
            "guidance from entitlement through vertical construction, let's connect."
        ),
    },
    {
        "name": "project milestone / announcement",
        "guidance": (
            "Warm and celebratory. This is an announcement format. Because Rowan cannot "
            "know what is safe to disclose publicly, write the post in James's warm "
            "announcement voice but insert a clear placeholder '[PROJECT / DETAILS — fill "
            "in before posting]' wherever a specific project name or scope detail would go. "
            "Never invent or guess a real project name, location, or scope."
        ),
        "example": (
            "Miami Coastline Management is proud to announce our newest project at "
            "[PROJECT — fill in], one of the region's standout properties. This work will "
            "focus on [SCOPE — fill in]—all while preserving the character that makes the "
            "property special. We're excited to collaborate with the ownership and partners "
            "involved to bring this transformation to life, and will share updates as the "
            "project progresses."
        ),
    },
]

def get_recent_activity():
    """Pull recent tasks across both projects as raw material for a post."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name, t.description, t.status, t.due_date
        FROM tasks t
        JOIN projects p ON p.id = t.project_id
        ORDER BY t.due_date DESC NULLS LAST
        LIMIT 15
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"project": r[0], "task": r[1], "status": r[2], "due_date": str(r[3])}
        for r in rows
    ]

def get_weekly_topic():
    """Optional steer. Returns the topic string or None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_topic (
            id INT PRIMARY KEY DEFAULT 1,
            topic TEXT
        )
    """)
    conn.commit()
    cur.execute("SELECT topic FROM linkedin_topic WHERE id = 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row and row[0] else None

def generate_post(activity, topic, angle):
    activity_lines = "\n".join(
        f"- [{a['project']}] {a['task']} ({a['status']})" for a in activity
    ) or "No recent project activity logged."

    topic_line = f"\nThis week's steer from James: {topic}" if topic else ""

    prompt = f"""You are drafting a LinkedIn post for James Galvin, owner of Miami Coastline Management, a construction project management and owner's rep firm in Miami, Florida. He works on high-end South Florida and Florida Keys construction projects.

Today's angle: {angle['name']}{topic_line}

Voice and structure for this angle:
{angle['guidance']}

Here is a real example of James writing in this exact voice — match its cadence, tone, and length:
---
{angle['example']}
---

Recent project activity (use ONLY as loose inspiration — never disclose client names, dollar amounts, or confidential details):
{activity_lines}

Write ONE LinkedIn post in James's voice:
- 130-200 words
- No client names, no dollar figures, no confidential specifics (use placeholders if the angle calls for specifics)
- 3-5 relevant hashtags at the end
- Sound like a seasoned practitioner, not a marketer
- Vary the opening hook; don't start with "Excited to share"

Return only the post text, ready to paste."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# ============================================================
# DRAFT STORE + DELIVERY (Telegram approval, email fallback)
# ============================================================

def _ensure_drafts_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_drafts (
            id SERIAL PRIMARY KEY,
            angle TEXT,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            telegram_message_id BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            decided_at TIMESTAMPTZ
        )
    """)


def _db(sql, params=(), fetch=False):
    conn = get_connection()
    cur = conn.cursor()
    _ensure_drafts_table(cur)
    cur.execute(sql, params)
    row = cur.fetchone() if fetch else None
    conn.commit()
    cur.close()
    conn.close()
    return row


def get_draft(draft_id):
    row = _db("SELECT id, angle, body, status, telegram_message_id FROM linkedin_drafts WHERE id = %s",
              (draft_id,), fetch=True)
    if not row:
        return None
    return {"id": row[0], "angle": row[1], "body": row[2], "status": row[3], "telegram_message_id": row[4]}


def _telegram_live() -> bool:
    channel = os.getenv("LINKEDIN_CHANNEL", "telegram").strip().lower()
    return channel == "telegram" and bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_ID"))


def _keyboard(draft_id):
    return {"inline_keyboard": [
        [{"text": "Approve", "callback_data": f"liok:{draft_id}"},
         {"text": "Edit", "callback_data": f"liedit:{draft_id}"}],
        [{"text": "New draft", "callback_data": f"linew:{draft_id}"},
         {"text": "Skip today", "callback_data": f"liskip:{draft_id}"}],
    ]}


def _email_draft(post):
    resend.Emails.send({
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"LinkedIn draft for {date.today().strftime('%A, %b %d')} — ready to paste",
        "text": post + "\n\n---\nDrafted by Rowan. Edit before posting. To steer this week's topic, update the linkedin_topic table.",
    })


def deliver(draft_id, heading="LinkedIn draft") -> str:
    """Send a stored draft to James. Returns 'telegram' or 'email'."""
    d = get_draft(draft_id)
    if _telegram_live():
        from telegram_bot import send_message
        msg_id = send_message(
            f"{heading} ({d['angle']}):\n\n{d['body']}\n\n"
            "Approve to get a clean copy to paste into LinkedIn. Edit lets you say what to change.",
            reply_markup=_keyboard(draft_id),
        )
        if msg_id:
            _db("UPDATE linkedin_drafts SET telegram_message_id = %s WHERE id = %s", (msg_id, draft_id))
            return "telegram"
        print("[linkedin] Telegram delivery failed; emailing instead.")
    _email_draft(d["body"])
    return "email"


def create_draft(angle=None) -> int:
    angle = angle or ANGLES[date.today().timetuple().tm_yday % len(ANGLES)]
    post = generate_post(get_recent_activity(), get_weekly_topic(), angle).strip()
    row = _db("INSERT INTO linkedin_drafts (angle, body) VALUES (%s, %s) RETURNING id",
              (angle["name"], post), fetch=True)
    return row[0]


def _set_status(draft_id, status):
    _db("UPDATE linkedin_drafts SET status = %s, decided_at = NOW() WHERE id = %s", (status, draft_id))


def approve(draft_id):
    from telegram_bot import send_message
    d = get_draft(draft_id)
    if not d:
        send_message("That draft is gone.")
        return
    _set_status(draft_id, "approved")
    send_message("Approved. Long-press the next message to copy it:")
    send_message(d["body"])


def skip(draft_id):
    from telegram_bot import send_message
    _set_status(draft_id, "skipped")
    send_message("Skipped. No post today.")


def new_version(draft_id):
    """Replace a draft with a fresh one from the next angle."""
    d = get_draft(draft_id)
    if d:
        _set_status(draft_id, "replaced")
    names = [a["name"] for a in ANGLES]
    idx = (names.index(d["angle"]) + 1) % len(ANGLES) if d and d["angle"] in names else 0
    new_id = create_draft(ANGLES[idx])
    deliver(new_id, heading="New LinkedIn draft")


REVISE_PROMPT = """Revise this LinkedIn post by James Galvin (Miami Coastline Management, owner's rep firm in South Florida).

<post>
{post}
</post>

<james_instruction>
{instruction}
</james_instruction>

James may describe a change ("shorter", "more about hurricane season") or give wording to use. Apply it and return the COMPLETE revised post.
Keep: his voice, 130-200 words unless he asks otherwise, 3-5 hashtags at the end, no client names, no dollar figures, no confidential specifics (keep any [PLACEHOLDER] brackets unless he fills them in).
Return only the post text."""


def revise(draft_id, instruction):
    d = get_draft(draft_id)
    if not d or d["status"] != "pending":
        from telegram_bot import send_message
        send_message("That draft isn't open any more. Send /linkedin for a new one.")
        return
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[{"role": "user", "content": REVISE_PROMPT.format(post=d["body"], instruction=instruction)}],
    )
    body = (resp.content[0].text or "").strip() or d["body"]
    _db("UPDATE linkedin_drafts SET body = %s WHERE id = %s", (body, draft_id))
    deliver(draft_id, heading="Revised LinkedIn draft")


def run_linkedin_draft():
    draft_id = create_draft()
    d = get_draft(draft_id)
    print("\n--- LINKEDIN DRAFT ---")
    print(f"[Angle: {d['angle']}]\n")
    print(d["body"])
    print("----------------------\n")
    where = deliver(draft_id)
    print(f"LinkedIn draft sent via {where}.")


if __name__ == "__main__":
    run_linkedin_draft()
