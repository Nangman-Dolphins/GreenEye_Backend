import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime
from werkzeug.utils import secure_filename

from database import get_db_connection, get_device_by_device_id
from ai_inference import run_inference_on_image

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env.local")

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
query_api = None 

# --- Redis 연결 ---
def connect_redis():
    global redis_client
    try:
        redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=0,
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        redis_client.ping()
        print("Redis connected.")
    except Exception as e:
        redis_client = None
        print(f"Redis connection failed: {e}")

def connect_influxdb():
    """InfluxDB v2 연결"""
    global influxdb_client, influxdb_write_api, query_api
    try:
        influxdb_client = InfluxDBClient(
            url=INFLUXDB_URL,
            token=INFLUXDB_TOKEN,
            org=INFLUXDB_ORG,
            timeout=30000,  # ms
        )
        influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        query_api = influxdb_client.query_api()
        print("InfluxDB connected.")
    except Exception as e:
        influxdb_client = None
        influxdb_write_api = None
        print(f"InfluxDB connection failed: {e}")

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
    # ✅ 환경변수에서 가져오되 없으면 기본값 사용
    broker_host = os.getenv("MQTT_BROKER_HOST", "localhost")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", 1883))

    if MQTT_USERNAME and MQTT_PASSWORD:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    try:
        mqtt_client.connect(broker_host, broker_port, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Could not connect to MQTT broker at {broker_host}:{broker_port} → {e}")

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
    try:
        tables = query_api.query(org=INFLUXDB_ORG, query=query)
        result = []
        for table in tables:
            for record in table.records:
                values = record.values
                # 🔍 pivot 여부에 따라 처리 다르게
                if "_field" in values and "_value" in values:
                    # 🟢 pivot 되지 않은 일반 쿼리
                    result.append({
                        "_time": values.get("_time"),
                        "_field": values.get("_field"),
                        "_value": values.get("_value"),
                        "device_id": values.get("device_id")
                    })
                else:
                    # 🔵 pivot된 결과 (열 이름이 각 필드)
                    result.append(values)
        return result
    except Exception as e:
        print(f"[InfluxDB] Query failed: {e}")
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

                if mac:
                    try:
                        with get_db_connection() as conn:
                            conn.execute(
                                "INSERT INTO plant_images (device_id, mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?, ?)",
                                (device_id, mac, filename, path, datetime.utcnow().isoformat()),
                            )
                            conn.commit()
                    except Exception as e:
                        print(f"Failed to save image meta to DB for {device_id}: {e}")
                else:
                    # 디바이스 미등록이면 plant_images는 device_id / mac_address NOT NULL 때문에 에러 나니 저장 스킵
                    print(f"Skip DB insert for image because device not registered: {device_id}")

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

# 이 함수는 현재 사용되지 않으며, SD 장치가 자율적으로 센싱 주기를 관리하기 때문에 일단 임시로 주석 처리
# def request_data_from_device(device_id: str, sensor_only: bool = False):
#     topic = f"GreenEye/req/{device_id}"
#     payload = {"req": 1 if sensor_only else 0}
#     mqtt_client.publish(topic, json.dumps(payload))
#     print(f"Sent data request to topic: {topic} payload={payload}")

# 프리셋 모드 매핑(임시) 정의
PRESET_MODES = {
    "Z": {"pwr_mode": "Z", "nht_mode": 1, "flash_en": 0, "flash_nt": 0, "flash_level": 0},
    "L": {"pwr_mode": "L", "nht_mode": 1, "flash_en": 1, "flash_nt": 0, "flash_level": 120},
    "M": {"pwr_mode": "M", "nht_mode": 1, "flash_en": 1, "flash_nt": 0, "flash_level": 160},
    "H": {"pwr_mode": "H", "nht_mode": 1, "flash_en": 1, "flash_nt": 1, "flash_level": 200},
    "U": {"pwr_mode": "U", "nht_mode": 0, "flash_en": 1, "flash_nt": 1, "flash_level": 255},
}

# 프리셋 모드 전송 함수 정의
def send_mode_to_device(device_id: str, mode: str):
    if mode not in PRESET_MODES:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {list(PRESET_MODES.keys())}.")
    config = PRESET_MODES[mode]
    send_config_to_device(device_id, config)
    return config

def send_config_to_device(device_id: str, config_payload: dict):
    if not mqtt_client.is_connected():
        connect_mqtt()

    allowed_int = {
        "flash_en": (0, 1),
        "flash_nt": (0, 1),
        "flash_level": (0, 255),
        "nht_mode": (0, 1)
    }
    allowed_str = {
        "pwr_mode": {"Z", "L", "M", "H", "U"}
    }

    to_send = {}
    for k, v in (config_payload or {}).items():
        if k in allowed_int:
            lo, hi = allowed_int[k]
            if isinstance(v, int) and lo <= v <= hi:
                to_send[k] = v
        elif k in allowed_str:
            if isinstance(v, str) and v.upper() in allowed_str[k]:
                to_send[k] = v.upper()

    topic = f"GreenEye/conf/{device_id}"
    result = mqtt_client.publish(topic, json.dumps(to_send), retain=True)
    result.wait_for_publish()  # ✅ 메시지 전송 완료까지 기다림
    print(f"Sent config to topic: {topic} payload={to_send}")  

    if any(k in to_send for k in ("flash_en", "flash_nt", "flash_level")):
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
    for name in ("connect_mqtt", "connect_influxdb", "connect_redis"):
        func = globals().get(name, None)
        if callable(func):
            try:
                func()
                print(f"{name} ok")
            except Exception as e:
                print(f"{name} failed: {e}")
        else:
            print(f"{name} not defined — skipping")
    print("--- All services connection attempts made. ---\n")
