import paho.mqtt.client as mqtt
import redis
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

# --- 더미 데이터 설정 ---
DEVICE_IDS = ["6c18", "eef2", "00a9"]
PLANT_CONDITIONS = [
    {
        "status": "healthy",
        "comment": "식물이 건강하게 자라고 있어요! 🌱",
        "confidence": 0.92
    },
    {
        "status": "need_water",
        "comment": "물을 조금 더 주시면 좋을 것 같아요. 💧",
        "confidence": 0.85
    },
    {
        "status": "too_much_water",
        "comment": "수분이 너무 많아요. 며칠 동안 물을 주지 말아주세요. 💦",
        "confidence": 0.88
    },
    {
        "status": "need_light",
        "comment": "빛이 부족해 보입니다. 햇빛이 잘 드는 곳으로 옮겨주세요. ☀️",
        "confidence": 0.87
    },
    {
        "status": "optimal",
        "comment": "토양 상태가 최적입니다. 👍",
        "confidence": 0.95
    }
]

# --- 환경 변수 ---
MQTT_BROKER_HOST = "localhost"  # MQTT 브로커 호스트를 localhost로 고정
MQTT_BROKER_PORT = 1883        # 기본 MQTT 포트

MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!')

# Redis 연결 설정
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

# --- MQTT 클라이언트 설정 ---
mqtt_client = mqtt.Client()  # 기본 버전 사용

def connect_redis():
    """Redis 연결"""
    try:
        client = redis.Redis(
            host="localhost",  # Redis 호스트를 localhost로 고정
            port=REDIS_PORT,
            password=REDIS_PASSWORD,
            decode_responses=True
        )
        client.ping()  # 연결 테스트
        print(f"Redis connected at {REDIS_HOST}:{REDIS_PORT}")
        return client
    except Exception as e:
        print(f"Redis connection failed: {e}")
        return None

def on_mqtt_connect(client, userdata, flags, rc):
    """MQTT 브로커 접속 결과 콜백"""
    if rc == 0:
        print(f"Dummy sensor/image connected to MQTT Broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    else:
        print(f"Dummy sensor/image failed to connect, return code {rc}")
        print("Check your MQTT broker status or credentials in .env file.")

def make_sensor_payload():
    """센서 데이터 생성"""
    return {
        "bat_level": 50, 
        "amb_temp": round(random.uniform(20.0, 30.0), 2),
        "amb_humi": round(random.uniform(50.0, 80.0), 2),
        "amb_light": round(random.uniform(800, 2000), 2),
        "soil_temp": round(random.uniform(18.0, 25.0), 2),
        "soil_humi": round(random.uniform(50.0, 90.0), 2),
        "soil_ec": round(random.uniform(0.5, 3.0), 2)
    }

def generate_ai_inference(device_id: str, sensor_data: dict):
    """센서 데이터를 기반으로 AI 추론 결과 생성"""
    # 센서 값에 따라 상태 결정
    if sensor_data["soil_humi"] < 60:
        condition = next(c for c in PLANT_CONDITIONS if c["status"] == "need_water")
    elif sensor_data["soil_humi"] > 85:
        condition = next(c for c in PLANT_CONDITIONS if c["status"] == "too_much_water")
    elif sensor_data["amb_light"] < 1000:
        condition = next(c for c in PLANT_CONDITIONS if c["status"] == "need_light")
    else:
        condition = random.choice([c for c in PLANT_CONDITIONS if c["status"] in ["healthy", "optimal"]])

    return {
        "device_id": device_id,
        "predicted_label": condition["status"],
        "comment": condition["comment"],
        "confidence": condition["confidence"],
        "timestamp": datetime.utcnow().isoformat(),
        "plant_type": "tomato",
        "sensor_context": {
            "temperature": sensor_data["amb_temp"],
            "humidity": sensor_data["amb_humi"],
            "light": sensor_data["amb_light"],
            "soil_moisture": sensor_data["soil_humi"]
        }
    }

def main():
    # Redis 연결
    redis_client = connect_redis()
    if not redis_client:
        print("Redis connection failed. Exiting...")
        return

    # MQTT 연결 설정
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqtt_client.on_connect = on_mqtt_connect

    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")
        print("Please ensure your Docker Mosquitto container is running and credentials are correct.")
        return

    print(f"\n--- Starting dummy data publishing every 5 seconds ---")
    print(f"Publishing to MQTT topic 'GreenEye/data/{{device_id}}' for {', '.join(DEVICE_IDS)}")
    print(f"Publishing to Redis with key 'latest_ai_diagnosis:{{device_id}}'\n")

    try:
        while True:
            for device_id in DEVICE_IDS:
                # 1. 센서 데이터 생성 및 MQTT 발행
                sensor_data = make_sensor_payload()
                mqtt_client.publish(f"GreenEye/data/{device_id}", json.dumps(sensor_data))
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Device {device_id}:")
                print("  MQTT - Sensor Values:")
                print(f"    Temperature: {sensor_data['amb_temp']}°C")
                print(f"    Humidity: {sensor_data['amb_humi']}%")
                print(f"    Light: {sensor_data['amb_light']} lux")
                print(f"    Soil Temp: {sensor_data['soil_temp']}°C")
                print(f"    Soil Humidity: {sensor_data['soil_humi']}%")
                print(f"    Soil EC: {sensor_data['soil_ec']} dS/m")

                # 2. AI 추론 결과 생성 및 Redis 저장
                inference = generate_ai_inference(device_id, sensor_data)
                redis_key = f"latest_ai_diagnosis:{device_id}"
                redis_client.set(redis_key, json.dumps(inference))

                print("  Redis - AI Inference:")
                print(f"    Status: {inference['predicted_label']}")
                print(f"    Confidence: {inference['confidence']:.2%}")
                print(f"    Comment: {inference['comment']}")
                print()

            time.sleep(5)

    except KeyboardInterrupt:
        print("\n--- Dummy data publishing stopped. ---")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        redis_client.close()
        print("MQTT and Redis connections closed.")

if __name__ == "__main__":
    main()
