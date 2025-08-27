import time, sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_FILE = BASE_DIR / "data" / "greeneye_users.db"
DB_PATH = str(DATABASE_FILE)  # ← join 하지 말고 str()로!

def get_db_connection():
     db_path = str(DATABASE_FILE)
     os.makedirs(DATABASE_FILE.parent, exist_ok=True)
     conn = sqlite3.connect(
         db_path,
         timeout=30,
         check_same_thread=False,  # MQTT 콜백 스레드에서도 OK
     )
     conn.row_factory = sqlite3.Row
     try:
         # Windows 바인드볼륨에서 WAL은 문제를 잘 일으킴 → DELETE 권장
         conn.execute("PRAGMA journal_mode=DELETE;")
         conn.execute("PRAGMA synchronous=NORMAL;")
         conn.execute("PRAGMA busy_timeout=5000;")
     except Exception:
         pass
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
    # 기존 행에 device_id 채우기
    cur.execute("SELECT id, mac_address, device_id FROM devices")
    for row in cur.fetchall():
        if not row["device_id"] and row["mac_address"]:
            dev_id = row["mac_address"].replace(":", "").lower()[-4:]
            conn.execute("UPDATE devices SET device_id = ? WHERE id = ?", (dev_id, row["id"]))
    conn.commit()

    # device_id 고유 인덱스
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

    print(f"Database initialized/migrated at {DB_PATH}")
    
    # ★ owner_user_id 마이그레이션 자동 적용
    if not _column_exists(conn, "devices", "owner_user_id"):
        conn.execute("ALTER TABLE devices ADD COLUMN owner_user_id INTEGER")
        conn.commit()
    if not _index_exists(conn, "idx_devices_owner"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id)")
        conn.commit()
    # ★ device_image 컬럼 자동 추가 (대표 이미지 경로)
    if not _column_exists(conn, "devices", "device_image"):
        conn.execute("ALTER TABLE devices ADD COLUMN device_image TEXT")
        conn.commit()
    # ★ plant_type 컬럼 자동 추가 (식물 종류)
    if not _column_exists(conn, "devices", "plant_type"):
        conn.execute("ALTER TABLE devices ADD COLUMN plant_type TEXT")
        conn.commit()
    # ★ room 컬럼 자동 추가 (설치 위치/방 정보)
    if not _column_exists(conn, "devices", "room"):
        conn.execute("ALTER TABLE devices ADD COLUMN room TEXT")
        conn.commit()
    conn.close()
    

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

def add_device(
    mac_address: str,
    friendly_name: str,
    owner_user_id: int,
    device_image: Optional[str] = None,
    plant_type: Optional[str] = None,
    room: Optional[str] = None,
) -> bool:
    """
    디바이스 등록/소유권 클레임/정보 갱신.
    - mac_address, friendly_name, (선택) device_image, plant_type, room 저장
    - plant_type/room이 None이면 기존 값을 유지 (COALESCE)
    """
    device_id = (mac_address.split("-")[-1]).lower()
    conn = get_db_connection()
    try:
        # 1) 내 소유인 경우 필드 업데이트 허용
        if device_image:
            cur = conn.execute(
                """UPDATE devices
                     SET mac_address=?,
                         friendly_name=?,
                         device_image=?,
                         plant_type=COALESCE(?, plant_type),
                         room=COALESCE(?, room)
                   WHERE device_id=? AND owner_user_id=?""",
                (mac_address, friendly_name, device_image, plant_type, room, device_id, owner_user_id),
            )
        else:
            cur = conn.execute(
                """UPDATE devices
                     SET mac_address=?,
                         friendly_name=?,
                         plant_type=COALESCE(?, plant_type),
                         room=COALESCE(?, room)
                   WHERE device_id=? AND owner_user_id=?""",
                (mac_address, friendly_name, plant_type, room, device_id, owner_user_id),
            )
        if cur.rowcount > 0:
            conn.commit()
            return True

        # 2) 미귀속(주인 없음) 디바이스라면 내가 가져간다(Claim)
        if device_image:
            cur = conn.execute(
                """UPDATE devices
                     SET mac_address=?,
                         friendly_name=?,
                         owner_user_id=?,
                         device_image=?,
                         plant_type=COALESCE(?, plant_type),
                         room=COALESCE(?, room)
                   WHERE device_id=? AND owner_user_id IS NULL""",
                (mac_address, friendly_name, owner_user_id, device_image, plant_type, room, device_id),
            )
        else:
            cur = conn.execute(
                """UPDATE devices
                     SET mac_address=?,
                         friendly_name=?,
                         owner_user_id=?,
                         plant_type=COALESCE(?, plant_type),
                         room=COALESCE(?, room)
                   WHERE device_id=? AND owner_user_id IS NULL""",
                (mac_address, friendly_name, owner_user_id, plant_type, room, device_id),
            )
        if cur.rowcount > 0:
            conn.commit()
            return True

        # 3) 새로 삽입 시도
        if device_image:
            conn.execute(
                """INSERT INTO devices
                       (device_id, mac_address, friendly_name, registered_at, owner_user_id,
                        device_image, plant_type, room)
                   VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?)""",
                (device_id, mac_address, friendly_name, owner_user_id, device_image, plant_type, room),
            )
        else:
            conn.execute(
                """INSERT INTO devices
                       (device_id, mac_address, friendly_name, registered_at, owner_user_id,
                        plant_type, room)
                   VALUES (?, ?, ?, datetime('now'), ?, ?, ?)""",
                (device_id, mac_address, friendly_name, owner_user_id, plant_type, room),
            )
        conn.commit()
        return True

    except sqlite3.IntegrityError:
        # 여기까지 왔는데도 무결성 위반이면 대부분 '남이 소유한' 케이스
        return False
    finally:
        conn.close()

def get_device_by_mac(mac_address):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices WHERE mac_address = ?", (mac_address,))
    device = cur.fetchone()
    conn.close()
    return device

def get_device_by_device_id(device_id: str, owner_user_id: int) -> Optional[dict]:
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT * FROM devices WHERE device_id = ? AND owner_user_id = ?",
            (device_id, owner_user_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

def get_all_devices_any():
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT device_id, friendly_name, device_image, plant_type, room FROM devices ORDER BY device_id"
        ).fetchall()
        return [dict(r) for r in rows]

def get_device_by_device_id_any(device_id: str):
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE device_id = ?",
            (device_id,)
        ).fetchone()
        return dict(row) if row else None

def get_device_by_friendly_name(friendly_name):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM devices WHERE friendly_name = ?", (friendly_name,))
    device = cur.fetchone()
    conn.close()
    return device

def get_all_devices(owner_user_id: int):
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT device_id, friendly_name, device_image, plant_type, room "
            "FROM devices WHERE owner_user_id = ? ORDER BY device_id",
            (owner_user_id,),
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def delete_device_from_db(device_id: str, owner_user_id: int | None = None) -> bool:
    """
    디바이스 레코드 삭제. owner_user_id가 주어지면 소유자 검증 포함.
    """
    with get_db_connection() as conn:
        if owner_user_id is not None:
            cur = conn.execute(
                "DELETE FROM devices WHERE device_id = ? AND owner_user_id = ?",
                (device_id, owner_user_id),
            )
        else:
            cur = conn.execute(
                "DELETE FROM devices WHERE device_id = ?",
                (device_id,),
            )
        return cur.rowcount > 0

def update_device_image(device_id: str, owner_user_id: int, device_image: Optional[str]) -> bool:
    """
    대표 이미지 경로를 갱신하거나(None) 제거한다.
    """
    with get_db_connection() as conn:
        cur = conn.execute(
            "UPDATE devices SET device_image = ? WHERE device_id = ? AND owner_user_id = ?",
            (device_image, device_id, owner_user_id),
        )
        return cur.rowcount > 0

if __name__ == '__main__':
    init_db()
    print("Database ready.")
