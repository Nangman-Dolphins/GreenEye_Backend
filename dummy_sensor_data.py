import paho.mqtt.client as mqtt
import time
import json
import random
import os
from datetime import datetime # datetime 모듈 추가
from dotenv import load_dotenv
import base64 # Base64 인코딩을 위해 추가
import uuid

# .env 파일 로드
load_dotenv()

# --- 환경 변수 가져오기 ---
# .env 파일과 동일하게 설정 (app.py, services.py와 동일한 값)
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!') # 실제 비밀번호로 바꿔야 함

# --- 이미지 전송 설정 ---
# 테스트용 이미지 파일 경로 (프로젝트 루트 폴더에 있어야 함)
TEST_IMAGE_PATH = 'test_plant.jpg' 
# 이미지 전송 주기 (센서 데이터 전송 주기의 배수로 설정)
IMAGE_SEND_INTERVAL_CYCLES = 2 # 2번째 센서 데이터 발송 시점에 이미지 데이터도 함께 보냄 (5초 * 2 = 10초)

# --- MQTT 클라이언트 설정 ---
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    """MQTT 브로커에 연결되었을 때 호출되는 콜백 함수."""
    if rc == 0:
        print(f"Dummy sensor/image connected to MQTT Broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    else:
        print(f"Dummy sensor/image failed to connect, return code {rc}")
        print(f"Check your MQTT broker status or credentials in .env file.")

# 사용자명과 비밀번호 설정
client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.on_connect = on_connect

# MQTT 브로커에 연결
try:
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
    client.loop_start() # 메시지 발행을 위해 백그라운드 루프 시작
except Exception as e:
    print(f"Could not connect dummy sensor/image to MQTT broker: {e}")
    print("Please ensure your Docker Mosquitto container is running and credentials are correct.")
    exit(1) # 연결 실패 시 스크립트 종료

# --- 더미 데이터 발행 로직 ---
# 테스트용 MAC 주소 목록 (database.py에서 등록한 MAC 주소와 동일해야 함)
plant_mac_addresses = ["AA:BB:CC:DD:EE:F1", "AA:BB:CC:DD:EE:F2", "AA:BB:CC:DD:EE:F3"] 
sensor_read_interval_sec = 5 # 데이터를 발행할 주기 (초)
image_send_counter = {mac: 0 for mac in plant_mac_addresses} # 이미지 전송 카운터 초기화

print(f"\n--- Starting dummy sensor/image data publishing every {sensor_read_interval_sec} seconds ---")
print(f"Publishing to topics: sensor/data/{{mac_address}} and image/data/{{mac_address}} for {', '.join(plant_mac_addresses)}")

try:
    while True:
        for mac_address in plant_mac_addresses:
            # 1. 센서 데이터 발행
            sensor_topic = f"sensor/data/{mac_address}"
            temp = round(random.uniform(20.0, 30.0), 2) # 온도 (섭씨)
            hum = round(random.uniform(50.0, 80.0), 2)  # 습도 (%)
            light = random.randint(200, 1000)          # 조도 (lux)
            soil_moisture = random.randint(100, 900)   # 토양 수분
            soil_ec = round(random.uniform(0.5, 3.0), 2) # 토양 전도도 (mS/cm)

            sensor_payload = {
                "temperature": temp,
                "humidity": hum,
                "light_lux": light,
                "soil_moisture": soil_moisture,
                "soil_ec": soil_ec
            }
            client.publish(sensor_topic, json.dumps(sensor_payload))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Published sensor data to {sensor_topic}: {sensor_payload}")

            # 2. 이미지 데이터 발행 (새로 추가)
            image_send_counter[mac_address] += 1
            if image_send_counter[mac_address] >= IMAGE_SEND_INTERVAL_CYCLES:
                image_topic = f"image/data/{mac_address}"
                
                # 테스트 이미지 파일을 Base64로 인코딩
                image_base64_string = ""
                try:
                    with open(TEST_IMAGE_PATH, "rb") as image_file:
                        image_base64_string = base64.b64encode(image_file.read()).decode('utf-8')
                    
                    image_payload = {
                        "timestamp": datetime.utcnow().isoformat() + "Z", # UTC ISO 8601 형식
                        "image_data_base64": image_base64_string
                    }
                    client.publish(image_topic, json.dumps(image_payload))
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Published image data to {image_topic} (Payload length: {len(json.dumps(image_payload))} bytes)")
                    
                except FileNotFoundError:
                    print(f"ERROR: Test image file not found at {TEST_IMAGE_PATH}. Skipping image publish.")
                except Exception as e:
                    print(f"ERROR publishing image: {e}")
                
                image_send_counter[mac_address] = 0 # 카운터 초기화

        time.sleep(sensor_read_interval_sec) # 설정된 주기만큼 대기

except KeyboardInterrupt:
    print("\n--- Dummy sensor/image data publishing stopped. ---")
finally:
    client.loop_stop()
    client.disconnect()
    print("MQTT client disconnected.")

