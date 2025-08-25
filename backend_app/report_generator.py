# PDF 보고서 생성
import os
import socket
from datetime import datetime, timedelta, timezone
import pytz
import smtplib
import tempfile
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
import io
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from dotenv import load_dotenv
from .services import connect_influxdb, query_influxdb_data
from .database import get_all_devices, get_all_users
from pathlib import Path

load_dotenv()
connect_influxdb()

BASE_DIR = Path(__file__).resolve().parents[1]   # 프로젝트 루트
font_path = BASE_DIR / "fonts" / "noto.ttf"

# font 등록 및 설정
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

font_prop = None

if os.path.exists(font_path):
    # ✅ matplotlib 설정
    fm.fontManager.addfont(font_path)
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams['font.family'] = font_prop.get_name()
    plt.rcParams['axes.unicode_minus'] = False

    # ✅ reportlab 설정
    pdfmetrics.registerFont(TTFont("NotoSansKR", font_path))

    print(f"[✔] 등록된 matplotlib 폰트 이름: {font_prop.get_name()}")
else:
    print("[ℹ] NotoSansKR 폰트 파일이 없어 기본 폰트로 진행합니다.")

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET") or "sensor_data"


def _fmt_iso_utc(dt):
    return dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

import matplotlib.dates as mdates

def generate_graph_image(rows, field, label):
    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return None

    times, values = [], []
    for r in rows:
        v = _to_float(r.get(field))
        if v is None:
            continue
        t = r.get("_time")
        if not isinstance(t, datetime) and t is not None:
            t = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        times.append(t)
        values.append(v)

    print(f"[GRAPH DEBUG] field={field}, data points={len(values)}")
    if not times or not values:
        return None

    fig, ax = plt.subplots(figsize=(6, 2.5), dpi=100)

    # 선만 (green)
    ax.plot(times, values, color='green', linewidth=1.5, marker='o', markersize=2.5)

    # ✅ x축을 우리가 명확히 지정 + 보기 좋은 날짜 포맷
    tmin, tmax = min(times), max(times)
    if tmin == tmax:
        # 포인트 1개일 때는 주변으로 약간의 여유
        pad = timedelta(minutes=5)
        ax.set_xlim(tmin - pad, tmax + pad)
    else:
        ax.set_xlim(tmin, tmax)

    # 범위에 따라 적당한 눈금 간격 선택
    span = (ax.get_xlim()[1] - ax.get_xlim()[0])  # days 단위 float
    locator: mdates.DateLocator
    if span <= 1/24 * 6:            # <= 6시간
        locator = mdates.MinuteLocator(interval=10)
    elif span <= 1:                  # <= 1일
        locator = mdates.HourLocator(interval=3)
    elif span <= 7:                  # <= 1주
        locator = mdates.DayLocator(interval=1)
    else:
        locator = mdates.AutoDateLocator()

    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    ax.set_xlabel("시간")
    # ax.set_ylabel(label)

    ax.grid(True)
    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_pdf_report_by_device(device_id, start_dt, end_dt, friendly_name, plant_type=None):
    filename = f"greeneye_report_{device_id}_{start_dt.strftime('%Y%m%d')}.pdf"
    temp_dir = tempfile.gettempdir()
    filepath = os.path.join(temp_dir, filename)
    doc = SimpleDocTemplate(filepath, pagesize=A4)
    styles = getSampleStyleSheet()
    story = []

    from reportlab.lib.styles import ParagraphStyle

    base_font = 'NotoSansKR' if os.path.exists(font_path) else 'Helvetica'
    styles.add(ParagraphStyle(name='NotoTitle',    parent=styles['Title'],    fontName=base_font))
    styles.add(ParagraphStyle(name='NotoNormal',   parent=styles['Normal'],   fontName=base_font))
    styles.add(ParagraphStyle(name='NotoHeading4', parent=styles['Heading4'], fontName=base_font))
    
    story.append(Paragraph(f"<b>GreenEye 월간 식물 보고서 - {friendly_name} ({device_id})</b>", styles['NotoTitle']))
    story.append(Paragraph(f"기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}", styles['NotoNormal']))
    if plant_type:
        story.append(Paragraph(f"식물 종류: {plant_type}", styles['NotoNormal']))

    # --- 배터리 상태 문자열 함수: 사용 전에 정의 ---
    def battery_status_string(level):
        if level is None:
            return "데이터 없음"
        elif level >= 75:
            return f"매우 양호 ({level:.2f}%)"
        elif level >= 40:
            return f"양호 ({level:.2f}%)"
        elif level >= 15:
            return f"부족 ({level:.2f}%)"
        else:
            return f"매우 낮음 ({level:.2f}%)"

    start = _fmt_iso_utc(start_dt)
    end = _fmt_iso_utc(end_dt)
    plant_id = device_id
    
    query = f"""
    from(bucket: "{INFLUXDB_BUCKET}")
    |> range(start: {start}, stop: {end})
    |> filter(fn: (r) => r["_measurement"] == "sensor_readings")
    |> filter(fn: (r) => r["device_id"] == "{plant_id}")  // ← 중요: plant_id가 사실은 device_id
    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
    |> keep(columns: ["_time", "device_id", "temperature", "humidity", "light_lux", "soil_moisture", "soil_temp", "soil_ec", "battery"])
    """

    rows = query_influxdb_data(query)
    if rows is None:
        print("[DEBUG] InfluxDB 쿼리 실패 또는 데이터 없음 → rows=None")
        story.append(Paragraph("이 기간 동안의 센서 데이터를 가져올 수 없습니다.", styles['NotoNormal']))
        doc.build(story)
        return filepath

    print(f"[DEBUG] {len(rows)} rows fetched from InfluxDB")
    for i, r in enumerate(rows[:5]):
        print(f"[DEBUG] Row {i}: {r}")


    if not rows:
        story.append(Paragraph("이 기간 동안의 센서 데이터가 없습니다.", styles['NotoNormal']))
        doc.build(story)
        return filepath

    # ── 숫자/시간 타입 정규화 ─────────────────────────────
    num_fields = ["temperature","humidity","light_lux","soil_moisture","soil_temp","soil_ec","battery"]
    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return None
    for r in rows:
        for k in num_fields:
            if r.get(k) is not None:
                r[k] = _to_float(r[k])
        if r.get("_time") is not None and not isinstance(r["_time"], datetime):
            r["_time"] = datetime.fromisoformat(str(r["_time"]).replace("Z", "+00:00"))

    def pick(key):
        return [r.get(key) for r in rows if r.get(key) is not None]

    def avg(values):
        return sum(float(v) for v in values) / len(values) if values else 0

    # 최근값(가장 최신 non-null) 헬퍼
    def last_value(key):
        for r in sorted(rows, key=lambda x: x.get("_time") or datetime.min, reverse=True):
            v = r.get(key)
            if v is not None:
                return v
        return None


    # 5) 최신 배터리 값 계산 (가장 최근 non-null, float)
    latest_battery = None
    if rows:
        for r in sorted(rows, key=lambda x: x.get("_time") or datetime.min):
            if r.get("battery") is not None:
                latest_battery = r["battery"]  # 이미 float

    # 배터리 상태 라인 추가
    story.append(Paragraph(
        f"배터리 상태: {battery_status_string(latest_battery)}",
        styles['NotoNormal']
    ))
    story.append(Spacer(1, 0.4 * cm))

    # 📊 평균 + 최근값 테이블
    story.append(Paragraph("센서 요약 (평균값 & 최근값)", styles['NotoHeading4']))
    table_data = [["항목", "평균값", "최근값"]]

    # 최근값 미리 계산 + 문자열 포맷(빈 값 안전 처리)
    def fmt(v): return "N/A" if v is None else f"{float(v):.2f}"
    last = {
        "temperature":   last_value("temperature"),
        "humidity":      last_value("humidity"),
        "light_lux":     last_value("light_lux"),
        "soil_temp":     last_value("soil_temp"),
        "soil_moisture": last_value("soil_moisture"),
        "soil_ec":       last_value("soil_ec"),
    }
    table_data += [
        ["주변 온도 (°C)",     f"{avg(pick('temperature')):.2f}",   fmt(last["temperature"])],
        ["주변 습도 (%)",       f"{avg(pick('humidity')):.2f}",      fmt(last["humidity"])],
        ["주변 조도 (lux)",     f"{avg(pick('light_lux')):.2f}",     fmt(last["light_lux"])],
        ["토양 온도 (°C)",      f"{avg(pick('soil_temp')):.2f}",     fmt(last["soil_temp"])],
        ["토양 수분 (%)",       f"{avg(pick('soil_moisture')):.2f}", fmt(last["soil_moisture"])],
        ["토양 전도도 (uS/cm)", f"{avg(pick('soil_ec')):.2f}",       fmt(last["soil_ec"])],
    ]
    avg_table = Table(table_data, colWidths=[6*cm, 4*cm, 4*cm])
    avg_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, -1), base_font),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    story.append(avg_table)
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
    msg["From"] = formataddr(("GreenEye", EMAIL_USERNAME))
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")  # 한글 제목 안전
    msg.attach(MIMEText(body_text, "plain", _charset="utf-8"))  # 본문 인코딩 명시

    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        # 첨부파일 이름 인코딩(한글 파일명 대비)
        part.add_header('Content-Disposition', 'attachment', filename=(Header(os.path.basename(pdf_path), 'utf-8').encode()))
        msg.attach(part)

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)  # 헤더/인코딩 자동 처리
        print(f"✅ PDF 보고서 전송 성공: {to_email}")
        return True
    except Exception as e:
        print(f"❌ 이메일 전송 오류: {e}")
        return False

def send_all_reports():
    print(f"\n--- PDF 보고서 전송 시작: {datetime.now()} ---")
    users = get_all_users()
    devices = get_all_devices()
    now = datetime.now().astimezone(pytz.utc)  # ✅ 로컬시간 -> UTC로 변환
    start = now - timedelta(days=1)

    for user in users:
        email = user["email"]
        for device in devices:
            pdf = generate_pdf_report_by_device(
                device["device_id"],
                start,
                now,
                device["friendly_name"],
                device["plant_type"]
            )
            subject = f"GreenEye 월간 식물 보고서 - {device['friendly_name']}"
            body = "안녕하세요, GreenEye 시스템에서 자동 생성된 식물 생장 보고서를 첨부드립니다."
            send_email_with_pdf(email, subject, body, pdf)
    print(f"--- PDF 보고서 전송 완료 ---\n")

if __name__ == "__main__":
    send_all_reports()
