import os
import json
import requests
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
import redis
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
import re

import csv
from io import StringIO

from .database import get_db_connection, get_device_by_device_id_any

from .ai_inference import run_inference_on_image

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env", override=False)
if os.getenv("ENV_MODE", "local") == "local":
    load_dotenv(dotenv_path=".env.local", override=True)

# 안전한 JSON 디코더 (BOM/작은따옴표/잘못된 이스케이프 보정)
def _safe_json_loads(b: bytes):
    raw = b  # 원본 보관
    s = None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("utf-8-sig", errors="replace")

    t = s.strip()

    # 양 끝이 작은따옴표로 감싸져 있으면 큰따옴표로 치환
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        t = '"' + t[1:-1].replace('"', '\\"') + '"'

    # 흔한 실수: 키가 작은따옴표로 둘러싸인 JSON 흉내
    # {'a':1,'b':2} -> {"a":1,"b":2}
    if t.startswith("{") and "'" in t and '"' not in t.split(":", 1)[0]:
        t = t.replace("'", '"')

    # 역슬래시가 잘못 들어와 Invalid \escape 터질 때 완화
    # \n, \t 등 정상 시퀀스는 두고, 나머지 lone backslash는 이스케이프
    import re
    def _fix_bad_backslash(m):
        seq = m.group(0)
        # 유효한 \", \\, \/, \b, \f, \n, \r, \t, \uXXXX 는 그대로 둠
        if re.match(r'\\["\\/bfnrt]', seq) or re.match(r'\\u[0-9a-fA-F]{4}', seq):
            return seq
        return '\\\\' + seq[1:]  # 나머지는 백슬래시 이스케이프

    t = re.sub(r'\\.', _fix_bad_backslash, t)

    return json.loads(t)


def _pick(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None

def _to_float(x):
    if x is None: return None
    try: return float(x)
    except (TypeError, ValueError): return None

def _to_int(x):
    if x is None: return None
    try: return int(float(x))  # "40"이나 "40.0"도 정수 40으로
    except (TypeError, ValueError): return None

    
# --- 환경 변수 ---
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

INFLUXDB_URL = os.getenv("INFLUXDB_URL")
INFLUXDB_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG")
INFLUX_MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "sensor_readings")

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
            timeout=30000,
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

def _parse_mqtt_payload(b: bytes):
    s = b.decode("utf-8", "replace").strip()
    # 1차: 정상 JSON 시도
    try:
        return json.loads(s)
    except Exception:
        pass
    # 2차: 흔한 오류 보정
    t = s.replace("'", '"')  # 작은따옴표 -> 큰따옴표
    # {key: ...} 형태의 키에 따옴표 붙이기
    t = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', t)
    # device_id / time 값이 따옴표 없이 올 때 보정
    t = re.sub(r'("device_id"\s*:\s*)([A-Za-z0-9_\-]+)', r'\1"\2"', t)
    t = re.sub(r'("(_time|time)"\s*:\s*)([^",}\s][^,}\s]*)', r'\1"\3"', t)
    return json.loads(t)

# --- MQTT 콜백 ---
def on_message(client, userdata, msg):
    print(f"MQTT Message received: Topic - {msg.topic}")
    if msg.topic.startswith("GreenEye/data/"):
        try:
            payload = _parse_mqtt_payload(msg.payload)
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

def write_sensor_data_to_influxdb(measurement, tags, fields, ts=None):
    from influxdb_client import Point, WritePrecision
    from datetime import datetime, timezone

    global influxdb_client, influxdb_write_api
    if influxdb_client is None:
        connect_influxdb()

    # lazy init or recreate write_api
    if influxdb_write_api is None:
        try:
            influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        except Exception as e:
            print(f"[Influx] write_api init failed: {e}")
            return
    
    point = Point(measurement)
    for k, v in (tags or {}).items():
        point.tag(k, v)
    for k, v in (fields or {}).items():
        # Influx는 float/int만 필드에 허용 → None은 건너뜀
        if v is not None:
            point.field(k, v)
    

    # ✅ 타임스탬프 반영 (ISO8601 / epoch seconds / epoch ms 모두 허용)
    if ts:
        try:
            ts_dt = None
            if isinstance(ts, (int, float)):
                if ts > 1e12:  # ms
                    ts_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
                else:          # s
                    ts_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            elif isinstance(ts, str):
                s = ts.strip()
                if s.isdigit():
                    iv = int(s)
                    ts_dt = datetime.fromtimestamp(
                        iv / (1000.0 if iv > 1e12 else 1.0), tz=timezone.utc
                    )
                else:
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    ts_dt = datetime.fromisoformat(s)
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            if ts_dt:
                point.time(ts_dt, WritePrecision.NS)
        except Exception as e:
            print(f"[Influx] invalid ts '{ts}': {e} ( → server time )")
    lp = point.to_line_protocol()
    print(f"[Influx] write TRY bucket={INFLUXDB_BUCKET} org={INFLUXDB_ORG} lp={lp}")

    # 실제 쓰기 — 실패 시 1회 재연결 후 재시도
    try:
        lp = point.to_line_protocol()  # 🔍 디버깅용
        print(f"[Influx] write TRY bucket={INFLUXDB_BUCKET} org={INFLUXDB_ORG} lp={lp[:200]}")
        influxdb_write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        print("[Influx] write OK")
    except Exception as e:
        print(f"[Influx] write failed once, retrying with fresh client: {e}")
        try:
            influxdb_client.close() if influxdb_client else None
        except Exception:
            pass
        influxdb_client = InfluxDBClient(
            url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG, timeout=30000
        )
        influxdb_write_api = influxdb_client.write_api(write_options=SYNCHRONOUS)
        try:
            influxdb_write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            print("[Influx] write OK after reconnect")
        except Exception as e2:
            print(f"[Influx] write retry failed: {e2}")


def query_influxdb_data(query: str):
    print(f"[DEBUG] 실행할 Flux 쿼리:\n{query}")
    try:
        url = f"{INFLUXDB_URL}/api/v2/query"
        headers = {
            "Authorization": f"Token {INFLUXDB_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv"
        }
        params = {"org": INFLUXDB_ORG}

        response = requests.post(url, params=params, data=query.encode("utf-8"), headers=headers)
        response.raise_for_status()

        decoded = response.content.decode("utf-8", errors="replace")
        
        
        # 🔍 응답 확인용 프리뷰/길이
        preview = "\n".join(decoded.splitlines()[:20])
        print(f"[DEBUG] Influx CSV bytes={len(response.content)} / lines_preview=\n{preview}")

        rows = parse_csv_result(decoded)
        print(f"[DEBUG] parsed_rows_count={len(rows)}")
        if rows:
            print(f"[DEBUG] parsed_sample_keys={list(rows[0].keys())}")
        return rows
        print(f"[DEBUG] parsed_rows={len(rows)}")
        return rows
    except Exception as e:
        print(f"[InfluxDB] Query failed: {e}")
        return None


def set_redis_data(key: str, value):
    if not redis_client:
        print(f"[REDIS] client not ready; skip set {key}")
        return
    try:
        redis_client.set(key, json.dumps(value))
        print(f"[REDIS] SET {key} -> {value}")
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
def process_incoming_data(topic: str, payload):
    try:
        # 추가: 혹시 문자열로 오면 json.loads 한 번 더
        if isinstance(payload, (bytes, str)):
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="ignore")
            payload = json.loads(payload)

        # 토픽: GreenEye/data/{DeviceID}
        device_id = topic.split("/")[-1].lower()
        print(f"Processing data for device_id: {device_id}")

        dev = get_device_by_device_id_any(device_id)
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
            tags = {"device_id": device_id}
            if mac:
                tags["mac_address"] = mac

            # 키 맵핑(서로 다른 펌웨어/테스트 포맷 모두 수용)
            battery        = _pick(payload, "battery", "bat_level", "bat")
            temperature    = _pick(payload, "temperature", "amb_temp", "temp")
            humidity       = _pick(payload, "humidity", "amb_humi", "hum")
            light_lux      = _pick(payload, "light_lux", "amb_light", "lux")
            soil_temp      = _pick(payload, "soil_temp")
            soil_moisture  = _pick(payload, "soil_moisture", "soil_humi")
            soil_ec        = _pick(payload, "soil_ec")

            # 타입 캐스팅: battery는 정수, 나머지는 float
            fields = {
                "battery": _to_int(payload.get("battery") or payload.get("bat_level")),
                "temperature": _to_float(payload.get("temperature") or payload.get("amb_temp")),
                "humidity": _to_float(payload.get("humidity") or payload.get("amb_humi")),
                "light_lux": _to_float(payload.get("light_lux") or payload.get("amb_light")),
                "soil_temp": _to_float(payload.get("soil_temp")),
                "soil_moisture": _to_float(payload.get("soil_moisture") or payload.get("soil_humi")),
                "soil_ec": _to_float(payload.get("soil_ec")),
            }
            valid_fields = {k: v for k, v in fields.items() if v is not None}

            if valid_fields:
                ts_str = payload.get("_time") or payload.get("time") or payload.get("timestamp") or None
                # InfluxDB: 디바이스 타임스탬프 우선
                write_sensor_data_to_influxdb("sensor_readings", tags, valid_fields, ts=ts_str)

                # Redis 캐시: 프론트 조회용, 동일 타입 유지
                redis_doc = {"timestamp": ts_str or datetime.utcnow().isoformat(), **valid_fields}
                set_redis_data(f"latest_sensor_data:{device_id}", redis_doc)
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
    print("[services] ⏳ Connecting to services...")
    connect_mqtt()
    print("[services] ✅ MQTT connected (or tried)")
    connect_influxdb()
    print("[services] ✅ InfluxDB connected (or tried)")
    connect_redis()
    print("[services] ✅ Redis connected (or tried)")
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

def get_influx_client():
    return influxdb_client

__all__ = [
    "connect_influxdb",
    "query_influxdb_data",
    "write_sensor_data_to_influxdb",
    "get_influx_client",
]

def parse_csv_result(decoded_csv: str):
    """
    InfluxDB CSV 응답에서 주석(#...)은 제거하고,
    헤더 맨 앞에 빈 컬럼이 있으면 제거한 뒤 DictReader로 파싱한다.
    '_time' 또는 'time' 컬럼이 있는 행만 반환.
    """
    # 1) 줄 단위 정리: 빈 줄/주석 제거
    raw_lines = decoded_csv.splitlines()
    lines = [ln for ln in raw_lines if ln and not ln.startswith("#")]
    if not lines:
        print("[DEBUG] parse_csv_result: no non-comment lines")
        return []

    # 2) 헤더 파싱 & 맨 앞 빈 컬럼 제거
    header_cols = lines[0].split(",")
    drop_first = (len(header_cols) > 0 and header_cols[0] == "")
    if drop_first:
        header_cols = header_cols[1:]

    # 3) 데이터 라인도 동일하게 첫 컬럼 제거
    fixed_data_lines = []
    for ln in lines[1:]:
        cols = ln.split(",")
        if drop_first and len(cols) > 0:
            cols = cols[1:]
        fixed_data_lines.append(",".join(cols))

    # 4) DictReader로 재구성해서 읽기
    csv_text = ",".join(header_cols) + "\n" + "\n".join(fixed_data_lines)
    reader = csv.DictReader(StringIO(csv_text))

    rows = []
    for r in reader:
        # 실제 데이터만 수집
        if r.get("_time") or r.get("time"):
            rows.append(r)

    print(f"[DEBUG] parsed_rows_count={len(rows)}")
    if rows:
        print(f"[DEBUG] parsed_sample_keys={list(rows[0].keys())}")
    return rows