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

def run_linkedin_draft():
    angle = ANGLES[date.today().timetuple().tm_yday % len(ANGLES)]
    activity = get_recent_activity()
    topic = get_weekly_topic()
    post = generate_post(activity, topic, angle)

    print("\n--- LINKEDIN DRAFT ---")
    print(f"[Angle: {angle['name']}]\n")
    print(post)
    print("----------------------\n")

    params = {
        "from": "Rowan <onboarding@resend.dev>",
        "to": "james@miami-coastline.com",
        "subject": f"LinkedIn draft for {date.today().strftime('%A, %b %d')} — ready to paste",
        "text": post + "\n\n---\nDrafted by Rowan. Edit before posting. To steer this week's topic, update the linkedin_topic table.",
    }
    resend.Emails.send(params)
    print("LinkedIn draft emailed.")

if __name__ == "__main__":
    run_linkedin_draft()