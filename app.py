import os
from flask import Flask, jsonify, request
from dotenv import load_dotenv
import time
import json # json 추가 (publish_mqtt_message 때문)

# services.py에서 정의한 함수들을 import
from services import initialize_services, mqtt_client, get_redis_data, query_influxdb_data, publish_mqtt_message, process_sensor_data

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


# --- Flask 앱 시작 시 서비스 초기화 ---
with app.app_context():
    initialize_services() # 모든 서비스 연결을 초기화 (MQTT, InfluxDB, Redis)
    # MQTT 구독: 모든 센서 데이터 토픽을 구독
    # 'sensor/data/#'는 'sensor/data/plant_001', 'sensor/data/plant_002' 등 모든 하위 토픽을 구독
    mqtt_client.subscribe("sensor/data/#")
    print("Subscribed to MQTT topic 'sensor/data/#'")


# --- 기본 라우트 (API 엔드포인트) 정의 ---
@app.route('/')
def home():
    return "Hello, GreenEye Backend is running!"

@app.route('/api/status')
def status():
    return jsonify({"status": "ok", "message": "Backend API is working!"})

@app.route('/api/latest_sensor_data/<plant_id>')
def get_latest_sensor_data(plant_id):
    """
    Redis에서 특정 식물의 최신 5가지 센서 데이터를 가져오는 API.
    """
    data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if data:
        return jsonify(data)
    return jsonify({"error": "No data found for this plant ID"}), 404

@app.route('/api/historical_sensor_data/<plant_id>')
def get_historical_sensor_data(plant_id):
    """
    InfluxDB에서 특정 식물의 과거 센서 데이터를 조회하는 API.
    실제 프론트엔드 요구사항에 맞춰 쿼리 결과를 가공해야 함.
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
    # 여기서는 간략하게만 반환
    formatted_data = []
    for record in data:
        # InfluxDB record.values에서 키-값 쌍을 직접 가져오는 방식
        # 예시: {'_time': '...', 'plant_id': '...', 'temperature': 25.0, ...}
        formatted_data.append(record)
    return jsonify(formatted_data)

@app.route('/api/control_plant/<plant_id>', methods=['POST']) # POST 메서드로 변경
def control_plant(plant_id):
    """
    MQTT를 통해 식물 제어 명령을 발행하는 API.
    웹에서 JSON 형식의 명령을 받습니다.
    """
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400

    command_data = request.get_json()
    action = command_data.get('action') # 예: "turn_on_water_pump"
    duration = command_data.get('duration_sec', 0) # 예: 10 (초)
    # 다른 제어 파라미터도 추가 가능 (예: 'device': 'water_pump' 등)

    if not action:
        return jsonify({"error": "Missing 'action' in command"}), 400

    topic = f"plant/control/{plant_id}"
    message_payload = json.dumps({"action": action, "duration_sec": duration, "timestamp": datetime.utcnow().isoformat()})
    
    publish_mqtt_message(topic, message_payload)
    return jsonify({"status": "success", "message": f"Control command '{action}' sent to {plant_id}"})

# --- 앱 실행 부분 ---
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)