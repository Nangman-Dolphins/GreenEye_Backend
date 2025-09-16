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
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Image, Spacer
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
import io
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from dotenv import load_dotenv
from .services import connect_influxdb, query_influxdb_data, get_influx_client
from .database import get_db_connection, get_all_devices_any, get_all_users, get_device_by_device_id_any, get_all_devices
from pathlib import Path
import pandas as pd
from typing import List, Tuple, Optional
import math

load_dotenv()
connect_influxdb()

BASE_DIR = Path(__file__).resolve().parents[1]   # 프로젝트 루트
font_path = BASE_DIR / "fonts" / "noto.ttf"
STANDARDS_PATH = BASE_DIR / "reference_data" / "plant_standards_cleaned.xlsx"
LOGO_PATH = os.getenv("GE_LOGO_PATH") or str(BASE_DIR / "assets" / "GreenEye_logo.png")

# font 등록 및 설정
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

SYSTEM_KR_FONT = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"
KR_FONT_NAME   = "KR"

def _setup_fonts_unified():
    """
    1) 내부 noto.ttf 있으면 최우선 사용
    2) 없으면 시스템 NanumGothic 사용
    3) 실패 시 Helvetica (경고만)
    """
    candidate = None
    if os.path.exists(font_path):
        candidate = str(font_path)
    elif os.path.exists(SYSTEM_KR_FONT):
        candidate = SYSTEM_KR_FONT

    base_font = "Helvetica"
    try:
        if candidate:
            # Matplotlib
            fm.fontManager.addfont(candidate)
            fp = fm.FontProperties(fname=candidate)
            plt.rcParams["font.family"]     = fp.get_name()
            plt.rcParams["axes.unicode_minus"] = False
            plt.rcParams["pdf.fonttype"]    = 42
            plt.rcParams["ps.fonttype"]     = 42
            # ReportLab
            pdfmetrics.registerFont(TTFont(KR_FONT_NAME, candidate))
            base_font = KR_FONT_NAME
            print(f"[✔] PDF/Matplotlib font set to: {fp.get_name()} ({candidate})")
        else:
            print("[ℹ] No KR font found. Falling back to Helvetica (may show ????).")
    except Exception as e:
        print(f"[WARN] font setup failed: {e} (fallback Helvetica)")
    return base_font

# 전역 기본 폰트명
BASE_FONT = _setup_fonts_unified()

font_prop = None

#테스트로 window 5분
REPORT_AGG_WINDOW = os.getenv("REPORT_AGG_WINDOW", "1h")

EMAIL_HOST = os.getenv("EMAIL_HOST")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET") or "sensor_data"

def _looks_mojibake(s: Optional[str]) -> bool:
    """
    None/공백이거나, 물음표가 포함된 경우(특히 '??' 이상)를 오염으로 간주.
    괄호 안 영문(예: '(Monstera)') 때문에 정상으로 오판하지 않도록 괄호 내용은 무시.
    """
    if not s:
        return True
    t = str(s).strip()
    if not t:
        return True
    # 빠른 판정: '??'가 들어가면 오염으로 본다
    if "??" in t:
        return True
    # 괄호 안 내용 제거 후 다시 검사
    import re
    core = re.sub(r"\([^)]*\)", "", t)
    q = core.count('?')
    alnum = sum(ch.isalnum() for ch in core)
    return q >= 2 and alnum == 0

def _display_text(s: Optional[str]) -> str:
    """표시용 텍스트: 절대 ASCII 강제 변환 금지(원문 유지)."""
    return "" if s is None else str(s)

def _ascii_slug(s: Optional[str]) -> str:
    """파일명 안전화 전용(표시문구와 절대 섞지 않음)."""
    import re, unicodedata
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")
    return s or "report"

def _fmt_iso_utc(dt):
    return dt.astimezone(pytz.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def generate_graph_image(rows, field, label, lo=None, hi=None):
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

    # ① 정상범위 음영
    if lo is not None or hi is not None:
        lo_line = lo if lo is not None else min(values)
        hi_line = hi if hi is not None else max(values)
        ax.fill_between(times, lo_line, hi_line, alpha=0.12, step="pre", zorder=0)

    # ② 메인 라인
    ax.plot(times, values, linewidth=1.6, marker='o', markersize=2.5, zorder=2)

    # ③ 이탈 포인트 마커
    if lo is not None or hi is not None:
        xs = mdates.date2num(times)
        import numpy as np
        vals = np.array(values, dtype=float)
        mask_low  = (lo is not None) & (vals <  lo)
        mask_high = (hi is not None) & (vals >  hi)
        if mask_low.any():
            ax.scatter(xs[mask_low], vals[mask_low], s=14, marker='o', zorder=3)
        if mask_high.any():
            ax.scatter(xs[mask_high], vals[mask_high], s=14, marker='^', zorder=3)

    # X축 눈금/포맷
    tmin, tmax = min(times), max(times)
    if tmin == tmax:
        pad = timedelta(minutes=5)
        ax.set_xlim(tmin - pad, tmax + pad)
    else:
        ax.set_xlim(tmin, tmax)
    span = (ax.get_xlim()[1] - ax.get_xlim()[0])  # days float
    if span <= 1/24 * 6:
        locator = mdates.MinuteLocator(interval=10)
    elif span <= 1:
        locator = mdates.HourLocator(interval=3)
    elif span <= 7:
        locator = mdates.DayLocator(interval=1)
    else:
        locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    ax.set_xlabel("시간")
    ax.grid(True)
    ax.legend(
        [lbl for lbl in ["측정값", "정상범위", "이탈 포인트"] if True],
        loc="upper right", fontsize=8
    )
    fig.tight_layout(pad=0.2)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=180, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    buf.seek(0)
    return buf

# ─────────────────────────────────────────────────────────────────────────────
# 정상 범위 로딩 / 조회
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
        # [ADD] openpyxl 엔진 우선 시도 (컨테이너에 openpyxl 설치 시 안정)
        df = pd.read_excel(STANDARDS_PATH, engine="openpyxl")
    except Exception as e:
        print(f"[WARN] could not load standards: {e}")
        # [ADD] 엔진 미설치 등으로 실패하면 기본 엔진 한 번 더 시도
        try:
            df = pd.read_excel(STANDARDS_PATH)
        except Exception as e2:
            print(f"[WARN] fallback read_excel also failed: {e2} (path={STANDARDS_PATH})")
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

# 품종명 정규화 + 영문명 fallback을 포함한 견고한 범위 매칭 함수
import re
def _norm_name(s: str) -> str:
    s = str(s or "").strip().lower()
    s = s.replace("（","(").replace("）",")")
    s = re.sub(r"\s+", " ", s)
    return s

def _eng_in_paren(s: str) -> str:
    m = re.search(r"\(([^)]+)\)", s or "")
    return (m.group(1).strip().lower() if m else "")

def get_range_robust(standards: Optional[pd.DataFrame], plant_type: Optional[str], field: str):
    if standards is None or not plant_type:
        return None, None
    if "plant_name_norm" not in standards.columns:
        return None, None
    key = _norm_name(plant_type)
    key_eng = _eng_in_paren(key)
    # 1) 부분일치(전체 표기)
    row = standards.loc[standards["plant_name_norm"].str.contains(re.escape(key), na=False)]
    # 2) 영문명만으로 fallback
    if row.empty and key_eng:
        row = standards.loc[standards["plant_name_norm"].str.contains(re.escape(key_eng), na=False)]
    # 3) 완전 일치 최종 시도
    if row.empty:
        row = standards.loc[standards["plant_name_norm"] == key]
        if row.empty:
            return None, None
    lo_col = f"{field}_min"; hi_col = f"{field}_max"
    lo = row.iloc[0].get(lo_col, None); hi = row.iloc[0].get(hi_col, None)
    try: lo = float(lo) if lo is not None else None
    except: lo = None
    try: hi = float(hi) if hi is not None else None
    except: hi = None
    return lo, hi

get_range = get_range_robust

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
    
    plant_disp = plant_type
    room_disp  = room
    try:
        if _looks_mojibake(plant_disp) or not plant_disp:
            d = get_device_by_device_id_any(device_id)
            if d and d.get("plant_type"):
                plant_disp = d["plant_type"]
        if _looks_mojibake(room_disp) or not room_disp:
            if 'd' not in locals():
                d = get_device_by_device_id_any(device_id)
            if d and d.get("room"):
                room_disp = d["room"]
    except Exception:
        pass
    
    filename = f"greeneye_report_{_ascii_slug(device_id)}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pdf"
    temp_dir = tempfile.gettempdir()
    filepath = os.path.join(temp_dir, filename)
    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        leftMargin=5*mm, rightMargin=5*mm, topMargin=5*mm, bottomMargin=5*mm
    )
    styles = getSampleStyleSheet()
    story = []

    # 스타일 정의/보정 (먼저 추가 → 이후 정렬 변경)
    styles.add(ParagraphStyle(name='NotoTitle',    parent=styles['Title'],    fontName=BASE_FONT))
    styles.add(ParagraphStyle(name='NotoNormal',   parent=styles['Normal'],   fontName=BASE_FONT))
    styles.add(ParagraphStyle(name='NotoHeading4', parent=styles['Heading4'], fontName=BASE_FONT))
    for st in styles.byName.values():
        st.fontName = BASE_FONT
    styles['NotoTitle'].alignment = 0  # 제목 왼쪽 정렬

    # 우측 메타 스타일(작은 글자, 옅은 색, 오른쪽 정렬)
    styles.add(ParagraphStyle(
        name='MetaRight',
        parent=styles['NotoNormal'],
        alignment=2,  # RIGHT
        fontSize=8.5,
        textColor=colors.HexColor("#64748B"),
        leading=11,
        wordWrap='CJK'
    ))

    # 배터리 상태 포맷터
    def battery_status_string(level):
        if level is None: return "데이터 없음"
        if level >= 45:  return f"양호 ({level:.2f}%)"
        if level >= 30:  return f"낮음 ({level:.2f}%)"
        if level >= 15:  return f"매우 낮음 ({level:.2f}%)"
        return f"위험 ({level:.2f}%)"

    # Influx 쿼리
    start = _fmt_iso_utc(start_dt)
    end   = _fmt_iso_utc(end_dt)
    #    |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    query = f"""
    from(bucket: "{INFLUXDB_BUCKET}")
    |> range(start: {start}, stop: {end})
    |> filter(fn: (r) => r["_measurement"] == "sensor_readings")
    |> filter(fn: (r) => r["device_id"] == "{device_id}")
    |>aggregateWindow(every: {REPORT_AGG_WINDOW}, fn: mean, createEmpty: false)
    |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
    |> keep(columns: ["_time","device_id","temperature","humidity","light_lux","soil_moisture","soil_temp","soil_ec","battery"])
    """
    rows = query_influxdb_data(query)

    # 숫자/시간 정규화
    def _to_float(v):
        try: return float(v)
        except: return None
    for r in (rows or []):
        for k in ["temperature","humidity","light_lux","soil_moisture","soil_temp","soil_ec","battery"]:
            if r.get(k) is not None:
                r[k] = _to_float(r[k])
        if r.get("_time") is not None and not isinstance(r["_time"], datetime):
            r["_time"] = datetime.fromisoformat(str(r["_time"]).replace("Z","+00:00"))

    def pick(key): return [r.get(key) for r in rows if r.get(key) is not None]
    def avg(values): return sum(v for v in values if v is not None)/len(values) if values else 0

    # 최신 배터리/최근값
    def last_value(key):
        for r in sorted((rows or []), key=lambda x: x.get("_time") or datetime.min, reverse=True):
            v = r.get(key)
            if v is not None: return v
        return None
    latest_battery = last_value("battery")

    # ── 헤더: 좌(제목) + 우(메타 한 줄) ──────────────────────────────
    title_p = Paragraph(
        f"<b>주간 식물 보고서 - {_display_text(friendly_name)} ({_display_text(device_id)})</b>",
        styles['NotoTitle']
    )
    # 식물 표시에서 괄호 앞 줄바꿈 방지 NBSP 처리
    _plant_label = None
    if plant_disp:
        _plant_label = _display_text(plant_disp).replace(" (", "&nbsp;(")
    # ── 메타(우측) 4줄로: 1열×N행 중첩 테이블 ─────────────────────────────
    meta_lines = [
        f"기간: {start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}",
        (f"식물: {_plant_label}" if _plant_label else None),
        f"위치: {_display_text(room_disp) or '(미설정)'}",
        (f"배터리: {battery_status_string(latest_battery).replace('(%.2f' , '(%').replace('.00%', '%')}"
         if isinstance(latest_battery, (int, float)) else f"배터리: {battery_status_string(latest_battery)}"),
    ]
    meta_rows = [[Paragraph(line, styles['MetaRight'])] for line in meta_lines if line]
    right_nested = Table(meta_rows, colWidths=[8.2*cm], hAlign='RIGHT')
    right_nested.setStyle(TableStyle([
        ('ALIGN',         (0,0), (-1,-1), 'RIGHT'),
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',   (0,0), (-1,-1), 0),
        ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))

    # 좌측 셀: 로고(폭/높이 모두 지정해 안전하게) → 제목을 한 덩어리로
    _logo_img = None
    try:
        print(f"[DEBUG] LOGO_PATH={LOGO_PATH} | cwd={os.getcwd()}")
        with open(LOGO_PATH, "rb") as _fh:
            _bytes = _fh.read()
        _bio = io.BytesIO(_bytes)
        # 원본 픽셀 크기 → 비율 계산
        ir = ImageReader(io.BytesIO(_bytes))
        iw_px, ih_px = ir.getSize()             # 픽셀
        max_w_pt = 4.2*cm                       # 원하는 폭 (pt)
        scale = max_w_pt / float(iw_px)         # 비율
        w_pt = max_w_pt
        h_pt = float(ih_px) * scale
        _bio.seek(0)
        _logo_img = Image(_bio, width=w_pt, height=h_pt)  # kind 생략(직접 지정)
        _logo_img.hAlign = "LEFT"
    except Exception as _e:
        print(f"[WARN] Logo load failed: {type(_e).__name__}: {_e} (path={LOGO_PATH})")

    # 좌측 셀 구성: 로고가 있으면 2행짜리 중첩 테이블(로고 / 제목), 없으면 제목만
    if _logo_img:
        left_nested = Table([[ _logo_img ],
                             [ title_p ]],
                            colWidths=[12.0*cm])  # 좌측 영역 폭(임의)
        left_nested.setStyle(TableStyle([
            ('ALIGN',        (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
        ]))
        left_cell = left_nested
    else:
        left_cell = title_p

    # 같은 행에 좌:로고+제목, 우:메타
    header = Table([[left_cell, right_nested]], colWidths=[11.8*cm, 8.2*cm], hAlign='LEFT')
    header.setStyle(TableStyle([
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('ALIGN',         (0,0), (0,0),   'LEFT'),
        ('ALIGN',         (1,0), (1,0),   'RIGHT'),
        ('LEFTPADDING',   (0,0), (-1,-1), 0),
        ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LINEBELOW',     (0,0), (-1,0), 0.8, colors.HexColor("#E5E7EB")),
        # ▶ 우측 셀만 바닥 정렬 & 패딩 살짝 축소해 룰과 더 가까이
        ('VALIGN',        (1,0), (1,0), 'BOTTOM'),
        ('BOTTOMPADDING', (1,0), (1,0), 1),
    ]))
    story.append(header)
    story.append(Spacer(1, 0.3*cm))

    # 데이터가 없을 때도 헤더는 보이도록, 여기서 처리
    if not rows:
        story.append(Paragraph("이 기간 동안의 센서 데이터를 가져올 수 없거나, 데이터가 없습니다.", styles['NotoNormal']))
        doc.build(story)
        return filepath

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
        ('FONTSIZE',(0,0),(-1,-1),10),
        ('BOTTOMPADDING',(0,0),(-1,0),6),
        ('GRID',(0,0),(-1,-1),1,colors.black),
    ]))
    # 테이블도 확실히 폰트 지정
    avg_table.setStyle(TableStyle([
        ('FONTNAME',(0,0),(-1,-1),BASE_FONT),
    ]))
    story.append(avg_table)
    story.append(Spacer(1, 0.5*cm))

    # 표준범위 로딩
    standards_df = load_standards()
    # 로딩 실패 시 한 번 더 시도(엔진 문제 등)
    if standards_df is None:
        try:
            standards_df = pd.read_excel(STANDARDS_PATH, engine="openpyxl")
        except Exception as e:
            print(f"[WARN] standards second try failed: {e}")
    
    
    # ── 2열 레이아웃: 좌(주변 온도/습도/조도), 우(토양 온도/수분/전도도) ──
    left_fields  = [
        ("temperature","주변 온도 (°C)"),
        ("humidity","주변 습도 (%)"),
        ("light_lux","조도 (lux)"),
    ]
    right_fields = [
        ("soil_temp","토양 온도 (°C)"),
        ("soil_moisture","토양 수분 (%)"),
        ("soil_ec","토양 전도도 (uS/cm)"),
    ]

    col_w = 9.6*cm          # 각 칸 너비
    img_h = 4.3*cm          # 그래프 높이 (한 페이지 3개씩)

    def build_metric_block(field, label):
        # 값 목록만 추출 (요약 계산용)
        v_list = []
        for r in rows:
            v = r.get(field)
            if v is not None:
                v_list.append(_to_float(v))
        # 정상 범위
        lo, hi = get_range(standards_df, plant_type, field)
        # 그래프 이미지
        img_buf = generate_graph_image(rows, field, label, lo=lo, hi=hi)
        # 구성 파트(1열 N행)
        parts = [[Paragraph(label, styles['NotoHeading4'])]]
        if img_buf:
            parts.append([Image(img_buf, width=col_w, height=img_h)])
        # 요약 텍스트
        import math as _math
        vals = [float(x) for x in v_list if x is not None and not (_math.isnan(x) if isinstance(x, float) else False)]
        if lo is None and hi is None:
            parts.append([Paragraph("※ 이 항목의 정상 범위를 찾을 수 없어 비교를 생략했습니다.", styles['NotoNormal'])])
        else:
            lo_s = f"{lo:.0f}" if lo is not None else "-"
            hi_s = f"{hi:.0f}" if hi is not None else "-"
            low_cnt  = sum(1 for x in vals if lo is not None and x <  lo)
            high_cnt = sum(1 for x in vals if hi is not None and x >  hi)
            total_cnt = low_cnt + high_cnt
            summary_txt = f"정상범위 {lo_s}–{hi_s} 기준, 이번 주 이탈: 낮음 {low_cnt}회 · 높음 {high_cnt}회 (총 {total_cnt}회)"
            summary_html = f"<font size=9 color='#64748B'>{summary_txt}</font>"
            parts.append([Paragraph(summary_html, styles['NotoNormal'])])
        # 칸 안 여백
        parts.append([Spacer(1, 0.25*cm)])
        t = Table(parts, colWidths=[col_w])
        t.setStyle(TableStyle([
            ('ALIGN',        (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
        ]))
        return t

    # 좌/우 컬럼 구성
    left_column  = Table([[build_metric_block(f,l)] for f,l in left_fields],  colWidths=[col_w])
    right_column = Table([[build_metric_block(f,l)] for f,l in right_fields], colWidths=[col_w])
    for col in (left_column, right_column):
        col.setStyle(TableStyle([
            ('ALIGN',        (0,0), (-1,-1), 'LEFT'),
            ('VALIGN',       (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING',  (0,0), (-1,-1), 0),
            ('RIGHTPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING',   (0,0), (-1,-1), 0),
            ('BOTTOMPADDING',(0,0), (-1,-1), 0),
        ]))

    # 두 컬럼 사이 가터(여백) 0.8cm 확보를 위해 빈 열을 끼운 3열 테이블 사용
    grid = Table([[left_column, '', right_column]], colWidths=[col_w, 0.8*cm, col_w], hAlign='LEFT')
    grid.setStyle(TableStyle([
        ('VALIGN',        (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING',   (0,0), (-1,-1), 0),
        ('RIGHTPADDING',  (0,0), (-1,-1), 0),
        ('TOPPADDING',    (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(grid)

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
        print(f"PDF 보고서 전송 성공: {to_email}")
        return True
    except Exception as e:
        print(f"이메일 전송 오류: {e}")
        return False

# 계정이 하나일 때, PDF를 한 통으로 묶어 전송
def send_email_with_pdfs(to_email: str, subject: str, body_text: str, pdf_paths: list[str]) -> bool:
    msg = MIMEMultipart()
    if not EMAIL_USERNAME:
        print("[WARN] EMAIL_USERNAME not set. Skipping email send; PDFs only.")
        return False
    msg["From"] = formataddr(("GreenEye", EMAIL_USERNAME))
    msg["To"] = to_email
    msg["Subject"] = Header(subject, "utf-8")
    msg.attach(MIMEText(body_text, "plain", _charset="utf-8"))

    attached = 0
    for p in pdf_paths:
        try:
            if not p or not os.path.exists(p):
                print(f"[WARN] skip attach (not found): {p}")
                continue
            with open(p, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header(
                "Content-Disposition",
                "attachment",
                filename=(Header(os.path.basename(p), "utf-8").encode()),
            )
            msg.attach(part)
            attached += 1
        except Exception as e:
            print(f"[WARN] attach failed: {p} ({e})")

    if attached == 0:
        print("[WARN] no attachments -> skip sending")
        return False

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=20) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
        print(f"[OK] bundled mail sent to {to_email} with {attached} attachments")
        return True
    except Exception as e:
        print(f"[ERR] bundled email send failed: {e}")
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
                device.get("room")          # room 전달
            )
            subject = f"GreenEye 주간 식물 보고서 - {device['friendly_name']}"
            body = "안녕하세요, GreenEye 시스템에서 자동 생성된 식물 생장 보고서를 첨부드립니다."
            send_email_with_pdf(email, subject, body, pdf)
    print(f"--- PDF 보고서 전송 완료 ---\n")

# 사용자별로 소유 디바이스 PDF를 모아서 한 통으로 발송
def send_all_reports_grouped(days: int = 7):
    print(f"\n--- 그룹 전송 시작(days={days}): {datetime.now()} ---")
    _users   = get_all_users() or []
    users    = [dict(u) if not isinstance(u, dict) else u for u in _users]
    _devices = get_all_devices_any() or []
    devices  = [dict(d) if not isinstance(d, dict) else d for d in _devices]
    now      = datetime.now().astimezone(pytz.utc)
    start    = now - timedelta(days=days)

    for u in users:
        email = u.get("email")
        uid   = u.get("id")
        if not email or uid is None:
            continue
        # 미리 읽어둔 목록에서 owner_user_id 매칭 (문자열/정수 불일치 대비)
        owned = [d for d in devices if str(d.get("owner_user_id")) == str(uid)]
        # 폴백: 미리 읽은 devices에 owner_user_id가 없을 수도 있으므로, 사용자별 쿼리 함수(get_all_devices) 사용
        if not owned:
            _owned = get_all_devices(uid) or []
            owned  = [dict(d) if not isinstance(d, dict) else d for d in _owned]
            print(f"[DEBUG] fallback devices for user_id={uid}: {len(owned)} found")
        if not owned:
            print(f"[INFO] skip {email}: no devices")
            continue

        pdfs = []
        for d in owned:
            dev   = d.get("device_id")
            fname = d.get("friendly_name") or dev
            plant = d.get("plant_type")
            room  = d.get("room")
            print(f"[INFO] generate PDF → user={email}, device={dev} ({fname})")
            path = generate_pdf_report_by_device(dev, start, now, fname, plant, room)
            pdfs.append(path)

        subject = f"GreenEye 주간 식물 보고서 - {len(pdfs)}개 디바이스"
        body    = "안녕하세요, GreenEye입니다.\n주간 식물 생장 보고서를 보내드립니다."
        send_email_with_pdfs(email, subject, body, pdfs)
    print(f"--- 그룹 전송 완료 ---\n")

if __name__ == "__main__":
    send_all_reports_grouped()
    # --- InfluxDB 클라이언트 정리 ---
    try:
        cli = get_influx_client()
        if cli:
            cli.close()
            print("[INFO] InfluxDB client closed cleanly.")
    except Exception as e:
        print(f"[WARN] Influx client close failed: {e}")