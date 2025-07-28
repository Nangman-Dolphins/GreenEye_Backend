import os
import json
from datetime import datetime, timedelta
import pytz # 시간대 처리를 위해 추가

# 이메일 발송을 위한 라이브러리
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# services.py에서 필요한 함수 임포트
# query_influxdb_data: InfluxDB 데이터 조회
from services import query_influxdb_data 

# database.py에서 필요한 함수 임포트
# get_db_connection: 사용자 정보 조회 (누구에게 보낼지)
from database import get_db_connection 

# .env 파일에서 환경 변수 로드 (app.py에서 이미 로드하겠지만, 명시적으로 다시 로드)
from dotenv import load_dotenv
load_dotenv()


# --- 환경 변수 가져오기 (.env 파일에서 설정된 값 사용) ---
INFLUXDB_URL = os.getenv('INFLUXDB_URL')
INFLUXDB_TOKEN = os.getenv('INFLUXDB_TOKEN')
INFLUXDB_ORG = os.getenv('INFLUXDB_ORG')
INFLUXDB_BUCKET = os.getenv('INFLUXDB_BUCKET')

EMAIL_HOST = os.getenv('EMAIL_HOST')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USERNAME = os.getenv('EMAIL_USERNAME')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')


# --- 보고서 생성 함수 ---
def generate_monthly_report_content(plant_id, start_time_obj, end_time_obj):
    """
    특정 식물의 지정된 기간 센서 데이터를 조회하여 보고서 내용을 HTML 형식으로 생성합니다.
    start_time_obj, end_time_obj는 시간대 정보가 포함된(aware) datetime 객체여야 합니다.
    """
    # InfluxDB Flux 쿼리에서 시간대 정보를 포함한 ISO 8601 문자열을 사용합니다.
    # **수정된 부분: .replace('+00:00', 'Z')를 사용하여 정확한 UTC 형식으로 변환**
    start_time_str_iso_utc = start_time_obj.astimezone(pytz.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    end_time_str_iso_utc = end_time_obj.astimezone(pytz.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    # --- Flux 쿼리 문자열 ---
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_time_str_iso_utc}, stop: {end_time_str_iso_utc})
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.plant_id == "{plant_id}")
      |> aggregateWindow(every: 1h, fn: mean, createEmpty: false) 
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time", "plant_id", "temperature", "humidity", "light_lux", "soil_moisture", "soil_ec"])
      |> yield(name: "hourly_summary")
    '''
    
    print("\n--- [DEBUG] Generated Flux Query ---")
    print(query)
    print("------------------------------------\n")
    
    print(f"[Report] Querying InfluxDB for {plant_id} from {start_time_obj.strftime('%Y-%m-%d %H:%M:%S')} to {end_time_obj.strftime('%Y-%m-%d %H:%M:%S')}")
    historical_data = query_influxdb_data(query)

    # 보고서 내용 HTML 형식으로 생성
    report_html = f"<h2>GreenEye 월간 식물 보고서 - {plant_id}</h2>"
    report_html += f"<p>기간: {start_time_obj.strftime('%Y-%m-%d')} ~ {end_time_obj.strftime('%Y-%m-%d')}</p>"
    
    if not historical_data:
        report_html += "<p>이 기간 동안의 센서 데이터가 없습니다.</p>"
        return report_html

    # 데이터 요약 (평균, 최대, 최소 - 간단한 예시)
    temps = [d.get('temperature') for d in historical_data if d.get('temperature') is not None]
    hums = [d.get('humidity') for d in historical_data if d.get('humidity') is not None]
    lights = [d.get('light_lux') for d in historical_data if d.get('light_lux') is not None]
    soil_moistures = [d.get('soil_moisture') for d in historical_data if d.get('soil_moisture') is not None]
    soil_ecs = [d.get('soil_ec') for d in historical_data if d.get('soil_ec') is not None]

    avg_temp = sum(temps) / len(temps) if temps else 0
    max_temp = max(temps) if temps else 0
    min_temp = min(temps) if temps else 0
    
    avg_hum = sum(hums) / len(hums) if hums else 0
    avg_light = sum(lights) / len(lights) if lights else 0
    avg_soil_moisture = sum(soil_moistures) / len(soil_moistures) if soil_moistures else 0
    avg_soil_ec = sum(soil_ecs) / len(soil_ecs) if soil_ecs else 0

    report_html += f"<h3>주요 통계</h3>"
    report_html += "<ul>"
    report_html += f"<li>평균 온도: {avg_temp:.2f} °C (최고: {max_temp:.2f} °C, 최저: {min_temp:.2f} °C)</li>"
    report_html += f"<li>평균 습도: {avg_hum:.2f} %</li>"
    report_html += f"<li>평균 조도: {avg_light:.2f} lux</li>"
    report_html += f"<li>평균 토양 수분: {avg_soil_moisture:.2f}</li>"
    report_html += f"<li>평균 토양 전도도: {avg_soil_ec:.2f} mS/cm</li>"
    report_html += "</ul>"

    report_html += "<h3>시간별 데이터 요약 (최근 10개)</h3><table border='1' style='width:100%; border-collapse: collapse;'><tr><th>시간</th><th>온도</th><th>습도</th><th>조도</th><th>토양수분</th><th>토양전도도</th></tr>"
    for i, record in enumerate(historical_data):
        if i >= 10: # 너무 많으면 최근 10개만 테이블에 보여줌
            break
        # InfluxDB에서 가져온 시간은 UTC이므로, 필요시 Asia/Seoul로 변환하여 표시
        # _time 필드가 InfluxDB에서 타임스탬프 (int) 또는 ISO 문자열로 올 수 있으므로 타입을 확인
        if isinstance(record.get('_time'), int):
            record_time_utc = datetime.fromtimestamp(record.get('_time') / 10**9, tz=pytz.utc) 
        elif isinstance(record.get('_time'), str):
            record_time_utc = datetime.fromisoformat(record.get('_time').replace('Z', '+00:00'))
        else:
            record_time_utc = datetime.now(pytz.utc) # 기본값 또는 오류 처리

        record_time_korea = record_time_utc.astimezone(pytz.timezone('Asia/Seoul'))

        report_html += f"<tr><td>{record_time_korea.strftime('%Y-%m-%d %H:%M')}</td><td>{record.get('temperature', '-'):.2f}</td><td>{record.get('humidity', '-'):.2f}</td><td>{record.get('light_lux', '-')}</td><td>{record.get('soil_moisture', '-'):.2f}</td><td>{record.get('soil_ec', '-'):.2f}</td></tr>"
    report_html += "</table>"
    report_html += "<p>전체 데이터는 GreenEye 대시보드를 통해 확인해주세요.</p>"

    return report_html


# --- 이메일 발송 함수 ---
def send_email(to_email, subject, html_content):
    """
    지정된 이메일 주소로 HTML 형식의 이메일을 발송합니다.
    """
    if not EMAIL_HOST or not EMAIL_USERNAME or not EMAIL_PASSWORD:
        print("Email settings are not fully configured in .env file. Skipping email sending.")
        return False

    msg = MIMEMultipart("alternative")
    msg['From'] = EMAIL_USERNAME
    msg['To'] = to_email
    msg['Subject'] = subject

    # HTML 내용을 추가
    msg.attach(MIMEText(html_content, 'html'))

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls() # TLS 보안 연결 시작 (Port 587)
            # server.send_message(msg) # Python 3.6+에서 권장되는 send_message() 대신 sendmail() 사용
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD) # SMTP 서버 로그인
            server.sendmail(EMAIL_USERNAME, to_email, msg.as_string()) # 이메일 발송
        print(f"Monthly report email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"Error sending email to {to_email}: {e}")
        return False


# --- 월별 보고서 발송 총괄 함수 ---
def send_monthly_reports_for_users():
    """
    모든 사용자에게 각자의 식물에 대한 월별 보고서를 발송합니다.
    APScheduler를 통해 매월 특정 날짜에 호출될 예정.
    """
    print(f"\n--- Starting monthly report generation and sending at {datetime.now()} ---")
    
    conn = get_db_connection() # database.py의 함수 호출
    cursor = conn.cursor()
    
    # 1. 모든 사용자 조회
    cursor.execute("SELECT id, email FROM users")
    users = cursor.fetchall()
    conn.close() # DB 연결 닫기 (항상 닫는 것이 중요)

    if not users:
        print("No users found in the database. Skipping monthly reports.")
        return

    # 2. 보고서 기간 설정 (InfluxDB는 UTC 데이터를 사용하므로, 쿼리 시간은 UTC aware로 설정)
    # 현재 시점의 UTC datetime 객체 생성 (timezone aware하게)
    now_utc_aware = datetime.utcnow().replace(tzinfo=pytz.utc)

    # 테스트를 위한 '지난 7일' 기간을 UTC 기준으로 설정
    # (주의: 더미 데이터가 충분히 쌓여 있어야 보고서 내용이 나옵니다.)
    test_end_utc_aware = now_utc_aware # 현재 시점까지
    test_start_utc_aware = test_end_utc_aware - timedelta(days=7) # 지난 7일

    # 3. 각 사용자에게 보고서 발송
    for user in users:
        user_id = user['id']
        user_email = user['email']
        print(f"Processing report for user: {user_email}")

        # (TODO: 나중에 사용자와 식물을 연결하는 테이블이 생기면 해당 사용자의 식물만 조회)
        # 지금은 모든 사용자에게 동일한 더미 식물 ID의 보고서 발송 (또는 plant_ids_to_monitor 활용)
        plant_ids_to_monitor = ["plant_001", "plant_002", "plant_003"] # 더미 센서에서 사용하는 식물 ID 목록

        for plant_id in plant_ids_to_monitor:
            report_subject = f"GreenEye 월간 식물 보고서 - {plant_id} ({test_start_utc_aware.strftime('%Y-%m-%d')} ~ {test_end_utc_aware.strftime('%Y-%m-%d')})"
            
            # generate_monthly_report_content 함수 호출 시 UTC aware datetime 객체 전달
            report_content = generate_monthly_report_content(plant_id, test_start_utc_aware, test_end_utc_aware)
            
            if report_content:
                send_email(user_email, report_subject, report_content)
            else:
                print(f"No report content generated for {plant_id} for user {user_email}.")
    
    print("--- Monthly report generation and sending finished. ---\n")

# 이 파일을 직접 실행해서 테스트할 수 있도록 (개발용)
# APScheduler를 통해 app.py에서 호출하는 것이 주된 사용법입니다.
if __name__ == '__main__':
    # 이 파일을 단독으로 실행할 경우 필요한 최소한의 초기화 (주석 해제 후 사용)
    # from dotenv import load_dotenv
    # load_dotenv()
    # from services import connect_influxdb, connect_redis, connect_mqtt
    # connect_influxdb()
    # connect_redis()
    # connect_mqtt() # MQTT는 단독 실행에서 필요 없을 수 있음 (로그 수신 등)
    # from database import init_db, add_user
    # init_db()
    # add_user("testuser@example.com", "testpassword123") # 테스트 사용자 추가 (필요시)
    
    # send_monthly_reports_for_users()
    print("This file is meant to be imported and called by app.py or APScheduler.")
    print("To test, run 'python app.py' and call send_monthly_reports_for_users() via scheduler or Flask shell.")