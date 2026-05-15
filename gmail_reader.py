import os
import base64
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from task_extractor import extract_tasks_from_email
from db import get_connection

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def get_gmail_service():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds)

def get_unread_emails(service):
    results = service.users().messages().list(
        userId='me',
        labelIds=['INBOX'],
        q='is:unread'
    ).execute()
    return results.get('messages', [])

def get_email_body(service, msg_id):
    message = service.users().messages().get(
        userId='me',
        id=msg_id,
        format='full'
    ).execute()
    payload = message['payload']
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body']['data']
                return base64.urlsafe_b64decode(data).decode('utf-8')
    elif 'body' in payload:
        data = payload['body']['data']
        return base64.urlsafe_b64decode(data).decode('utf-8')
    return ""

def scan_gmail_for_tasks(project_id):
    service = get_gmail_service()
    emails = get_unread_emails(service)
    if not emails:
        print("No unread emails found.")
        return
    print(f"Found {len(emails)} unread emails. Scanning for tasks...")
    for email in emails[:5]:
        body = get_email_body(service, email['id'])
        if body:
            tasks = extract_tasks_from_email(body)
            if tasks:
                conn = get_connection()
                cur = conn.cursor()
                for task in tasks:
                    cur.execute(
                        "INSERT INTO tasks (description, due_date, priority, status, source, project_id) VALUES (%s, %s, %s, %s, %s, %s)",
                        (task.get("task"), task.get("due_date"), task.get("priority", "medium"), "open", "gmail", project_id)
                    )
                conn.commit()
                cur.close()
                conn.close()
                print(f"Saved {len(tasks)} tasks from email.")

scan_gmail_for_tasks(2)