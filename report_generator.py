# PDF 보고서 생성
import os
from datetime import datetime, timedelta
import pytz
import smtplib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
import io
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from dotenv import load_dotenv
from services import connect_influxdb, query_influxdb_data
from database import get_all_devices, get_all_users

connect_influxdb()
load_dotenv()

font_path = os.path.join(os.path.dirname(__file__), "fonts", "noto.ttf")

# font 등록 및 설정
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

if os.path.exists(font_path):
    # ✅ matplotlib 설정
    fm.fontManager.addfont(font_path)
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams['font.family'] = font_prop.get_name()
    plt.rcParams['axes.unicode_minus'] = False

    # ✅ reportlab 설정
    pdfmetrics.registerFont(TTFont("NotoSansKR", font_path))

    print(f"[✔] 등록된 matplotlib 폰트 이름: {font_prop.get_name()}")

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET")


def _fmt_iso_utc(dt):
    return dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def generate_graph_image(rows, field, label):
    times = [
        r["_time"] if isinstance(r["_time"], datetime)
        else datetime.fromisoformat(r["_time"].replace("Z", "+00:00"))
        for r in rows if r.get(field) is not None
    ]
    values = [r.get(field) for r in rows if r.get(field) is not None]

    if not times or not values:
        return None

    fig, ax = plt.subplots(figsize=(6, 2.5), dpi=100)
    ax.set_title(label, fontproperties=font_prop)
    ax.plot(times, values, marker='o', linestyle='-', color='blue')
    ax.set_xlabel("시간", fontproperties=font_prop)
    ax.set_ylabel(label, fontproperties=font_prop)
    ax.grid(True)
    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close(fig)
    buf.seek(0)
    return buf

def generate_pdf_report_by_device(device_id, start_dt, end_dt, friendly_name):
    filename = f"greeneye_report_{device_id}_{start_dt.strftime('%Y%m%d')}.pdf"
    filepath = os.path.join("/tmp", filename)
    doc = SimpleDocTemplate(filepath, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    from reportlab.lib.styles import ParagraphStyle

    styles.add(ParagraphStyle(name='NotoTitle', parent=styles['Title'], fontName='NotoSansKR'))
    styles.add(ParagraphStyle(name='NotoNormal', parent=styles['Normal'], fontName='NotoSansKR'))
    styles.add(ParagraphStyle(name='NotoHeading4', parent=styles['Heading4'], fontName='NotoSansKR'))
    
    story.append(Paragraph(f"<b>GreenEye 월간 식물 보고서 - {friendly_name} ({device_id})</b>", styles['NotoTitle']))
    story.append(Paragraph(f"기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}", styles['NotoNormal']))
    story.append(Spacer(1, 0.4 * cm))

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {_fmt_iso_utc(start_dt)}, stop: {_fmt_iso_utc(end_dt)})
      |> filter(fn: (r) => r._measurement == "sensor_readings")
      |> filter(fn: (r) => r.device_id == "{device_id}")
      |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
      |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
      |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","soil_temp","soil_moisture","soil_ec","battery"])
    '''
    rows = query_influxdb_data(query)

    if not rows:
        story.append(Paragraph("이 기간 동안의 센서 데이터가 없습니다.", styles['NotoNormal']))
        doc.build(story)
        return filepath

    def pick(key):
        return [r.get(key) for r in rows if r.get(key) is not None]

    def avg(values):
        return sum(values) / len(values) if values else 0

    latest_battery = None
    for r in reversed(rows):
        if r.get("battery") is not None:
            latest_battery = r["battery"]
            break

    # 📊 통계 테이블
    stats = [
        ["주변 평균 온도 (°C)", f"{avg(pick('temperature')):.2f}"],
        ["주변 평균 습도 (%)", f"{avg(pick('humidity')):.2f}"],
        ["주변 평균 조도 (lux)", f"{avg(pick('light_lux')):.2f}"],
        ["토양 평균 온도 (°C)", f"{avg(pick('soil_temp')):.2f}"],
        ["토양 평균 수분 (%)", f"{avg(pick('soil_moisture')):.2f}"],
        ["토양 평균 전도도 (uS/cm)", f"{avg(pick('soil_ec')):.2f}"],
        ["현재 배터리 잔량 (%)", f"{latest_battery:.2f}" if latest_battery is not None else "데이터 없음"],
    ]
    
    # ✅ 열 제목 행 추가
    table_data = [["항목", "평균값"]] + stats
    table = Table(table_data, colWidths=[6*cm, 4*cm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),             # 제목 행 배경색
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),       # 제목 행 글자색
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), 'NotoSansKR'),            # 전체에 폰트 적용
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),                  # 제목 행 아래 여백
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    story.append(table)
    story.append(Spacer(1, 0.5 * cm))

    for field, label in [
        ("temperature", "주변 온도 (°C)"),
        ("humidity", "주변 습도 (%)"),
        ("light_lux", "조도 (lux)"),
        ("soil_temp", "토양 온도 (°C)"),
        ("soil_moisture", "토양 수분 (%)"),
        ("soil_ec", "토양 전도도 (uS/cm)"),
    ]:
        img_buf = generate_graph_image(rows, field, label)
        if img_buf:
            story.append(Paragraph(label, styles['NotoHeading4']))
            img = Image(img_buf, width=15*cm, height=5*cm)
            story.append(img)
            story.append(Spacer(1, 0.5 * cm))

    doc.build(story)
    return filepath

def send_email_with_pdf(to_email, subject, body_text, pdf_path):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_USERNAME
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))

    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(pdf_path))
        msg.attach(part)

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.sendmail(EMAIL_USERNAME, to_email, msg.as_string())
        print(f"✅ PDF 보고서 전송 성공: {to_email}")
        return True
    except Exception as e:
        print(f"❌ 이메일 전송 오류: {e}")
        return False

def send_all_reports():
    print(f"\n--- PDF 보고서 전송 시작: {datetime.now()} ---")
    users = get_all_users()
    devices = get_all_devices()
    now = datetime.utcnow().replace(tzinfo=pytz.utc)
    start = now - timedelta(days=7)

    for user in users:
        email = user["email"]
        for device in devices:
            pdf = generate_pdf_report_by_device(device["device_id"], start, now, device["friendly_name"])
            subject = f"GreenEye 월간 식물 보고서 - {device['friendly_name']}"
            body = "안녕하세요, GreenEye 시스템에서 자동 생성된 식물 생장 보고서를 첨부드립니다."
            send_email_with_pdf(email, subject, body, pdf)
    print(f"--- PDF 보고서 전송 완료 ---\n")

if __name__ == "__main__":
    send_all_reports()
