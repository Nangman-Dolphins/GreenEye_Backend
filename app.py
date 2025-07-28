import os
from flask import Flask, jsonify, request
from dotenv import load_dotenv
import time
import json
from datetime import datetime

# services.py에서 정의한 함수들을 import
from services import initialize_services, mqtt_client, get_redis_data, query_influxdb_data, publish_mqtt_message, process_sensor_data, set_redis_data # set_redis_data 추가

# database.py에서 정의한 함수들을 import
from database import init_db, add_user, get_user_by_email, check_password

# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 (services.py에서 사용하므로 여기서는 정의만) ---
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


# --- Flask 앱 시작 시 서비스 초기화 및 DB 초기화 ---
with app.app_context():
    initialize_services()
    mqtt_client.subscribe("sensor/data/#")
    print("Subscribed to MQTT topic 'sensor/data/#'")
    init_db()
    print("--- All backend services and DB initialized. ---\n")


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
    웹에서 JSON 형식의 명령을 받습니다. (수동 제어용)
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    command_data = request.get_json()
    action = command_data.get('action') # 예: "turn_on_water_pump"
    duration = command_data.get('duration_sec', 0) # 예: 10 (초)
    device = command_data.get('device') # 제어할 장치 (예: "water_pump", "led", "humidifier")

    # 명령 유효성 검사 (아주 간단한 예시)
    if not action or not device:
        return jsonify({"error": "Missing 'action' or 'device' in command"}), 400
    
    # --- 나중에 control_logic.py 모듈로 이동될 부분 ---
    # 실제 액추에이터 제어 명령을 보낼 토픽과 페이로드 구성
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