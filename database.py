import time, sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

DATABASE_FILE = 'greeneye_users.db'
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, DATABASE_FILE)

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn

def _column_exists(conn, table, column):
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [r["name"] for r in cur.fetchall()]
    return column in cols

def _index_exists(conn, index_name):
    cur = conn.execute("PRAGMA index_list(devices)")
    names = [r["name"] for r in cur.fetchall()]
    return index_name in names

def init_db():
    """
    테이블 생성 + 경량 마이그레이션:
      - devices: device_id(UNIQUE) 추가
      - plant_images: device_id 컬럼 추가
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # 1) users
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)

    # 2) devices
    cur.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT UNIQUE NOT NULL,
            friendly_name TEXT UNIQUE NOT NULL,
            registered_at TEXT NOT NULL,
            device_id TEXT UNIQUE NOT NULL
        )
    """)
    # 기존 행에 device_id 채우기 (mac 마지막 4자리)
    cur.execute("SELECT id, mac_address, device_id FROM devices")
    for row in cur.fetchall():
        if not row["device_id"] and row["mac_address"]:
            dev_id = row["mac_address"].replace(":", "").lower()[-4:]
            conn.execute("UPDATE devices SET device_id = ? WHERE id = ?", (dev_id, row["id"]))
    conn.commit()
    # 고유 인덱스
    if not _index_exists(conn, "idx_devices_device_id_unique"):
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_device_id_unique ON devices(device_id)")
        conn.commit()

    # 3) plant_images
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plant_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac_address TEXT NOT NULL,
            filename TEXT NOT NULL UNIQUE,
            filepath TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            device_id TEXT NOT NULL
        )
    """)
    conn.close()
    print(f"Database initialized/migrated at {DB_PATH}")

def add_user(email, password):
    conn = get_db_connection()
    cur = conn.cursor()
    password_hash = generate_password_hash(password)
    try:
        cur.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
        conn.commit()
        print(f"User '{email}' added successfully.")
        return True
    except sqlite3.IntegrityError:
        print(f"User with email '{email}' already exists.")
        return False
    finally:
        conn.close()

def get_user_by_email(email):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email = ?", (email,))
    user = cur.fetchone()
    conn.close()
    return user

def get_all_users():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users")
    users = cur.fetchall()
    conn.close()
    return users

def check_password(hashed_password, password):
    return check_password_hash(hashed_password, password)

def _retry_locked(fn, *args, retries=5, delay=0.2, **kwargs):
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                time.sleep(delay * (i + 1))  # 점증 대기
                continue
            raise

def add_device(mac_address, friendly_name):
    registered_at = datetime.utcnow().isoformat()
    device_id = mac_address.replace(":", "").lower()[-4:]
    try:
        with get_db_connection() as conn:
            _retry_locked(
                conn.execute,
                "INSERT INTO devices (mac_address, friendly_name, registered_at, device_id) VALUES (?, ?, ?, ?)",
                (mac_address, friendly_name, registered_at, device_id)
            )
            conn.commit()
        print(f"Device '{mac_address}' as '{friendly_name}' (device_id={device_id}) added.")
        return True
    except sqlite3.IntegrityError:
        print("Device (MAC or name or device_id) already exists.")
        return False

def get_device_by_mac(mac_address):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices WHERE mac_address = ?", (mac_address,))
    device = cur.fetchone()
    conn.close()
    return device

def get_device_by_device_id(device_id: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices WHERE device_id = ?", (device_id.lower(),))
    device = cur.fetchone()
    conn.close()
    return device

def get_device_by_friendly_name(friendly_name):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices WHERE friendly_name = ?", (friendly_name,))
    device = cur.fetchone()
    conn.close()
    return device

def get_all_devices():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices")
    devices = cur.fetchall()
    conn.close()
    return devices

if __name__ == '__main__':
    init_db()
    print("Database ready.")
