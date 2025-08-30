# -*- coding: utf-8 -*-
import logging

# 절대 임포트: 패키지 내부 모듈은 backend_app 접두사 사용
from backend_app.app import app

# 선택: init 함수가 있으면 불러 사용
try:
    from backend_app.app import init_runtime_and_scheduler
except Exception:
    init_runtime_and_scheduler = None

logger = logging.getLogger("backend_app.wsgi")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_h)

logger.info("[WSGI] wsgi.py booting")

# 런타임 초기화(있을 때만)
if init_runtime_and_scheduler:
    try:
        init_runtime_and_scheduler()
        logger.info("[WSGI] init_runtime_and_scheduler OK")
    except Exception as e:
        logger.exception("[WSGI] init failed: %s", e)

# gunicorn이 import하여 app을 노출
# 로컬 실행용 진입점도 유지
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
