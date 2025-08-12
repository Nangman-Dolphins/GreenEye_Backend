import json
from datetime import datetime
import pytz

from services import send_config_to_device, get_redis_data

def check_and_apply_auto_control(mac_address: str):
    """
    특정 단말기의 센서 데이터를 확인하고 자동 제어 규칙을 적용합니다.
    """
    tz = pytz.timezone("Asia/Seoul")
    now_kst = datetime.now(tz)
    hour = now_kst.hour

    print(f"\n[Auto Control] Checking conditions for {mac_address} at {now_kst.strftime('%Y-%m-%d %H:%M:%S')}")

    latest = get_redis_data(f"latest_sensor_data:{mac_address}")
    if not latest:
        print(f"[Auto Control] No latest sensor data found for {mac_address}. Skipping.")
        return

    soil_moisture = latest.get("soil_moisture")
    light_lux = latest.get("light_lux")

    # 현재 액추에이터 상태(중복 명령 방지)
    pump_state = get_redis_data(f"actuator_state:{mac_address}:water_pump") or {}
    pump_on = pump_state.get("status") == "on"

    led_state = get_redis_data(f"actuator_state:{mac_address}:led") or {}
    led_on = led_state.get("status") == "on"

    # 1) 토양 수분 펌프
    if soil_moisture is not None:
        if soil_moisture < 300 and not pump_on:
            print(f"[Auto Control] {mac_address}: Soil moisture {soil_moisture} LOW -> pump ON")
            send_config_to_device(mac_address, {"device": "water_pump", "action": "on", "duration_sec": 5})
        elif soil_moisture > 700 and pump_on:
            print(f"[Auto Control] {mac_address}: Soil moisture {soil_moisture} HIGH -> pump OFF")
            send_config_to_device(mac_address, {"device": "water_pump", "action": "off"})

    # 2) 조도 LED (07~20시)
    if light_lux is not None and 7 <= hour <= 20:
        if light_lux < 500 and not led_on:
            print(f"[Auto Control] {mac_address}: Light {light_lux} LOW -> LED ON")
            send_config_to_device(mac_address, {"device": "led", "action": "on"})
        elif light_lux > 800 and led_on:
            print(f"[Auto Control] {mac_address}: Light {light_lux} HIGH -> LED OFF")
            send_config_to_device(mac_address, {"device": "led", "action": "off"})
    elif led_on:
        print(f"[Auto Control] {mac_address}: Night time {hour}h -> LED OFF")
        send_config_to_device(mac_address, {"device": "led", "action": "off"})
