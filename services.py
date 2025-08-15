import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime
import base64
from werkzeug.utils import secure_filename
import uuid
import requests

from database import get_db_connection, get_device_by_device_id
from ai_inference import run_inference_on_image

from dotenv import load_dotenv
load_dotenv()

# --- 환경 변수 ---
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")

IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "images")

# --- 클라이언트 ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
influxdb_client = None
influxdb_write_api = None
redis_client = None

# --- MQTT 콜백 ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT Broker Connected successfully")
        client.subscribe("GreenEye/data/#")
        print("Subscribed to MQTT topic 'GreenEye/data/#'")
    else:
        print(f"Failed to connect to MQTT broker, return code {rc}")

def on_message(client, userdata, msg):
    print(f"MQTT Message received: Topic - {msg.topic}")
    if msg.topic.startswith("GreenEye/data/"):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            process_incoming_data(msg.topic, payload)
        except Exception as e:
            print(f"Error processing incoming data: {e}")

def connect_mqtt():
    if MQTT_USERNAME and MQTT_PASSWORD:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")

def write_sensor_data_to_influxdb(measurement, tags, fields):
    if not influxdb_write_api:
        return
    point = Point(measurement)
    for k, v in tags.items():
        point.tag(k, v)
    for k, v in fields.items():
        point.field(k, v)
    point.time(datetime.utcnow())
    try:
        influxdb_write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    except Exception as e:
        print(f"Error writing to InfluxDB: {e}")

def query_influxdb_data(query: str):
    if not influxdb_client:
        return []
    try:
        tables = influxdb_client.query_api().query(query, org=INFLUXDB_ORG)
        return [record.values for table in tables for record in table.records]
    except Exception as e:
        print(f"Error querying data from InfluxDB: {e}")
        return []

def set_redis_data(key: str, value):
    if not redis_client:
        return
    try:
        redis_client.set(key, json.dumps(value))
    except Exception as e:
        print(f"Error setting data in Redis: {e}")

def get_redis_data(key: str):
    if not redis_client:
        return None
    try:
        data = redis_client.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        print(f"Error getting data from Redis: {e}")
        return None

# --- 데이터 파이프라인 ---
def process_incoming_data(topic: str, payload: dict):
    """
    MQTT로 수신된 센서(+이미지) 데이터 처리.
    센서 스펙: bat_level, amb_temp, amb_humi, amb_light, soil_temp, soil_humi, soil_ec, plant_img(HEX)
    """
    try:
        # 토픽: GreenEye/data/{DeviceID}
        device_id = topic.split("/")[-1].lower()
        print(f"Processing data for device_id: {device_id}")

        dev = get_device_by_device_id(device_id)
        mac = dev["mac_address"] if dev else None

        # --- 데이터 종류에 따라 분기 처리 (plant_img 키 유무로 판단) ---
        if "plant_img" in payload:
            # 이미지 데이터 처리
            image_hex = payload.get("plant_img")
            if image_hex:
                image_bytes = bytes.fromhex(image_hex)
                filename = f"{device_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
                path = os.path.join(IMAGE_UPLOAD_FOLDER, filename)
                os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(image_bytes)

                conn = get_db_connection()
                conn.execute(
                    "INSERT INTO plant_images (device_id, mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (device_id, mac, filename, path, datetime.utcnow().isoformat()),
                )
                conn.commit()
                conn.close()

                set_redis_data(f"latest_image:{device_id}", {"filename": filename})
                print(f"Image saved: {path}")

                diagnosis = run_inference_on_image(device_id, path)
                set_redis_data(f"latest_ai_diagnosis:{device_id}", diagnosis)
                print(f"AI inference complete for {device_id}")

        else:
            # 센서 데이터 처리
            tags = {"device_id": device_id}
            if mac:
                tags["mac_address"] = mac

            fields = {
                "battery": payload.get("bat_level"),
                "temperature": payload.get("amb_temp"),
                "humidity": payload.get("amb_humi"),
                "light_lux": payload.get("amb_light"),
                "soil_temp": payload.get("soil_temp"),
                "soil_moisture": payload.get("soil_humi"),
                "soil_ec": payload.get("soil_ec"),
            }
            valid_fields = {k: v for k, v in fields.items() if v is not None}
            if valid_fields:
                write_sensor_data_to_influxdb("sensor_readings", tags, valid_fields)
                set_redis_data(f"latest_sensor_data:{device_id}", {"timestamp": datetime.utcnow().isoformat(), **valid_fields})
                print(f"Sensor data processed and stored for {device_id}")

    except Exception as e:
        print(f"Error in process_incoming_data for topic {topic}: {e}")

# --- 장치 통신 (DeviceID 기준) ---
def request_data_from_device(device_id: str, sensor_only: bool = False):
    topic = f"GreenEye/req/{device_id}"
    payload = {"req": 1 if sensor_only else 0}
    mqtt_client.publish(topic, json.dumps(payload))
    print(f"Sent data request to topic: {topic} payload={payload}")

def send_config_to_device(device_id: str, config_payload: dict):
    allowed = {"flash_en": (0, 1), "flash_nt": (0, 1), "flash_level": (0, 255)}
    to_send = {}
    for k, v in (config_payload or {}).items():
        if k in allowed:
            lo, hi = allowed[k]
            if isinstance(v, int) and lo <= v <= hi:
                to_send[k] = v

    topic = f"GreenEye/conf/{device_id}"
    mqtt_client.publish(topic, json.dumps(to_send))
    print(f"Sent config to topic: {topic} payload={to_send}")

    if "flash_en" in to_send or "flash_nt" in to_send or "flash_level" in to_send:
        set_redis_data(
            f"actuator_state:{device_id}:flash",
            {"ts": datetime.utcnow().isoformat(), **to_send},
        )
        
# --- MQTT 퍼블리시(앱에서 기대하는 공개 API) ---
def publish_mqtt_message(topic: str, payload, qos: int = 0, retain: bool = False) -> bool:
    """
    앱(app.py)이 import 해서 쓰는 표준 퍼블리시 함수.
    payload가 dict/list면 JSON 문자열로 변환해서 전송.
    MQTT 연결이 안 되어 있으면 자동으로 연결 시도.
    """
    try:
        # payload를 문자열로 정규화
        if isinstance(payload, (dict, list)):
            payload_str = json.dumps(payload, ensure_ascii=False)
        else:
            payload_str = str(payload)

        # 필요 시 연결
        if not mqtt_client.is_connected():
            connect_mqtt()

        info = mqtt_client.publish(topic, payload_str, qos=qos, retain=retain)
        try:
            # 전송 완료까지 최대 5초 대기 (성공 시 True)
            info.wait_for_publish(timeout=5)
        except TypeError:
            # 일부 버전에선 timeout 파라미터가 없을 수 있음
            info.wait_for_publish()

        return getattr(info, "rc", 0) == 0
    except Exception as e:
        print(f"Error publishing MQTT message to {topic}: {e}")
        return False

# --- 헬스체크용 ---
def is_connected_mqtt():
    return mqtt_client.is_connected()

def is_connected_influx():
    return influxdb_client is not None

def is_connected_redis():
    try:
        return redis_client is not None and redis_client.ping()
    except:
        return False

# --- 초기화 ---
def initialize_services():
    print("\n--- Initializing Backend Services ---")
    connect_mqtt()
    connect_influxdb()
    connect_redis()
    print("--- All services connection attempts made. ---\n")
