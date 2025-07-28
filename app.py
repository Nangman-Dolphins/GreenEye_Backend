# app.py (이미지 수집 파이프라인 추가 최종 코드)

import os
from flask import Flask, jsonify, request, send_from_directory # send_from_directory 추가
from dotenv import load_dotenv
import time
import json
from datetime import datetime
from werkzeug.utils import secure_filename # 파일 이름 보안 처리용
import uuid # 고유한 파일명 생성을 위해 추가

# services.py에서 정의한 함수들을 import
from services import initialize_services, mqtt_client, get_redis_data, query_influxdb_data, publish_mqtt_message, process_sensor_data, set_redis_data

# database.py에서 정의한 함수들을 import (사용자 인증 및 이미지 메타데이터 저장에 필요)
from database import init_db, add_user, get_user_by_email, check_password, get_db_connection


# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 (services.py에서 사용하므로 여기서는 정의만) ---
# .env 파일과 docker-compose.yml에 설정한 값들과 동일하게 맞춰줍니다.
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

# Flask SECRET_KEY (세션 관리 및 JWT 서명에 사용)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')


# --- Flask 앱 시작 시 서비스 초기화 및 DB 초기화 ---
# 이 블록은 Flask 앱이 처음 시작될 때 단 한 번만 실행됩니다.
with app.app_context():
    initialize_services() # 모든 서비스 연결을 초기화 (MQTT, InfluxDB, Redis)
    
    # MQTT 구독: 모든 센서 데이터 토픽을 구독
    mqtt_client.subscribe("sensor/data/#")
    print("Subscribed to MQTT topic 'sensor/data/#'")
    
    # SQLite 데이터베이스 초기화 (user 테이블 생성 등)
    init_db() # database.py에 정의된 함수 호출
    
print("--- All backend services and DB initialized. ---\n")


# --- 기본 라우트 (API 엔드포인트) 정의 ---

@app.route('/')
def home():
    """기본 홈 페이지 응답"""
    return "Hello, GreenEye Backend is running!"

@app.route('/api/status')
def status():
    """백엔드 API의 상태를 확인하는 엔드포인트"""
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route('/api/latest_sensor_data/<plant_id>')
def get_latest_sensor_data(plant_id):
    """
    Redis에서 특정 식물의 최신 5가지 센서 데이터를 가져오는 API.
    GET 요청으로, 프론트엔드에서 최신 데이터를 표시할 때 사용합니다.
    """
    data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if data:
        return jsonify(data)
    return jsonify({"error": "No data found for this plant ID"}), 404

@app.route('/api/historical_sensor_data/<plant_id>')
def get_historical_sensor_data(plant_id):
    """
    InfluxDB에서 특정 식물의 과거 센서 데이터를 조회하는 API.
    GET 요청으로, 프론트엔드에서 통계나 그래프를 그릴 때 사용합니다.
    """
    # Flux 쿼리 예시: 특정 plant_id의 지난 24시간 센서 데이터 조회
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
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    command_data = request.get_json()
    action = command_data.get('action') # 예: "turn_on_water_pump"
    duration = command_data.get('duration_sec', 0) # 예: 10 (초)
    device = command_data.get('device') # 제어할 장치 (예: "water_pump", "led", "humidifier")

    if not action or not device:
        return jsonify({"error": "Missing 'action' or 'device' in command"}), 400
    
    topic = f"plant/control/{plant_id}/{device}" # 토픽을 plant_id/device 형태로 세분화
    message_payload = json.dumps({
        "action": action,
        "duration_sec": duration,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    publish_mqtt_message(topic, message_payload) # services.py의 함수 호출
    
    # Redis에 액추에이터의 현재 상태 업데이트 (선택 사항이지만 권장)
    # 나중에 프론트엔드에서 장치 상태를 실시간으로 보여줄 때 사용
    set_redis_data(f"plant_control_state:{plant_id}:{device}", {"status": action, "timestamp": datetime.utcnow().isoformat()})

    return jsonify({"status": "success", "message": f"Control command '{action}' for '{device}' sent to {plant_id}"}), 200


# --- 이미지 업로드 및 조회 API 엔드포인트 ---
IMAGE_UPLOAD_FOLDER = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'images') # 'uploads' 대신 'images'로 변경
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'} # 허용할 이미지 확장자

def allowed_file(filename):
    """허용된 확장자를 가진 파일인지 확인합니다."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/api/upload_image/<plant_id>', methods=['POST'])
def upload_image(plant_id):
    """
    단말기로부터 이미지 파일을 업로드 받는 API.
    POST 요청으로 이미지 파일을 받습니다.
    """
    if 'image' not in request.files:
        return jsonify({"error": "No image file part in the request"}), 400
    
    file = request.files['image']
    
    if file.filename == '':
        return jsonify({"error": "No selected image file"}), 400
    
    if file and allowed_file(file.filename):
        # 안전한 파일 이름 생성 (원래 파일명 + UUID)
        original_filename = secure_filename(file.filename)
        file_extension = original_filename.rsplit('.', 1)[1].lower()
        unique_filename = f"{plant_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex}.{file_extension}"
        
        filepath = os.path.join(IMAGE_UPLOAD_FOLDER, unique_filename) # 변경된 UPLOAD_FOLDER 사용
        
        try:
            # 이미지 파일을 로컬 폴더에 저장
            file.save(filepath)
            
            # SQLite에 이미지 메타데이터 저장
            conn = get_db_connection() # database.py에서 가져온 함수 사용
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

            # Redis에 최신 이미지 경로 캐싱 (AI 진단 결과와 함께 사용될 수 있음)
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
    # 보안을 위해 filename에 ../ 같은 경로 이동 문자가 없는지 확인
    safe_filename = secure_filename(filename)
    # 실제 파일 경로가 UPLOAD_FOLDER 내부에 있는지 다시 확인하는 것이 보안상 좋음.
    # send_from_directory는 기본적으로 안전한 경로만 제공하므로 보통 괜찮음.
    
    filepath_to_serve = os.path.join(IMAGE_UPLOAD_FOLDER, safe_filename)

    if os.path.exists(filepath_to_serve) and \
       os.path.abspath(filepath_to_serve).startswith(IMAGE_UPLOAD_FOLDER): # 경로 조작 방지
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
    # Flask SECRET_KEY는 .env 파일에서 가져옴
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')
    
    app.run(debug=True, host='0.0.0.0', port=5000)