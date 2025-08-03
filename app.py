import os
import json
import uuid
import pytz
from datetime import datetime, timedelta

# Flask 프레임워크 관련 라이브러리
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

# 스케줄링을 위한 라이브러리
from apscheduler.schedulers.background import BackgroundScheduler

# 프로젝트 내부 모듈 import
from services import (
    initialize_services,
    mqtt_client,
    get_redis_data,
    query_influxdb_data,
    publish_mqtt_message,
    process_sensor_data,
    process_image_data,
    set_redis_data
)
from database import (
    init_db,
    add_user,
    get_user_by_email,
    check_password,
    get_db_connection,
    add_device,
    get_device_by_friendly_name,
    get_device_by_mac
)
from control_logic import (
    handle_manual_control,
    check_and_apply_auto_control
)
from report_generator import send_monthly_reports_for_users

# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 ---
# 이 변수들은 .env 파일에 실제 값으로 저장될 거야.
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!')

INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', '5be92638-5260-458c-8287-2ce175a387aa')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'GreenEye')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'sensor_data')

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', 'kitel1976!')

app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')

# 이미지 저장 폴더 (Nginx와 공유)
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'images')

# --- Flask 앱 시작 시 서비스 초기화 및 DB 초기화 ---
with app.app_context():
    initialize_services()
    
    # MQTT 구독: 센서 데이터와 이미지 데이터 토픽 모두 구독
    # (주의: 단말기가 MAC 주소를 포함하여 토픽을 보내도록 변경됨)
    mqtt_client.subscribe("sensor/data/#")
    mqtt_client.subscribe("image/data/#")
    print("Subscribed to MQTT topics 'sensor/data/#' and 'image/data/#'")
    
    # SQLite 데이터베이스 초기화 (users, devices, plant_images 테이블 생성 등)
    init_db()
    print("--- All backend services and DB initialized. ---\n")

    # --- APScheduler 초기화 및 작업 추가 ---
    scheduler = BackgroundScheduler(daemon=True)
    plant_friendly_names = ["Plant1", "Plant2", "Plant3"] # 테스트를 위한 임시 이름

    for p_name in plant_friendly_names:
        scheduler.add_job(func=check_and_apply_auto_control, trigger='interval', seconds=60, args=[p_name], id=f'auto_control_job_{p_name}')
        print(f"Scheduled auto control job for {p_name} every 60 seconds.")
    
    scheduler.add_job(func=send_monthly_reports_for_users, trigger='cron', hour='0', minute='5', id='monthly_report_job', timezone='Asia/Seoul')
    print("Scheduled monthly report job to run daily at 00:05 (for testing).")
    
    scheduler.start()
    print("APScheduler started for auto control and report tasks.")


# --- 기본 라우트 (API 엔드포인트) 정의 ---

@app.route('/')
def home():
    """기본 홈 페이지 응답"""
    return "Hello, GreenEye Backend is running!"

@app.route('/api/status')
def status():
    """백엔드 API의 상태를 확인하는 엔드포인트"""
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route('/api/latest_sensor_data/<plant_friendly_name>')
def get_latest_sensor_data(plant_friendly_name):
    """
    Redis에서 특정 식물의 최신 5가지 센서 데이터를 가져오는 API.
    GET 요청으로, 프론트엔드에서 최신 데이터를 표시할 때 사용합니다.
    """
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found with this friendly name"}), 404
    
    data = get_redis_data(f"latest_sensor_data:{device['mac_address']}")
    if data:
        # 응답에 plant_friendly_name 포함
        data['plant_friendly_name'] = plant_friendly_name
        return jsonify(data)
    return jsonify({"error": "No data found for this plant ID"}), 404

@app.route('/api/historical_sensor_data/<plant_friendly_name>')
def get_historical_sensor_data(plant_friendly_name):
    """
    InfluxDB에서 특정 식물의 과거 센서 데이터를 조회하는 API.
    GET 요청으로, 프론트엔드에서 통계나 그래프를 그릴 때 사용합니다.
    """
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found with this friendly name"}), 404
        
    # Flux 쿼리 예시: 특정 MAC 주소의 지난 7일 센서 데이터 조회
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -7d)
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.mac_address == "{device['mac_address']}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time", "mac_address", "temperature", "humidity", "light_lux", "soil_moisture", "soil_ec"])
      |> yield(name: "mean")
    '''
    data = query_influxdb_data(query)
    
    formatted_data = []
    for record in data:
        record['plant_friendly_name'] = plant_friendly_name
        formatted_data.append(record)
    return jsonify(formatted_data)

@app.route('/api/control_plant/<plant_friendly_name>', methods=['POST'])
def control_plant(plant_friendly_name):
    """
    MQTT를 통해 식물 제어 명령을 발행하는 API.
    POST 요청으로, 웹에서 JSON 형식의 명령을 받습니다. (수동 제어용)
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found with this friendly name"}), 404
        
    command_data = request.get_json()
    action = command_data.get('action')
    duration = command_data.get('duration_sec', 0)
    device_type = command_data.get('device')

    if not action or not device_type:
        return jsonify({"error": "Missing 'action' or 'device' in command"}), 400
    
    result = handle_manual_control(device['mac_address'], device_type, action, duration)
    return jsonify(result), (200 if result.get("status") == "success" else 500)


# --- 이미지 조회 API 엔드포인트 ---
@app.route('/api/images/<plant_friendly_name>/<filename>')
def get_image(plant_friendly_name, filename):
    """
    저장된 이미지 파일을 프론트엔드에 제공하는 API.
    """
    device = get_device_by_friendly_name(plant_friendly_name)
    if not device:
        return jsonify({"error": "Device not found with this friendly name"}), 404
        
    safe_filename = secure_filename(filename)
    
    filepath_to_serve = os.path.join(IMAGE_UPLOAD_FOLDER, safe_filename)

    if os.path.exists(filepath_to_serve) and \
       os.path.abspath(filepath_to_serve).startswith(IMAGE_UPLOAD_FOLDER):
        return send_from_directory(IMAGE_UPLOAD_FOLDER, safe_filename)
    else:
        return jsonify({"error": "Image not found"}), 404


# --- 사용자 인증 및 장치 등록 API 엔드포인트 ---
@app.route('/api/auth/register', methods=['POST'])
def register_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    if "@" not in email or "." not in email:
        return jsonify({"error": "Invalid email format"}), 400

    if add_user(email, password):
        return jsonify({"status": "success", "message": "User registered successfully"}), 201
    else:
        return jsonify({"error": "Email already exists"}), 409


@app.route('/api/auth/login', methods=['POST'])
def login_user():
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = get_user_by_email(email)

    if user and check_password(user['password_hash'], password):
        return jsonify({"status": "success", "message": "Logged in successfully", "token": "dummy_jwt_token_for_now"}), 200
    else:
        return jsonify({"error": "Invalid email or password"}), 401

@app.route('/api/register_device', methods=['POST'])
def register_device():
    """
    사용자가 MAC 주소를 입력하여 단말기를 등록하는 API.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
        
    data = request.get_json()
    mac_address = data.get('mac_address')
    plant_friendly_name = data.get('plant_friendly_name')

    if not mac_address or not plant_friendly_name:
        return jsonify({"error": "MAC address and plant friendly name are required"}), 400

    # MAC 주소 형식 검사 (간단한 예시)
    # if not all(c in '0123456789abcdefABCDEF:' for c in mac_address) or len(mac_address) != 17:
    #     return jsonify({"error": "Invalid MAC address format"}), 400

    if add_device(mac_address, plant_friendly_name):
        return jsonify({"status": "success", "plant_friendly_name": plant_friendly_name, "mac_address": mac_address, "message": "Device registered successfully"}), 201
    else:
        return jsonify({"error": "Device with this MAC address or friendly name already exists"}), 409


# --- 앱 실행 부분 ---
if __name__ == '__main__':
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')
    
    app.run(debug=True, host='0.0.0.0', port=5000)
