import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "agent.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Таблица для откликнутых вакансий
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applied_jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            url TEXT,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Вакансии, где отклик не подтвердился. Без счетчика попыток агент возвращался
    # бы к ним при каждом проходе выдачи, каждый раз тратя полный цикл модели.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS failed_responses (
            id TEXT PRIMARY KEY,
            title TEXT,
            attempts INTEGER DEFAULT 0,
            last_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Таблица для истории сообщений чатов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            msg_id TEXT PRIMARY KEY,
            chat_id TEXT,
            text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_job_applied(job_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM applied_jobs WHERE id = ?", (job_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_applied_job(job_id: str, title: str, url: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO applied_jobs (id, title, url) VALUES (?, ?, ?)", (job_id, title, url))
    conn.commit()
    conn.close()

def bump_failed_response(job_id: str, title: str) -> int:
    """Отмечает неудачную попытку отклика и возвращает их общее число."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO failed_responses (id, title, attempts) VALUES (?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET attempts = attempts + 1, last_at = CURRENT_TIMESTAMP
    """, (job_id, title))
    conn.commit()
    attempts = cursor.execute("SELECT attempts FROM failed_responses WHERE id = ?", (job_id,)).fetchone()[0]
    conn.close()
    return attempts


def is_message_processed(msg_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM chat_messages WHERE msg_id = ?", (msg_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def add_processed_message(msg_id: str, chat_id: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_messages (msg_id, chat_id, text) VALUES (?, ?, ?)", (msg_id, chat_id, text))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized.")
