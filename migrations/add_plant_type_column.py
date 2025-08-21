# add_plant_type_column.py
import sqlite3
import os
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "greeneye_users.db"

try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # plant_type 컬럼이 이미 있는지 확인
    cursor.execute("PRAGMA table_info(devices);")
    columns = [row[1] for row in cursor.fetchall()]
    if 'plant_type' in columns:
        print("⚠ 'plant_type' 컬럼은 이미 존재합니다.")
    else:
        # 컬럼 추가
        cursor.execute("ALTER TABLE devices ADD COLUMN plant_type TEXT;")
        conn.commit()
        print("✅ 'plant_type' 컬럼이 성공적으로 추가되었습니다.")

except Exception as e:
    print("❌ 오류 발생:", e)

finally:
    conn.close()
