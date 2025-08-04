# 베이스 이미지: Python 3.12를 기반으로 하는 가벼운 Alpine Linux 이미지 사용
FROM python:3.12-alpine

# 컨테이너 내에서 작업할 디렉토리를 /app으로 설정
WORKDIR /app

# 로컬의 requirements.txt 파일을 컨테이너의 /app 디렉토리로 복사
COPY requirements.txt ./

# 복사한 requirements.txt를 사용해 파이썬 패키지들을 설치
RUN pip install --no-cache-dir -r requirements.txt

# 로컬의 모든 프로젝트 파일을 컨테이너의 /app 디렉토리로 복사
COPY . .

# Flask 앱이 외부에서 접속 가능하도록 포트 5000번을 노출
EXPOSE 5000

# 컨테이너 시작 시 실행될 명령어
# Flask 앱을 Gunicorn이라는 프로덕션용 WSGI 서버로 실행합니다.
# 워커(worker)는 2개로 설정하며, 호스트 0.0.0.0의 5000번 포트에서 실행합니다.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]