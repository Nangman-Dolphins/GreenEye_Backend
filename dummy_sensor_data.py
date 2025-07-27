import paho.mqtt.client as mqtt
import time
import json
import random
import os
from datetime import datetime # datetime 모듈 추가
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# --- 환경 변수 가져오기 ---
# .env 파일과 동일하게 설정 (app.py, services.py와 동일한 값)
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!')

# --- MQTT 클라이언트 설정 ---
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    """MQTT 브로커에 연결되었을 때 호출되는 콜백 함수."""
    if rc == 0:
        print(f"Dummy sensor connected to MQTT Broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    else:
        print(f"Dummy sensor failed to connect, return code {rc}")
        print(f"Check your MQTT broker status or credentials in .env file.")

# 사용자명과 비밀번호 설정
client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.on_connect = on_connect

# MQTT 브로커에 연결
try:
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
    client.loop_start() # 메시지 발행을 위해 백그라운드 루프 시작
except Exception as e:
    print(f"Could not connect dummy sensor to MQTT broker: {e}")
    print("Please ensure your Docker Mosquitto container is running and credentials are correct.")
    exit(1) # 연결 실패 시 스크립트 종료

# --- 더미 데이터 발행 로직 ---
plant_id_list = ["plant_001", "plant_002", "plant_003"] # 테스트할 식물 ID 목록
sensor_read_interval_sec = 5 # 데이터를 발행할 주기

print(f"\n--- Starting dummy sensor data publishing every {sensor_read_interval_sec} seconds ---")
print(f"Publishing to topics: sensor/data/{{plant_id}} for {', '.join(plant_id_list)}")

try:
    while True:
        for plant_id in plant_id_list:
            topic = f"sensor/data/{plant_id}"

            # 5가지 센서 데이터 더미 값 생성
            temp = round(random.uniform(20.0, 30.0), 2) # 온도 (섭씨)
            hum = round(random.uniform(50.0, 80.0), 2)  # 습도 (%)
            light = random.randint(200, 1000)          # 조도 (lux)
            soil_moisture = random.randint(100, 900)   # 토양 수분
            soil_ec = round(random.uniform(0.5, 3.0), 2) # 토양 전도도 (mS/cm)

            payload = {
                "temperature": temp,
                "humidity": hum,
                "light_lux": light,
                "soil_moisture": soil_moisture,
                "soil_ec": soil_ec
            }
            client.publish(topic, json.dumps(payload))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Published dummy data to {topic}: {payload}")
        
        time.sleep(sensor_read_interval_sec) # 설정된 주기만큼 대기

except KeyboardInterrupt:
    print("\n--- Dummy sensor data publishing stopped. ---")
finally:
    client.loop_stop()
    client.disconnect()
    print("MQTT client disconnected.")