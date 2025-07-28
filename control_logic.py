# control_logic.py (수정된 내용 - 자동 제어 로직 활성화)

import json
from datetime import datetime
import os
import pytz # 시간대 처리를 위해 추가

# services.py에서 필요한 함수 임포트
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


# --- 수동 제어 처리 함수 (이전과 동일) ---
def handle_manual_control(plant_id, device, action, duration_sec=0):
    """
    웹 UI에서 들어온 수동 제어 명령을 처리하고 발행합니다.
    """
    print(f"[Manual Control] Received command for {plant_id}, device: {device}, action: {action}")
    # 여기서는 단순 발행. 나중에 자동/수동 모드 상태 체크 로직 추가 가능
    send_actuator_command(plant_id, device, action, duration_sec)
    return {"status": "success", "message": f"Manual control command '{action}' for '{device}' sent to {plant_id}"}


# --- 자동 제어 로직 ---
# 이 함수는 APScheduler를 통해 주기적으로 실행될 거야.
def check_and_apply_auto_control(plant_id):
    """
    특정 식물의 센서 데이터를 확인하고 자동 제어 규칙을 적용합니다.
    지금은 더미 로직입니다.
    """
    # 현재 시간 (대한민국 시간대 반영)
    korea_timezone = pytz.timezone('Asia/Seoul')
    now_korea = datetime.now(korea_timezone)
    current_time_hour = now_korea.hour

    print(f"\n[Auto Control] Checking conditions for {plant_id} at {now_korea.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Redis에서 최신 센서 데이터 가져오기 (더미 데이터가 쌓이고 있으니 확인 가능)
    latest_data = get_redis_data(f"latest_sensor_data:{plant_id}")
    if not latest_data:
        print(f"[Auto Control] No latest sensor data found for {plant_id}. Skipping auto control.")
        return

    # 센서 값 추출 (payload.get()으로 None 체크)
    soil_moisture = latest_data.get("soil_moisture")
    temperature = latest_data.get("temperature")
    light_lux = latest_data.get("light_lux")
    
    # 해당 장치의 현재 상태 가져오기 (Redis에서)
    pump_state_data = get_redis_data(f"plant_control_state:{plant_id}:water_pump")
    current_pump_status = pump_state_data.get("status") if pump_state_data else "off" # 기본은 꺼짐
    
    led_state_data = get_redis_data(f"plant_control_state:{plant_id}:led")
    current_led_status = led_state_data.get("status") if led_state_data else "off" # 기본은 꺼짐

    # --- 자동 제어 규칙 예시 (지금은 주석 해제하여 활성화) ---
    # 나중에 자동/수동 모드 설정에 따라 이 규칙 적용 여부 결정 로직 추가 필요

    # 2. 토양 수분 규칙 (워터펌프)
    if soil_moisture is not None:
        if soil_moisture < 300 and current_pump_status != "on": # 토양 수분 낮고 현재 꺼져있을 때
            print(f"[Auto Control] {plant_id}: Soil moisture ({soil_moisture}) is LOW. Turning on water pump for 5 sec.")
            send_actuator_command(plant_id, "water_pump", "on", duration_sec=5)
        elif soil_moisture > 700 and current_pump_status != "off": # 토양 수분 높고 현재 켜져있을 때
            print(f"[Auto Control] {plant_id}: Soil moisture ({soil_moisture}) is HIGH. Turning off water pump.")
            send_actuator_command(plant_id, "water_pump", "off")
    else:
        print(f"[Auto Control] {plant_id}: Soil moisture data not available.")

    # 3. 조도 규칙 (LED 랜턴)
    # 낮 시간 (예: 오전 7시부터 오후 8시)에만 조도 규칙 적용
    if light_lux is not None and 7 <= current_time_hour <= 20: 
        if light_lux < 500 and current_led_status != "on": # 조도 낮고 현재 꺼져있을 때
            print(f"[Auto Control] {plant_id}: Light level ({light_lux}) is LOW. Turning on LED.")
            send_actuator_command(plant_id, "led", "on")
        elif light_lux > 800 and current_led_status != "off": # 조도 충분하고 현재 켜져있을 때
            print(f"[Auto Control] {plant_id}: Light level ({light_lux}) is HIGH. Turning off LED.")
            send_actuator_command(plant_id, "led", "off")
    elif light_lux is not None: # 밤 시간 (오후 8시 이후 ~ 오전 7시 이전)
        if current_led_status != "off": # 밤인데 LED가 켜져있으면 끄기
            print(f"[Auto Control] {plant_id}: It's night time ({current_time_hour}h). Turning off LED.")
            send_actuator_command(plant_id, "led", "off")
    else:
        print(f"[Auto Control] {plant_id}: Light lux data not available.")


    # 4. AI 진단 결과 활용 (현재는 주석 처리, 나중에 AI 모듈 완성 시 연동)
    # latest_ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{plant_id}")
    # if latest_ai_diagnosis and latest_ai_diagnosis.get("diagnosis") == "병충해 의심":
    #     print(f"[Auto Control] {plant_id}: AI detected possible disease. Activating special LED mode.")
    #     send_actuator_command(plant_id, "led", "special_mode")
    #     pass