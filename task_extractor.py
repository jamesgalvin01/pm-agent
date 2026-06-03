import anthropic
import json
import os
from dotenv import load_dotenv
from db import save_tasks

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

def extract_tasks_from_email(email_content):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[
            {
                "role": "user",
                "content": "Extract all action items from this email. Return ONLY a JSON array. Each item should have: task, assignee, due date (in YYYY-MM-DD format only, or null if unclear), priority (high, medium, or low). Email: " + email_content
            }
        ]
    )
    raw = response.content[0].text
    clean = raw.replace("```json", "").replace("```", "").strip()
    start = clean.find('[')
    end = clean.rfind(']') + 1

    if start == -1 or end == 0:
        print("No JSON array found in response; skipping this email.")
        return []

    try:
        return json.loads(clean[start:end])
    except json.JSONDecodeError as e:
        print("Failed to parse tasks from email; skipping. Error:", e)
        return []

if __name__ == "__main__":
    test_email = "James please send the client proposal by Friday. Maria needs to review the budget spreadsheet by tomorrow. John to schedule the contractor call next week."
    tasks = extract_tasks_from_email(test_email)
    print("Extracted tasks:")
    print(json.dumps(tasks, indent=2))
    save_tasks(tasks)
