import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime
import base64
import uuid
from werkzeug.utils import secure_filename

# database.py에서 필요한 함수 임포트 (SQLite에 이미지 메타데이터 저장용)
from database import get_db_connection
# ai_inference.py에서 필요한 함수 임포트 (AI 추론 로직)
from ai_inference import run_inference_on_image # <-- 이 줄 추가

# .env 파일에서 환경 변수 로드
from dotenv import load_dotenv
load_dotenv()

# --- 환경 변수 가져오기 ---
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

# 이미지 저장 폴더 (app.py와 동일한 경로)
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'images')

# --- MQTT 클라이언트 설정 ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"MQTT Broker Connected successfully with result code {rc}")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    print(f"MQTT Message received: Topic - {msg.topic}, Payload length - {len(msg.payload)}")
    
    if msg.topic.startswith("sensor/data/"):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            process_sensor_data(msg.topic, payload)
        except json.JSONDecodeError:
            print(f"Error decoding JSON from MQTT payload (sensor): {msg.payload.decode()}")
        except Exception as e:
            print(f"Error processing MQTT sensor message: {e}")
    elif msg.topic.startswith("image/data/"):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            process_image_data(msg.topic, payload)
        except json.JSONDecodeError:
            print(f"Error decoding JSON from MQTT payload (image): {msg.payload.decode()}")
        except Exception as e:
            print(f"Error processing MQTT image message: {e}")
    else:
        print(f"Unhandled MQTT topic: {msg.topic}")

def connect_mqtt():
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
        mqtt_client.loop_start()
        print(f"Attempting to connect to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")

def publish_mqtt_message(topic, message):
    try:
        mqtt_client.publish(topic, message)
        print(f"Published MQTT message to topic '{topic}': {message}")
    except Exception as e:
        print(f"Error publishing MQTT message: {e}")


# --- InfluxDB 클라이언트 설정 ---
influxdb_client = None
influxdb_write_api = None

def connect_influxdb():
    global influxdb_client, influxdb_write_api
    try:
        influxdb_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        print(f"Connected to InfluxDB at {INFLUXDB_URL}")
    except Exception as e:
        print(f"Could not connect to InfluxDB: {e}")

def write_sensor_data_to_influxdb(measurement, tags, fields):
    if not influxdb_write_api:
        print("InfluxDB write API not initialized.")
        return

    point = Point(measurement)
    for tag_key, tag_value in tags.items():
        point.tag(tag_key, tag_value)
    for field_key, field_value in fields.items():
        point.field(field_key, field_value)
    point.time(datetime.utcnow())

    try:
        influxdb_write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        print(f"Successfully wrote data to InfluxDB: {point.to_line_protocol()}")
    except Exception as e:
        print(f"Error writing data to InfluxDB: {e}")

def query_influxdb_data(query):
    if not influxdb_client:
        print("InfluxDB client not initialized.")
        return []

    try:
        query_api = influxdb_client.query_api()
        tables = query_api.query(query, org=INFLUXDB_ORG)
        results = []
        for table in tables:
            for record in table.records:
                results.append(record.values)
        print(f"Successfully queried data from InfluxDB. Rows: {len(results)}")
        return results
    except Exception as e:
        print(f"Error querying data from InfluxDB: {e}")
        return []


# --- Redis 클라이언트 설정 ---
redis_client = None

def connect_redis():
    global redis_client
    try:
        redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
        redis_client.ping()
        print(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except Exception as e:
        print(f"Could not connect to Redis: {e}")

def set_redis_data(key, value):
    if not redis_client:
        print("Redis client not initialized.")
        return
    try:
        redis_client.set(key, json.dumps(value))
        print(f"Successfully set data in Redis: Key - {key}")
    except Exception as e:
        print(f"Error setting data in Redis: {e}")

def get_redis_data(key):
    if not redis_client:
        print("Redis client not initialized.")
        return None
    try:
        data = redis_client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        print(f"Error getting data from Redis: {e}")
        return None


# --- 센서 데이터 처리 로직 ---
def process_sensor_data(topic, payload):
    """
    MQTT로 수신된 센서 데이터를 InfluxDB에 저장하고 Redis에 캐시하는 함수.
    5가지 센서 값(온도, 습도, 조도, 토양 수분, 토양 전도도)을 처리합니다.
    """
    try:
        topic_parts = topic.split('/')
        if len(topic_parts) >= 3 and topic_parts[0] == "sensor" and topic_parts[1] == "data":
            mac_address = topic_parts[2]
        else:
            print(f"Invalid MQTT topic format for sensor data: {topic}")
            return

        tags = {"mac_address": mac_address}
        fields = {
            "temperature": payload.get("temperature"),
            "humidity": payload.get("humidity"),
            "light_lux": payload.get("light_lux"),
            "soil_moisture": payload.get("soil_moisture"),
            "soil_ec": payload.get("soil_ec")
        }
        for key, value in fields.items():
            if value == 9999:
                fields[key] = None
        
        fields = {k: v for k, v in fields.items() if v is not None}

        if fields:
            write_sensor_data_to_influxdb("sensor_readings", tags, fields)
            latest_data = {
                "timestamp": datetime.utcnow().isoformat(),
                "mac_address": mac_address,
                **fields
            }
            set_redis_data(f"latest_sensor_data:{mac_address}", latest_data)
        else:
            print(f"No valid sensor fields found in payload for {mac_address}. Payload: {payload}")

    except Exception as e:
        print(f"Error in process_sensor_data for topic {topic}: {e}")

# --- 이미지 데이터 처리 로직 ---
def process_image_data(topic, payload):
    """
    MQTT로 수신된 이미지 데이터를 Base64 디코딩하여 로컬에 저장하고 SQLite에 메타데이터를 저장합니다.
    """
    try:
        topic_parts = topic.split('/')
        if len(topic_parts) >= 3 and topic_parts[0] == "image" and topic_parts[1] == "data":
            mac_address = topic_parts[2]
        else:
            print(f"Invalid MQTT topic format for image data: {topic}")
            return

        image_base64 = payload.get("image_data_base64")
        timestamp_str = payload.get("timestamp")

        if not image_base64:
            print(f"No Base64 image data found in payload for {mac_address}.")
            return
        
        image_bytes = base64.b64decode(image_base64)
        
        file_extension = "jpg"
        unique_filename = f"{secure_filename(mac_address)}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}.{file_extension}"
        filepath = os.path.join(IMAGE_UPLOAD_FOLDER, unique_filename)

        os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)
        
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS plant_images (id INTEGER PRIMARY KEY AUTOINCREMENT, mac_address TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, filepath TEXT NOT NULL, timestamp TEXT NOT NULL)",
            ()
        )
        cursor.execute(
            "INSERT INTO plant_images (mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?)",
            (mac_address, unique_filename, filepath, timestamp_str or datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

        set_redis_data(f"latest_image:{mac_address}", {
            "filename": unique_filename,
            "filepath": filepath,
            "timestamp": timestamp_str or datetime.utcnow().isoformat()
        })
        
        print(f"Image uploaded via MQTT and saved: {filepath}")

        # --- AI 추론 로직 호출 부분 ---
        # 이미지 저장이 완료된 후에 AI 추론 함수를 호출합니다.
        from ai_inference import run_inference_on_image
        diagnosis_result = run_inference_on_image(mac_address, filepath)
        # AI 진단 결과를 Redis에 캐싱
        set_redis_data(f"latest_ai_diagnosis:{mac_address}", {"diagnosis": diagnosis_result, "timestamp": datetime.utcnow().isoformat()})

    except Exception as e:
        print(f"Error in process_image_data for topic {topic}: {e}")

# --- 모든 서비스 연결 초기화 함수 ---
def initialize_services():
    print("\n--- Initializing Backend Services ---")
    connect_mqtt()
    connect_influxdb()
    connect_redis()
    print("--- All backend services initialized. ---\n")