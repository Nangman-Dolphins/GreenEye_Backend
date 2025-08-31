import sqlite3
import os
from datetime import datetime

# 데이터베이스 파일 경로
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'conversations.db')

def init_chat_db():
    """대화 기록을 저장할 데이터베이스 초기화"""
    # 디렉토리가 없으면 생성
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 대화 기록 테이블 생성
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    conn.commit()
    conn.close()

def save_message(conversation_id, user_id, role, content):
    """대화 메시지 저장"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
    INSERT INTO conversations (conversation_id, user_id, role, content, timestamp)
    VALUES (?, ?, ?, ?, ?)
    ''', (conversation_id, user_id, role, content, datetime.now()))

    conn.commit()
    conn.close()

def load_history(conversation_id, user_id):
    """대화 기록 불러오기"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
    SELECT role, content FROM conversations 
    WHERE conversation_id = ? AND user_id = ?
    ORDER BY timestamp ASC
    ''', (conversation_id, user_id))

    history = cursor.fetchall()
    conn.close()

    return history

def get_user_conversations(user_id):
    """사용자의 모든 대화 목록 가져오기"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
    SELECT DISTINCT conversation_id, MAX(timestamp) as last_update
    FROM conversations 
    WHERE user_id = ?
    GROUP BY conversation_id
    ORDER BY last_update DESC
    ''', (user_id,))

    conversations = cursor.fetchall()
    conn.close()

    return conversations