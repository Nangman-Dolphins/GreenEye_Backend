import os
from datetime import datetime, timedelta
import pytz
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from services import query_influxdb_data
from database import get_db_connection, get_all_devices  # [변경] 디바이스 목록 사용

from dotenv import load_dotenv
load_dotenv()

INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

def generate_monthly_report_content_by_mac(mac_address: str, start_dt, end_dt):
    """
    mac_address 기준으로 기간 데이터를 조회하여 HTML 보고서를 생성.
    """
    start_iso = start_dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = end_dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_iso}, stop: {end_iso})
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.mac_address == "{mac_address}")
      |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time", "mac_address", "temperature", "humidity", "light_lux", "soil_moisture", "soil_ec"])
    '''

    print("\n--- [DEBUG] Generated Flux Query ---")
    print(query)
    print("------------------------------------\n")

    rows = query_influxdb_data(query)
    html = f"<h2>GreenEye 월간 식물 보고서 - {mac_address}</h2>"
    html += f"<p>기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}</p>"

    if not rows:
        html += "<p>이 기간 동안의 센서 데이터가 없습니다.</p>"
        return html

    temps = [r.get("temperature") for r in rows if r.get("temperature") is not None]
    hums = [r.get("humidity") for r in rows if r.get("humidity") is not None]
    lights = [r.get("light_lux") for r in rows if r.get("light_lux") is not None]
    moist = [r.get("soil_moisture") for r in rows if r.get("soil_moisture") is not None]
    ecs = [r.get("soil_ec") for r in rows if r.get("soil_ec") is not None]

    avg_temp = sum(temps) / len(temps) if temps else 0
    max_temp = max(temps) if temps else 0
    min_temp = min(temps) if temps else 0
    avg_hum = sum(hums) / len(hums) if hums else 0
    avg_light = sum(lights) / len(lights) if lights else 0
    avg_moist = sum(moist) / len(moist) if moist else 0
    avg_ec = sum(ecs) / len(ecs) if ecs else 0

    html += "<h3>주요 통계</h3><ul>"
    html += f"<li>평균 온도: {avg_temp:.2f} °C (최고: {max_temp:.2f} °C, 최저: {min_temp:.2f} °C)</li>"
    html += f"<li>평균 습도: {avg_hum:.2f} %</li>"
    html += f"<li>평균 조도: {avg_light:.2f} lux</li>"
    html += f"<li>평균 토양 수분: {avg_moist:.2f}</li>"
    html += f"<li>평균 토양 전도도: {avg_ec:.2f} mS/cm</li>"
    html += "</ul>"

    html += "<h3>시간별 데이터 요약 (최근 10개)</h3>"
    html += "<table border='1' style='width:100%; border-collapse: collapse;'>"
    html += "<tr><th>시간</th><th>온도</th><th>습도</th><th>조도</th><th>토양수분</th><th>토양전도도</th></tr>"

    tz = pytz.timezone("Asia/Seoul")
    for record in rows[:10]:
        # _time은 ISO string일 가능성이 높음
        t_iso = record.get("_time")
        if isinstance(t_iso, str):
            dt_utc = datetime.fromisoformat(t_iso.replace("Z", "+00:00"))
        else:
            dt_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
        dt_kst = dt_utc.astimezone(tz)

        def fmt(x, digits=2):
            return f"{x:.{digits}f}" if isinstance(x, (int, float)) else "-"

        html += (
            f"<tr>"
            f"<td>{dt_kst.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{fmt(record.get('temperature'))}</td>"
            f"<td>{fmt(record.get('humidity'))}</td>"
            f"<td>{record.get('light_lux') if record.get('light_lux') is not None else '-'}</td>"
            f"<td>{fmt(record.get('soil_moisture'))}</td>"
            f"<td>{fmt(record.get('soil_ec'))}</td>"
            f"</tr>"
        )
    html += "</table><p>전체 데이터는 GreenEye 대시보드를 통해 확인해주세요.</p>"
    return html

def send_email(to_email: str, subject: str, html_content: str) -> bool:
    if not EMAIL_HOST or not EMAIL_USERNAME or not EMAIL_PASSWORD:
        print("Email settings are not fully configured in .env file. Skipping email sending.")
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_USERNAME
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_content, "html"))
    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, to_email, msg.as_string())
        print(f"Monthly report email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"Error sending email to {to_email}: {e}")
        return False

def send_monthly_reports_for_users():
    """
    모든 사용자에게 등록된 장치(mac 기준)의 지난 7일 보고서 발송.
    (운영에서는 월간 크론 스케줄에 의해 호출)
    """
    print(f"\n--- Starting monthly report generation and sending at {datetime.now()} ---")

    # 사용자 목록
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, email FROM users")
    users = cur.fetchall()
    conn.close()

    if not users:
        print("No users found in the database. Skipping monthly reports.")
        return

    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    end_utc = now_utc
    start_utc = end_utc - timedelta(days=7)

    # [변경] 등록된 장치 목록에서 mac 추출
    devices = get_all_devices()
    mac_list = [d["mac_address"] for d in devices] if devices else []
    if not mac_list:
        print("No devices registered. Skipping monthly reports.")
        return

    for user in users:
        email = user["email"]
        print(f"Processing report for user: {email}")
        for mac in mac_list:
            subject = f"GreenEye 월간 식물 보고서 - {mac} ({start_utc.strftime('%Y-%m-%d')} ~ {end_utc.strftime('%Y-%m-%d')})"
            html = generate_monthly_report_content_by_mac(mac, start_utc, end_utc)
            if html:
                send_email(email, subject, html)

    print("--- Monthly report generation and sending finished. ---\n")

if __name__ == "__main__":
    print("Run via app.py scheduler or Flask context.")
