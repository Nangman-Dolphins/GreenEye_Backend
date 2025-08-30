import os, json, sys
from paho.mqtt import client as mqtt_client

HOST = os.getenv("MQTT_HOST", "localhost")
PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC = os.getenv("MQTT_TOPIC", "GreenEye/conf/2e52")
USER = os.getenv("MQTT_USERNAME")
PWD  = os.getenv("MQTT_PASSWORD")

got_one = False

def on_connect(c, u, f, rc):
    if rc == 0:
        print(f"[MQTT] connected to {HOST}:{PORT}, subscribe {TOPIC}")
        c.subscribe(TOPIC, qos=1)
    else:
        print(f"[MQTT] connect failed rc={rc}")

def on_message(c, u, msg):
    global got_one
    try:
        print(f"[MQTT] topic={msg.topic} retain={getattr(msg, 'retain', None)}")
        payload = msg.payload.decode("utf-8", "replace")
        print("[MQTT] payload:", payload)
        got_one = True
        c.disconnect()  # 첫 메시지 받고 종료
    except Exception as e:
        print("on_message error:", e)

client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1)
if USER and PWD:
    client.username_pw_set(USER, PWD)
client.on_connect = on_connect
client.on_message = on_message
client.connect(HOST, PORT, keepalive=60)
client.loop_forever()