from datetime import datetime
import pytz
import json

from .services import send_config_to_device, get_redis_data
from .database import get_device_by_friendly_name

def check_and_apply_auto_control(device_id: str):
    tz = pytz.timezone("Asia/Seoul")
    now_kst = datetime.now(tz)
    hour = now_kst.hour

    latest = get_redis_data(f"latest_sensor_data:{device_id}")
    if not latest:
        print(f"[Auto Control] No latest sensor data for {device_id}.")
        return

    soil_moisture = latest.get("soil_moisture")
    light_lux = latest.get("light_lux")

    pump_state_data = get_redis_data(f"actuator_state:{device_id}:water_pump")
    current_pump_status = pump_state_data.get("status") if pump_state_data else "off"

    led_state_data = get_redis_data(f"actuator_state:{device_id}:flash") # flash_en 값을 제어
    current_led_status = led_state_data.get("flash_en") if led_state_data else 0 # 0=off

    if soil_moisture is not None:
        if soil_moisture < 300 and current_pump_status != "on":
            send_config_to_device(device_id, {"water_pump_action": 1, "water_pump_duration": 5}) # 가정
        elif soil_moisture > 700 and current_pump_status != "off":
            send_config_to_device(device_id, {"water_pump_action": 0})

    if light_lux is not None and 7 <= hour <= 20:
        if light_lux < 500 and current_led_status != 1: # flash_en이 켜지지 않았을 때
            send_config_to_device(device_id, {"flash_en": 1, "flash_level": 128})
        elif light_lux > 800 and current_led_status != 0:
            send_config_to_device(device_id, {"flash_en": 0})
    else:
        if current_led_status != 0:
            send_config_to_device(device_id, {"flash_en": 0, "flash_nt": 1, "flash_level": 180})

def handle_manual_control(device_id: str, device_type: str, action: str, duration_sec: int = 0):
    print(f"[Manual Control] Received command for {device_id}, device: {device_type}, action: {action}")
    
    config_payload = {}
    if device_type == "water_pump":
        config_payload = {"water_pump_action": 1 if action == "on" else 0, "water_pump_duration": duration_sec}
    elif device_type == "led":
        flash_state = 1 if action == "on" else 0
        config_payload = {"flash_en": flash_state}
    elif device_type == "humidifier":
        config_payload = {"humidifier_action": 1 if action == "on" else 0}
        
    if config_payload:
        send_config_to_device(device_id, config_payload)
        return {"status": "success", "message": f"Manual control command '{action}' for '{device_type}' sent to {device_id}"}
    
    return {"status": "error", "message": f"Unknown device type '{device_type}'"}

