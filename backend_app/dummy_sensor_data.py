import paho.mqtt.client as mqtt
import time
import json
import random
import os
import socket
from datetime import datetime
from dotenv import load_dotenv
import re

# .env 파일 로드
load_dotenv()

# --- 더미 코멘트 목록 (임의로 선택하여 이미지와 함께 전송 가능) ---
DUMMY_COMMENTS = [
    "싱그러운 토마토! 아주 건강하게 크고 있네요. 🍅",
    "잎사귀가 아주 튼튼해 보여요!",
    "수분이 충분해 보입니다. 😊",
    "햇빛을 많이 받아 무럭무럭 자라는 중입니다. ☀️",
    "성장 상태가 양호합니다. 다음 분석을 기대해주세요.",
    "토양 상태가 최적입니다. 👍"
]

# --- 환경 변수 ---
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
try:
    socket.gethostbyname(MQTT_BROKER_HOST)
except socket.gaierror:
    print(f"[!] '{MQTT_BROKER_HOST}' 를 인식하지 못했습니다. localhost로 대체합니다.")
    MQTT_BROKER_HOST = "localhost"

MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!')

# --- 이미지 전송 설정 ---
TEST_IMAGE_PATH = 'test_plant.jpg'   # 존재하는 테스트 이미지를 여기에 둠
IMAGE_SEND_INTERVAL_CYCLES = 2       # 예: 센서 2회 발행마다 이미지 1회

# --- MQTT 클라이언트 설정 ---
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    """MQTT 브로커 접속 결과 콜백"""
    if rc == 0:
        print(f"Dummy sensor/image connected to MQTT Broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    else:
        print(f"Dummy sensor/image failed to connect, return code {rc}")
        print("Check your MQTT broker status or credentials in .env file.")

client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.on_connect = on_connect

# MQTT 브로커에 연결
try:
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
    client.loop_start()
except Exception as e:
    print(f"Could not connect dummy sensor/image to MQTT broker: {e}")
    print("Please ensure your Docker Mosquitto container is running and credentials are correct.")
    raise SystemExit(1)

# --- 테스트용 "커스텀 MAC 형식" 목록 (ge-sd-0000) ---
# 마지막 4자리는 반드시 16진수여야 함 (0-9a-fA-F)
plant_ids = ["ge-sd-6C18", "ge-sd-EEF2", "ge-sd-00a9"]
sensor_read_interval_sec = 5
image_send_counter = {pid: 0 for pid in plant_ids}

# ge-sd-XXXX -> device_id(소문자 4자리) 추출
_id_pattern = re.compile(r"^[a-z]{2}-[a-z]{2}-([0-9a-fA-F]{4})$")

def device_id_from_custom(mac_like_id: str) -> str:
    """
    'ge-sd-6C18' 같은 문자열에서 마지막 4자리(16진수)를 소문자로 추출.
    형식이 틀리면 ValueError.
    """
    m = _id_pattern.match(mac_like_id)
    if not m:
        raise ValueError(f"Invalid id format: {mac_like_id} (expected ge-sd-XXXX, XXXX is hex)")
    return m.group(1).lower()

def make_sensor_payload():
    return {
        "bat_level": 50, 
        "amb_temp": round(random.uniform(20.0, 30.0), 2),
        "amb_humi": round(random.uniform(50.0, 80.0), 2),
        "amb_light": round(random.uniform(800, 2000), 2),
        "soil_temp": round(random.uniform(18.0, 25.0), 2),
        "soil_humi": round(random.uniform(50.0, 90.0), 2),
        "soil_ec": round(random.uniform(0.5, 3.0), 2)
    }

def publish_image_hex(device_id: str, img_path: str = TEST_IMAGE_PATH):
    try:
        with open(img_path, "rb") as f:
            hex_str = f.read().hex()  # 이미지 HEX로 전송
        payload = {
            "plant_img": hex_str,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        client.publish(f"GreenEye/data/{device_id}", json.dumps(payload))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Published image (HEX) to GreenEye/data/{device_id}")
    except FileNotFoundError:
        print(f"ERROR: Test image file not found at {img_path}.")
    except Exception as e:
        print(f"ERROR publishing image: {e}")

print(f"\n--- Starting dummy sensor/image data publishing every {sensor_read_interval_sec} seconds ---")
print(f"Publishing to topic 'GreenEye/data/{{device_id}}' for {', '.join(plant_ids)}")
print(f"Image mode: HEX (per manual)\n")

try:
    while True:
        for pid in plant_ids:
            try:
                device_id = device_id_from_custom(pid)
            except ValueError as ve:
                print(f"[SKIP] {ve}")
                continue

            # 1) 센서 데이터 발행 (매뉴얼 키 사용)
            sensor_payload = make_sensor_payload()
            client.publish(f"GreenEye/data/{device_id}", json.dumps(sensor_payload))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Published sensor data to GreenEye/data/{device_id} (src '{pid}')")
            print(f"DEBUG - Sensor Values for {pid}:")
            print(f"  Temperature: {sensor_payload['amb_temp']}°C")
            print(f"  Humidity: {sensor_payload['amb_humi']}%")
            print(f"  Light: {sensor_payload['amb_light']} lux")
            print(f"  Soil Temp: {sensor_payload['soil_temp']}°C")
            print(f"  Soil Humidity: {sensor_payload['soil_humi']}%")
            print(f"  Soil EC: {sensor_payload['soil_ec']} dS/m")
            print(f"  Comment: {sensor_payload['comment']}")
            print("  Full payload:", json.dumps(sensor_payload, ensure_ascii=False))

            # 2) 주기적으로 이미지 데이터(HEX) 발행
            image_send_counter[pid] += 1
            if image_send_counter[pid] >= IMAGE_SEND_INTERVAL_CYCLES:
                publish_image_hex(device_id)
                image_send_counter[pid] = 0

        time.sleep(sensor_read_interval_sec)

except KeyboardInterrupt:
    print("\n--- Dummy sensor/image data publishing stopped. ---")
finally:
    client.loop_stop()
    client.disconnect()
    print("MQTT client disconnected.")