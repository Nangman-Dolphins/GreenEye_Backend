FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 필요한 CLI (sqlite3, curl 등) 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 ca-certificates curl tzdata \
  && rm -rf /var/lib/apt/lists/*

# 컨테이너 내에서 작업할 디렉토리를 /app으로 설정
WORKDIR /app

# 로컬의 requirements.txt 파일을 컨테이너의 /app 디렉토리로 복사
COPY requirements.txt .

# 폰트 파일을 컨테이너 안으로 복사
COPY fonts /app/fonts

# 복사한 requirements.txt를 사용해 파이썬 패키지들을 설치
RUN pip install --no-cache-dir -r requirements.txt



# 로컬의 모든 프로젝트 파일을 컨테이너의 /app 디렉토리로 복사
COPY . .

#타임존 설정
ENV TZ=Asia/Seoul

# Flask 앱이 외부에서 접속 가능하도록 포트 5000번을 노출
EXPOSE 5000

# 컨테이너 시작 시 실행될 명령어
# Flask 앱을 Gunicorn이라는 프로덕션용 WSGI 서버로 실행합니다.
# 워커(worker)는 2개로 설정하며, 호스트 0.0.0.0의 5000번 포트에서 실행합니다.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "backend_app.wsgi:app"]
