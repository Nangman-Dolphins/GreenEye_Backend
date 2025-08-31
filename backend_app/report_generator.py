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
from .services import connect_influxdb, query_influxdb_data, get_influx_client
from .database import get_db_connection, get_all_devices_any, get_all_users, get_device_by_device_id_any
from pathlib import Path
import pandas as pd
from typing import List, Tuple, Optional

load_dotenv()
connect_influxdb()

BASE_DIR = Path(__file__).resolve().parents[1]   # 프로젝트 루트
font_path = BASE_DIR / "fonts" / "noto.ttf"
STANDARDS_PATH = BASE_DIR / "reference_data" / "plant_standards_cleaned.xlsx"

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

# ─────────────────────────────────────────────────────────────────────────────
# ✅ 정상 범위 로딩 / 조회
#   - standards 파일은 열(Column) 이름 예: plant(식물명), temperature_min/max, humidity_min/max, ...
#   - 프로젝트 내 실제 컬럼명에 맞춰 key 매핑을 조정하세요.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_range(s):
    """'10 ~ 20' 같은 문자열을 (10.0, 20.0)로 변환"""
    if s is None:
        return (None, None)
    try:
        txt = str(s).replace('~', ' ~ ').replace('−', '-')
        parts = [p.strip() for p in txt.split('~')]
        if len(parts) == 2:
            lo = float(parts[0].replace(',', '').split()[0])
            hi = float(parts[1].replace(',', '').split()[0])
            return (lo, hi)
    except:
        pass
    return (None, None)

def load_standards() -> Optional[pd.DataFrame]:
    """
    엑셀의 한글 컬럼/문자열 범위를 내부 표준 컬럼으로 정규화:
      식물명 → plant_name
      환경온도(°C) → temperature_min/max
      환경습도(%) → humidity_min/max
      환경광도(lux) 또는 ' 환경광도(lux)' → light_lux_min/max
      토양온도(°C) → soil_temp_min/max
      토양수분(%) → soil_moisture_min/max
      토양전도도(uS/cm) → soil_ec_min/max
    """
    try:
        df = pd.read_excel(STANDARDS_PATH)
    except Exception as e:
        print(f"[WARN] could not load standards: {e}")
        return None

    # 1) 컬럼명 공백 제거
    df.columns = [str(c).strip() for c in df.columns]

    # 2) 한글 컬럼명 매핑 사전
    COLS = {
        "plant_name": "식물명",
        "temperature": "환경온도(°C)",
        "humidity": "환경습도(%)",
        "light_lux": "환경광도(lux)",
        "soil_temp": "토양온도(°C)",
        "soil_moisture": "토양수분(%)",
        "soil_ec": "토양전도도(uS/cm)",
    }
    # 환경광도 컬럼이 앞 공백으로 들어온 경우도 보정
    if "환경광도(lux)" not in df.columns and "환경광도(lux)".strip() not in df.columns:
        for c in df.columns:
            if c.replace(" ", "") == "환경광도(lux)":
                COLS["light_lux"] = c
                break

    # 3) plant_name 표준화
    if COLS["plant_name"] not in df.columns:
        print("[WARN] standards: 식물명 컬럼을 찾지 못했습니다.")
        return None
    df["plant_name_norm"] = (
        df[COLS["plant_name"]]
        .astype(str).str.strip().str.lower()
    )

    # 4) 각 항목을 min/max 숫자 컬럼으로 분해
    def split_to_minmax(colname, out_prefix):
        if colname not in df.columns:
            return
        mins, maxs = zip(*df[colname].map(_parse_range))
        df[f"{out_prefix}_min"] = mins
        df[f"{out_prefix}_max"] = maxs

    split_to_minmax(COLS["temperature"],   "temperature")
    split_to_minmax(COLS["humidity"],      "humidity")
    split_to_minmax(COLS["light_lux"],     "light_lux")
    split_to_minmax(COLS["soil_temp"],     "soil_temp")
    split_to_minmax(COLS["soil_moisture"], "soil_moisture")
    split_to_minmax(COLS["soil_ec"],       "soil_ec")

    return df

def get_range(standards: Optional[pd.DataFrame], plant_type: Optional[str], field: str) -> Tuple[Optional[float], Optional[float]]:
    """
    field: 'temperature' | 'humidity' | 'light_lux' | 'soil_temp' | 'soil_moisture' | 'soil_ec'
    standards 내 컬럼명 규칙 예: temperature_min, temperature_max ...
    """
    if standards is None or not plant_type:
        return None, None
    df = standards
    # 동의어/부분일치 허용 (예: 'rhododendron' ↔ '진달래/철쭉' 표기)
    aliases = {
        "rhododendron": ["rhododendron", "진달래", "철쭉", "azalea"],
        "monstera": ["monstera", "몬스테라"],
        "sansevieria": ["sansevieria", "산세베리아", "스투키", "dracaena trifasciata"],
        "ficus elastica": ["ficus elastica", "고무나무"],
    }
    key = str(plant_type).strip().lower()
    keys = aliases.get(key, [key])
    mask = df["plant_name_norm"].apply(lambda s: any(k in s for k in keys))
    row = df.loc[mask]
    if row.empty:
        # 완전일치 시도 (마지막 보루)
        row = df.loc[df["plant_name_norm"] == key]
        if row.empty:
            return None, None
    lo_col = f"{field}_min"
    hi_col = f"{field}_max"
    lo = row.iloc[0].get(lo_col, None)
    hi = row.iloc[0].get(hi_col, None)
    try:
        lo = float(lo) if lo is not None else None
    except: lo = None
    try:
        hi = float(hi) if hi is not None else None
    except: hi = None
    return lo, hi

def _to_float(v):
    try: return float(v)
    except: return None

def find_out_of_range_intervals(times: List[datetime], values: List[float], lo: Optional[float], hi: Optional[float]) -> List[Tuple[datetime, datetime, str]]:
    """
    연속 구간 단위로 정상 범위를 벗어난 시간대를 찾아 (start, end, 'high'|'low') 리스트로 반환
    """
    if not times or not values or (lo is None and hi is None):
        return []
    out = []
    cur_state = None  # 'high' | 'low' | None
    cur_start = None
    for t, v in zip(times, values):
        cond_high = (hi is not None and v > hi)
        cond_low  = (lo is not None and v < lo)
        state = 'high' if cond_high else ('low' if cond_low else None)
        if state != cur_state:
            # 이전 구간 종료
            if cur_state is not None and cur_start is not None:
                out.append((cur_start, t, cur_state))
            # 새 구간 시작
            cur_state = state
            cur_start = t if state is not None else None
    # 마지막 구간 열려 있으면 닫기
    if cur_state is not None and cur_start is not None:
        out.append((cur_start, times[-1], cur_state))
    # 정상(범위 내)만 있었다면 빈 리스트일 수 있음
    return out

def _resolve_room(device_id: str, room: str | None) -> str | None:
    if room:  # 호출 시 이미 넘겨준 경우
        return room
    try:
        c = get_db_connection()
        row = c.execute("SELECT room FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return row["room"] if row and row["room"] else None
    except Exception:
        return None

def generate_pdf_report_by_device(device_id, start_dt, end_dt, friendly_name, plant_type=None, room=None):
    room = _resolve_room(device_id, room)
    filename = f"greeneye_report_{device_id}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pdf"
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

    # 제목/메타
    story.append(Paragraph(f"<b>GreenEye 주간 식물 보고서 - {friendly_name} ({device_id})</b>", styles['NotoTitle']))
    story.append(Paragraph(f"기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}", styles['NotoNormal']))
    if plant_type:
        story.append(Paragraph(f"식물 종류: {plant_type}", styles['NotoNormal']))
    story.append(Paragraph(f"위치: {room or '(미설정)'}", styles['NotoNormal']))
    story.append(Spacer(1, 0.3*cm))

    # 배터리 상태 포맷터
    def battery_status_string(level):
        if level is None: return "데이터 없음"
        if level >= 65:  return f"양호 ({level:.2f}%)"
        if level >= 40:  return f"낮음 ({level:.2f}%)"
        if level >= 15:  return f"매우 낮음 ({level:.2f}%)"
        return f"위험 ({level:.2f}%)"

    # Influx 쿼리
    start = _fmt_iso_utc(start_dt)
    end   = _fmt_iso_utc(end_dt)
    query = f"""
    from(bucket: "{INFLUXDB_BUCKET}")
    |> range(start: {start}, stop: {end})
    |> filter(fn: (r) => r["_measurement"] == "sensor_readings")
    |> filter(fn: (r) => r["device_id"] == "{device_id}")
    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
    |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","soil_moisture","soil_temp","soil_ec","battery"])
    """
    rows = query_influxdb_data(query)  # 성공적으로 데이터 가져옴/없음 처리 포함  :contentReference[oaicite:9]{index=9}
    if not rows:
        story.append(Paragraph("이 기간 동안의 센서 데이터를 가져올 수 없거나, 데이터가 없습니다.", styles['NotoNormal']))
        doc.build(story)
        return filepath

    # 숫자/시간 정규화
    def _to_float(v):
        try: return float(v)
        except: return None
    for r in rows:
        for k in ["temperature","humidity","light_lux","soil_moisture","soil_temp","soil_ec","battery"]:
            if r.get(k) is not None:
                r[k] = _to_float(r[k])
        if r.get("_time") is not None and not isinstance(r["_time"], datetime):
            r["_time"] = datetime.fromisoformat(str(r["_time"]).replace("Z","+00:00"))

    def pick(key): return [r.get(key) for r in rows if r.get(key) is not None]
    def avg(values): return sum(v for v in values if v is not None)/len(values) if values else 0

    # 최신 배터리/최근값
    def last_value(key):
        for r in sorted(rows, key=lambda x: x.get("_time") or datetime.min, reverse=True):
            v = r.get(key)
            if v is not None: return v
        return None
    latest_battery = last_value("battery")

    # 배터리 상태
    story.append(Paragraph(f"배터리 상태: {battery_status_string(latest_battery)}", styles['NotoNormal']))
    story.append(Spacer(1, 0.4*cm))

    # 평균/최근값 표
    story.append(Paragraph("센서 요약 (평균값 & 최근값)", styles['NotoHeading4']))
    def fmt(v): return "N/A" if v is None else f"{float(v):.2f}"
    last = {
        "temperature":   last_value("temperature"),
        "humidity":      last_value("humidity"),
        "light_lux":     last_value("light_lux"),
        "soil_temp":     last_value("soil_temp"),
        "soil_moisture": last_value("soil_moisture"),
        "soil_ec":       last_value("soil_ec"),
    }
    table_data = [
        ["항목","평균값","최근값"],
        ["주변 온도 (°C)",     f"{avg(pick('temperature')):.2f}",   fmt(last["temperature"])],
        ["주변 습도 (%)",       f"{avg(pick('humidity')):.2f}",      fmt(last["humidity"])],
        ["주변 조도 (lux)",     f"{avg(pick('light_lux')):.2f}",     fmt(last["light_lux"])],
        ["토양 온도 (°C)",      f"{avg(pick('soil_temp')):.2f}",     fmt(last["soil_temp"])],
        ["토양 수분 (%)",       f"{avg(pick('soil_moisture')):.2f}", fmt(last["soil_moisture"])],
        ["토양 전도도 (uS/cm)", f"{avg(pick('soil_ec')):.2f}",       fmt(last["soil_ec"])],
    ]
    avg_table = Table(table_data, colWidths=[6*cm,4*cm,4*cm])
    avg_table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.grey),
        ('TEXTCOLOR',(0,0),(-1,0),colors.whitesmoke),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),
        ('FONTNAME',(0,0),(-1,-1),base_font),
        ('FONTSIZE',(0,0),(-1,-1),10),
        ('BOTTOMPADDING',(0,0),(-1,0),12),
        ('GRID',(0,0),(-1,-1),1,colors.black),
    ]))
    story.append(avg_table)
    story.append(Spacer(1, 0.5*cm))

    # 표준범위 로딩
    standards_df = load_standards()  # 엑셀 -> min/max 컬럼으로 정규화  :contentReference[oaicite:10]{index=10}

    # 항목별 그래프 + 범위 비교 문장
    field_labels = [
        ("temperature","주변 온도 (°C)"),
        ("humidity","주변 습도 (%)"),
        ("light_lux","조도 (lux)"),
        ("soil_temp","토양 온도 (°C)"),
        ("soil_moisture","토양 수분 (%)"),
        ("soil_ec","토양 전도도 (uS/cm)"),
    ]
    for field, label in field_labels:
        img_buf = generate_graph_image(rows, field, label)
        if not img_buf:
            continue
        story.append(Paragraph(label, styles['NotoHeading4']))
        story.append(Image(img_buf, width=15*cm, height=5*cm))

        # 범위 비교 설명
        t_list, v_list = [], []
        for r in rows:
            v = r.get(field)
            if v is None: 
                continue
            t = r.get("_time")
            t = t if isinstance(t, datetime) else datetime.fromisoformat(str(t).replace("Z","+00:00"))
            t_list.append(t); v_list.append(_to_float(v))

        lo, hi = get_range(standards_df, plant_type, field)  # 식물종에 맞춘 범위 찾기  :contentReference[oaicite:11]{index=11}
        intervals = find_out_of_range_intervals(t_list, v_list, lo, hi)

        if lo is None and hi is None:
            story.append(Paragraph("※ 이 항목의 정상 범위를 찾을 수 없어 비교를 생략했습니다.", styles['NotoNormal']))
        elif not intervals:
            rng = ""
            if lo is not None or hi is not None:
                lo_s = f"{lo:.0f}" if lo is not None else "-"
                hi_s = f"{hi:.0f}" if hi is not None else "-"
                rng = f" (정상범위 {lo_s}–{hi_s})"
            story.append(Paragraph(f"이번 주 이 항목은 대부분 정상 범위였습니다{rng}.", styles['NotoNormal']))
        else:
            # 구간 요약
            segs = []
            for s, e, kind in intervals:
                kind_ko = "상한 초과" if kind=="high" else "하한 미만"
                segs.append(f"{s.strftime('%m/%d %H:%M')}–{e.strftime('%m/%d %H:%M')} ({kind_ko})")
            lo_s = f"{lo:.0f}" if lo is not None else "-"
            hi_s = f"{hi:.0f}" if hi is not None else "-"
            story.append(Paragraph(
                f"정상범위 {lo_s}–{hi_s} 기준으로 다음 구간에서 벗어났습니다: " + ", ".join(segs),
                styles['NotoNormal']
            ))

        story.append(Spacer(1, 0.4*cm))

    # PDF 저장
    doc.build(story)
    return filepath

def send_email_with_pdf(to_email, subject, body_text, pdf_path):
    msg = MIMEMultipart()
    if not EMAIL_USERNAME:
        print("[WARN] EMAIL_USERNAME not set. Skipping email send; PDF only.")
        return False
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
    devices = get_all_devices_any()
    now = datetime.now().astimezone(pytz.utc)  # 로컬시간 -> UTC로 변환
    # 주간 리포트
    start = now - timedelta(days=7)

    for user in users:
        email = user["email"]
        for device in devices:
            pdf = generate_pdf_report_by_device(
                device["device_id"],
                start,
                now,
                device.get("friendly_name"),
                device.get("plant_type"),   # devices.plant_type 컬럼
                device.get("room")          # ★ room 전달
            )
            subject = f"GreenEye 주간 식물 보고서 - {device['friendly_name']}"
            body = "안녕하세요, GreenEye 시스템에서 자동 생성된 식물 생장 보고서를 첨부드립니다."
            send_email_with_pdf(email, subject, body, pdf)
    print(f"--- PDF 보고서 전송 완료 ---\n")

if __name__ == "__main__":
    send_all_reports()
    # --- InfluxDB 클라이언트 정리 ---
    try:
        cli = get_influx_client()
        if cli:
            cli.close()
            print("[INFO] InfluxDB client closed cleanly.")
    except Exception as e:
        print(f"[WARN] Influx client close failed: {e}")