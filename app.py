import os
import json
import pytz
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.utils import secure_filename
import jwt

# 내부 모듈
from services import (
    initialize_services,
    get_redis_data,
    query_influxdb_data,
    request_data_from_device,
    send_config_to_device,
)
from database import (
    init_db,
    add_user,
    get_user_by_email,
    check_password,
    add_device,
    get_device_by_friendly_name,
    get_all_devices,
)
from control_logic import check_and_apply_auto_control
from report_generator import send_monthly_reports_for_users
from ai_inference import load_ai_model

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- 설정 (보안 강화) ---
_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret:
    raise RuntimeError("FLASK_SECRET_KEY is not set. Set it in .env before running.")
app.config["SECRET_KEY"] = _secret

IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), "images")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")

# --- 실시간 데이터 브로드캐스트 ---
def send_realtime_data_to_clients(mac_address: str):
    latest_data = get_redis_data(f"latest_sensor_data:{mac_address}")
    latest_image_info = get_redis_data(f"latest_image:{mac_address}")
    latest_ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{mac_address}")

    if latest_data:
        if latest_image_info:
            latest_data["latest_image_filename"] = latest_image_info["filename"]
        if latest_ai_diagnosis:
            latest_data["ai_diagnosis"] = latest_ai_diagnosis
        socketio.emit("realtime_data", latest_data)
        print(f"[WebSocket] Sent realtime data for {mac_address}.")

# --- 스케줄러 작업 ---
def scheduled_data_request_job():
    print(f"\n--- [{datetime.now()}] Running scheduled data request job ---")
    devices = get_all_devices()
    if not devices:
        print("No devices registered, skipping data request.")
        return
    for device in devices:
        request_data_from_device(device["mac_address"])

def scheduled_auto_control_job():
    print(f"\n--- [{datetime.now()}] Running scheduled auto-control job ---")
    devices = get_all_devices()
    if not devices:
        return
    for device in devices:
        check_and_apply_auto_control(device["mac_address"])

def init_runtime_and_scheduler():
    """
    서비스/DB/AI 초기화 후 스케줄러 등록.
    [중복실행방지] Flask debug 리로더 환경에서 2회 실행되지 않도록 가드 처리.
    """
    with app.app_context():
        initialize_services()
        load_ai_model()
        init_db()

        scheduler = BackgroundScheduler(daemon=True, timezone="Asia/Seoul")
        scheduler.add_job(scheduled_data_request_job, "interval", minutes=1, id="data_request_job", replace_existing=True)
        scheduler.add_job(scheduled_auto_control_job, "interval", minutes=1, id="auto_control_job", replace_existing=True)
        scheduler.add_job(send_monthly_reports_for_users, "cron", hour="0", minute="5", id="monthly_report_job", replace_existing=True)

        devices = get_all_devices()
        for device in devices:
            scheduler.add_job(
                send_realtime_data_to_clients,
                "interval",
                seconds=5,
                args=[device["mac_address"]],
                id=f"realtime_data_job_{device['mac_address']}",
                replace_existing=True,
            )

        # 디버그 리로더 가드
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or os.getenv("FLASK_DEBUG", "0") == "0":
            scheduler.start()
            print("APScheduler started.")
        else:
            print("APScheduler not started due to dev reloader safeguard.")

# --- API ---
@app.route("/api/status")
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route("/api/latest_sensor_data/<plant_friendly_name>")
def api_get_latest_sensor_data(plant_friendly_name):
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found"}), 404

    data = get_redis_data(f"latest_sensor_data:{device['mac_address']}")
    ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{device['mac_address']}")

    if not data:
        return jsonify({"error": "No data found"}), 404
    if ai_diagnosis:
        data["ai_diagnosis"] = ai_diagnosis
    return jsonify(data)

@app.route("/api/historical_sensor_data/<plant_friendly_name>")
def get_historical_sensor_data(plant_friendly_name):
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found"}), 404

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.mac_address == "{device['mac_address']}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time", "mac_address", "temperature", "humidity", "light_lux", "soil_moisture", "soil_ec"])
    '''
    data = query_influxdb_data(query)
    return jsonify(data)

@app.route("/api/device_config/<plant_friendly_name>", methods=["POST"])
def configure_device(plant_friendly_name):
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found"}), 404

    config_data = request.get_json()
    if not config_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    send_config_to_device(device["mac_address"], config_data)
    return jsonify({"status": "success", "message": f"Configuration sent to {plant_friendly_name}"}), 200

@app.route("/api/images/<filename>")
def get_image(filename):
    safe_filename = secure_filename(filename)
    return send_from_directory(IMAGE_UPLOAD_FOLDER, safe_filename)

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
        try:
            token = jwt.encode({"email": user["email"], "id": user["id"]}, app.config["SECRET_KEY"], algorithm="HS256")
            return jsonify({"status": "success", "message": "Logged in successfully", "token": token}), 200
        except Exception as e:
            return jsonify({"error": f"Failed to create token: {e}"}), 500
    else:
        return jsonify({"error": "Invalid email or password"}), 401

@app.route("/api/register_device", methods=["POST"])
def register_device():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    data = request.get_json()
    mac_address = data.get("mac_address")
    plant_friendly_name = data.get("plant_friendly_name")
    if not mac_address or not plant_friendly_name:
        return jsonify({"error": "MAC address and plant friendly name are required"}), 400
    if add_device(mac_address, plant_friendly_name):
        return jsonify({"status": "success", "message": "Device registered successfully"}), 201
    else:
        return jsonify({"error": "Device with this MAC or name already exists"}), 409

# --- 실행 ---
if __name__ == "__main__":
    init_runtime_and_scheduler()
    socketio.run(app, debug=os.getenv("FLASK_DEBUG", "0") == "1", host="0.0.0.0", port=5000)
