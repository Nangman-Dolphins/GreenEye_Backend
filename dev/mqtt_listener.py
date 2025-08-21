# mqtt_listener.py
import os
import json
from datetime import datetime
import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from dotenv import load_dotenv

load_dotenv()

MQTT_BROKER = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_BROKER_PORT", 1883))
MQTT_TOPIC = "GreenEye/data/#"

INFLUX_URL = os.getenv("INFLUXDB_URL")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN")
INFLUX_ORG = os.getenv("INFLUXDB_ORG")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET")

client_influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client_influx.write_api()

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        device_id = msg.topic.split("/")[-1]

        # 센서 데이터만 저장
        if "amb_temp" in payload:
            point = (
                Point("sensor_readings")
                .tag("device_id", device_id)
                .field("temperature", payload["amb_temp"])
                .field("humidity", payload["amb_humi"])
                .field("light_lux", payload["amb_light"])
                .field("soil_temp", payload["soil_temp"])
                .field("soil_moisture", payload["soil_humi"])
                .field("soil_ec", payload["soil_ec"])
                .field("battery", payload["bat_level"])
                .time(datetime.utcnow(), WritePrecision.NS)
            )
            write_api.write(bucket=INFLUX_BUCKET, record=point)
            print(f"[InfluxDB] Saved sensor data from {device_id}")

    except Exception as e:
        print(f"[Error] {e}")

client = mqtt.Client()
client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
client.on_message = on_message
client.connect(MQTT_BROKER, MQTT_PORT)
client.subscribe(MQTT_TOPIC)
print(f"Subscribed to {MQTT_TOPIC}")
client.loop_forever()