# dummy_sensor_data.py (최종 코드 - MAC 주소 및 매뉴얼 키 이름 기반 데이터 발행)

import paho.mqtt.client as mqtt
import time
import json
import random
import os
from datetime import datetime
from dotenv import load_dotenv
import base64
import uuid

# .env 파일 로드
load_dotenv()

# --- 환경 변수 가져오기 ---
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!')

# --- 이미지 전송 설정 ---
TEST_IMAGE_PATH = 'test_plant.jpg' 
IMAGE_SEND_INTERVAL_CYCLES = 2

# --- MQTT 클라이언트 설정 ---
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    """MQTT 브로커에 연결되었을 때 호출되는 콜백 함수."""
    if rc == 0:
        print(f"Dummy sensor/image connected to MQTT Broker at {MQTT_BROKER_PORT}:{MQTT_BROKER_PORT}")
    else:
        print(f"Dummy sensor/image failed to connect, return code {rc}")
        print(f"Check your MQTT broker status or credentials in .env file.")

client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
client.on_connect = on_connect

# MQTT 브로커에 연결
try:
    client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
    client.loop_start()
except Exception as e:
    print(f"Could not connect dummy sensor/image to MQTT broker: {e}")
    print("Please ensure your Docker Mosquitto container is running and credentials are correct.")
    exit(1)

# --- 더미 데이터 발행 로직 ---
# 테스트용 MAC 주소 목록
plant_mac_addresses = ["AA:BB:CC:DD:EE:F1", "AA:BB:CC:DD:EE:F2", "AA:BB:CC:DD:EE:F3"] 
sensor_read_interval_sec = 5
image_send_counter = {mac: 0 for mac in plant_mac_addresses}

print(f"\n--- Starting dummy sensor/image data publishing every {sensor_read_interval_sec} seconds ---")
print(f"Publishing to topic 'GreenEye/data/{{DeviceID}}' for {', '.join(plant_mac_addresses)}")

try:
    while True:
        for mac_address in plant_mac_addresses:
            device_id = mac_address.replace(":", "").lower()[-4:]

            # 1. 센서 데이터 발행 (매뉴얼 키 이름 amb_temp, amb_humi 등으로 변경)
            sensor_payload = {
                "bat_level": random.randint(1, 100),
                "amb_temp": round(random.uniform(20.0, 30.0), 2),
                "amb_humi": round(random.uniform(50.0, 80.0), 2),
                "amb_light": round(random.uniform(1000, 2000), 2),
                "soil_temp": round(random.uniform(18.0, 25.0), 2),
                "soil_humi": round(random.uniform(50.0, 90.0), 2),
                "soil_ec": round(random.uniform(0.5, 3.0), 2),
            }
            client.publish(f"GreenEye/data/{device_id}", json.dumps(sensor_payload))
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Published sensor data to GreenEye/data/{device_id}")

            # 2. 이미지 데이터 발행 (매뉴얼 키 이름 plant_img으로 변경)
            image_send_counter[mac_address] += 1
            if image_send_counter[mac_address] >= IMAGE_SEND_INTERVAL_CYCLES:
                image_base64_string = ""
                try:
                    with open(TEST_IMAGE_PATH, "rb") as image_file:
                        image_base64_string = base64.b64encode(image_file.read()).decode('utf-8')
                    
                    image_payload = {
                        "plant_img": image_base64_string, # 매뉴얼 키 이름으로 변경
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    }
                    client.publish(f"GreenEye/data/{device_id}", json.dumps(image_payload))
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Published image data to GreenEye/data/{device_id}")
                except FileNotFoundError:
                    print(f"ERROR: Test image file not found at {TEST_IMAGE_PATH}.")
                except Exception as e:
                    print(f"ERROR publishing image: {e}")
                
                image_send_counter[mac_address] = 0

        time.sleep(sensor_read_interval_sec)

except KeyboardInterrupt:
    print("\n--- Dummy sensor/image data publishing stopped. ---")
finally:
    client.loop_stop()
    client.disconnect()
    print("MQTT client disconnected.")
