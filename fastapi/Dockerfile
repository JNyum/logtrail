FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지 설치 (sqlite3 CLI, curl, vim 등)
RUN apt-get update && apt-get install -y \
    sqlite3 \
    curl \
    vim \
    nano \
    && rm -rf /var/lib/apt/lists/*

# Python 패키지 설치
# 기본: fastapi, uvicorn
# 추가: python-multipart (파일업로드), httpx (HTTP클라이언트), pytest (테스트), lxml (HTML 파싱)
RUN pip install --no-cache-dir \
    fastapi==0.109.0 \
    uvicorn[standard]==0.27.0 \
    python-multipart \
    httpx \
    pytest \
    pytest-asyncio \
    lxml

# 앱 코드 복사
COPY ./app /app

# 데이터 디렉토리 생성
RUN mkdir -p /data

# Uvicorn으로 FastAPI 실행
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
