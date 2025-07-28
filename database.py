# database.py

import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash

# 데이터베이스 파일 경로 설정
DATABASE_FILE = 'greeneye_users.db'
# 프로젝트 루트 폴더에 DB 파일이 생성되도록 경로 설정
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, DATABASE_FILE)

def get_db_connection():
    """SQLite 데이터베이스 연결을 반환합니다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # 결과를 딕셔너리처럼 접근할 수 있게 설정
    return conn

def init_db():
    """데이터베이스를 초기화하고 user 테이블을 생성합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    print(f"Database initialized and 'users' table created at {DB_PATH}")

def add_user(email, password):
    """새로운 사용자를 데이터베이스에 추가합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 비밀번호 해싱: 평문 비밀번호를 저장하지 않고 해시 값으로 저장
    password_hash = generate_password_hash(password)
    
    try:
        cursor.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
        conn.commit()
        print(f"User '{email}' added successfully.")
        return True
    except sqlite3.IntegrityError:
        print(f"User with email '{email}' already exists.")
        return False
    finally:
        conn.close()

def get_user_by_email(email):
    """이메일을 통해 사용자를 조회합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cursor.fetchone() # 결과를 딕셔너리처럼 가져옴
    conn.close()
    return user

def check_password(hashed_password, password):
    """해싱된 비밀번호와 입력된 비밀번호를 비교합니다."""
    return check_password_hash(hashed_password, password)

# 이 파일을 직접 실행할 때만 DB를 초기화하도록 설정
if __name__ == '__main__':
    init_db()
    # 테스트용 사용자 추가 (실제 운영에서는 이 코드는 삭제)
    # print("\n--- Adding test users (if not exists) ---")
    # add_user("testuser@example.com", "testpassword123")
    # add_user("admin@greeneye.com", "adminpass123!")
    # print("--- Test user setup complete ---")