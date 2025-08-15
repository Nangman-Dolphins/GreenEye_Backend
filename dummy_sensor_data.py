import paho.mqtt.client as mqtt
import time
import json
import random
import os
from datetime import datetime
from dotenv import load_dotenv

# .env 파일 로드
load_dotenv()

# --- 환경 변수 ---
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
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

# --- 테스트용 MAC 주소 목록 (device_id = MAC 하위 4자리, 소문자) ---
plant_mac_addresses = ["AA:BB:CC:DD:EE:F1", "AA:BB:CC:DD:EE:F2", "AA:BB:CC:DD:EE:F3"]
sensor_read_interval_sec = 5
image_send_counter = {mac: 0 for mac in plant_mac_addresses}

def device_id_from_mac(mac: str) -> str:
    return mac.replace(":", "").lower()[-4:]

def make_sensor_payload():
    """매뉴얼 키 이름으로 센서 페이로드 생성"""
    return {
        "bat_level": random.randint(1, 100),
        "amb_temp": round(random.uniform(20.0, 30.0), 2),
        "amb_humi": round(random.uniform(50.0, 80.0), 2),
        "amb_light": round(random.uniform(800, 2000), 2),
        "soil_temp": round(random.uniform(18.0, 25.0), 2),
        "soil_humi": round(random.uniform(50.0, 90.0), 2),
        "soil_ec": round(random.uniform(0.5, 3.0), 2),
    }

def publish_image_hex(device_id: str, img_path: str = TEST_IMAGE_PATH):
    """이미지를 HEX 문자열로 읽어 plant_img로 발행 (매뉴얼 준수)"""
    try:
        with open(img_path, "rb") as f:
            hex_str = f.read().hex()  # ★ 핵심: base64 대신 HEX로 전송
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
print(f"Publishing to topic 'GreenEye/data/{{DeviceID}}' for {', '.join(plant_mac_addresses)}")
print(f"Image mode: HEX (per manual)\n")

try:
    while True:
        for mac_address in plant_mac_addresses:
            device_id = device_id_from_mac(mac_address)

            # 1) 센서 데이터 발행 (매뉴얼 키 사용)
            sensor_payload = make_sensor_payload()
            client.publish(f"GreenEye/data/{device_id}", json.dumps(sensor_payload))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Published sensor data to GreenEye/data/{device_id}")

            # 2) 주기적으로 이미지 데이터(HEX) 발행
            image_send_counter[mac_address] += 1
            if image_send_counter[mac_address] >= IMAGE_SEND_INTERVAL_CYCLES:
                publish_image_hex(device_id)
                image_send_counter[mac_address] = 0

        time.sleep(sensor_read_interval_sec)

except KeyboardInterrupt:
    print("\n--- Dummy sensor/image data publishing stopped. ---")
finally:
    client.loop_stop()
    client.disconnect()
    print("MQTT client disconnected.")