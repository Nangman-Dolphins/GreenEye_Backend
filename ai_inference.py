"""
GreenEye - AI Inference (Stub)

실제 모델 연동 전까지 안전하게 동작하는 스텁 구현.
- load_ai_model(): 부팅 시 호출되어 모델 준비 여부만 표시
- run_inference_on_image(device_id, image_path): 이미지 추론 스텁 리턴

운영에서 실제 모델을 쓰려면, 이 파일을 동일 인터페이스로 교체하세요.
"""

from __future__ import annotations

import os
from typing import Dict, Any

_MODEL_READY = False


def load_ai_model() -> bool:
    """
    실제에선 여기서 모델 파일 로드/초기화 수행.
    실패 시 예외를 던지거나 False를 리턴하도록 구현하면 됨.
    """
    global _MODEL_READY
    _MODEL_READY = True
    print("[AI] (stub) model loaded")
    return True


def run_inference_on_image(device_id: str, image_path: str) -> Dict[str, Any]:
    """
    이미지 파일이 존재하는지만 확인하고, 고정된 스텁 결과를 반환.
    실제 모델로 교체 시, 반환 포맷은 유지하는 걸 권장.
    """
    exists = os.path.exists(image_path)
    return {
        "status": "ready" if _MODEL_READY else "unavailable",
        "device_id": device_id,
        "image": os.path.basename(image_path),
        "image_exists": bool(exists),
        "diagnosis": None,          # 예: "powdery_mildew"
        "confidence": 0.0,          # 예: 0.92
        "notes": "stub result — replace with real model output",
    }
