# 자동/수동 제어 관련 로직 모음

import json
from datetime import datetime
import os
import pytz

# services.py에서 필요한 함수 임포트
from services import publish_mqtt_message, set_redis_data, get_redis_data, query_influxdb_data

# --- 액추에이터 제어 명령 전송 함수 (내부 사용) ---
def send_actuator_command(mac_address, device, action, duration_sec=0):
    """
    지정된 단말기(MAC 주소)의 액추에이터에 제어 명령을 MQTT로 발행합니다.
    """
    topic = f"plant/control/{mac_address}/{device}"
    message_payload = json.dumps({
        "action": action,
        "duration_sec": duration_sec,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    publish_mqtt_message(topic, message_payload)
    print(f"[Control Logic] Command '{action}' for '{device}' sent to {mac_address}")
    
    # Redis에 액추에이터의 현재 상태 업데이트 (프론트엔드 실시간 표시용)
    set_redis_data(f"plant_control_state:{mac_address}:{device}", {"status": action, "timestamp": datetime.utcnow().isoformat()})
    print(f"[Control Logic] Updated Redis state for {mac_address}:{device} to {action}")


# --- 수동 제어 처리 함수 ---
def handle_manual_control(mac_address, device, action, duration_sec=0):
    """
    웹 UI에서 들어온 수동 제어 명령을 처리하고 발행합니다.
    """
    print(f"[Manual Control] Received command for {mac_address}, device: {device}, action: {action}")
    # 여기서는 단순 발행. 나중에 자동/수동 모드 상태 체크 로직 추가 가능
    send_actuator_command(mac_address, device, action, duration_sec)
    return {"status": "success", "message": f"Manual control command '{action}' for '{device}' sent to {mac_address}"}


# --- 자동 제어 로직 ---
def check_and_apply_auto_control(mac_address):
    """
    특정 단말기의 센서 데이터를 확인하고 자동 제어 규칙을 적용합니다.
    """
    # 현재 시간 (대한민국 시간대 반영)
    korea_timezone = pytz.timezone('Asia/Seoul')
    now_korea = datetime.now(korea_timezone)
    current_time_hour = now_korea.hour

    print(f"\n[Auto Control] Checking conditions for {mac_address} at {now_korea.strftime('%Y-%m-%d %H:%M:%S')}")

    latest_data = get_redis_data(f"latest_sensor_data:{mac_address}")
    if not latest_data:
        print(f"[Auto Control] No latest sensor data found for {mac_address}. Skipping auto control.")
        return

    soil_moisture = latest_data.get("soil_moisture")
    temperature = latest_data.get("temperature")
    light_lux = latest_data.get("light_lux")
    
    # 해당 장치의 현재 상태 가져오기 (Redis에서)
    pump_state_data = get_redis_data(f"plant_control_state:{mac_address}:water_pump")
    current_pump_status = pump_state_data.get("status") if pump_state_data else "off"
    
    led_state_data = get_redis_data(f"plant_control_state:{mac_address}:led")
    current_led_status = led_state_data.get("status") if led_state_data else "off"


    # --- 자동 제어 규칙 예시 ---

    # 2. 토양 수분 규칙 (워터펌프)
    if soil_moisture is not None:
        if soil_moisture < 300 and current_pump_status != "on":
            print(f"[Auto Control] {mac_address}: Soil moisture ({soil_moisture}) is LOW. Turning on water pump for 5 sec.")
            send_actuator_command(mac_address, "water_pump", "on", duration_sec=5)
        elif soil_moisture > 700 and current_pump_status != "off":
            print(f"[Auto Control] {mac_address}: Soil moisture ({soil_moisture}) is HIGH. Turning off water pump.")
            send_actuator_command(mac_address, "water_pump", "off")

    # 3. 조도 규칙 (LED 랜턴)
    if light_lux is not None and 7 <= current_time_hour <= 20: 
        if light_lux < 500 and current_led_status != "on":
            print(f"[Auto Control] {mac_address}: Light level ({light_lux}) is LOW. Turning on LED.")
            send_actuator_command(mac_address, "led", "on")
        elif light_lux > 800 and current_led_status != "off":
            print(f"[Auto Control] {mac_address}: Light level ({light_lux}) is HIGH. Turning off LED.")
            send_actuator_command(mac_address, "led", "off")
    elif light_lux is not None:
        if current_led_status != "off":
            print(f"[Auto Control] {mac_address}: It's night time ({current_time_hour}h). Turning off LED.")
            send_actuator_command(mac_address, "led", "off")


    # 4. AI 진단 결과 활용 (나중에 AI 모듈 완성 시 연동)
    # latest_ai_diagnosis = get_redis_data(f"latest_ai_diagnosis:{mac_address}")
    # if latest_ai_diagnosis and latest_ai_diagnosis.get("diagnosis") == "병충해 의심":
    #     print(f"[Auto Control] {mac_address}: AI detected possible disease. Activating special LED mode.")
    #     send_actuator_command(mac_address, "led", "special_mode")
    #     pass