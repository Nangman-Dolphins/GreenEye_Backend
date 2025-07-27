import os
import json
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime

# .env 파일에서 환경 변수 로드
# from dotenv import load_dotenv
# load_dotenv()

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


# --- MQTT 클라이언트 설정 ---
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"MQTT Broker Connected successfully with result code {rc}")
        # 여기에 구독할 토픽을 추가
        # client.subscribe("sensor/data")
    else:
        print(f"Failed to connect, return code {rc}\n")

def on_message(client, userdata, msg):
    print(f"MQTT Message received: Topic - {msg.topic}, Payload - {msg.payload.decode()}")
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
        process_sensor_data(msg.topic, payload) # 수신된 데이터를 처리하는 함수 호출
    except json.JSONDecodeError:
        print(f"Error decoding JSON from MQTT payload: {msg.payload.decode()}")
    except Exception as e:
        print(f"Error processing MQTT message: {e}")

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
    5가지 센서 값을 처리
    """
    try:
        # 토픽에서 plant_id 추출 ("sensor/data/plant_001")
        topic_parts = topic.split('/')
        if len(topic_parts) >= 3 and topic_parts[0] == "sensor" and topic_parts[1] == "data":
            plant_id = topic_parts[2]
        else:
            print(f"Invalid MQTT topic format: {topic}")
            return

        # InfluxDB에 저장할 데이터 준비
        # measurement: 'sensor_readings' (측정값의 종류)
        # tags: 어떤 식물에서 온 데이터인지 식별 (plant_id)
        # fields: 실제 센서 값들
        tags = {"plant_id": plant_id}
        fields = {
            "temperature": payload.get("temperature"),     # 온도
            "humidity": payload.get("humidity"),           # 습도
            "light_lux": payload.get("light_lux"),         # 조도
            "soil_moisture": payload.get("soil_moisture"), # 토양 수분
            "soil_ec": payload.get("soil_ec")              # 토양 전도도
        }
        # None 값 필터링: InfluxDB는 None 값을 저장하지 않으므로 유효한 필드만 남김
        fields = {k: v for k, v in fields.items() if v is not None}

        if fields: # 유효한 필드 데이터가 있을 때만 저장
            write_sensor_data_to_influxdb("sensor_readings", tags, fields)

            # Redis에 최신 데이터 캐시
            # 키는 'latest_sensor_data:식물ID' 형식으로 저장.
            latest_data = {
                "timestamp": datetime.utcnow().isoformat(), # ISO 형식 UTC 시간
                "plant_id": plant_id,
                **fields # 센서 필드 값들을 최신 데이터에 포함
            }
            set_redis_data(f"latest_sensor_data:{plant_id}", latest_data)
        else:
            print(f"No valid sensor fields found in payload for {plant_id}. Payload: {payload}")

    except Exception as e:
        print(f"Error in process_sensor_data for topic {topic}: {e}")

# --- 모든 서비스 연결 초기화 함수 ---
def initialize_services():
    print("\n--- Initializing Backend Services ---")
    connect_mqtt()
    connect_influxdb()
    connect_redis()
    print("--- All backend services initialized. ---\n")