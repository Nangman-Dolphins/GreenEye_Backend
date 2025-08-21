# wsgi.py
import logging
from app import app, init_runtime_and_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info("🧪 [WSGI] wsgi.py 시작됨")
init_runtime_and_scheduler()
logger.info("🧪 [WSGI] init_runtime_and_scheduler() 완료됨")
