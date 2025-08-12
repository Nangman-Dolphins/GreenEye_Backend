import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime
import uuid
from werkzeug.utils import secure_filename

# 다른 모듈에서 필요한 함수 임포트
from database import get_db_connection
from ai_inference import run_inference_on_image

# .env 파일 로드
from dotenv import load_dotenv
load_dotenv()

# --- 환경 변수 ---
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT'))
MQTT_USERNAME = os.getenv('MQTT_USERNAME')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD')

INFLUXDB_URL = os.getenv('INFLUXDB_URL')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET')

REDIS_HOST = os.getenv('REDIS_HOST')
REDIS_PORT = int(os.getenv('REDIS_PORT'))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD')

IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'images')

# --- 클라이언트 초기화 ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
influxdb_client = None
influxdb_write_api = None
redis_client = None

# --- MQTT 클라이언트 콜백 및 연결 ---
def on_connect(client, userdata, flags, rc):
    """MQTT 브로커 연결 성공 시 호출되는 콜백 함수"""
    if rc == 0:
        print(f"MQTT Broker Connected successfully")
        # [변경] 데이터 수신 토픽만 구독
        client.subscribe("GreenEye/data/#")
        print("Subscribed to MQTT topic 'GreenEye/data/#'")
    else:
        print(f"Failed to connect to MQTT broker, return code {rc}\n")

def on_message(client, userdata, msg):
    """MQTT 메시지 수신 시 호출되는 콜백 함수"""
    print(f"MQTT Message received: Topic - {msg.topic}")
    # [변경] GreenEye/data/ 토픽의 메시지만 처리
    if msg.topic.startswith("GreenEye/data/"):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            process_incoming_data(msg.topic, payload)
        except Exception as e:
            print(f"Error processing incoming data: {e}")

def connect_mqtt():
    """MQTT 브로커에 연결을 시도하는 함수"""
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")

# --- InfluxDB 및 Redis 연결/헬퍼 함수 ---
def connect_influxdb():
    """InfluxDB에 연결하는 함수"""
    global influxdb_client, influxdb_write_api
    try:
        influxdb_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        print(f"Connected to InfluxDB at {INFLUXDB_URL}")
    except Exception as e:
        print(f"Could not connect to InfluxDB: {e}")

def connect_redis():
    """Redis에 연결하는 함수"""
    global redis_client
    try:
        redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
        redis_client.ping()
        print(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except Exception as e:
        print(f"Could not connect to Redis: {e}")

def write_sensor_data_to_influxdb(measurement, tags, fields):
    """센서 데이터를 InfluxDB에 쓰는 함수"""
    if not influxdb_write_api: return
    point = Point(measurement)
    for tag_key, tag_value in tags.items(): point.tag(tag_key, tag_value)
    for field_key, field_value in fields.items(): point.field(field_key, field_value)
    point.time(datetime.utcnow())
    try:
        influxdb_write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
    except Exception as e:
        print(f"Error writing to InfluxDB: {e}")

def query_influxdb_data(query):
    """InfluxDB에서 데이터를 조회하는 함수"""
    if not influxdb_client: return []
    try:
        query_api = influxdb_client.query_api()
        tables = query_api.query(query, org=INFLUXDB_ORG)
        results = [record.values for table in tables for record in table.records]
        return results
    except Exception as e:
        print(f"Error querying data from InfluxDB: {e}")
        return []

def set_redis_data(key, value):
    """Redis에 데이터를 쓰는 함수"""
    if not redis_client: return
    try:
        redis_client.set(key, json.dumps(value))
    except Exception as e:
        print(f"Error setting data in Redis: {e}")

def get_redis_data(key):
    """Redis에서 데이터를 읽는 함수"""
    if not redis_client: return None
    try:
        data = redis_client.get(key)
        return json.loads(data) if data else None
    except Exception as e:
        print(f"Error getting data from Redis: {e}")
        return None

# --- 데이터 처리 메인 로직 ---
def process_incoming_data(topic, payload):
    """
    [신규] MQTT로 수신된 통합 데이터(센서+이미지)를 처리하는 함수
    """
    try:
        # 'GreenEye/data/{Device_ID}'에서 Device_ID 추출
        mac_address = topic.split('/')[-1]
        print(f"Processing data for device: {mac_address}")

        # 1. 센서 데이터 처리 (매뉴얼 기반 Key 사용)
        tags = {"mac_address": mac_address}
        fields = {
            "battery": payload.get("bat_level"),
            "temperature": payload.get("amb_temp"),
            "humidity": payload.get("amb_humi"),
            "light_lux": payload.get("amb_light"),
            "soil_temp": payload.get("soil_temp"),
            "soil_moisture": payload.get("soil_humi"),
            "soil_ec": payload.get("soil_ec")
        }
        valid_fields = {k: v for k, v in fields.items() if v is not None}

        if valid_fields:
            write_sensor_data_to_influxdb("sensor_readings", tags, valid_fields)
            set_redis_data(f"latest_sensor_data:{mac_address}", {"timestamp": datetime.utcnow().isoformat(), **valid_fields})
            print(f"Sensor data processed and stored for {mac_address}")

        # 2. 이미지 데이터 처리 (HEX → JPG)
        image_hex = payload.get("plant_img")
        if image_hex:
            image_bytes = bytes.fromhex(image_hex)
            unique_filename = f"{secure_filename(mac_address)}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
            filepath = os.path.join(IMAGE_UPLOAD_FOLDER, unique_filename)
            os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)
            
            with open(filepath, "wb") as f:
                f.write(image_bytes)
            
            conn = get_db_connection()
            conn.execute(
                "INSERT INTO plant_images (mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?)",
                (mac_address, unique_filename, filepath, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()
            set_redis_data(f"latest_image:{mac_address}", {"filename": unique_filename})
            print(f"Image saved: {filepath}")

            diagnosis_result = run_inference_on_image(mac_address, filepath)
            set_redis_data(f"latest_ai_diagnosis:{mac_address}", diagnosis_result)
            print(f"AI inference complete for {mac_address}")

    except Exception as e:
        print(f"Error in process_incoming_data for topic {topic}: {e}")

# --- 데이터 발행 함수 ---
def request_data_from_device(mac_address):
    """
    [신규] 특정 장치에 데이터 전송을 요청합니다.
    """
    topic = f"GreenEye/req/{mac_address}"
    payload = json.dumps({"req": 0})
    mqtt_client.publish(topic, payload)
    print(f"Sent data request to topic: {topic}")

def send_config_to_device(mac_address, config_payload):
    """
    [신규] 특정 장치에 설정 변경 명령을 보냅니다. (플래시, 액추에이터 제어 등)
    """
    topic = f"GreenEye/conf/{mac_address}"
    payload = json.dumps(config_payload)
    mqtt_client.publish(topic, payload)
    print(f"Sent config to topic: {topic} with payload: {payload}")

# --- 서비스 초기화 ---
def initialize_services():
    """모든 외부 서비스(MQTT, InfluxDB, Redis) 연결을 초기화합니다."""
    print("\n--- Initializing Backend Services ---")
    connect_mqtt()
    connect_influxdb()
    connect_redis()
    print("--- All services connection attempts made. ---\n")
