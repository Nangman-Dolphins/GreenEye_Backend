import os
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
import time
import json
from datetime import datetime
from werkzeug.utils import secure_filename
import uuid

from apscheduler.schedulers.background import BackgroundScheduler # 스케줄러 임포트
import pytz # 시간대 처리를 위해 추가

# services.py에서 정의한 함수들을 import
from services import initialize_services, mqtt_client, get_redis_data, query_influxdb_data, publish_mqtt_message, process_sensor_data, set_redis_data

# database.py에서 정의한 함수들을 import
from database import init_db, add_user, get_user_by_email, check_password, get_db_connection

# control_logic.py에서 정의한 함수들을 import
from control_logic import handle_manual_control, check_and_apply_auto_control

# report_generator.py에서 정의한 함수들을 import
from report_generator import send_monthly_reports_for_users # 새로 추가


# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 (나머지 동일) ---
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


# --- Flask 앱 시작 시 서비스 초기화 및 DB 초기화 ---
with app.app_context():
    initialize_services()
    mqtt_client.subscribe("sensor/data/#")
    print("Subscribed to MQTT topic 'sensor/data/#'")
    init_db()
    print("--- All backend services and DB initialized. ---\n")

    # --- APScheduler 초기화 및 작업 추가 ---
    scheduler = BackgroundScheduler(daemon=True) # daemon=True로 설정하여 앱 종료 시 함께 종료
    
    # 1. 자동 제어 작업 추가
    plant_ids_to_monitor = ["plant_001", "plant_002", "plant_003"] # 더미 센서에서 사용하는 식물 ID 리스트
    for p_id in plant_ids_to_monitor:
        # 'interval'은 매 60초(1분)마다 실행 (센서 데이터가 5초마다 오니 1분에 한 번만 검사해도 충분)
        scheduler.add_job(func=check_and_apply_auto_control, trigger='interval', seconds=60, args=[p_id], id=f'auto_control_job_{p_id}')
        print(f"Scheduled auto control job for {p_id} every 60 seconds.")
    
    # 2. 월별 보고서 발송 작업 추가
    # 주의: 지금은 테스트용으로 매일 00시 05분에 실행되도록 설정.
    #       실제 배포 시에는 매월 1일 특정 시간으로 변경 (e.g., 'cron', day='1', hour='0', minute='5')
    scheduler.add_job(func=send_monthly_reports_for_users, trigger='cron', hour='0', minute='5', id='monthly_report_job', timezone='Asia/Seoul')
    print("Scheduled monthly report job to run daily at 00:05 (for testing).")
    
    scheduler.start() # 스케줄러 시작
    print("APScheduler started for auto control and report tasks.")

# --- 기본 라우트 (API 엔드포인트) 정의 ---

@app.route('/')
def home():
    return "Hello, GreenEye Backend is running!"

@app.route('/api/status')
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route('/api/latest_sensor_data/<plant_id>')
def get_latest_sensor_data(plant_id):
    data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if data:
        return jsonify(data)
    return jsonify({"error": "No data found for this plant ID"}), 404

@app.route('/api/historical_sensor_data/<plant_id>')
def get_historical_sensor_data(plant_id):
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: -24h)
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.plant_id == "{plant_id}")
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time", "plant_id", "temperature", "humidity", "light_lux", "soil_moisture", "soil_ec"])
      |> yield(name: "mean")
    '''
    data = query_influxdb_data(query)
    
    formatted_data = []
    for record in data:
        formatted_data.append(record)
    return jsonify(formatted_data)

@app.route('/api/control_plant/<plant_id>', methods=['POST'])
def control_plant(plant_id):
    """
    MQTT를 통해 식물 제어 명령을 발행하는 API.
    POST 요청으로, 웹에서 JSON 형식의 명령을 받습니다. (수동 제어용)
    이제 control_logic.py 모듈을 통해 처리됩니다.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    command_data = request.get_json()
    action = command_data.get('action') # 예: "turn_on_water_pump"
    duration = command_data.get('duration_sec', 0) # 예: 10 (초)
    device = command_data.get('device') # 제어할 장치 (예: "water_pump", "led", "humidifier")

    if not action or not device:
        return jsonify({"error": "Missing 'action' or 'device' in command"}), 400
    
    # --- control_logic.py의 handle_manual_control 함수 호출 ---
    # 이제 publish_mqtt_message 와 set_redis_data 호출을 handle_manual_control 함수로 대체합니다.
    result = handle_manual_control(plant_id, device, action, duration) # <--- 이 부분으로 변경해주세요!
    return jsonify(result), (200 if result.get("status") == "success" else 500)


# --- 이미지 업로드 및 조회 API 엔드포인트 ---
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/upload_image/<plant_id>', methods=['POST'])
def upload_image(plant_id):
    if 'image' not in request.files:
        return jsonify({"error": "No image file part in the request"}), 400
    
    file = request.files['image']
    
    if file.filename == '':
        return jsonify({"error": "No selected image file"}), 400
    
    if file and allowed_file(file.filename):
        original_filename = secure_filename(file.filename)
        file_extension = original_filename.rsplit('.', 1)[1].lower()
        unique_filename = f"{plant_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}.{file_extension}"
        
        filepath = os.path.join(IMAGE_UPLOAD_FOLDER, unique_filename)
        
        try:
            file.save(filepath)
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS plant_images (id INTEGER PRIMARY KEY AUTOINCREMENT, plant_id TEXT NOT NULL, filename TEXT NOT NULL UNIQUE, filepath TEXT NOT NULL, timestamp TEXT NOT NULL)",
                ()
            )
            cursor.execute(
                "INSERT INTO plant_images (plant_id, filename, filepath, timestamp) VALUES (?, ?, ?, ?)",
                (plant_id, unique_filename, filepath, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()

            set_redis_data(f"latest_image:{plant_id}", {
                "filename": unique_filename,
                "filepath": filepath,
                "timestamp": datetime.utcnow().isoformat()
            })

            print(f"Image uploaded and saved: {filepath}")
            # 이 시점에서 AI 추론 로직을 호출 (나중에 추가)
            # from ai_inference import run_inference_on_image
            # diagnosis_result = run_inference_on_image(filepath)
            # set_redis_data(f"latest_ai_diagnosis:{plant_id}", {"diagnosis": diagnosis_result, "timestamp": datetime.utcnow().isoformat()})

            return jsonify({"status": "success", "message": "Image uploaded successfully", "filename": unique_filename}), 200
        except Exception as e:
            print(f"Error saving image or writing to DB: {e}")
            return jsonify({"error": f"Server error: {e}"}), 500
    else:
        return jsonify({"error": "File type not allowed"}), 400

@app.route('/api/images/<plant_id>/<filename>')
def get_image(plant_id, filename):
    """
    저장된 이미지 파일을 프론트엔드에 제공하는 API.
    """
    safe_filename = secure_filename(filename)
    
    filepath_to_serve = os.path.join(IMAGE_UPLOAD_FOLDER, safe_filename)

    if os.path.exists(filepath_to_serve) and \
       os.path.abspath(filepath_to_serve).startswith(IMAGE_UPLOAD_FOLDER):
        return send_from_directory(IMAGE_UPLOAD_FOLDER, safe_filename)
    else:
        return jsonify({"error": "Image not found"}), 404


# --- 사용자 인증 API 엔드포인트 ---
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


# --- 앱 실행 부분 ---
if __name__ == '__main__':
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')
    
    app.run(debug=True, host='0.0.0.0', port=5000)