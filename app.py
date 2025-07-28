# app.py (사용자 인증 API 추가 최종 코드)

import os
from flask import Flask, jsonify, request # request 추가
from dotenv import load_dotenv
import time
import json # json 추가 (publish_mqtt_message 때문)
from datetime import datetime # datetime 추가 (control_plant 함수 때문)

# services.py에서 정의한 함수들을 import
from services import initialize_services, mqtt_client, get_redis_data, query_influxdb_data, publish_mqtt_message, process_sensor_data
# database.py에서 정의한 함수들을 import (사용자 인증 기능에 필요)
from database import init_db, add_user, get_user_by_email, check_password

# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 (services.py에서 사용하므로 여기서는 정의만) ---
# .env 파일과 docker-compose.yml에 설정한 값들과 동일하게 맞춰줍니다.
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'kitel1976!') # 사용자 비밀번호 일치

INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', '5be92638-5260-458c-8287-2ce175a387aa') # InfluxDB 토큰 일치
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'GreenEye') # InfluxDB 조직명 일치
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'sensor_data') # InfluxDB 버킷명 일치

REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', 'kitel1976!') # Redis 비밀번호 일치


# --- Flask 앱 시작 시 서비스 초기화 및 DB 초기화 ---
# 이 블록은 Flask 앱이 처음 시작될 때 단 한 번만 실행됩니다.
with app.app_context():
    initialize_services() # 모든 서비스 연결을 초기화 (MQTT, InfluxDB, Redis)
    
    # MQTT 구독: 모든 센서 데이터 토픽을 구독
    # 'sensor/data/#'는 'sensor/data/plant_001', 'sensor/data/plant_002' 등 모든 하위 토픽을 구독
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
    GET 요청으로, 프론트엔드에서 최신 데이터를 표시할 때 사용.
    """
    data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if data:
        return jsonify(data)
    return jsonify({"error": "No data found for this plant ID"}), 404

@app.route('/api/historical_sensor_data/<plant_id>')
def get_historical_sensor_data(plant_id):
    """
    InfluxDB에서 특정 식물의 과거 센서 데이터를 조회하는 API.
    GET 요청으로, 프론트엔드에서 통계나 그래프를 그릴 때 사용.
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
    
    # InfluxDB 쿼리 결과는 복잡할 수 있으므로, 실제 프론트엔드 요구사항에 맞춰 가공 필요
    # 여기서는 간략하게 각 레코드의 값을 리스트로 변환하여 반환
    formatted_data = []
    for record in data:
        # record.values는 딕셔너리 형태의 레코드를 제공
        formatted_data.append(record)
    return jsonify(formatted_data)

@app.route('/api/control_plant/<plant_id>', methods=['POST'])
def control_plant(plant_id):
    """
    MQTT를 통해 식물 제어 명령을 발행하는 API.
    POST 요청으로, 웹에서 JSON 형식의 명령을 수신. (수동 제어용)
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    command_data = request.get_json()
    action = command_data.get('action') #제어 기능 ex-> "turn_on_water_pump"
    duration = command_data.get('duration_sec', 0)
    # 다른 제어 파라미터도 이후 추가 (ex-> 'device': 'water_pump' 등)

    if not action:
        return jsonify({"error": "Missing 'action' in command"}), 400

    topic = f"plant/control/{plant_id}"
    # 메시지에 타임스탬프 추가
    message_payload = json.dumps({"action": action, "duration_sec": duration, "timestamp": datetime.utcnow().isoformat()})
    
    publish_mqtt_message(topic, message_payload) # services.py의 함수 호출
    return jsonify({"status": "success", "message": f"Control command '{action}' sent to {plant_id}"})


# --- 사용자 인증 API 엔드포인트 ---
@app.route('/api/auth/register', methods=['POST'])
def register_user():
    """
    새로운 사용자를 등록하는 API.
    POST 요청으로 JSON 형식의 이메일과 비밀번호를 받아옴.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    # 간단한 이메일 형식 검사 (실제 서비스에서는 더 복잡하게 검사 필요)
    if "@" not in email or "." not in email:
        return jsonify({"error": "Invalid email format"}), 400

    # database.py의 add_user 함수를 사용하여 사용자 추가
    if add_user(email, password):
        return jsonify({"status": "success", "message": "User registered successfully"}), 201 # 201 Created
    else:
        # add_user 함수에서 IntegrityError를 처리하므로, 여기서는 단순히 False 반환 시 중복 처리
        return jsonify({"error": "Email already exists"}), 409 # 409 Conflict


@app.route('/api/auth/login', methods=['POST'])
def login_user():
    """
    사용자 로그인을 처리하는 API.
    POST 요청으로 JSON 형식의 이메일과 비밀번호를 받아옴.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    # database.py의 get_user_by_email 함수로 사용자 조회
    user = get_user_by_email(email)

    # 사용자가 존재하고 비밀번호가 일치하는지 확인
    if user and check_password(user['password_hash'], password): # database.py의 check_password 함수 사용
        # JWT 토큰 생성 (PyJWT 라이브러리 필요)
        # 지금은 더미 토큰 반환, 실제 구현에서는 아래 코드 활성화
        # from jwt import encode, PyJWTError # app.py 상단에 import 필요

        # try:
        #     token = encode({"user_id": user['id'], "email": user['email']}, app.config['SECRET_KEY'], algorithm="HS256")
        #     return jsonify({"status": "success", "message": "Logged in successfully", "token": token}), 200
        # except PyJWTError as e:
        #     print(f"Error creating JWT token: {e}")
        #     return jsonify({"error": "Failed to create authentication token"}), 500
        
        return jsonify({"status": "success", "message": "Logged in successfully", "token": "dummy_jwt_token_for_now"}), 200
    else:
        # 사용자 없거나 비밀번호 불일치 시
        return jsonify({"error": "Invalid email or password"}), 401 # 401 Unauthorized


# --- 앱 실행 부분 ---
if __name__ == '__main__':
    # JWT를 위한 SECRET_KEY 설정 (실제 배포 시에는 강력하고 랜덤한 값 사용)
    # .env 파일에서 FLASK_SECRET_KEY 값을 가져옴
    app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super_secret_key_for_dev')
    
    app.run(debug=True, host='0.0.0.0', port=5000)