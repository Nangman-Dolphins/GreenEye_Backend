# control_logic.py

import json
from datetime import datetime
import os

# services.py에서 필요한 함수 임포트
# publish_mqtt_message: MQTT 메시지 발행
# set_redis_data: Redis에 데이터 저장
# get_redis_data: Redis에서 데이터 조회 (나중에 자동 제어 시 활용)
# query_influxdb_data: InfluxDB에서 데이터 조회 (나중에 자동 제어 시 활용)
from services import publish_mqtt_message, set_redis_data, get_redis_data, query_influxdb_data


# --- 액추에이터 제어 명령 전송 함수 (내부 사용) ---
def send_actuator_command(plant_id, device, action, duration_sec=0):
    """
    지정된 식물 ID의 액추에이터에 제어 명령을 MQTT로 발행합니다.
    """
    topic = f"plant/control/{plant_id}/{device}"
    message_payload = json.dumps({
        "action": action,
        "duration_sec": duration_sec,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    publish_mqtt_message(topic, message_payload)
    print(f"[Control Logic] Command '{action}' for '{device}' sent to {plant_id}")
    
    # Redis에 액추에이터의 현재 상태 업데이트 (프론트엔드 실시간 표시용)
    set_redis_data(f"plant_control_state:{plant_id}:{device}", {"status": action, "timestamp": datetime.utcnow().isoformat()})
    print(f"[Control Logic] Updated Redis state for {plant_id}:{device} to {action}")


# --- 수동 제어 처리 함수 ---
def handle_manual_control(plant_id, device, action, duration_sec=0):
    """
    웹 UI에서 들어온 수동 제어 명령을 처리하고 발행합니다.
    """
    print(f"[Manual Control] Received command for {plant_id}, device: {device}, action: {action}")
    # 여기서는 단순 발행. 나중에 자동/수동 모드 상태 체크 로직 추가 가능
    send_actuator_command(plant_id, device, action, duration_sec)
    return {"status": "success", "message": f"Manual control command '{action}' for '{device}' sent to {plant_id}"}


# --- 자동 제어 로직 (초기 뼈대) ---
# 이 함수는 APScheduler를 통해 주기적으로 실행될 거야.
def check_and_apply_auto_control(plant_id):
    """
    특정 식물의 센서 데이터를 확인하고 자동 제어 규칙을 적용합니다.
    지금은 더미 로직입니다.
    """
    print(f"[Auto Control] Checking conditions for {plant_id} at {datetime.now().isoformat()}")

    # 1. Redis에서 최신 센서 데이터 가져오기 (더미 데이터가 쌓이고 있으니 확인 가능)
    latest_data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if not latest_data:
        print(f"[Auto Control] No latest sensor data found for {plant_id}. Skipping auto control.")
        return

    soil_moisture = latest_data.get("soil_moisture")
    temperature = latest_data.get("temperature")
    
    # --- 자동 제어 규칙 예시 (나중에 강화) ---
    # 2. 토양 수분 규칙 (워터펌프)
    if soil_moisture is not None and soil_moisture < 300: # 토양 수분 임계값
        print(f"[Auto Control] {plant_id}: Soil moisture ({soil_moisture}) is low. Turning on water pump.")
        # send_actuator_command(plant_id, "water_pump", "on", duration_sec=5) # 실제 명령은 주석 처리
        pass # 실제 명령 대신 pass로 일단 아무것도 안 함
    elif soil_moisture is not None and soil_moisture > 700:
        print(f"[Auto Control] {plant_id}: Soil moisture ({soil_moisture}) is high. Turning off water pump.")
        # send_actuator_command(plant_id, "water_pump", "off") # 실제 명령은 주석 처리
        pass

    # 3. 온도 규칙 (가습기 예시)
    if temperature is not None and temperature > 28: # 온도 임계값 (가상)
        print(f"[Auto Control] {plant_id}: Temperature ({temperature}) is high. Turning on humidifier.")
        # send_actuator_command(plant_id, "humidifier", "on") # 실제 명령은 주석 처리
        pass
    
    # 4. AI 진단 결과 활용 (나중에 AI 모듈 완성 시 연동)
    # latest_ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{plant_id}")
    # if latest_ai_diagnosis and latest_ai_diagnosis.get("diagnosis") == "병충해 의심":
    #     print(f"[Auto Control] {plant_id}: AI detected possible disease. Activating special LED.")
    #     send_actuator_command(plant_id, "led", "special_mode")
    #     pass


# --- APScheduler 설정 (나중에 Flask 앱에 통합될 부분) ---
# 이건 control_logic.py 파일 자체에서는 실행되지 않아.
# app.py에서 이 함수를 스케줄러에 등록해서 주기적으로 호출할 거야.