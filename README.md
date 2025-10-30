# GreenEye CCU 백엔드 시스템

본 레포지토리는 '모듈형 AI/DB 기반 Plant-care 시스템'인 **GreenEye** 프로젝트의 핵심 백엔드(CCU)이다.

이 백엔드는 `Python (Flask)`를 기반으로 구축되었으며, 전체 시스템의 '두뇌' 역할을 수행한다. 화분에 설치된 여러 `GreenEye_SensorDevice` (센서 단말)로부터 데이터를 수집하고, `GreenEye_Frontend` (웹 앱)에 API를 제공하며, AI 분석 및 원격 제어 명령을 중계하는 모든 서버사이드 로직을 담당한다.

## 연관 레포지토리

* [**GreenEye_Frontend**](https://github.com/Nangman-Dolphins/GreenEye_Frontend): (CCU) React 기반의 사용자 웹 애플리케이션이다. (대시보드, 챗봇, 제어판)

* [**GreenEye_SensorDevice**](https://github.com/Nangman-Dolphins/GreenEye_SensorDevice): (SD) ESP32-CAM 기반의 센서 단말 펌웨어이다.

## 시스템 아키텍처

본 백엔드는 Docker Compose를 통해 Nginx, Flask, Mosquitto, InfluxDB, Redis 등 여러 서비스가 유기적으로 동작하도록 설계되었다.

1. **데이터 수집 (MQTT)**

   * `SensorDevice` (ESP32)가 측정한 센서/이미지 데이터를 `Mosquitto` (MQTT 브로커)의 `GreenEye/data/+` 토픽으로 발행한다.

   * 이때 발행되는 데이터는 `SensorDevice`가 측정한 값을 담은 **JSON 페이로드**이다. (예: `{"bat_level": 90, "amb_temp": 25.5, "soil_humi": 60.1, ...}` 또는 이미지 데이터 `{"plant_img": "[base64-encoded-string]"}`)

   * 백엔드의 `mqtt_client` (별도 Python 서비스)가 이 토픽을 구독(Subscribe)하고, 수신된 데이터를 실시간으로 `InfluxDB` (시계열 DB)와 `Redis` (최신 상태 캐시)에 저장한다. `Redis`에는 각 장치의 '최신 상태 값'이 캐시되어 API 응답 속도를 높이는 데 사용된다.

2. **데이터 제공 (REST API)**

   * `Frontend` (React)가 사용자 인증, 기기 목록, 최신 센서 값, 이미지 등을 요청하기 위해 `Flask` (API 서버)가 제공하는 `/api/...` 엔드포인트를 호출한다.

   * 예를 들어, `GET /api/devices` 요청 시 `Flask` 서버는 `Redis` 또는 `PostgreSQL`에서 **장치 메타데이터 목록(JSON 배열)**을 반환한다. `GET /api/latest_sensor_data/{id}` 요청 시 `Redis`에 캐시된 **최신 센서 값(JSON 객체)**을 즉시 반환한다.

   * `Flask` 서버는 필요시 `InfluxDB` 또는 `Redis`에서 데이터를 조회하여 JSON 형태로 응답한다.

3. **원격 제어 (API → MQTT)**

   * 사용자가 `Frontend`의 `ControlPanel`에서 제어 버튼(예: 워터펌프 ON)을 클릭한다.

   * `Frontend`는 `Flask`의 `/api/control_device/{id}` 엔드포인트로 **제어 명령이 담긴 JSON**을 POST한다. (예: `{"water_pump_action": 1, "water_pump_duration": 3}`)

   * `Flask` 서버는 이 요청 JSON을 검증한 후, 해당 기기의 `GreenEye/conf/{id}` 토픽으로 **동일하거나 유사한 JSON 페이로드**를 발행한다.

   * `SensorDevice` (ESP32)가 이 메시지를 수신하여 실제 액추에이터를 작동시킨다.

4. **AI 연동**

   * `Frontend`의 `ChatAssistant`가 이미지와 프롬프트를 `/api/chat/gemini`로 전송한다. 이 요청의 Body는 `{"prompt": "...", "image": "base64...", "conversation_id": "..."}` 형식의 JSON이다.

   * `Flask` 서버가 이 데이터를 받아 `Google Gemini API`를 호출하고, 응답(예: `{"answer": "..."}`)을 받아 사용자에게 JSON으로 다시 전달한다.

   * (PPTX 기반) 내부 `PyTorch` 모델을 통해 수집된 이미지를 주기적으로 분석하고, `SensorInfo`에 표시될 '약식 조언'을 생성하여 `Redis`에 저장한다.

## 주요 기능 및 API 엔드포인트

본 백엔드 서버(`app.py` 및 관련 모듈)는 다음과 같은 핵심 기능과 API를 제공한다.

### 1. MQTT 데이터 처리

* **MQTT Client Service:** `services/mqtt_client.py` (혹은 유사 모듈)

* `Mosquitto` 브로커를 구독하며, `GreenEye/data/+` 토픽으로 들어오는 모든 센서 단말의 JSON 데이터를 `InfluxDB`와 `Redis`에 실시간으로 파싱 및 저장한다.

* 이미지 데이터(Base64)를 디코딩하여 파일 스토리지(예: `/images`)에 저장하고, 관련 메타데이터를 DB에 기록한다.

### 2. RESTful API (Flask)

* **인증 (Auth)**

  * `POST /api/auth/register`: (Request Body: `{"email": "...", "password": "..."}`) 회원가입을 처리한다.

  * `POST /api/auth/login`: (Request Body: `{"email": "...", "password": "..."}`) 로그인을 처리하며, 성공 시 **JWT 토큰**을 JSON으로 반환한다. (Response Body: `{"access_token": "..."}`)

* **기기 관리 (Device)**

  * `POST /api/register_device`: (Request Body: `FormData` - `mac_address`, `friendly_name`, `image` 등) 새 센서 단말기(SD)를 등록한다. (Frontend의 `DeviceLink` 연동)

  * `GET /api/devices`: 현재 사용자가 등록한 모든 기기 목록을 **JSON 배열**로 반환한다. (`PlantGallery`용) (Response Body: `[{"deviceCode": "ge-sd-xxxx", "name": "...", "room": "...", "imageUrl": "..."}]`)

  * `DELETE /api/devices/{id}`: 특정 기기를 삭제한다.

* **데이터 조회 (Data)**

  * `GET /api/latest_sensor_data/{id}`: 특정 기기의 가장 최신 센서 값을 **JSON 객체**로 반환한다. (`SensorInfo`용) (Response Body: `{"values": {"temperature": {"value": 22, "status": "middle"}, ...}, "ai_diagnosis": "..."}`)

  * `GET /api/devices/{id}/latest-image`: 특정 기기가 촬영한 최신 이미지 정보(URL)를 **JSON**으로 반환한다. (Response Body: `{"image_url": "/api/images/...", "timestamp": "..."}`)

  * `GET /api/images/{...}`: 저장된 이미지 파일(JPEG)을 직접 반환한다. (Static File Serving)

* **기기 제어 (Control)**

  * `POST /api/control_device/{id}`: (Request Body: `{"humidifier_action": 1, ...}`) 특정 기기에 원격 제어 명령 JSON을 전송한다. (`ControlPanel` 연동)

* **AI 및 설정 (AI & Settings)**

  * `POST /api/chat/gemini`: (Request Body: `{"prompt": "...", "image": "base64...", "conversation_id": "..."}`) Gemini AI 챗봇 API 프록시 역할을 수행한다. (Response Body: `{"answer": "..."}`)

  * `GET /api/chat/history`: (Query: `?conversation_id=...`) 이전 대화 기록을 **JSON 배열**로 반환한다.

  * `GET /api/user/email-consent`: (Response Body: `{"email_consent": true}`) PDF 리포트 수신 동의 여부를 반환한다.

  * `PUT /api/user/email-consent`: (Request Body: `{"email_consent": false}`) 수신 동의 여부를 설정한다.

## 기술 스택

* **Backend Framework:** Python, Flask

* **Database:** InfluxDB (시계열 센서 데이터), Redis (최신 상태 캐시, 세션), PostgreSQL/SQLite (사용자, 기기 메타데이터)

* **Messaging:** Mosquitto (MQTT 브로커)

* **AI:** Google Gemini API (외부 연동), PyTorch (내부 이미지 추론 모델)

* **Deployment:** Docker, Docker Compose

* **Web Server / Proxy:** Nginx