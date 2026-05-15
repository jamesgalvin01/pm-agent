import os
import requests
import msal
from dotenv import load_dotenv
from task_extractor import extract_tasks_from_email
from db import get_connection

load_dotenv()

CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
TENANT_ID = os.getenv("AZURE_TENANT_ID")
SCOPES = ["Mail.Read"]

def get_access_token():
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}"
    )
    token = app.acquire_token_interactive(scopes=SCOPES)
    return token["access_token"]

def get_unread_emails(token):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$filter=isRead eq false&$top=5",
        headers=headers
    )
    return response.json().get("value", [])

def scan_outlook_for_tasks(project_id):
    print("Opening Microsoft login...")
    token = get_access_token()
    emails = get_unread_emails(token)
    if not emails:
        print("No unread emails found.")
        return
    print(f"Found {len(emails)} unread emails. Scanning for tasks...")
    for email in emails:
        body = email.get("body", {}).get("content", "")
        if body:
            tasks = extract_tasks_from_email(body)
            if tasks:
                conn = get_connection()
                cur = conn.cursor()
                for task in tasks:
                    cur.execute(
                        "INSERT INTO tasks (description, due_date, priority, status, source, project_id) VALUES (%s, %s, %s, %s, %s, %s)",
                        (task.get("task"), task.get("due_date"), task.get("priority", "medium"), "open", "outlook", project_id)
                    )
                conn.commit()
                cur.close()
                conn.close()
                print(f"Saved {len(tasks)} tasks from email.")

scan_outlook_for_tasks(1)