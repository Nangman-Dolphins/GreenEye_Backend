import os, secrets
import json
import uuid
import pytz
from datetime import datetime, timedelta
from urllib.parse import urlparse
import base64
import requests
import jwt
import re
from functools import wraps
from threading import Lock

from flask_cors import CORS

from flask import Flask, jsonify, request, send_from_directory, g
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler

from .chat_database import init_chat_db, save_message, load_history, get_user_conversations

from .services import (
    initialize_services,
    mqtt_client,
    get_redis_data,
    query_influxdb_data,
    publish_mqtt_message,
    set_redis_data,
    process_incoming_data,
    send_config_to_device,
    is_connected_influx, is_connected_mqtt, is_connected_redis,
    send_mode_to_device
)

from .database import (
    init_db,
    add_user,
    get_user_by_email,
    check_password,
    add_device,
    get_device_by_device_id,        
    get_device_by_device_id_any,               
    get_all_devices,
    get_all_devices_any,
)

from backend_app.database import get_db_connection  # 최신 이미지 DB 폴백용

from backend_app.control_logic import (
    handle_manual_control,
    check_and_apply_auto_control
)
from backend_app.report_generator import send_all_reports
from backend_app.standards_loader import classify_payload

load_dotenv()
app = Flask(__name__)

# 채팅 이미지 저장을 위한 폴더 경로를 정의합니다.
CHAT_IMAGE_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "uploads", "chat_images")
os.makedirs(CHAT_IMAGE_FOLDER, exist_ok=True) # 폴더가 없으면 생성

ENV = os.getenv("FLASK_ENV", "production").lower()
SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
if not SECRET_KEY:
    if ENV in ("development", "dev", "debug"):
        # 개발 환경: 임시 키 허용(로그로만 알림)
        SECRET_KEY = secrets.token_urlsafe(32)
        print("[warn] SECRET_KEY not set; generated a dev-only key.")
    else:
        raise RuntimeError("SECRET_KEY is not set (production)")
app.config["SECRET_KEY"] = SECRET_KEY

socketio = SocketIO(app, cors_allowed_origins="*")

CORS(app, resources={r"/api/*": {
    "origins": ["http://localhost:5173", "http://localhost:3000"]
}})

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization")
        if not token or not token.startswith("Bearer "):
            return jsonify({"message": "Token is missing!"}), 401
        token = token.split(" ")[1]
        try:
            data = jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])
            g.current_user = get_user_by_email(data["email"])
        except jwt.PyJWTError as e:
            return jsonify({"message": "Token is invalid!", "error": str(e)}), 401
        return f(*args, **kwargs)
    return decorated

if not hasattr(app, "before_first_request"):
    _run_once_lock = Lock()
    _run_once_flag = {"done": False}
    _health_skip_paths = {"/healthz", "/health", "/api/healthz", "/api/health"}

    def _before_first_request_decorator(func):
        @wraps(func)
        def _return_original(*args, **kwargs):
            return func(*args, **kwargs)

        @app.before_request
        def _run_once_wrapper():
            # 헬스 체크 경로는 초기화 건너뛰기
            if request.path in _health_skip_paths:
                return

            if _run_once_flag["done"]:
                return
            with _run_once_lock:
                if _run_once_flag["done"]:
                    return
                _run_once_flag["done"] = True
                func()  # 에러는 그대로 올려서 로그에 보이게

        return _return_original

    app.before_first_request = _before_first_request_decorator

from werkzeug.utils import secure_filename

# === App-level constants & helper bindings ===
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "images")
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
DEFAULT_SHARED_PREFIXES = {"default_", "common_"}  # 공용 이미지 삭제 방지 접두사
os.makedirs(IMAGE_UPLOAD_FOLDER, exist_ok=True)

def _allowed_ext(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTS

def _save_device_image(file_storage, device_id: str) -> str | None:
    """
    업로드된 이미지를 device_id 기반 단일 파일로 저장하고,
    DB에 넣을 상대경로('images/<filename>')를 반환한다.
    """
    if not file_storage or not file_storage.filename.strip():
        return None
    fname = secure_filename(file_storage.filename)
    if not _allowed_ext(fname):
        raise ValueError("Unsupported file type")
    ext = fname.rsplit(".", 1)[1].lower()
    out_name = f"{device_id}.{ext}"
    abs_path = os.path.join(IMAGE_UPLOAD_FOLDER, out_name)
    file_storage.save(abs_path)
    return f"images/{out_name}"

def _is_shared_image(rel_path: str) -> bool:
    try:
        base = os.path.basename(rel_path)
        return any(base.startswith(px) for px in DEFAULT_SHARED_PREFIXES)
    except Exception:
        return False

def _delete_device_image(rel_path: str) -> bool:
    """
    DB에 저장된 상대경로('images/<file>')를 실제 경로로 변환하여 삭제.
    공용 접두사 이미지는 건너뜀.
    """
    if not rel_path or _is_shared_image(rel_path):
        return False
    base = os.path.basename(rel_path)  # 안전
    abs_path = os.path.join(IMAGE_UPLOAD_FOLDER, base)
    if os.path.exists(abs_path):
        os.remove(abs_path)
        return True
    return False

def _delete_all_images_for_device(device_id: str) -> int:
    """
    확장자가 달라질 수 있으니 device_id.* 패턴을 모두 정리한다.
    반환값: 삭제한 파일 개수
    """
    import glob
    removed = 0
    pattern = os.path.join(IMAGE_UPLOAD_FOLDER, f"{device_id}.*")
    for path in glob.glob(pattern):
        base = os.path.basename(path)
        if any(base.startswith(px) for px in DEFAULT_SHARED_PREFIXES):
            continue
        try:
            os.remove(path)
            removed += 1
        except Exception:
            pass
    return removed

def _image_public_url(device_id: str, filename: str) -> str:
    """
    프론트에서 바로 <img src> 로 쓸 수 있는 내부 API URL 생성.
    기존 이미지 서빙 라우트(`/api/images/<device_id>/<filename>`)를 그대로 활용.
    """
    if not filename:
        return None
    safe = secure_filename(filename)
    return f"/api/images/{device_id}/{safe}"

def _get_latest_image_row_from_db(device_id: str):
    """
    Redis에 최신 포인터가 없을 때 DB `plant_images`에서 가장 최근 이미지를 1건 가져온다.
    services.py의 process_incoming_data가 이 테이블에 적재함. (filename, filepath, timestamp 등)
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT filename, filepath, timestamp FROM plant_images WHERE device_id=? ORDER BY timestamp DESC LIMIT 1",
                (device_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[latest-image] DB fallback failed for {device_id}: {e}")
        return None

def _compose_latest_image_payload(device, include_ai: bool = True):
    """
    device(dict): get_device_by_device_id() 가 반환한 장치 레코드
    - Redis `latest_image:{device_id}` → DB plant_images 폴백 → 페이로드 생성
    - AI 진단은 Redis `latest_ai_diagnosis:{device_id}` 에서 함께 포함(선택)
    """
    device_id = device["device_id"]

    # Redis 최신 포인터 조회
    latest = get_redis_data(f"latest_image:{device_id}") or {}
    filename = latest.get("filename")
    timestamp = latest.get("timestamp")

    # 폴백: DB 최근 이미지 1건
    if not filename:
        row = _get_latest_image_row_from_db(device_id)
        if row:
            # services.py는 filepath에 풀 경로, filename에는 접두 없는 파일명 저장
            # 여기서는 filename만 쓰고, 내려줄 URL은 기존 이미지 라우트로 구성
            filename = row.get("filename")
            timestamp = row.get("timestamp")

    payload = {
        "device_id": device_id,
        "friendly_name": device.get("friendly_name") or device_id,
        "filename": filename,
        "timestamp": timestamp,
        "image_url": _image_public_url(device_id, filename) if filename else None,
    }

    # (옵션) 최신 AI 진단 포함
    if include_ai:
        ai = get_redis_data(f"latest_ai_diagnosis:{device_id}") or None
        if ai:
            payload["ai"] = ai

    return payload

@app.get("/api/devices/<device_id>/latest-image")
@token_required
def api_latest_image(device_id: str):
    """
    단일 디바이스의 최신 이미지를 반환.
    - Redis `latest_image:{device_id}` 우선
    - 없으면 DB `plant_images`의 최근 레코드로 폴백
    - 최종 URL은 기존 `/api/images/<device_id>/<filename>` 로 접근
    """
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    info = _compose_latest_image_payload(dev, include_ai=True)
    if not info.get("filename"):
        return jsonify({"error": "No image found"}), 404
    return jsonify(info), 200

@app.get("/api/devices/latest-images")
@token_required
def api_latest_images_for_user():
    """
    현재 사용자 소유 모든 디바이스의 최신 이미지를 묶어서 반환.
    쿼리 ?limit=N 은 향후 갤러리 확장 시를 위해 예약(현재는 단건).
    """
    owner_user_id = g.current_user["id"]
    devices = get_all_devices(owner_user_id) or []
    results = []
    for d in devices:
        results.append(_compose_latest_image_payload(d, include_ai=True))
    return jsonify(results), 200

# Influx 기본 설정
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUX_MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "sensor_readings")

DEVICE_PREFIX = os.getenv("DEVICE_PREFIX", "ge-sd")

def normalize_device_id(raw: str) -> str:
    if not raw:
        return raw
    r = raw.strip().lower()
    m = re.fullmatch(rf"{DEVICE_PREFIX}-([0-9a-f]{{4}})", r)
    return m.group(1) if m else r

def to_device_code(short_id: str) -> str:
    sid = (short_id or "").strip().lower()
    return f"{DEVICE_PREFIX}-{sid}" if re.fullmatch(r"[0-9a-f]{4}", sid) else short_id

def _to_device_id_from_any(s: str) -> str:
    if not s:
        return ""
    t = s.strip()
    if t.lower().startswith("ge-sd-"):
        t = t.split("-", 2)[-1]  # 'ge-sd-' 뒤쪽
    t = t.replace(":", "").replace("-", "")
    return t[-4:].lower()

def _normalize_mac_like(s: str) -> str:
    if not s:
        return s
    t = s.strip()
    if t.lower().startswith("ge-sd-"):
        suf = t.split("-", 2)[-1]
        suf = "".join(ch for ch in suf if ch.isalnum())[-4:].upper()
        return f"{DEVICE_PREFIX}{suf}"
    if ":" in t:  # 풀 MAC
        return t.upper()
    # 짧은 식별자(마지막 4자리만 넘겨온 경우 등)
    suf = "".join(ch for ch in t if ch.isalnum())[-4:].upper()
    return f"{DEVICE_PREFIX}{suf}"

# Alert thresholds 파일 경로 + 기본값 (경고 임계치 API가 필요하다면)
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
TH_FILE = os.path.join(DATA_DIR, "alert_thresholds.json")
DEFAULT_TH = {
    "temperature": {"min": 10, "max": 35},
    "humidity": {"min": 30, "max": 85},
    "soil_moisture": {"min": 40, "max": 90},
}

# Redis 키/헬퍼
def _redis_key_latest_sensor(device_id: str) -> str:
    return f"latest_sensor_data:{device_id}"

def _redis_key_latest_ai(device_id: str) -> str:
    return f"latest_ai_diagnosis:{device_id}"

def get_latest_sensor_data_from_redis(device_id: str):
    return get_redis_data(_redis_key_latest_sensor(device_id)) or None

def get_latest_ai_from_redis(device_id: str):
    return get_redis_data(_redis_key_latest_ai(device_id)) or None

# DB에서 friendly_name 조회
def get_friendly_name(device_id: str) -> str:
    dev = get_device_by_device_id_any(device_id)
    return (dev and dev.get("friendly_name")) or device_id



@app.before_first_request
def _boot_once():
    with app.app_context():
        init_runtime_and_scheduler()

def send_realtime_data_to_clients(device_id: str):
    """Redis에 캐시된 최신 센서 데이터를 Socket.IO로 브로드캐스트."""
    try:
        data = get_redis_data(f"latest_sensor_data:{device_id}") or {}
        # ★ plant_type을 DB에서 읽어 상태까지 포함해 내려준다
        dev = get_device_by_device_id_any(device_id)
        plant_type = (dev and dev.get("plant_type")) or None
        values = classify_payload(plant_type, data)  # {"temperature": {"value":..,"status":..,"range":[..]}, ...}
        payload = {
            "device_id": device_id,
            "plant_type": plant_type,
            "timestamp": data.get("timestamp"),
            "values": values,
        }
        socketio.emit("realtime_data", payload)
    except Exception as e:
        print(f"Realtime push failed for {device_id}: {e}")

def init_runtime_and_scheduler():
    print("🧪 [DEBUG] init_runtime_and_scheduler() 시작됨")
    try:
        print("[init] ⏳ initialize_services()...")
        initialize_services()
        print("[init] ✅ initialize_services() done")

        print("[init] ⏳ init_db()...")
        init_db()
        print("[init] ✅ init_db() done")

        print("[init] ⏳ init_chat_db()...")
        init_chat_db()
        print("[init] ✅ init_chat_db() done")

        scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Seoul")

        print("[init] ⏳ get_all_devices_any()...")
        devices = get_all_devices_any()
        print(f"[init] ✅ Found {len(devices)} device(s) in DB")

        if not devices:
            print("No devices found in DB. Skipping scheduler setup for auto control.")
        else:
            for device in devices:
                try:
                    device_id = device['device_id']
                    friendly_name = device['friendly_name']
                    print(f"[init] ⏳ Scheduling jobs for {friendly_name} ({device_id})")

                    scheduler.add_job(check_and_apply_auto_control, "interval", minutes=1, args=[device_id], id=f"auto_control_job_{device_id}", replace_existing=True)
                    scheduler.add_job(send_realtime_data_to_clients, "interval", seconds=5, args=[device_id], id=f"realtime_data_job_{device_id}", replace_existing=True)
                except Exception as inner_e:
                    print(f"[init] ❌ Error scheduling job for device: {device} -> {inner_e}")

        scheduler.add_job(send_all_reports, "cron", day="1", hour="0", minute="5", id="monthly_report_job", replace_existing=True)
        print("[init] ✅ Scheduled monthly report job to run on the 1st of every month at 00:05")

        scheduler.start()
        print("[init] ✅ APScheduler started.")
    
    except Exception as e:
        import traceback
        print("[init] ❌ Exception occurred in init_runtime_and_scheduler:")
        traceback.print_exc()

@app.route("/")
def home():
    return "Hello, GreenEye Backend is running!"

@app.get("/healthz")
@app.get("/api/healthz")
def healthz():
    return {"status": "ok"}, 200

@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.get("/api/health")
def health():
    return jsonify({
        "api": "ok",
        "mqtt": "ok" if is_connected_mqtt() else "down",
        "influxdb": "ok" if is_connected_influx() else "down",
        "redis": "ok" if is_connected_redis() else "down",
    })

@app.get("/health")
def root_health():
    return health()

@app.route("/api/latest_sensor_data/<device_id>")
@token_required
def api_latest_sensor_data(device_id):
    device_id = normalize_device_id(device_id)
    owner_user_id = g.current_user["id"]

    # 이 유저의 장치인지 확인
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error":"Device not found"}), 404

    # Redis → Influx 폴백은 기존 로직 그대로
    data = get_latest_sensor_data_from_redis(device_id)
    ai   = get_latest_ai_from_redis(device_id)
    # Redis가 없거나 값이 비어 있으면 Influx 폴백
    def _is_empty_payload(d: dict) -> bool:
        if not d: return True
        keys = ["temperature","humidity","light_lux","soil_moisture","soil_ec","soil_temp","battery"]
        return all(d.get(k) in (None, "", []) for k in keys)
    if not data or _is_empty_payload(data):
        flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -7d)
          |> filter(fn: (r) => r._measurement == "{INFLUX_MEASUREMENT}")
          |> filter(fn: (r) => r.device_id == "{device_id}")
          |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
          |> keep(columns: ["_time","device_id",
                            "temperature","Temperature",
                            "humidity","Humidity",
                            "light_lux","lightLux","light","Light","Lux",
                            "soil_moisture","soilMoisture",
                            "soil_ec","soilEC",
                            "soil_temp","soilTemp",
                            "battery","Battery"])
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
        '''
        rows = query_influxdb_data(flux) or []
        if rows:
            r = rows[0]
            # 필드 alias 대응 + 숫자형 변환
            rl = {str(k).lower(): r[k] for k in r.keys()}
            def pick(*names):
                for n in names:
                    if n in r and r[n] not in (None, ""):
                        return r[n]
                for n in names:
                    v = rl.get(str(n).lower())
                    if v not in (None, ""):
                        return v
                return None
            def to_num(x):
                try:
                    # 이미 숫자면 그대로
                    if isinstance(x, (int, float)):
                        return x
                    if x is None or x == "":
                        return None
                    return float(str(x))
                except Exception:
                    return None
            data = {
                "timestamp": pick("_time", "time"),
                "temperature": to_num(pick("temperature","Temperature")),
                "humidity": to_num(pick("humidity","Humidity")),
                "light_lux": to_num(pick("light_lux","lightLux","light","Light","Lux")),
                "soil_moisture": to_num(pick("soil_moisture","soilMoisture")),
                "soil_ec": to_num(pick("soil_ec","soilEC")),
                "soil_temp": to_num(pick("soil_temp","soilTemp")),
                "battery": to_num(pick("battery","Battery")),
            }
        else:
            return jsonify({"error": "No data found"}), 404
    else:
        # Redis 경로도 숫자형으로 정규화(문자열 숫자 → float)
        def to_num(x):
            try:
                if isinstance(x, (int, float)):
                    return x
                if x is None or x == "":
                    return None
                return float(str(x))
            except Exception:
                return None
        for k in ["temperature","humidity","light_lux","soil_moisture","soil_ec","soil_temp","battery"]:
            if k in data:
                data[k] = to_num(data.get(k))
    # 여기서 상태값을 계산해서 프론트로 전달
    plant_type = dev.get("plant_type")
    values = classify_payload(plant_type, data)
    resp = {
        "device_id": device_id,
        "friendly_name": dev["friendly_name"],
        "plant_type": plant_type,
        "timestamp": data.get("timestamp"),
        "values": values,
    }
    if ai:
        resp["ai_diagnosis"] = ai
    return jsonify(resp)

def _to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _normalize_latest_row(d):
    # Redis/Influx/내부 dict 키가 섞여 있을 때 timestamp 키만 맞춰줌
    t = d.get("timestamp") or d.get("_time") or d.get("time")
    d["timestamp"] = t
    return d

def build_device_code(prefix: str, device_id: str) -> str:
    """prefix와 device_id를 안전하게 결합하여 'prefix-device_id' 형태로 반환.
    - prefix 양끝의 하이픈 제거
    - 빈 조각은 제외
    - 최종 문자열 내 연속 하이픈을 한 개로 축약
    """
    p = (prefix or "").strip().strip("-")
    d = (device_id or "").strip().lower()
    parts = [x for x in [p, d] if x]           # 빈 값 제거
    s = "-".join(parts)
    return re.sub(r"-+", "-", s)               # 연속 하이픈 축약

def _rget(row, key, default=None):
    """sqlite3.Row 또는 dict 모두에서 안전하게 키를 꺼낸다."""
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default

@app.route("/api/historical_sensor_data/<device_id>")
@token_required
def get_historical_sensor_data(device_id: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    flux_pivot = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "{INFLUX_MEASUREMENT}")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","soil_moisture","soil_ec","soil_temp","battery"])
      |> rename(columns: {{_time: "time"}})
      |> sort(columns: ["time"])
    '''
    data = query_influxdb_data(flux_pivot) or []
    print(f"[DEBUG] api/historical -> device={device_id} rows={len(data)}")
    if not data:
        # --- 폴백: pivot 없이 raw 50개만 확인 ---
        flux_raw = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -7d)
          |> filter(fn: (r) => r._measurement == "{INFLUX_MEASUREMENT}")
          |> filter(fn: (r) => r.device_id == "{device_id}")
          |> keep(columns: ["_time","_field","_value","device_id"])
          |> sort(columns: ["_time"])
          |> limit(n: 50)
        '''
        raw = query_influxdb_data(flux_raw) or []
        # raw를 time 기준으로 필드 병합s
        by_time = {}
        for r in raw:
            t = r.get("_time")
            if not t:
                continue
            d = by_time.setdefault(t, {"time": t, "device_id": r.get("device_id")})
            fld = r.get("_field")
            val = r.get("_value")
            if fld:
                d[fld] = (float(val) if isinstance(val, str) and val.replace('.','',1).isdigit() else val)
        data = list(by_time.values())
        data.sort(key=lambda x: x.get("time"))

    # friendly_name 부여
    for row in data:
        row["friendly_name"] = dev["friendly_name"]

    return jsonify(data)

# --- 디버그: Influx 폴백 원시 1건을 그대로 확인 ---
@app.get("/api/debug/latest_sensor_raw/<device_id>")
@token_required
def debug_latest_sensor_raw(device_id: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    flux = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "{INFLUX_MEASUREMENT}")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","lightLux","light",
                        "soil_moisture","soilMoisture","soil_ec","soilEC","soil_temp","soilTemp","battery","Battery"])
      |> sort(columns: ["_time"], desc: true)
      |> limit(n: 1)
    '''
    rows = query_influxdb_data(flux) or []
    if not rows:
        return jsonify({"error": "No data found"}), 404
    # rows[0]를 그대로 반환(프론트에서 필드 확인용)
    return jsonify(rows[0]), 200

# ---- 디버그 유틸: 라우트 목록 ----
@app.get("/api/debug/routes")
def debug_routes():
    out = []
    for rule in app.url_map.iter_rules():
        out.append({
            "rule": str(rule),
            "methods": sorted(list(rule.methods - {'HEAD','OPTIONS'})),
            "endpoint": rule.endpoint,
        })
    out.sort(key=lambda x: x["rule"])
    return jsonify(out), 200

# ---- 디버그: 셀프체크(어떤 경로를 탔는지) ----
@app.get("/api/debug/selfcheck/<device_id>")
@token_required
def debug_selfcheck(device_id: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    def _is_empty_payload(d: dict) -> bool:
        if not d:
            return True
        keys = ["temperature","humidity","light_lux","soil_moisture","soil_ec","soil_temp","battery"]
        return all(d.get(k) in (None, "", []) for k in keys)

    redis_data = get_latest_sensor_data_from_redis(device_id)
    path_used = "redis"
    chosen = redis_data

    if _is_empty_payload(redis_data or {}):
        path_used = "influx_fallback"
        flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -7d)
          |> filter(fn: (r) => r._measurement == "{INFLUX_MEASUREMENT}")
          |> filter(fn: (r) => r.device_id == "{device_id}")
          |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: 1)
        '''
        rows = query_influxdb_data(flux) or []
        chosen = rows[0] if rows else None

    return jsonify({
        "device_id": device_id,
        "path_used": path_used,
        "redis_has_data": bool(redis_data),
        "redis_payload_preview_keys": (list(chosen.keys()) if isinstance(chosen, dict) else None)
    }), 200

@app.route("/api/control_device/<device_id>", methods=["POST"])
@token_required
def control_device(device_id: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error":"Device not found"}), 404

    config_data = request.get_json()
    if not config_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    send_config_to_device(device_id, config_data)
    return jsonify({"status": "success", "message": f"Configuration sent to {device_id}"})

# 변경(보안/소유자 확인 추가)
@app.route("/api/control_mode/<device_id>", methods=["POST"])
@token_required
def control_device_by_mode(device_id: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error":"Device not found"}), 404

    data = request.get_json(silent=True) or {}

    # 1) 프론트에서 오는 모드 키를 1글자 코드로 정규화 (둘 다 허용)
    #    ultra_low → Z, low → L, normal → M, high → H, ultra_high → U
    raw_mode = str(data.get("mode", "")).strip()
    mode_map = {
        "ultra_low":"Z", "low":"L", "normal":"M", "high":"H", "ultra_high":"U",
        "Z":"Z", "L":"L", "M":"M", "H":"H", "U":"U"
    }
    mode_char = mode_map.get(raw_mode.upper() if len(raw_mode) == 1 else raw_mode.lower())
    if not mode_char:
        return jsonify({"error": "Invalid 'mode'. Use one of Z/L/M/H/U or ultra_low/low/normal/high/ultra_high."}), 400

    # 야간 플래시를 야간 모드로 통합
    night_option = data.get("night_option")
    #flash_level  = data.get("flash_level")  # 0~255 정수 (미지정이면 그대로 None)

    try:
        applied = send_mode_to_device(device_id, mode_char, night_option=night_option)
        return jsonify({"status":"success", "device_id": device_id, "mode": mode_char, "applied_config": applied})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/auth/register", methods=["POST"])
def register_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if add_user(email, password):
        return jsonify({"status": "success", "message": "User registered successfully"}), 201
    else:
        return jsonify({"error": "Email already exists"}), 409

@app.route("/api/auth/login", methods=["POST"])
def login_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    user = get_user_by_email(email)
    if user and check_password(user["password_hash"], password):
        token = jwt.encode({"email": user["email"], "id": user["id"]}, app.config["SECRET_KEY"], algorithm="HS256")
        return jsonify({"status": "success", "message": "Logged in successfully", "token": token}), 200
    else:
        return jsonify({"error": "Invalid email or password"}), 401

@app.route("/api/register_device", methods=["POST"])
@token_required
def register_device():
    try:
        device_image_path = None
        room = ""
        species = ""

        if request.content_type and "multipart/form-data" in request.content_type:
            mac = request.form.get("mac_address")
            friendly_name = request.form.get("friendly_name")
            room = request.form.get("room") or ""
            species = request.form.get("species") or ""
            if not mac or not friendly_name:
                return jsonify({"error": "mac_address and friendly_name are required"}), 400
        else:
            if not request.is_json:
                return jsonify({"error": "Request must be JSON"}), 400
            data = request.get_json(silent=True) or {}
            mac = data.get("mac_address")
            friendly_name = data.get("friendly_name")
            room = data.get("room") or ""
            species = data.get("species") or ""
            if not mac or not friendly_name:
                return jsonify({"error": "mac_address and friendly_name are required"}), 400

            # base64 이미지는 일단 파싱만(저장은 device_id 계산 후)
            pending_b64 = None
            image_base64 = data.get("image_base64")
            if image_base64:
                header, b64data = image_base64.split(",", 1) if "," in image_base64 else ("", image_base64)
                pending_b64 = b64data

        mac = mac.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{2}-[A-Za-z0-9]{2}-[0-9a-fA-F]{4}", mac) and \
            not re.fullmatch(r"ge-sd-[0-9a-fA-F]{4}", mac.lower()):
            return jsonify({"error":"mac_address must match 'ge-sd-0000' (4 hex)"}), 400

        mac_norm = mac.upper()
        device_id = mac_norm.split("-")[-1].lower()
        owner_user_id = g.current_user["id"]

        # ✅ 유효성 검사 통과 후에만 파일 저장 (multipart)
        if request.content_type and "multipart/form-data" in request.content_type:
            file = request.files.get("image")
            if file and file.filename:
                try:
                    device_image_path = _save_device_image(file, device_id)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
                
        # ✅ JSON base64 저장도 여기서(device_id 확보 후)
        elif 'pending_b64' in locals() and pending_b64:
            try:
                img_bytes = base64.b64decode(pending_b64)
                filename = f"{device_id}.png"
                save_path = os.path.join(IMAGE_UPLOAD_FOLDER, filename)
                with open(save_path, "wb") as f:
                    f.write(img_bytes)
                device_image_path = f"images/{filename}"
            except Exception as e:
                return jsonify({"error": f"Invalid base64 image: {e}"}), 400
        
        created = add_device(
            mac_norm,
            friendly_name,
            owner_user_id,
            device_image=device_image_path,
            plant_type=species,
            room=room
        )
        if created:
            return jsonify({
                "message":"registered",
                "mac_address": mac_norm,
                "device_id": device_id,
                "device_image": device_image_path,
                "plant_type": species,
                "room": room
            }), 201
        else:
            return jsonify({"error":"Device already exists","mac_address": mac_norm,"device_id": device_id}), 409

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "internal_error", "detail": str(e)}), 500

@app.route("/api/images/<device_id>/<filename>")
@token_required
def get_image(device_id: str, filename: str):
    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    safe_filename = secure_filename(filename)
    return send_from_directory(IMAGE_UPLOAD_FOLDER, safe_filename)


@app.route("/api/devices", methods=["GET"])
@token_required
def list_devices():
    owner_user_id = g.current_user["id"]
    # DB 기준으로 이 유저의 장치 목록
    devices = get_all_devices(owner_user_id) or []
    return jsonify(devices)

@app.route("/api/devices/<device_id>/image", methods=["POST"])
@token_required
def upload_device_image(device_id: str):
    """
    기존 디바이스에 대표 이미지를 추가/교체한다 (multipart/form-data, key: image).
    - 교체 시 기존 device_id.* 파일들을 먼저 정리한 뒤 새 파일을 저장.
    """
    from backend_app.database import get_device_by_device_id, update_device_image

    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    if not (request.content_type and "multipart/form-data" in request.content_type):
        return jsonify({"error": "Content-Type must be multipart/form-data"}), 400

    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"error": "Missing file 'image'"}), 400

    # 기존 파일들 정리(확장자 바뀌는 경우 대비)
    _delete_all_images_for_device(device_id)
    # 새 파일 저장
    try:
        rel_path = _save_device_image(file, device_id)  # images/<device_id>.<ext>
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    ok = update_device_image(device_id, owner_user_id, rel_path)
    if not ok:
        return jsonify({"error": "Failed to update device image"}), 500
    return jsonify({"message": "image_updated", "device_id": device_id, "device_image": rel_path}), 200

@app.route("/api/devices/<device_id>/image", methods=["DELETE"])
@token_required
def delete_device_image(device_id: str):
    """
    대표 이미지를 제거한다(파일 삭제 + DB 경로 NULL).
    """
    from backend_app.database import get_device_by_device_id, update_device_image

    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    removed_files = 0
    rel = dev.get("device_image")
    if rel:
        # 정확히 저장된 경로 제거 + 혹시 남아있을 확장자 변형도 제거
        _delete_device_image(rel)
        removed_files += _delete_all_images_for_device(device_id)

    ok = update_device_image(device_id, owner_user_id, None)
    if not ok:
        return jsonify({"error": "Failed to clear device image"}), 500
    return jsonify({"message": "image_deleted", "device_id": device_id, "removed_files": removed_files}), 200

@app.route("/api/devices/<device_id>", methods=["DELETE"])
@token_required
def delete_device(device_id: str):
    """
    디바이스 삭제: 소유자 검증 → (있다면) 대표 이미지 삭제 → DB 레코드 삭제
    """
    from backend_app.database import get_device_by_device_id, delete_device_from_db

    owner_user_id = g.current_user["id"]
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404

    removed = False
    rel = dev.get("device_image")
    if rel:
        removed = _delete_device_image(rel)

    ok = delete_device_from_db(device_id, owner_user_id)
    if not ok:
        # 이론상 여기 도달하지 않음(위의 fetch로 존재 확인을 했기 때문)
        return jsonify({"error": "Failed to delete device"}), 500

    return jsonify({
        "message": "Device deleted",
        "image_removed": bool(removed),
        "skipped_shared": _is_shared_image(rel) if rel else False
    }), 200

def _load_thresholds():
    try:
        with open(TH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_TH.copy()

def _save_thresholds(obj):
    os.makedirs(os.path.dirname(TH_FILE), exist_ok=True)
    with open(TH_FILE, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

@app.route("/api/alert_thresholds", methods=["GET"])
def get_alert_thresholds():
    return jsonify(_load_thresholds())

@app.route("/api/alert_thresholds", methods=["PUT"])
def put_alert_thresholds():
    body = request.get_json(force=True, silent=True) or {}
    th = _load_thresholds()
    # 부분 업데이트 허용
    for key in ["temperature", "humidity", "soil_moisture"]:
        if key in body and isinstance(body[key], dict):
            th.setdefault(key, {})
            for k in ["min","max"]:
                if k in body[key]:
                    th[key][k] = body[key][k]
    _save_thresholds(th)
    return jsonify(th)


# --- Gemini API 관련 설정 ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

# base64 이미지 예시
#{
#    "prompt": "이 이미지에 대해 설명해주세요",
#    "image": "data:image/jpeg;base64,/9j/4AAQSkZJRg..."
#}
# hex 이미지 예시
#{
#    "prompt": "이 이미지에 대해 설명해주세요",
#    "image": "0xFFD8FFE000104A46494600010101006000600000FFDB00430008060607060508..."
#}

@app.route('/api/chat/gemini', methods=['POST'])
@token_required
def chat_with_gemini():
    try:
        data = request.get_json()
        if not data or 'prompt' not in data:
            return jsonify({"error": "메시지를 입력해주세요."}), 400

        user_prompt = data.get('prompt')
        image_data = data.get('image')
        conversation_id = data.get('conversation_id', str(uuid.uuid4()))
        current_user_id = g.current_user['id']

        # --- 이미지 데이터 처리 (이 부분은 동일) ---
        image_base64 = None
        image_url_to_save = None
        if image_data:
            # ✅ 디버깅용 print문 추가
            print("✅ 이미지 데이터 수신됨, 파일 저장을 시도합니다.")
            # 데이터 URI 형식(e.g., "data:image/jpeg;base64,...")인 경우, 순수 Base64 부분만 추출
            if image_data.startswith('data:image'):
                image_base64 = image_data.split(',')[1]
            else:
                # 이미 순수 Base64 문자열인 경우, 그대로 사용
                image_base64 = image_data
            try:
                image_bytes = base64.b64decode(image_base64)
                filename = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
                save_path = os.path.join(CHAT_IMAGE_FOLDER, filename)
                 # ✅ 디버깅용 print문 추가
                print(f"➡️ 이미지를 다음 경로에 저장합니다: {save_path}")
                with open(save_path, "wb") as f:
                    f.write(image_bytes)
                 # ✅ 디버깅용 print문 추가
                print("✅ 이미지 파일 저장 성공!")
                # 프론트엔드에서 접근할 URL 경로를 생성합니다.
                image_url_to_save = f"/uploads/chat_images/{filename}"
            except Exception as e:
                print(f"Error saving image: {e}")

        # 1. 사용자 메시지를 DB에 저장합니다.
        save_message(conversation_id, current_user_id, 'user', user_prompt, image_url=image_url_to_save)
        
        # 2. 방금 저장한 메시지를 포함한 '전체' 대화 기록을 불러옵니다.
        chat_history = load_history(conversation_id, current_user_id)
        
        # 3. '전체' 대화 기록을 Gemini API 형식으로 변환합니다.
        contents = []
        for i, (sender, message, image_url) in enumerate(chat_history):
            role = 'user' if sender == 'user' else 'model'
            
            # 마지막 메시지(현재 사용자 메시지)에만 이미지를 추가합니다.
            if i == len(chat_history) - 1 and image_base64:
                parts = [
                    {"text": message},
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}}
                ]
            else:
                parts = [{"text": message}]
            
            contents.append({"role": role, "parts": parts})

        # API 요청 준비
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.7,
                "topK": 1,
                "topP": 1
            }
        }
        
        headers = {'Content-Type': 'application/json'}
        print(f"Gemini API 요청 페이로드: {json.dumps(payload)[:500]}...")  # 길 수 있으니 앞부분만 출력

        # Gemini API 호출 및 응답 처리 (이하 동일)
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload)
        print(f"Gemini API 응답 상태: {response.status_code}, 내용: {response.text[:500]}...")  # 앞부분만 출력
        response.raise_for_status()
        
        gemini_response = response.json()
        if 'candidates' not in gemini_response or not gemini_response['candidates']:
            raise Exception("응답에 candidates가 없습니다.")
            
        answer = gemini_response['candidates'][0]['content']['parts'][0]['text']

        save_message(conversation_id, current_user_id, 'model', answer)

        return jsonify({
            "answer": answer,
            "conversation_id": conversation_id,
        })

    except Exception as e:
        print(f"Gemini API 에러: {str(e)}")
        return jsonify({"error": f"AI 응답을 받아오는데 실패했습니다: {str(e)}"}), 500


@app.route('/api/chat/history', methods=['GET'])
@token_required
def get_chat_history():
    """
    사용자의 대화 기록을 조회하는 엔드포인트
    - conversation_id: (선택) 특정 대화의 기록을 조회. 없으면 모든 대화 목록 반환
    """
    try:
        current_user_id = g.current_user['id']
        conversation_id = request.args.get('conversation_id')
        
        if conversation_id:
            # 특정 대화의 전체 메시지 조회
            messages = load_history(conversation_id, current_user_id)
            if not messages:
                return jsonify({
                    "conversation_id": conversation_id,
                    "messages": [],
                    "message": "해당 대화의 기록이 없습니다."
                })
            return jsonify({
                "conversation_id": conversation_id,
                "messages": [
                    {"role": role, "content": content, "image_url": image_url}
                    for role, content, image_url in messages
                ]
            })
        else:
            # 모든 대화 목록 조회
            conversations = get_user_conversations(current_user_id)
            if not conversations:
                return jsonify({
                    "conversations": [],
                    "message": "대화 기록이 없습니다."
                })
            return jsonify({
                "conversations": [
                    {
                        "id": conv_id,
                        "last_update": last_update
                    }
                    for conv_id, last_update in conversations
                ]
            })
    except Exception as e:
        print(f"대화 기록 조회 에러: {str(e)}")
        return jsonify({"error": "대화 기록을 불러오는데 실패했습니다."}), 500


# 서버에 저장된 이미지를 프론트엔드가 불러갈 수 있도록 API 엔드포인트를 추가합니다.
@app.route('/uploads/chat_images/<path:filename>')
def serve_chat_image(filename):
    return send_from_directory(CHAT_IMAGE_FOLDER, filename)

if __name__ == "__main__":
    with app.app_context():
        init_runtime_and_scheduler()
    socketio.run(
        app,
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        host="0.0.0.0",
        port=8000,  # ← 5000 → 8000
    )