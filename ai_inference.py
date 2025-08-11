import random
from datetime import datetime

# --- 가상 AI 모델 및 상태 ---
ai_model = None
model_labels = ["정상", "과습", "병충해 의심", "영양 부족"]

def load_ai_model():
    """
    서버 시작 시 AI 모델을 메모리에 로드하는 함수입니다.
    실제 모델 파일(.tflite)을 로딩하는 로직이 여기에 위치합니다.
    """
    global ai_model
    try:
        # 예시: tflite_runtime.Interpreter(model_path="model.tflite")
        # 지금은 모델이 준비되지 않았으므로, 로드된 척 문자열을 할당
        ai_model = "Dummy Model Loaded"
        print(f"[AI Inference] AI Model Loading... Status: {ai_model}")
    except Exception as e:
        ai_model = None
        print(f"[AI Inference] Error loading AI model: {e}")

def run_inference_on_image(mac_address, image_path):
    """
    저장된 이미지 경로를 받아 AI 추론을 실행하고 진단 결과를 반환
    
    Args:
        mac_address (str): 진단 대상 장치의 MAC 주소
        image_path (str): 분석할 이미지의 서버 내 파일 경로

    Returns:
        dict: 상세한 진단 결과 (상태, 상세 설명, 신뢰도 등)
    """
    if not ai_model:
        print("[AI Inference] AI model is not loaded. Skipping inference.")
        return {
            "status": "오류",
            "details": "AI 모델이 로드되지 않았습니다.",
            "confidence": 0.0,
            "inference_timestamp": datetime.utcnow().isoformat()
        }
    
    print(f"[AI Inference] Running inference for {mac_address} on image: {image_path}")
    
    # --- 실제 AI 추론 로직 (시뮬레이션) ---
    # To-Do: 이 부분에 실제 이미지 처리 및 모델 추론 코드를 추가 예정
    # ------------------------------------

    # 현재는 가능한 상태 중 하나를 무작위로 선택하여 반환.
    random_status = random.choice(model_labels)
    
    diagnosis_details = {
        "정상": "식물의 상태가 매우 건강합니다. 현재 관리 방식을 유지하세요.",
        "과습": "토양 습도가 높은 것으로 보입니다. 물주기 횟수를 줄이는 것을 권장합니다.",
        "병충해 의심": "잎에서 병반 또는 해충의 흔적이 의심됩니다. 이미지를 자세히 확인하고 방제를 준비하세요.",
        "영양 부족": "잎의 색이 옅어지거나 성장이 더딘 것으로 보아 영양분이 부족할 수 있습니다. 비료 사용을 고려해 보세요."
    }
    
    result = {
        "status": random_status,
        "details": diagnosis_details.get(random_status, "진단 정보를 찾을 수 없습니다."),
        "confidence": round(random.uniform(0.75, 0.99), 2), # 신뢰도(가상)
        "inference_timestamp": datetime.utcnow().isoformat()
    }
    
    print(f"[AI Inference] Result: {result}")
    
    return result