# greeneye_backend/control_logic.py

import json
from datetime import datetime
import pytz

# [변경] services에서 제어/설정 전용 함수를 임포트합니다.
from services import send_config_to_device, get_redis_data

def check_and_apply_auto_control(mac_address):
    """
    특정 단말기의 센서 데이터를 확인하고 자동 제어 규칙을 적용합니다.
    """
    korea_timezone = pytz.timezone('Asia/Seoul')
    now_korea = datetime.now(korea_timezone)
    current_time_hour = now_korea.hour

    print(f"\n[Auto Control] Checking conditions for {mac_address} at {now_korea.strftime('%Y-%m-%d %H:%M:%S')}")

    latest_data = get_redis_data(f"latest_sensor_data:{mac_address}")
    if not latest_data:
        print(f"[Auto Control] No latest sensor data found for {mac_address}. Skipping.")
        return

    soil_moisture = latest_data.get("soil_moisture")
    temperature = latest_data.get("temperature")
    light_lux = latest_data.get("light_lux")
    
    # Redis에서 현재 액추에이터 상태 가져오기 (중복 명령 방지)
    pump_state_data = get_redis_data(f"actuator_state:{mac_address}:water_pump")
    current_pump_status = pump_state_data.get("status") if pump_state_data else "off"
    
    led_state_data = get_redis_data(f"actuator_state:{mac_address}:led")
    current_led_status = led_state_data.get("status") if led_state_data else "off"

    # --- 자동 제어 규칙 ---

    # 1. 토양 수분 규칙 (워터펌프)
    if soil_moisture is not None:
        if soil_moisture < 300 and current_pump_status != "on":
            print(f"[Auto Control] {mac_address}: Soil moisture ({soil_moisture}) is LOW. Turning ON pump.")
            # [변경] send_config_to_device 함수 사용
            send_config_to_device(mac_address, {"device": "water_pump", "action": "on", "duration_sec": 5})
        elif soil_moisture > 700 and current_pump_status == "on":
            print(f"[Auto Control] {mac_address}: Soil moisture ({soil_moisture}) is HIGH. Turning OFF pump.")
            send_config_to_device(mac_address, {"device": "water_pump", "action": "off"})

    # 2. 조도 규칙 (LED)
    # 주간 (오전 7시 ~ 오후 8시)
    if light_lux is not None and 7 <= current_time_hour <= 20: 
        if light_lux < 500 and current_led_status != "on":
            print(f"[Auto Control] {mac_address}: Light level ({light_lux}) is LOW. Turning ON LED.")
            send_config_to_device(mac_address, {"device": "led", "action": "on"})
        elif light_lux > 800 and current_led_status == "on":
            print(f"[Auto Control] {mac_address}: Light level ({light_lux}) is HIGH. Turning OFF LED.")
            send_config_to_device(mac_address, {"device": "led", "action": "off"})
    # 야간
    elif current_led_status == "on":
        print(f"[Auto Control] {mac_address}: It's night time ({current_time_hour}h). Turning OFF LED.")
        send_config_to_device(mac_address, {"device": "led", "action": "off"})
