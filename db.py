import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    return psycopg2.connect(host="aws-1-us-east-1.pooler.supabase.com",database="postgres",user="postgres.fywwujzmxdnhophhgaey",password=os.getenv("SUPABASE_PASSWORD"),port=5432,sslmode="require")

def save_tasks(tasks):
    conn = get_connection()
    cur = conn.cursor()
    for task in tasks:
        cur.execute(
            "INSERT INTO tasks (description, due_date, priority, status, source) VALUES (%s, %s, %s, %s, %s)",
            (task.get("task"), task.get("due_date"), task.get("priority", "medium"), "open", "email")
        )
    conn.commit()
    cur.close()
    conn.close()
    print(f"Saved {len(tasks)} tasks to database.")