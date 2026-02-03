# LogTrail - Core Keeper Playtime Tracker

Core Keeper 서버(linux)의 플레이어 접속/퇴장 로그를 실시간으로 추적하여 플레이타임을 기록하고 Discord 알림을 보내는 시스템입니다.

## 🎮 주요 기능

- **실시간 로그 추적**: Fluent Bit을 통한 Core Keeper 서버 로그 실시간 모니터링
- **플레이타임 기록**: SQLite 데이터베이스에 플레이어별 세션 기록 저장
- **Discord 알림**: 플레이어 접속/퇴장 시 Discord 웹훅으로 실시간 알림
- **날짜별 세션 관리**: 자정을 넘긴 세션 자동 분할 처리
- **통계 API**: 플레이타임 통계 조회 및 일일 리포트 생성
- **스팀 닉네임**: 스팀 닉네임 가져오기(오류 수정 중)

## 📋 시스템 구조

```
┌─────────────────┐
│  Core Keeper    │
│  Server Log     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Fluent Bit    │  ◄── 로그 필터링 & 파싱
└────────┬────────┘
         │ HTTP
         ▼
┌─────────────────┐
│    FastAPI      │  ◄── 로그 처리 & DB 저장
└────────┬────────┘
         │
         ├──► SQLite (playtime.db)
         └──► Discord Webhook
```

## 🚀 빠른 시작

### 1. 필수 요구사항

- Docker & Docker Compose
- Core Keeper Dedicated Server
- Discord Webhook URL

### 2. 설정

1. **프로젝트 클론**
   ```bash
   git clone <repository-url>
   cd logtrail
   ```

2. **디렉토리 구조 생성**
   ```bash
   mkdir -p data fluent_bit fastapi/app
   ```

3. **Core Keeper 로그 심볼릭 링크 생성**
   ```bash
   # example path: /home/corekeeper/.steam/steam/steamapps/common/Core Keeper Dedicated Server/CoreKeeperServerLog.txt
   ln -s "<LOG_PATH>" ./corekeeper_log
   ```

4. **환경 변수 설정** (`docker-compose.yml` 수정)
   ```yaml
   environment:
     - DISCORD_WEBHOOK_URL=<YOUR_WEBHOOK_URL>
   ```

5. **Docker 네트워크 생성**
   ```bash
   docker network create log_trail
   ```

### 3. 실행

```bash
docker-compose up -d
```

### 4. 상태 확인

```bash
# 헬스 체크
curl http://localhost:8000/health

# 통계 조회
curl http://localhost:8000/stats
```

## 📁 프로젝트 구조

```
logtrail/
├── docker-compose.yml          # Docker Compose 설정
├── corekeeper_log -> ...       # Core Keeper 로그 심볼릭 링크
├── data/
│   └── playtime.db            # SQLite 데이터베이스
├── fastapi/
│   ├── Dockerfile             # FastAPI 컨테이너 이미지
│   └── app/
│       └── main.py            # FastAPI 애플리케이션
└── fluent_bit/
    ├── fluent-bit.yaml        # Fluent Bit 설정
    └── parsers.conf           # 로그 파서 설정
```

## 🔧 API 엔드포인트

### `POST /ingest`
Fluent Bit에서 전송되는 로그 수신 및 처리

### `GET /health`
시스템 상태 확인
```json
{
  "status": "healthy",
  "pending_connections": 0,
  "active_sessions": 2,
  "timestamp": "2024-01-01T12:00:00"
}
```

### `GET /stats`
플레이타임 통계 조회
```json
{
  "total_playtime": [
    {
      "steamid": "76561198314730173",
      "username": "player1",
      "total_hours": 42.5
    }
  ],
  "active_today": [
    {
      "steamid": "76561198314730173",
      "username": "player1",
      "connect_time": "14:30:00"
    }
  ]
}
```

### `POST /send-daily-report`
Discord로 일일 리포트 전송

## 📊 데이터베이스 스키마

### `sessions` 테이블
```sql
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  steamid TEXT NOT NULL,            -- Steam ID (76561198...)
  session_userid TEXT NOT NULL,     -- 세션 ID
  username TEXT NOT NULL,           -- 게임 내 닉네임
  steam_name TEXT,                  -- Steam 프로필 이름
  date TEXT NOT NULL,               -- 날짜 (YYYY-MM-DD)
  connect_time TEXT NOT NULL,       -- 접속 시각 (HH:MM:SS)
  disconnect_time TEXT,             -- 퇴장 시각 (HH:MM:SS)
  playtime INTEGER NOT NULL         -- 플레이 시간 (초)
);
```

### `processed_logs` 테이블
```sql
CREATE TABLE processed_logs (
  log_id TEXT PRIMARY KEY,         -- 로그 ID (중복 방지)
  processed_at TEXT NOT NULL,       -- 처리 시각
  steamid TEXT,                     -- Steam ID
  session_userid TEXT,              -- 세션 ID
  action TEXT NOT NULL              -- 액션 (connected/disconnected)
);
```

## 🔍 로그 처리 흐름

Core Keeper 서버는 다음과 같은 3단계 접속 로그를 생성합니다:

1. **Steam 인증**: `Accepted connection from {steamid}`
2. **세션 생성**: `Connected to userid:{session_userid}`
3. **플레이어 입장**: `[userid:{session_userid}] player {username} connected`

LogTrail은 이 3단계를 메모리에서 매칭하여 Steam ID, 세션 ID, 게임 닉네임을 연결합니다.

### 퇴장 로그
- `Disconnected from userid:{session_userid}`

## 🔔 Discord 알림

### 접속 알림
- 플레이어 이름 (Steam 프로필 이름 + 게임 닉네임)
- Steam ID
- 접속 시각

### 퇴장 알림
- 플레이어 이름
- Steam ID
- 퇴장 시각
- 플레이 시간 (시간/분)

### 일일 리포트
- 전날 플레이타임 요약
- 전체 누적 순위 (Top 5)


**기타 설정**
### Fluent Bit 로그 레벨 변경
`fluent_bit/fluent-bit.yaml`:
```yaml
service:
  log_level: debug  # info, debug, trace
```

### 플레이타임 자정 분할
자정을 넘긴 세션은 자동으로 날짜별로 분할되어 기록됩니다:
- 당일 23:59:59에 세션 종료
- 다음 날 00:00:00에 새 세션 시작

### 로그 중복 방지
각 로그에는 고유한 `log_id`가 부여되며, `processed_logs` 테이블에서 중복 처리를 방지합니다.

## 🐛 트러블슈팅

### 로그가 수집되지 않는 경우
1. Core Keeper 로그 파일 경로 확인
   ```bash
   ls -la corekeeper_log
   ```

2. Fluent Bit 로그 확인
   ```bash
   docker logs logtrail_fluentbit
   ```

3. FastAPI 로그 확인
   ```bash
   docker logs logtrail_fastapi
   ```

### Discord 알림이 오지 않는 경우
1. Webhook URL 확인
2. FastAPI 환경 변수 확인
   ```bash
   docker exec logtrail_fastapi env | grep DISCORD
   ```

### 데이터베이스 오류
```bash
# 데이터베이스 초기화 (주의: 모든 데이터 삭제)
docker-compose down
rm data/playtime.db
docker-compose up -d
```

## 📝 Cron 설정 (일일 리포트)

매일 오전 9시에 일일 리포트를 자동 전송

```bash
crontab -e
```

다음 라인 추가:
```bash
0 9 * * * curl -X POST http://localhost:8000/send-daily-report
```