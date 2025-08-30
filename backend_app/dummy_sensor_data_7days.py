import random
from datetime import datetime, timedelta
import pytz

from backend_app.services import publish_mqtt_message

# 테스트할 디바이스 ID (DB에 있는 device_id로 맞추세요)
DEVICE_ID = "6c18"       # 예시
PLANT_TYPE = "Rhododendron"
ROOM = "LivingRoom"

def generate_dummy_data():
    now = datetime.now(pytz.utc)
    start = now - timedelta(days=7)

    current = start
    while current <= now:
        payload = {
            "device_id": DEVICE_ID,
            "timestamp": current.isoformat(),
            "temperature": round(random.uniform(20, 28), 2),
            "humidity": round(random.uniform(40, 70), 2),
            "light_lux": round(random.uniform(500, 5000), 2),
            "soil_temp": round(random.uniform(15, 25), 2),
            "soil_moisture": round(random.uniform(40, 80), 2),
            "soil_ec": round(random.uniform(0.5, 2.0), 2),
            "battery": round(random.uniform(40, 100), 2),
        }
        print("Publishing:", payload)
        publish_mqtt_message(f"GreenEye/data/{DEVICE_ID}", payload)
        current += timedelta(hours=1)

if __name__ == "__main__":
    generate_dummy_data()
