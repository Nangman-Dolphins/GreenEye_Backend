# gemini_routes.py

import os
import requests
import json
import uuid
from flask import Blueprint, request, jsonify, g

# 기존 app.py에서 사용하던 token_required 데코레이터를 가져와야 합니다.
# 만약 별도 파일에 있다면 from .auth import token_required 처럼 가져옵니다.
# 여기서는 임시로 정의하지만, 실제로는 공유되는 파일에서 import 하세요.
from functools import wraps

def token_required(f):
    # 이 부분은 app.py에 있는 token_required 함수와 동일해야 합니다.
    # 실제 프로젝트에서는 이 데코레이터를 공통 auth 모듈로 분리하는 것이 좋습니다.
    @wraps(f)
    def decorated(*args, **kwargs):
        # ... app.py의 token_required 로직 ...
        # 여기서는 간단히 g.current_user가 설정되었다고 가정합니다.
        if not hasattr(g, 'current_user'):
             return jsonify({"message": "Token is missing or invalid!"}), 401
        return f(*args, **kwargs)
    return decorated


# database.py에서 필요한 함수들을 가져옵니다.
from .database import save_message, load_history

# 'gemini_api'라는 이름으로 블루프린트를 생성합니다.
gemini_bp = Blueprint('gemini_api', __name__)

# --- 설정 부분 ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') # .env 파일에 GEMINI_API_KEY로 저장하세요.
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
# --- 설정 끝 ---

def format_history_for_gemini(history):
    formatted_history = []
    for sender, message in history:
        role = 'user' if sender == 'user' else 'model'
        formatted_history.append({"role": role, "parts": [{"text": message}]})
    return formatted_history

@gemini_bp.route('/ask-gemini', methods=['POST'])
@token_required # 기존 인증 시스템을 그대로 사용합니다.
def ask_gemini():
    data = request.get_json()
    user_prompt = data.get('prompt')
    if not user_prompt:
        return jsonify({"error": "No prompt provided"}), 400

    image_base64 = data.get('image')
    conversation_id = data.get('conversation_id', str(uuid.uuid4()))
    current_user_id = g.current_user['id'] # token_required에서 설정된 사용자 정보

    save_message(conversation_id, current_user_id, 'user', user_prompt)
    db_history = load_history(conversation_id, current_user_id)
    gemini_history = format_history_for_gemini(db_history[:-1])

    current_user_parts = [{"text": user_prompt}]
    if image_base64:
        current_user_parts.append({
            "inline_data": {"mime_type": "image/jpeg", "data": image_base64}
        })

    contents = gemini_history + [{"role": "user", "parts": current_user_parts}]
    gemini_payload = {"contents": contents}
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(GEMINI_API_URL, headers=headers, data=json.dumps(gemini_payload))
        response.raise_for_status()
        gemini_response = response.json()

        answer = gemini_response['candidates'][0]['content']['parts'][0]['text']
        save_message(conversation_id, current_user_id, 'ai', answer)

        return jsonify({"answer": answer, "conversation_id": conversation_id})
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        return jsonify({"error": "Failed to get response from Gemini"}), 500