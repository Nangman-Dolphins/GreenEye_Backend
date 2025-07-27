# app.py

import os
from flask import Flask, jsonify
from dotenv import load_dotenv

# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)

# --- 환경 변수 설정 ---
# .env 파일에 실제 값으로 저장
# Docker 컨테이너 서비스에 접속할 때 사용됨.
# Mosquitto
MQTT_BROKER_HOST = os.getenv('MQTT_BROKER_HOST', 'localhost')
MQTT_BROKER_PORT = int(os.getenv('MQTT_BROKER_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', 'greeneye_user') # 기본값 설정 (실제 사용자와 다를 수 있음)
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', 'your_mqtt_password') # 기본값 설정

# InfluxDB
INFLUXDB_URL = os.getenv('INFLUXDB_URL', 'http://localhost:8086')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN', 'your_influxdb_admin_token_here')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG', 'GreenEye_Org')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET', 'sensor_data')

# Redis
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', 'your_redis_password_here')


# --- 기본 라우트 (API 엔드포인트) 정의 ---
@app.route('/')
def home():
    return "Hello, GreenEye Backend is running!"

@app.route('/api/status')
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

# --- 앱 실행 부분 ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)