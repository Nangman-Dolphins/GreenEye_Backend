import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime
from werkzeug.utils import secure_filename

from database import get_db_connection
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

# --- InfluxDB / Redis ---
def connect_influxdb():
    global influxdb_client, influxdb_write_api
    try:
        influxdb_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        print(f"Connected to InfluxDB at {INFLUXDB_URL}")
    except Exception as e:
        print(f"Could not connect to InfluxDB: {e}")

def connect_redis():
    global redis_client
    try:
        redis_client = redis.StrictRedis(
            host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
        )
        redis_client.ping()
        print(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except Exception as e:
        print(f"Could not connect to Redis: {e}")

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
    MQTT로 수신된 센서(+이미지) 데이터를 처리.
    """
    try:
        mac = topic.split("/")[-1]
        print(f"Processing data for device: {mac}")

        # 1) 센서
        tags = {"mac_address": mac}
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
            set_redis_data(f"latest_sensor_data:{mac}", {"timestamp": datetime.utcnow().isoformat(), **valid_fields})
            print(f"Sensor data processed and stored for {mac}")

        # 2) 이미지
        image_hex = payload.get("plant_img")
        if image_hex:
            image_bytes = bytes.fromhex(image_hex)
            filename = f"{mac}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg"
            path = os.path.join(IMAGE_UPLOAD_FOLDER, filename)
            os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)
            with open(path, "wb") as f:
                f.write(image_bytes)

            conn = get_db_connection()
            conn.execute(
                "INSERT INTO plant_images (mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?)",
                (mac, filename, path, datetime.utcnow().isoformat()),
            )
            conn.commit()
            conn.close()

            set_redis_data(f"latest_image:{mac}", {"filename": filename})
            print(f"Image saved: {path}")

            diagnosis = run_inference_on_image(mac, path)
            set_redis_data(f"latest_ai_diagnosis:{mac}", diagnosis)
            print(f"AI inference complete for {mac}")

    except Exception as e:
        print(f"Error in process_incoming_data for topic {topic}: {e}")

# --- 장치 통신 ---
def request_data_from_device(mac_address: str):
    topic = f"GreenEye/req/{mac_address}"
    payload = json.dumps({"req": 0})
    mqtt_client.publish(topic, payload)
    print(f"Sent data request to topic: {topic}")

def send_config_to_device(mac_address: str, config_payload: dict):
    """
    설정 변경 명령 전송.
    [개선] 전송 직후 Redis에 낙관적 상태(optimistic) 기록 → UI/로직 동기화.
    """
    topic = f"GreenEye/conf/{mac_address}"
    payload = json.dumps(config_payload)
    mqtt_client.publish(topic, payload)
    print(f"Sent config to topic: {topic} with payload: {payload}")

    device = config_payload.get("device")
    action = config_payload.get("action")
    if device in ("water_pump", "led") and action in ("on", "off"):
        try:
            set_redis_data(
                f"actuator_state:{mac_address}:{device}",
                {"status": action, "ts": datetime.utcnow().isoformat()},
            )
        except Exception as e:
            print(f"Failed to set optimistic actuator state: {e}")

# --- 초기화 ---
def initialize_services():
    print("\n--- Initializing Backend Services ---")
    connect_mqtt()
    connect_influxdb()
    connect_redis()
    print("--- All services connection attempts made. ---\n")
