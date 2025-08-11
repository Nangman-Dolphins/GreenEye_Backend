# AI 모델 로드, 이미지 전처리, 추론을 담당하는 모듈
# 뼈대 구축 및 테스트를 위한 코드

import os
import random
import uuid
from datetime import datetime

# --- AI 모델 로드 함수 (실제 모델이 없으므로 placeholder) ---
def load_ai_model():
    """
    실제 AI 모델을 메모리에 로드합니다.
    지금은 더미 모델 로드 메시지만 출력합니다.
    """
    print("[AI Inference] Loading AI model... (Dummy)")
    # 나중에 여기에 PyTorch 또는 TFLite 모델 로드 로직 추가
    return True

def run_inference_on_image(mac_address, filepath):
    """
    이미지 경로를 받아 AI 추론을 실행하고 결과를 반환합니다.
    지금은 더미(가상) 결과를 반환합니다.
    """
    print(f"[AI Inference] Running inference on image for {mac_address} at {filepath} (Dummy)")
    
    # --- 더미(Mock) 진단 결과 반환 ---
    # 실제 모델이 없으므로, 정해진 진단 결과를 무작위로 반환.
    dummy_diagnoses = [
        "정상",
        "잎 황변",
        "병충해 의심",
        "수분 부족"
    ]
    diagnosis = random.choice(dummy_diagnoses)
    
    # 더미 결과를 Redis에 캐싱할 때 필요한 JSON 형식으로 반환
    return {"diagnosis": diagnosis, "timestamp": datetime.utcnow().isoformat()}