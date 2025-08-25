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
)

from backend_app.control_logic import (
    handle_manual_control,
    check_and_apply_auto_control
)
from backend_app.report_generator import send_all_reports

load_dotenv()
app = Flask(__name__)

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

# === App-level constants & helper bindings ===
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "images")

# Influx 기본 설정 (측정명 기본값 보강!)
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
INFLUX_MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "sensor_readings")

DEVICE_PREFIX = os.getenv("DEVICE_PREFIX", "ge-sd")

def normalize_device_id(raw: str) -> str:
    """
    'ge-sd-6c18' 같은 입력을 내부용 짧은 ID '6c18'으로 변환.
    이미 '6c18'이면 그대로 반환.
    """
    if not raw:
        return raw
    r = raw.strip().lower()
    m = re.fullmatch(rf"{DEVICE_PREFIX}-([0-9a-f]{{4}})", r)
    return m.group(1) if m else r

def to_device_code(short_id: str) -> str:
    """
    짧은 ID '6c18' -> 'ge-sd-6c18' 으로 표시용 코드 변환.
    """
    sid = (short_id or "").strip().lower()
    return f"{DEVICE_PREFIX}-{sid}" if re.fullmatch(r"[0-9a-f]{4}", sid) else short_id

def _to_device_id_from_any(s: str) -> str:
    """MAC(aa:bb:...) / ge-sd-XXXX / 그냥 XXXX 모두에서 마지막 4자리로 device_id 생성"""
    if not s:
        return ""
    t = s.strip()
    if t.lower().startswith("ge-sd-"):
        t = t.split("-", 2)[-1]  # 'ge-sd-' 뒤쪽
    t = t.replace(":", "").replace("-", "")
    return t[-4:].lower()

def _normalize_mac_like(s: str) -> str:
    """
    저장용 mac_address 표준화:
    - 'ge-sd-XXXX' 형태면 그대로 대문자 접미부로 보정
    - 일반 MAC이면 콜론 포함 대문자
    - 4~6글자 같은 짧은 식별자면 'ge-sd-XXXX'로 만들어 저장
    """
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

# DB에서 친화 이름 조회
def get_friendly_name(device_id: str) -> str:
    dev = get_device_by_device_id_any(device_id)
    return (dev and dev.get("friendly_name")) or device_id

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


# @app.before_first_request
# def before_first_request():
#     with app.app_context():
#         init_runtime_and_scheduler()

def send_realtime_data_to_clients(device_id: str):
    """Redis에 캐시된 최신 센서 데이터를 Socket.IO로 브로드캐스트."""
    try:
        data = get_redis_data(f"latest_sensor_data:{device_id}") or {}
        # 원하는 이벤트/네임스페이스 명은 프로젝트에 맞게 조정
        # 클라이언트에서 'realtime_data' 이벤트를 수신하도록 되어 있다면 그대로 사용
        socketio.emit("realtime_data", {"device_id": device_id, **data})
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

        scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Seoul")

        print("[init] ⏳ get_all_devices()...")
        devices = get_all_devices()
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

    # ✅ 이 유저의 장치인지 확인
    dev = get_device_by_device_id(device_id, owner_user_id)
    if not dev:
        return jsonify({"error":"Device not found"}), 404

    # Redis → Influx 폴백은 기존 로직 그대로
    data = get_latest_sensor_data_from_redis(device_id)
    ai   = get_latest_ai_from_redis(device_id)
    if not data:
        return jsonify({"error": "No data found"}), 404

    data["friendly_name"] = dev["friendly_name"]
    if ai:
        data["ai_diagnosis"] = ai
    return jsonify(data)

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
        # raw를 time 기준으로 필드 병합 (간단 버전)
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
    mode = data.get("mode")
    if not mode:
        return jsonify({"error": "Missing 'mode' in request body"}), 400

    try:
        config = send_mode_to_device(device_id, mode)
        return jsonify({"status": "success", "device_id": device_id, "mode": mode, "applied_config": config})
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

from werkzeug.utils import secure_filename  # get_image에서 사용

@app.route("/api/register_device", methods=["POST"])
@token_required
def register_device():
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        data = request.get_json(silent=True) or {}
        mac = data.get("mac_address")
        friendly_name = data.get("friendly_name")
        if not mac or not friendly_name:
            return jsonify({"error": "mac_address and friendly_name are required"}), 400

        # 형식: ge-sd-0000 (하이픈 1개, 뒤 4자리는 16진수)
        mac = mac.strip()
        if not re.fullmatch(r"[A-Za-z0-9]{2}-[A-Za-z0-9]{2}-[0-9a-fA-F]{4}", mac) and \
            not re.fullmatch(r"ge-sd-[0-9a-fA-F]{4}", mac.lower()):
                return jsonify({"error":"mac_address must match 'ge-sd-0000' (4 hex)"}), 400

        # 저장은 대문자로(보기 좋게), device_id는 소문자 4자리
        mac_norm = mac.upper()
        device_id = mac_norm.split("-")[-1].lower()
        owner_user_id = g.current_user["id"]

        created = add_device(mac_norm, friendly_name, owner_user_id)
        if created:
            return jsonify({"message":"registered", "mac_address": mac_norm, "device_id": device_id}), 201
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

if __name__ == "__main__":
    with app.app_context():
        init_runtime_and_scheduler()
    socketio.run(
        app,
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        host="0.0.0.0",
        port=8000,  # ← 5000 → 8000
    )