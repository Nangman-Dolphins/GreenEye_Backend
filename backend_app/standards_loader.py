# backend_app/standards_loader.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, re, time
from pathlib import Path
from typing import Dict, Tuple, Optional, Any

import pandas as pd

# --- 설정: .env에 PLANT_STANDARDS_XLSX가 있으면 우선, 없으면 repo 기본 경로 사용
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_XLSX = BASE_DIR / "reference_data" / "plant_standards_cleaned.xlsx"
XLSX_PATH = Path(os.getenv("PLANT_STANDARDS_XLSX") or DEFAULT_XLSX)

# 한국어 컬럼 -> 내부 센서 키 매핑
COLMAP = {
    "환경온도(°C)": "temperature",
    "환경습도(%)": "humidity",
    "환경광도(lux)": "light_lux",
    "토양온도(°C)": "soil_temp",
    "토양수분(%)": "soil_moisture",
    "토양전도도(uS/cm)": "soil_ec",
}
PLANT_COL = "식물명"

# --- 캐시 (파일이 크지 않으므로 프로세스 메모리 캐시)
_cache = {
    "mtime": None,
    "standards": {},   # {"Hydrangea": {"temperature": (16,25), ...}, ...}
    "aliases": {},     # {"hydrangea": "Hydrangea", "수국": "Hydrangea", ...}
}

# 문자열 "10 ~ 25" 같은 범위를 파싱 → (10.0, 25.0)
_rng_pat = re.compile(r"^\s*(?P<min>-?\d+(?:\.\d+)?)\s*[~\-–]\s*(?P<max>-?\d+(?:\.\d+)?)\s*$")
_num_pat = re.compile(r"-?\d+(?:\.\d+)?")

def _parse_range(cell: Any) -> Optional[Tuple[float, float]]:
    """엑셀 셀에서 최소~최대 범위를 파싱. 실패 시 None"""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    m = _rng_pat.match(s)
    if m:
        lo = float(m.group("min")); hi = float(m.group("max"))
        if lo > hi: lo, hi = hi, lo
        return (lo, hi)
    # 단일 값도 들어올 수 있음 → 최소=최대 처리
    m2 = _num_pat.findall(s)
    if len(m2) == 1:
        v = float(m2[0]); return (v, v)
    if len(m2) >= 2:
        lo, hi = float(m2[0]), float(m2[1])
        if lo > hi: lo, hi = hi, lo
        return (lo, hi)
    return None

def _norm_name(s: str) -> str:
    """매칭을 위한 느슨한 정규화: 소문자, 공백/특수문자 제거"""
    return re.sub(r"[^a-z0-9가-힣]", "", (s or "").lower())

def _extract_aliases(plant_raw: str) -> set[str]:
    """
    '팬지 / 삼색제비꽃 (Pansy)' → {'팬지', '삼색제비꽃', 'pansy', '팬지/삼색제비꽃', 원문 전체}
    - 슬래시(/)로 다중 이름 분리
    - 괄호() 안의 영문명도 alias에 추가
    """
    aliases = set()
    base = plant_raw.strip()
    aliases.add(base)

    # 괄호 안 텍스트 추출 (영문명 등)
    for m in re.finditer(r"\(([^)]+)\)", base):
        aliases.add(m.group(1).strip())

    # 슬래시 분리
    parts = re.split(r"[\/|]", base)
    for p in parts:
        p = p.strip()
        if p: aliases.add(p)

    # 슬래시 전 한국어 파트만 (예: "팬지 / 삼색제비꽃 (Pansy)" → "팬지", "삼색제비꽃")
    base_ko = re.sub(r"\([^)]*\)", "", base)  # 괄호 제거
    for p in re.split(r"[\/|]", base_ko):
        p = p.strip()
        if p: aliases.add(p)

    # 영문만 남기는 경우도 추가 (공백 제거)
    for a in list(aliases):
        if re.search(r"[A-Za-z]", a):
            aliases.add(a.replace(" ", ""))

    return {a for a in aliases if a}

def _build_cache():
    """엑셀을 읽어 standards & aliases 캐시 구성"""
    df = pd.read_excel(XLSX_PATH)

    standards: Dict[str, Dict[str, Tuple[float, float]]] = {}
    aliases: Dict[str, str] = {}

    if PLANT_COL not in df.columns:
        raise RuntimeError(f"엑셀에 '{PLANT_COL}' 컬럼이 없습니다. 헤더를 확인하세요.")

    for _, row in df.iterrows():
        plant_raw = str(row[PLANT_COL]).strip()
        if not plant_raw:
            continue

        # 내부 대표 이름: 괄호영문 제거, 앞뒤 공백 정리
        rep_name = re.sub(r"\([^)]*\)", "", plant_raw).strip()
        rep_name = re.sub(r"\s{2,}", " ", rep_name)

        # 각 센서 컬럼을 파싱
        slots = {}
        for ko_col, key in COLMAP.items():
            if ko_col in df.columns:
                rng = _parse_range(row.get(ko_col))
                if rng:
                    slots[key] = rng

        if not slots:
            continue

        # 표준 dict 채우기
        standards.setdefault(rep_name, {}).update(slots)

        # alias 매핑 구성(느슨한 키 → 대표명)
        for a in _extract_aliases(plant_raw):
            aliases[_norm_name(a)] = rep_name
        aliases[_norm_name(rep_name)] = rep_name

    _cache["standards"] = standards
    _cache["aliases"] = aliases

def _ensure_loaded():
    """최초 로드 또는 파일 변경 시 리로드"""
    mtime = XLSX_PATH.stat().st_mtime if XLSX_PATH.exists() else None
    if _cache["mtime"] is None or _cache["mtime"] != mtime or not _cache["standards"]:
        if not XLSX_PATH.exists():
            raise FileNotFoundError(f"기준 엑셀 파일을 찾을 수 없습니다: {XLSX_PATH}")
        _build_cache()
        _cache["mtime"] = mtime

def resolve_plant_name(user_value: str) -> Optional[str]:
    """사용자/DB의 plant_type을 엑셀 대표명으로 매핑"""
    if not user_value:
        return None
    _ensure_loaded()
    key = _norm_name(user_value)
    rep = _cache["aliases"].get(key)
    return rep

def get_ranges_for_plant(plant: str) -> Dict[str, Tuple[float, float]]:
    """대표 식물명으로 범위 dict 반환. 없으면 {}"""
    _ensure_loaded()
    return _cache["standards"].get(plant, {}).copy()

def classify_value(plant_type: str | None, field: str, value: Any) -> Tuple[str, Optional[Tuple[float, float]]]:
    """
    단일 값 분류: 'low' | 'middle' | 'high' | 'unknown'
    - plant_type가 None이거나, 범위가 없으면 'unknown'
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "unknown", None

    rep = resolve_plant_name(plant_type) if plant_type else None
    if not rep:
        return "unknown", None

    ranges = get_ranges_for_plant(rep)
    rng = ranges.get(field)
    if not rng:
        return "unknown", None

    lo, hi = rng
    if v < lo: return "low", rng
    if v > hi: return "high", rng
    return "middle", rng

def classify_payload(plant_type: str | None, raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    센서 dict({field:value,...}) → {field: {"value": v, "status": "...", "range": [lo,hi] or None}}
    """
    out: Dict[str, Dict[str, Any]] = {}
    keys = ["temperature","humidity","light_lux","soil_temp","soil_moisture","soil_ec","battery"]
    for k in keys:
        v = raw.get(k)
        status, rng = classify_value(plant_type, k, v)
        out[k] = {"value": v, "status": status, "range": (list(rng) if rng else None)}
    return out
