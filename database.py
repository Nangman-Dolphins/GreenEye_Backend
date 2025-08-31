# database.py
import sqlite3
import os

# --- 경로 설정 부분 ---
# 1. 데이터 폴더 이름 지정
DATA_DIR = r'C:\Users\yty23\OneDrive\문서\GitHub\GreenEye_Backend\data'
# 2. 데이터베이스 파일 경로 최종 조합
#    os.path.join을 사용하면 OS에 맞게 알아서 'data/conversations.db' 또는 'data\conversations.db'로 만들어줍니다.
DB_PATH = os.path.join(DATA_DIR, 'conversations.db')
# --- 경로 설정 끝 ---

def init_db():

    # 데이터 폴더가 없으면 생성합니다.
    os.makedirs(DATA_DIR, exist_ok=True)

    # 'conversations.db'라는 이름의 데이터베이스 파일에 연결합니다.
    # 파일이 없으면 자동으로 생성됩니다.
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 'conversations'라는 이름의 테이블을 생성합니다.
    # IF NOT EXISTS를 사용하면 테이블이 이미 있을 경우 오류 없이 넘어갑니다.
    # --- ▼ 아래 테이블 생성 코드 추가 ▼ ---
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,                  -- 어떤 유저의 대화인지 연결
            role TEXT NOT NULL,                      -- 'user' 또는 'ai'
            message TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) -- users 테이블과 연결
        )
    ''')
    # --- ▲ 여기까지 추가 ▲ ---

    # 변경사항을 저장하고 연결을 닫습니다.
    conn.commit()
    conn.close()
    print("Database initialized successfully.")

# --- ▼ 파일 맨 아래에 아래 함수들 추가 ▼ ---

def save_message(conversation_id, user_id, sender, message):
    conn = sqlite3.connect(DB_PATH) # DB_PATH는 기존에 정의된 변수 사용
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO conversations (conversation_id, user_id, sender, message)
        VALUES (?, ?, ?, ?)
    ''', (conversation_id, user_id, sender, message))
    conn.commit()
    conn.close()

def load_history(conversation_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT sender, message FROM conversations
        WHERE conversation_id = ? AND user_id = ?
        ORDER BY timestamp ASC
    ''', (conversation_id, user_id))
    history = cursor.fetchall()
    conn.close()
    return history

# 이 파일을 직접 실행했을 때 init_db 함수가 호출되도록 합니다.
if __name__ == '__main__':
    init_db()