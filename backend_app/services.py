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
import base64
from PIL import Image, ImageEnhance, ImageDraw, ImageFont
from io import BytesIO


import csv
from io import StringIO

from .database import get_db_connection, get_device_by_device_id_any

FLASH_MAP = {
    "always_on":  {"flash_en": 1, "flash_nt": 1},  # 주/야 모두 플래시
    "always_off": {"flash_en": 0, "flash_nt": 0},  # 항상 끔
    "night_off":  {"flash_en": 1, "flash_nt": 0},  # 주간만 켬
}

def _publish_conf(device_id: str, payload: dict):
    """GreenEye/conf/{device_id} 로 retain publish"""
    topic = f"GreenEye/conf/{device_id}"
    body = json.dumps(payload, ensure_ascii=False)
    mqtt_client.publish(topic, body, qos=1, retain=True)

from .inference import model_manager

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

# 컨테이너 안에서 'localhost'로 잡히면 서비스명으로 강제 전환
_env_mode = os.getenv("ENV_MODE", "docker").lower()
if _env_mode == "docker":
    if INFLUXDB_URL.startswith("http://localhost") or INFLUXDB_URL.startswith("http://127.0.0.1"):
        INFLUXDB_URL = "http://influxdb:8086"

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
    print(f"[InfluxDB] connecting url={INFLUXDB_URL}, org={INFLUXDB_ORG}, bucket={INFLUXDB_BUCKET}")
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
            payload = _safe_json_loads(msg.payload)
            process_incoming_data(msg.topic, payload)
        except Exception as e:
            print(f"Error processing incoming data: {e}")
            # 디버깅용 페이로드 프리뷰
            try:
                print("[payload preview]", msg.payload.decode("utf-8", "replace")[:200])
            except:
                pass


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
        if not data:
            return None
        # ✅ Redis에 BOM/비표준 JSON이 들어와도 복구 시도
        if isinstance(data, str):
            try:
                return json.loads(data)
            except Exception:
                return _safe_json_loads(data.encode("utf-8"))
        return _safe_json_loads(data)
    except Exception as e:
        print(f"Error getting data from Redis: {e}")
        return None

# === 추론 함수 추가 ===
def run_inference_on_image(device_id: str, image_path: str):
    """
    저장된 이미지 파일에 대해 AI 추론을 수행합니다.
    
    Args:
        device_id: 디바이스 ID
        image_path: 이미지 파일 경로
        
    Returns:
        추론 결과 딕셔너리
    """
    try:
        print(f"[AI] Starting inference for device {device_id}, image: {image_path}")
        
        # 이미지 파일 읽기
        with open(image_path, 'rb') as f:
            image_bytes = f.read()
        
        # 기본 plant_type 설정 (나중에 device별로 설정 가능하도록 확장 가능)
        # 예: DB에서 device_id로 plant_type 조회
        plant_type = "default"  # 기본값, 실제로는 DB나 설정에서 가져와야 함
        
        # device 정보에서 plant_type 가져오기 시도
        device_info = get_device_by_device_id_any(device_id)
        if device_info and device_info.get('plant_type'):
            raw_plant_type = device_info['plant_type']
            
            # find text inside parentheses
            # fallback to raw string if no match
            match = re.search(r'\((.*?)\)', raw_plant_type)
            if match:
                plant_type = match.group(1).strip()
            else:
                plant_type = raw_plant_type
        
        result = model_manager.predict(image_bytes, plant_type)

        if "error" in result:
            result['comment'] = get_plant_comment("_error")
        else:
            predicted_label = result.get("predicted_label", "")
            
            # 1. 주요 키 (e.g., "Rose_healthy")
            specific_key = f"{plant_type}_{predicted_label}"
            
            # 2. 대체 키 (e.g., "healthy")는 predicted_label 자체
            
            # 수정된 함수 호출
            result['comment'] = get_plant_comment(primary_key=specific_key, fallback_key=predicted_label)
        
        # 타임스탬프 추가
        result['timestamp'] = datetime.utcnow().isoformat()
        result['device_id'] = device_id
        result['plant_type'] = plant_type
        
        print(f"[AI] Inference completed for {device_id}: {result}")
        return result
        
    except FileNotFoundError:
        error_msg = f"Image file not found: {image_path}"
        print(f"[AI] Error: {error_msg}")
        return {
            "error": error_msg,
            "device_id": device_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    except Exception as e:
        error_msg = f"Inference failed: {str(e)}"
        print(f"[AI] Error: {error_msg}")
        return {
            "error": error_msg,
            "comment": get_plant_comment("_error"), # 예외 발생 시에도 에러 코멘트 추가
            "device_id": device_id,
            "timestamp": datetime.utcnow().isoformat()
        }


# --- 데이터 파이프라인 ---
def process_incoming_data(topic: str, payload):
    try:
        # 추가: 혹시 문자열로 오면 json.loads 한 번 더
        if isinstance(payload, (bytes, str)):
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="ignore")
            payload = json.loads(payload)

        # 토픽: GreenEye/data/{DeviceID}
        # ✅ 항상 4자리 short id로 정규화 (ge-sd-2e52 -> 2e52)
        raw_id = topic.split("/")[-1].strip().lower()
        m = re.fullmatch(r"(?:ge-sd-)?([0-9a-f]{4})", raw_id)
        device_id = m.group(1) if m else raw_id
        print(f"Processing data for device_id: {device_id}")

        dev = get_device_by_device_id_any(device_id)
        mac = dev["mac_address"] if dev else None

        # --- 데이터 종류에 따라 분기 처리 (plant_img 키 유무로 판단) ---
        if "plant_img" in payload:
            try: 
                # Handling Image Data
                # ~.jpg for enhanced image data
                # ~_wstamp.jpg for enhanced image data with timestamp
                # ~.b16 for base16 encoded text data
                image_base64 = payload.get("plant_img")
                if image_base64 and isinstance(image_base64, str):
                    image_dec = base64.b64decode(image_base64)

                    brightness_factor = 1.2   # 20% brighter
                    contrast_factor   = 1.2   # 20% more contrast
                    saturation_factor = 1.2   # 20% more saturation
                    sharpness_factor  = 1.3   # 30% more sharpness

                    with Image.open(BytesIO(image_dec)) as img:
                        # === apply enhancements sequentially ===
                        enhancer = ImageEnhance.Brightness(img)
                        img_enhanced = enhancer.enhance(brightness_factor)
                        
                        enhancer = ImageEnhance.Contrast(img_enhanced)
                        img_enhanced = enhancer.enhance(contrast_factor)
                        
                        enhancer = ImageEnhance.Color(img_enhanced)
                        img_enhanced = enhancer.enhance(saturation_factor)

                        enhancer = ImageEnhance.Sharpness(img_enhanced)
                        img_enhanced = enhancer.enhance(sharpness_factor)
                    
                    buffer = BytesIO()
                    img_enhanced.save(buffer, 'JPEG', quality=100)
                    enhanced_image_bytes = buffer.getvalue()

                    img_with_stamp = img_enhanced.copy() # copy for draw timestamp
                    draw = ImageDraw.Draw(img_with_stamp)

                    current_time = datetime.now()
                    timestamp_text = current_time.strftime(f"{device_id}_%Y-%m-%d %H:%M:%S")

                    try:
                        font = ImageFont.truetype("arial.ttf", size=20) #font select
                    except IOError:
                        font = ImageFont.load_default()

                    img_width, img_height = img_with_stamp.size
                    bbox = draw.textbbox((0, 0), timestamp_text, font=font)
                    text_width = bbox[2] - bbox[0]
                    text_height = bbox[3] - bbox[1]
                    margin = 15
                    x = img_width - text_width - margin
                    y = img_height - text_height - margin

                    # draw timestamp
                    draw.text((x, y), timestamp_text, font=font, fill="white", stroke_width=2, stroke_fill="black")

                    # save wstamp
                    buffer_wstamp = BytesIO()
                    img_with_stamp.save(buffer_wstamp, 'JPEG', quality=100)
                    stamped_image_bytes = buffer_wstamp.getvalue()

                    image_base16 = base64.b16encode(enhanced_image_bytes)
                    image_base16_str = image_base16.decode('UTF-8')

                    filename = f"{device_id}_{current_time.strftime('%Y%m%d%H%M%S')}"
                    filename_jpg = f"{filename}.jpg"
                    filename_jpg_wStamp = f"{filename}_wstamp.jpg"
                    filename_origin = f"{filename}.b16"

                    path_jpg = os.path.join(IMAGE_UPLOAD_FOLDER, filename_jpg)
                    path_jpg_wStamp = os.path.join(IMAGE_UPLOAD_FOLDER, filename_jpg_wStamp)
                    path_origin = os.path.join(IMAGE_UPLOAD_FOLDER, filename_origin)

                    os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)

                    with open(path_jpg, "wb") as f:
                        f.write(enhanced_image_bytes)
                    with open(path_jpg_wStamp, "wb") as f:
                        f.write(stamped_image_bytes)
                    with open(path_origin, "w", encoding="utf-8") as f:
                        f.write(image_base16_str)

                    if mac:
                        try:
                            with get_db_connection() as conn:
                                conn.execute(
                                    "INSERT INTO plant_images (device_id, mac_address, filename, filepath, timestamp) VALUES (?, ?, ?, ?, ?)",
                                    (device_id, mac, filename, path_jpg, datetime.utcnow().isoformat()),
                                )
                                conn.commit()
                        except Exception as e:
                            print(f"Failed to save image meta to DB for {device_id}: {e}")
                    else:
                        # 디바이스 미등록이면 plant_images는 device_id / mac_address NOT NULL 때문에 에러 나니 저장 스킵
                        print(f"Skip DB insert for image because device not registered: {device_id}")

                    set_redis_data(f"latest_image:{device_id}", {"filename": filename})
                    print(f"Image saved: {path_jpg}")

                    diagnosis = run_inference_on_image(device_id, path_jpg)
                    set_redis_data(f"latest_ai_diagnosis:{device_id}", diagnosis)
                    print(f"AI inference complete for {device_id}")
            except (base64.binascii.Error, TypeError) as e:
                print(f"Error decoding Base64 string for device {device_id}: {e}")
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
                "comment": payload.get("comment"),
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


# 프리셋 모드 전송 함수 정의
def send_mode_to_device(device_id: str, 
                        mode_char: str,
                        night_option: str ):
    mode = (mode_char or "M").upper()[:1]
    nht = 1 if night_option == "night_on" else 0
    
    base = {"pwr_mode": mode, "nht_mode": nht}

    payload = dict(base)

    _publish_conf(device_id, payload)
    return payload

def send_config_to_device(device_id: str, config_payload: dict):
    """
    sends a configuration payload to a device via mqtt.
    this function is flexible and accepts both high-level keys (like 'mode')
    and low-level keys (like 'pwr_mode').
    """
    if not mqtt_client.is_connected():
        connect_mqtt()

    if not config_payload:
        print(f"[warn] send_config_to_device received an empty payload for {device_id}.")
        return

    if not isinstance(config_payload, dict) or not config_payload:
        print(f"[error] received an invalid or empty payload for {device_id}: {config_payload}")
        return

    topic = f"GreenEye/gardening/{device_id}"
    payload_str = json.dumps(config_payload)

    try:
        # === publish the message with retain flag ===

        info = mqtt_client.publish(topic, payload_str, qos=1, retain=True)
        info.wait_for_publish(timeout=5) # wait for the message to be sent

        if info.rc == 0:
            print(f"successfully sent config to topic: {topic} payload={payload_str}")
        else:
            print(f"failed to send config to {topic}, return code: {info.rc}")

    except Exception as e:
        print(f"an exception occurred while publishing config for {device_id}: {e}")

        
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

# --- 한줄평 로더 (추가) ---
_comment_cache = {}

def get_plant_comment(primary_key: str = None, fallback_key: str = None) -> str:
    """
    AI가 예측한 레이블을 기반으로 사용자 친화적인 한줄평을 반환합니다.
    JSON 파일을 읽고 그 내용을 캐시에 저장하여 성능을 최적화합니다.
    """
    global _comment_cache

    if not _comment_cache:
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            comment_file_path = os.path.join(current_dir, 'plant_comments.json')
            with open(comment_file_path, 'r', encoding='utf-8') as f:
                _comment_cache = json.load(f)
            print("[INFO] Plant comments loaded successfully.")
        except Exception as e:
            print(f"[ERROR] Failed to load plant_comments.json: {e}")
            _comment_cache = {
                "_default": "분석 결과를 확인해주세요.",
                "_error": "분석 중 오류가 발생했습니다."
            }

    # 1. 주요 키 (e.g., "Rose_healthy")로 먼저 검색
    if primary_key:
        comment = _comment_cache.get(primary_key)
        if comment:
            return comment

    # 2. 주요 키가 없을 경우, 대체 키 (e.g., "healthy")로 검색
    if fallback_key:
        comment = _comment_cache.get(fallback_key)
        if comment:
            return comment

    # 3. 두 키 모두 없을 경우, 기본 메시지 반환
    return _comment_cache.get("_default", "분석 결과를 확인해주세요.")