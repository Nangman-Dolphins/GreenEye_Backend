import os
from datetime import datetime, timedelta
import pytz
import smtplib
import matplotlib.pyplot as plt
import io
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from services import query_influxdb_data
from database import get_db_connection, get_all_devices, get_device_by_device_id, get_all_users

from dotenv import load_dotenv
load_dotenv()

def generate_line_chart(rows, field, ylabel):
    times = [datetime.fromisoformat(r["_time"].replace("Z", "+00:00")) for r in rows if r.get(field) is not None]
    values = [r.get(field) for r in rows if r.get(field) is not None]

    if not times or not values:
        return None

    fig, ax = plt.subplots(figsize=(6, 2.5))
    ax.plot(times, values, marker='o', linestyle='-', color='blue')
    ax.set_title(ylabel)
    ax.set_xlabel("시간")
    ax.set_ylabel(ylabel)
    ax.grid(True)
    fig.tight_layout()

    buffer = io.BytesIO()
    plt.savefig(buffer, format='png')
    plt.close(fig)
    buffer.seek(0)
    encoded = base64.b64encode(buffer.read()).decode('utf-8')
    return f'<img src="data:image/png;base64,{encoded}" alt="{ylabel}" style="margin-bottom:10px;"/><br/>'

INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")
EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

def _fmt_iso_utc(dt):
    return dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def generate_monthly_report_content_by_device(device_id: str, start_dt, end_dt, friendly_name):
    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {_fmt_iso_utc(start_dt)}, stop: {_fmt_iso_utc(end_dt)})
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time","device_id","amb_temp","amb_humi","amb_light","soil_humi","soil_ec","soil_temp","bat_level"])
    '''
    rows = query_influxdb_data(query)

    html = f"<h2>GreenEye 월간 식물 보고서 - {friendly_name} ({device_id})</h2>"
    html += f"<p>기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}</p>"
    if not rows:
        html += "<p>이 기간 동안의 센서 데이터가 없습니다.</p>"
        return html

    def pick(key):
        return [r.get(key) for r in rows if r.get(key) is not None]

    temps = pick("amb_temp")
    hums = pick("amb_humi")
    lights = pick("amb_light")
    moist = pick("soil_humi")
    ecs = pick("soil_ec")
    soil_temps = pick("soil_temp")
    bat_levels = pick("bat_level")

    avg_temp = sum(temps)/len(temps) if temps else 0
    max_temp = max(temps) if temps else 0
    min_temp = min(temps) if temps else 0
    avg_hum = sum(hums)/len(hums) if hums else 0
    avg_light = sum(lights)/len(lights) if lights else 0
    avg_moist = sum(moist)/len(moist) if moist else 0
    avg_ec = sum(ecs)/len(ecs) if ecs else 0
    avg_soil_temp = sum(soil_temps)/len(soil_temps) if soil_temps else 0
    avg_bat_level = sum(bat_levels)/len(bat_levels) if bat_levels else 0


    html += "<h3>주요 통계</h3><ul>"
    html += f"<li>주변 평균 온도: {avg_temp:.2f} °C (최고: {max_temp:.2f} °C, 최저: {min_temp:.2f} °C)</li>"
    html += f"<li>주변 평균 습도: {avg_hum:.2f} %</li>"
    html += f"<li>주변 평균 조도: {avg_light:.2f} lux</li>"
    html += f"<li>토양 평균 온도: {avg_soil_temp:.2f} °C</li>"
    html += f"<li>토양 평균 수분: {avg_moist:.2f} %</li>"
    html += f"<li>토양 평균 전도도: {avg_ec:.2f} uS/cm</li>"
    html += f"<li>평균 배터리 잔량: {avg_bat_level:.2f} %</li>"
    html += "</ul>"

    html += "<h3>시간별 데이터 요약 (최근 10개)</h3>"
    html += "<h3>환경 센서 시계열</h3>"
    for field, label in [
        ("amb_temp", "주변 온도 (°C)"),
        ("amb_humi", "주변 습도 (%)"),
        ("amb_light", "조도 (lux)")
    ]:
        chart = generate_line_chart(rows, field, label)
        if chart:
            html += chart

    html += "<h3>토양 센서 시계열</h3>"
    for field, label in [
        ("soil_temp", "토양 온도 (°C)"),
        ("soil_humi", "토양 수분 (%)"),
        ("soil_ec", "토양 전도도 (uS/cm)")
    ]:
        chart = generate_line_chart(rows, field, label)
        if chart:
            html += chart
    html += "<table border='1' style='width:100%; border-collapse: collapse;'>"
    html += "<tr><th>시간</th><th>주변온도</th><th>주변습도</th><th>주변광도</th><th>토양온도</th><th>토양수분</th><th>토양전도도</th><th>배터리</th></tr>"

    tz = pytz.timezone("Asia/Seoul")
    for record in rows[:10]:
        t_iso = record.get("_time")
        dt_utc = datetime.fromisoformat(t_iso.replace("Z", "+00:00")) if isinstance(t_iso, str) else datetime.utcnow().replace(tzinfo=pytz.utc)
        dt_kst = dt_utc.astimezone(tz)

        def fmt(x, digits=2): return f"{x:.{digits}f}" if isinstance(x, (int, float)) else "-"

        html += (
            f"<tr>"
            f"<td>{dt_kst.strftime('%Y-%m-%d %H:%M')}</td>"
            f"<td>{fmt(record.get('amb_temp'))}</td>"
            f"<td>{fmt(record.get('amb_humi'))}</td>"
            f"<td>{record.get('amb_light') if record.get('amb_light') is not None else '-'}</td>"
            f"<td>{fmt(record.get('soil_temp'))}</td>"
            f"<td>{fmt(record.get('soil_humi'))}</td>"
            f"<td>{fmt(record.get('soil_ec'))}</td>"
            f"<td>{record.get('bat_level') if record.get('bat_level') is not None else '-'}</td>"
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
    print(f"\n--- Starting monthly report generation and sending at {datetime.now()} ---")

    users = get_all_users()

    if not users:
        print("No users found in the database. Skipping monthly reports.")
        return

    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    end_utc = now_utc
    start_utc = end_utc - timedelta(days=7)

    devices = get_all_devices()
    if not devices:
        print("No devices registered. Skipping monthly reports.")
        return

    for user in users:
        email = user["email"]
        print(f"Processing report for user: {email}")
        for device in devices:
            device_id = device["device_id"]
            friendly_name = device["friendly_name"]
            
            subject = f"GreenEye 월간 식물 보고서 - {friendly_name} ({device_id})"
            html = generate_monthly_report_content_by_device(device_id, start_utc, end_utc, friendly_name)
            if html:
                send_email(email, subject, html)
            else:
                print(f"No report content generated for {device_id} for user {email}.")

    print("--- Monthly report generation and sending finished. ---\n")

if __name__ == "__main__":
    print("Running test email report manually...")
    send_monthly_reports_for_users()
