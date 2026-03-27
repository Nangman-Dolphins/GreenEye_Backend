# GreenEye CCU 백엔드 시스템

본 레포지토리는 '모듈형 AI/DB 기반 Plant-care 시스템' 인 **GreenEye** 프로젝트의 핵심 백엔드(CCU) 이다.

이 백엔드는 `Python (Flask)` 를 기반으로 구축되었으며, 전체 시스템의 '두뇌' 역할을 수행한다. 화분에 설치된 여러 `GreenEye_SensorDevice` (센서 단말) 로부터 데이터를 수집하고, `GreenEye_Frontend` (웹 앱) 에 API 를 제공하며, AI 분석 및 원격 제어 명령을 중계하는 모든 서버사이드 로직을 담당한다.

## 연관 레포지토리

* [**GreenEye_Frontend**](https://github.com/Nangman-Dolphins/GreenEye_Frontend) : (CCU) React 기반의 사용자 웹 애플리케이션이다. (대시보드, 챗봇, 제어판)

* [**GreenEye_SensorDevice**](https://github.com/Nangman-Dolphins/GreenEye_SensorDevice) : (SD) ESP32-CAM 기반의 센서 단말 펌웨어이다.

## 시스템 아키텍처

본 백엔드는 Docker Compose 를 통해 Nginx, Flask, Mosquitto, InfluxDB, Redis 등 여러 서비스가 유기적으로 동작하도록 설계되었다.

* **데이터 수집 (MQTT)**

   * `SensorDevice` (ESP32) 가 측정한 센서/이미지 데이터를 `Mosquitto` (MQTT 브로커) 의 `GreenEye/data/+` 토픽으로 발행한다.

   * 이때 발행되는 데이터는 `SensorDevice` 가 측정한 값을 담은 **JSON 페이로드** 이다. (예: `{"bat_level": 90, "amb_temp": 25.5, "soil_humi": 60.1, ...}` 또는 이미지 데이터 `{"plant_img": "[base64-encoded-string]"}`)

   * 백엔드의 `mqtt_client` (별도 Python 서비스) 가 이 토픽을 구독(Subscribe) 하고, 수신된 데이터를 실시간으로 `InfluxDB` (시계열 DB) 와 `Redis` (최신 상태 캐시) 에 저장한다. `Redis` 에는 각 장치의 '최신 상태 값' 이 캐시되어 API 응답 속도를 높이는 데 사용된다.

* **데이터 제공 및 실시간 스트리밍 (REST API & Socket.IO)**

   * `Frontend` (React) 가 사용자 인증, 기기 목록, 최신 센서 값, 이미지 등을 요청하기 위해 `Flask` (API 서버) 가 제공하는 `/api/...` 엔드포인트를 호출한다.

   * 예를 들어, `GET /api/devices` 요청 시 `Flask` 서버는 `Redis` 또는 `PostgreSQL` 에서 **장치 메타데이터 목록(JSON 배열)** 을 반환한다. `GET /api/latest_sensor_data/{id}` 요청 시 `Redis` 에 캐시된 **최신 센서 값(JSON 객체)** 을 즉시 반환한다.

   * `Flask` 서버는 필요시 `InfluxDB` 또는 `Redis` 에서 데이터를 조회하여 JSON 형태로 응답한다.

   * 추가로 `Flask-SocketIO` 를 활용하여 5초 주기로 최신 센서 데이터를 클라이언트에 실시간 브로드캐스트하여 즉각적인 상태 업데이트를 지원한다.

* **원격 제어 (API → MQTT)**

   * 사용자가 `Frontend` 의 `ControlPanel` 에서 제어 버튼(예: 워터펌프 ON)를 조작한다.

      * `Frontend`는 Flask의 `/api/control_device/{id}` 엔드포인트로 수동 제어 명령이 담긴 JSON을 POST하여 연결된 장치에 커맨드를 보낼 수 있다.

   * 또는 사용자가 장치 설정에서 장치에 대한 모드 및 설정을 조작한다.

      * 이 경우 해당 기기의 `GreenEye/conf/{id}` 토픽으로 사용자가 변경한 설정에 대해 JSON에 정보를 담아 POST한다.
      * `SensorDevice`가 이 메시지를 수신하여 설정을 적용시킨다.

* **AI 연동**

   * `Frontend` 의 `ChatAssistant` 가 이미지와 프롬프트를 `/api/chat/gemini` 로 전송한다. 이 요청의 Body 는 `{"prompt": "...", "image": "base64...", "conversation_id": "..."}` 형식의 JSON 이다.

   * `Flask` 서버가 이 데이터를 받아 식물학자 페르소나가 적용된 `Google Gemini API` 를 호출하고, 응답(예: `{"answer": "..."}`) 을 받아 사용자에게 JSON 으로 다시 전달한다.

   * 내부 `PyTorch` 모델을 통해 수집된 이미지를 주기적으로 분석시키며, `SensorInfo` 에 표시될 '약식 조언' 을 생성하여 `Redis` 에 저장한다.

* **자동화 리포트 및 백그라운드 작업 (Scheduling)**

   * `APScheduler` 를 통해 백그라운드에서 주기적인 작업을 수행한다.
       
   * 매주 수집된 센서 시계열 데이터(온도, 습도, 조도, 토양 상태 등) 를 분석하여 **식물 생장 주간 리포트 PDF** 를 자동 생성하고, 이메일 수신에 동의한 사용자에게 발송한다. 리포트에는 `Matplotlib` 기반의 추이 그래프와 정상 범위 이탈 횟수 분석 결과가 포함된다.

   * 스토리지 확보를 위해 누적된 디바이스 이미지와 챗봇 업로드 이미지 파일들을 주기적으로 자동 정리하는 작업도 함께 수행된다.

## 주요 기능 및 API 엔드포인트

본 백엔드 서버( `app.py` 및 관련 모듈) 는 다음과 같은 핵심 기능과 API 를 제공한다.

### 핵심 기능

* **MQTT Client Service:** `services/mqtt_client.py` (혹은 유사 모듈)

* `Mosquitto` 브로커를 구독하며, `GreenEye/data/+` 토픽으로 들어오는 모든 센서 단말의 JSON 데이터를 `InfluxDB` 와 `Redis` 에 실시간으로 파싱 및 저장한다.

* 이미지 데이터(Base64) 를 디코딩하여 파일 스토리지(예: `/images`) 에 저장하고, 관련 메타데이터를 DB 에 기록한다.

### RESTful API (Flask)

* **인증 (Auth)**

  * `POST /api/auth/register` : (Request Body: `{"email": "...", "password": "..."}`) 회원가입을 처리한다.

  * `POST /api/auth/login` : (Request Body: `{"email": "...", "password": "..."}`) 로그인을 처리하며, 성공 시 **JWT 토큰** 을 JSON 으로 반환한다. (Response Body: `{"access_token": "..."}`)

* **기기 관리 (Device)**

  * `POST /api/register_device` : (Request Body: `FormData` - `mac_address`, `friendly_name`, `image` 등) 새 센서 단말기(SD) 를 등록한다. (Frontend 의 `DeviceLink` 연동)

  * `GET /api/devices` : 현재 사용자가 등록한 모든 기기 목록을 **JSON 배열** 로 반환한다. ( `PlantGallery` 용)

  * `DELETE /api/devices/{id}` : 특정 기기를 삭제한다.

  * `POST /api/devices/{id}/image` : 기존 디바이스의 대표 이미지를 추가하거나 교체한다.

  * `DELETE /api/devices/{id}/image` : 기존 디바이스의 대표 이미지를 삭제한다.

* **데이터 조회 (Data)**

  * `GET /api/latest_sensor_data/{id}` : 특정 기기의 가장 최신 센서 값을 **JSON 객체** 로 반환한다. ( `SensorInfo` 용)

  * `GET /api/historical_sensor_data/{id}` : 과거 7일간의 센서 시계열 데이터를 **JSON 배열** 로 반환하여 차트 렌더링을 지원한다.

  * `GET /api/devices/{id}/latest-image` : 특정 기기가 촬영한 최신 이미지 정보(URL) 를 **JSON** 으로 반환한다.

  * `GET /api/images/{...}` : 저장된 이미지 파일(JPEG) 을 직접 반환한다. (Static File Serving)

* **기기 제어 및 설정 (Control & Setting)**

  * `POST /api/control_device/{id}` : (Request Body: `{"humidifier_action": 1, ...}`) 특정 기기에 수동 원격 제어 명령 JSON 을 전송한다. ( `ControlPanel` 연동)

  * `POST /api/control_mode/{id}` : 기기의 동작 모드( `ultra_low` , `low` , `normal` , `high` , `ultra_high` ) 를 일괄적으로 변경한다.

  * `GET /api/alert_thresholds` : 시스템 전체 경고 임계치(온도, 습도 등) 를 조회한다.

  * `PUT /api/alert_thresholds` : 시스템 전체 경고 임계치를 수정 및 업데이트한다.

* **AI 및 보고서 (AI & Reports)**

  * `POST /api/chat/gemini` : (Request Body: `{"prompt": "...", "image": "base64...", "conversation_id": "..."}`) Gemini AI 챗봇 API 프록시 역할을 수행한다.

  * `GET /api/chat/history` : (Query: `?conversation_id=...`) 이전 대화 기록을 **JSON 배열** 로 반환한다.

  * `GET /api/user/email-consent` : PDF 리포트 이메일 수신 동의 여부를 반환한다.

  * `PUT /api/user/email-consent` : PDF 리포트 이메일 수신 동의 여부를 변경한다.

## 기술 스택

* **Backend Framework:** Python, Flask, Flask-SocketIO

* **Database:** InfluxDB (시계열 센서 데이터), Redis (최신 상태 캐시, 세션), PostgreSQL/SQLite (사용자, 기기 메타데이터)

* **Messaging:** Mosquitto (MQTT 브로커)

* **AI:** Google Gemini API (외부 연동), PyTorch (내부 이미지 추론 모델)

* **Task Scheduling & Reporting:** APScheduler, ReportLab, Matplotlib, Pandas

* **Deployment:** Docker, Docker Compose

* **Web Server / Proxy:** Nginx
