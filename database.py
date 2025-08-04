import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# 데이터베이스 파일 경로 설정
DATABASE_FILE = 'greeneye_users.db'
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, DATABASE_FILE)

def get_db_connection():
    """SQLite 데이터베이스 연결을 반환합니다."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """데이터베이스를 초기화하고 user, devices 테이블을 생성합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # users 테이블 (회원가입 정보)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')

    # devices 테이블 (MAC 주소, 친근한 이름 매핑)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT UNIQUE NOT NULL,
            plant_friendly_name TEXT UNIQUE NOT NULL,
            registered_at TEXT NOT NULL
        )
    ''')
    
    # plant_images 테이블 (이미지 메타데이터)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS plant_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT NOT NULL,
            filename TEXT NOT NULL UNIQUE,
            filepath TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()
    print(f"Database initialized and tables created at {DB_PATH}")

def add_user(email, password):
    """새로운 사용자를 데이터베이스에 추가합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
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
    user = cursor.fetchone()
    conn.close()
    return user

def check_password(hashed_password, password):
    """해싱된 비밀번호와 입력된 비밀번호를 비교합니다."""
    return check_password_hash(hashed_password, password)

def add_device(mac_address, plant_friendly_name):
    """새로운 단말기(MAC 주소)와 친근한 이름을 DB에 추가합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    registered_at = datetime.utcnow().isoformat()
    
    try:
        cursor.execute("INSERT INTO devices (mac_address, plant_friendly_name, registered_at) VALUES (?, ?, ?)", 
                       (mac_address, plant_friendly_name, registered_at))
        conn.commit()
        print(f"Device '{mac_address}' added with friendly name '{plant_friendly_name}'.")
        return True
    except sqlite3.IntegrityError:
        print(f"Device with MAC '{mac_address}' or friendly name '{plant_friendly_name}' already exists.")
        return False
    finally:
        conn.close()
        
def get_device_by_mac(mac_address):
    """MAC 주소로 단말기 정보를 조회합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM devices WHERE mac_address = ?", (mac_address,))
    device = cursor.fetchone()
    conn.close()
    return device
    
def get_device_by_friendly_name(plant_friendly_name):
    """친근한 이름으로 단말기 정보를 조회합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM devices WHERE plant_friendly_name = ?", (plant_friendly_name,))
    device = cursor.fetchone()
    conn.close()
    return device


if __name__ == '__main__':
    init_db()
    print("Database ready.")
