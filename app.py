import os
import json
import uuid
import pytz
from datetime import datetime, timedelta
from urllib.parse import urlparse
import base64
import requests
import jwt

from flask import Flask, jsonify, request, send_from_directory, g
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from apscheduler.schedulers.background import BackgroundScheduler

from services import (
    initialize_services,
    mqtt_client,
    get_redis_data,
    query_influxdb_data,
    publish_mqtt_message,
    set_redis_data,
    process_incoming_data,
    send_config_to_device,
    is_connected_influx, is_connected_mqtt, is_connected_redis
)
from database import (
    init_db,
    add_user,
    get_user_by_email,
    check_password,
    add_device,
    get_device_by_friendly_name,
    get_all_devices,
    get_device_by_device_id
)
from control_logic import (
    handle_manual_control,
    check_and_apply_auto_control
)
from report_generator import send_monthly_reports_for_users, generate_monthly_report_content_by_device

load_dotenv()
app = Flask(__name__)
from functools import wraps
from threading import Lock
from flask import request  # ← 추가

if not hasattr(app, "before_first_request"):
    _run_once_lock = Lock()
    _run_once_flag = {"done": False}
    _health_skip_paths = {"/healthz", "/health"}  # 필요하면 "/"도 추가 가능

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
socketio = SocketIO(app, cors_allowed_origins="*")

app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "super_secret_key_for_dev")

IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "images")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")


def token_required(f):
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


@app.before_first_request
def before_first_request():
    with app.app_context():
        init_runtime_and_scheduler()

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
    initialize_services()
    init_db()
    # load_ai_model() # 모델 준비 시 사용

    scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Seoul")
    
    devices = get_all_devices()
    
    if not devices:
        print("No devices found in DB. Skipping scheduler setup for auto control.")
    else:
        for device in devices:
            device_id = device['device_id']
            friendly_name = device['friendly_name']

            scheduler.add_job(check_and_apply_auto_control, "interval", minutes=1, args=[device_id], id=f"auto_control_job_{device_id}", replace_existing=True)
            print(f"Scheduled auto control job for {friendly_name} ({device_id}) every 1 minutes.")
            
            scheduler.add_job(send_realtime_data_to_clients, "interval", seconds=5, args=[device_id], id=f"realtime_data_job_{device_id}", replace_existing=True)
            print(f"Scheduled realtime data push for {friendly_name} ({device_id}) every 5 seconds.")

    scheduler.add_job(send_monthly_reports_for_users, "cron", day="1", hour="0", minute="5", id="monthly_report_job", replace_existing=True)
    print("Scheduled monthly report job to run on the 1st of every month at 00:05.")
    
    scheduler.start()
    print("APScheduler started.")


@app.route("/")
def home():
    return "Hello, GreenEye Backend is running!"

@app.get("/healthz")
@app.get("/health")
def healthz():
    return {"status": "ok"}, 200

@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route("/api/health")
def health():
    return jsonify({
        "api": "ok",
        "mqtt": "ok" if is_connected_mqtt() else "down",
        "influxdb": "ok" if is_connected_influx() else "down",
        "redis": "ok" if is_connected_redis() else "down",
    })

@app.route("/api/latest_sensor_data/<device_id>")
def get_latest_sensor_data(device_id: str):
    dev = get_device_by_device_id(device_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    data = get_redis_data(f"latest_sensor_data:{device_id}")
    ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{device_id}")
    if not data:
        return jsonify({"error": "No data found"}), 404
    if ai_diagnosis:
        data["ai_diagnosis"] = ai_diagnosis
    data["friendly_name"] = dev["friendly_name"]
    return jsonify(data)

@app.route("/api/historical_sensor_data/<device_id>")
def get_historical_sensor_data(device_id: str):
    dev = get_device_by_device_id(device_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","soil_moisture","soil_ec","soil_temp","battery"])
    '''
    data = query_influxdb_data(query)
    for row in data:
        row["friendly_name"] = dev["friendly_name"]
    return jsonify(data)

@app.route("/api/control_device/<device_id>", methods=["POST"])
def control_device(device_id: str):
    config_data = request.get_json() or {}
    print("💡 Received config_data:", config_data)

    send_config_to_device(device_id, config_data)
    return jsonify({"status": "success", "message": f"Configuration sent to {device_id}"})
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    dev = get_device_by_device_id(device_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    config_data = request.get_json()
    if not config_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    send_config_to_device(device_id, config_data)
    return jsonify({"status": "success", "message": f"Configuration sent to {device_id}"})

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
def register_device():
    try:
        if not request.is_json:
            return jsonify({"error": "Request must be JSON"}), 400

        data = request.get_json(silent=True) or {}
        mac = data.get("mac_address")
        friendly_name = data.get("friendly_name")
        if not mac or not friendly_name:
            return jsonify({"error": "mac_address and friendly_name are required"}), 400

        mac_norm = mac.upper()
        device_id = mac_norm.replace(":", "").lower()[-4:]

        created = add_device(mac_norm, friendly_name)  # add_device는 인자 2개

        if created:
            return jsonify({"message": "registered", "mac_address": mac_norm, "device_id": device_id}), 201
        else:
            return jsonify({"error": "Device already exists", "mac_address": mac_norm, "device_id": device_id}), 409

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "internal_error", "detail": str(e)}), 500

@app.route("/api/images/<device_id>/<filename>")
def get_image(device_id: str, filename: str):
    dev = get_device_by_device_id(device_id)
    if not dev:
        return jsonify({"error": "Device not found"}), 404
    safe_filename = secure_filename(filename)
    return send_from_directory(IMAGE_UPLOAD_FOLDER, safe_filename)

if __name__ == "__main__":
    init_runtime_and_scheduler()
    socketio.run(app, debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=5000)

