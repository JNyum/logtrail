# fastapi/app/main.py
from fastapi import FastAPI, Request
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager
import os, re, json, hashlib
import asyncio
import httpx

DB_PATH = os.getenv("DB_PATH", "/data/playtime.db")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

app = FastAPI()

# 메모리 상태 관리: steamid <-> session_userid 매핑
pending_connections = {}  # {steamid: session_userid}
session_to_steam = {}     # {session_userid: (steamid, username, steam_name)}

def db():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_transaction():
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      steamid TEXT NOT NULL,
      session_userid TEXT NOT NULL,
      username TEXT NOT NULL,
      steam_name TEXT,
      date TEXT NOT NULL,
      connect_time TEXT NOT NULL,
      disconnect_time TEXT,
      playtime INTEGER NOT NULL DEFAULT 0,
      UNIQUE(session_userid, connect_time)
    );
    
    CREATE UNIQUE INDEX IF NOT EXISTS idx_open_session 
    ON sessions(session_userid, date) 
    WHERE disconnect_time IS NULL;
    
    CREATE INDEX IF NOT EXISTS idx_sessions_steamid_date 
    ON sessions(steamid, date);
    
    CREATE TABLE IF NOT EXISTS processed_logs (
      log_id TEXT PRIMARY KEY,
      processed_at TEXT NOT NULL,
      steamid TEXT,
      session_userid TEXT,
      action TEXT NOT NULL
    );
    
    CREATE INDEX IF NOT EXISTS idx_processed_logs_time 
    ON processed_logs(processed_at);
    """)
    conn.commit()
    conn.close()

@app.on_event("startup")
def _startup():
    init_db()

async def send_discord_notification(message: str, embed: dict = None):
    """Discord 웹훅으로 알림 전송"""
    if not DISCORD_WEBHOOK_URL:
        return
    
    try:
        async with httpx.AsyncClient() as client:
            payload = {"content": message}
            if embed:
                payload["embeds"] = [embed]
            
            response = await client.post(
                DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=5.0
            )
            if response.status_code != 204:
                print(f"Discord notification failed: {response.status_code}")
    except Exception as e:
        print(f"Discord notification error: {e}")

async def fetch_steam_name(steamid: str) -> str:
    """steamid.io에서 Steam 프로필 이름 가져오기"""
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://steamid.io/lookup/{steamid}"
            response = await client.get(url, timeout=10.0)
            
            if response.status_code == 200:
                from lxml import html
                tree = html.fromstring(response.content)
                
                # XPath로 Steam 이름 추출
                steam_name_elements = tree.xpath('/html/body/div/div[3]/div[2]/section/dl/dd[7]/')
                                                  
                if steam_name_elements:
                    return steam_name_elements[0].strip()
    except Exception as e:
        print(f"Failed to fetch Steam name for {steamid}: {e}")
    
    return None

def parse_steamid(raw: str):
    """Accepted connection from 76561198314730173"""
    m = re.search(r"Accepted connection from (\d+)", raw)
    return m.group(1) if m else None

def parse_session_userid_connect(raw: str):
    """Connected to userid:2806406146"""
    m = re.search(r"Connected to userid:(\d+)", raw)
    return m.group(1) if m else None

def parse_player_connected(raw: str):
    """[userid:2806406146] player dujjonku connected islocalplayer=False"""
    m = re.search(r"\[userid:(\d+)\] player (\S+) connected", raw)
    if m:
        return m.group(1), m.group(2)  # (session_userid, username)
    return None, None

def parse_disconnect(raw: str):
    """Disconnected from userid:2806406146 with reason App_Min"""
    m = re.search(r"Disconnected from userid:(\d+)", raw)
    return m.group(1) if m else None

def secs_between(hhmmss_a: str, hhmmss_b: str) -> int:
    a = datetime.strptime(hhmmss_a, "%H:%M:%S")
    b = datetime.strptime(hhmmss_b, "%H:%M:%S")
    return max(0, int((b - a).total_seconds()))

def close_row(conn, row_id: int, disconnect_time: str):
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM sessions WHERE id=?", (row_id,)).fetchone()
    if not row:
        return
    pt = secs_between(row["connect_time"], disconnect_time)
    cur.execute(
        "UPDATE sessions SET disconnect_time=?, playtime=? WHERE id=?",
        (disconnect_time, pt, row_id),
    )

def open_row(conn, steamid: str, session_userid: str, username: str, steam_name: str, d: str, ct: str):
    try:
        conn.execute(
            """INSERT INTO sessions(steamid, session_userid, username, steam_name, date, connect_time, disconnect_time, playtime) 
               VALUES(?,?,?,?,?,?,NULL,0)""",
            (steamid, session_userid, username, steam_name, d, ct),
        )
    except sqlite3.IntegrityError:
        pass

def get_open_row(conn, session_userid: str):
    return conn.execute(
        """SELECT * FROM sessions
           WHERE session_userid=? AND disconnect_time IS NULL
           ORDER BY date DESC, connect_time DESC
           LIMIT 1""",
        (session_userid,),
    ).fetchone()

def split_if_needed(conn, steamid: str, session_userid: str, username: str, steam_name: str, now_dt: datetime):
    """열린 row가 과거 날짜면 오늘까지 날짜 단위로 쪼개서 이어준다."""
    open_row_ = get_open_row(conn, session_userid)
    if not open_row_:
        return

    open_date = datetime.strptime(open_row_["date"], "%Y-%m-%d").date()
    today = now_dt.date()
    if open_date >= today:
        return

    # 1) 열린 row(과거)를 해당 날짜 23:59:59로 닫기
    close_row(conn, open_row_["id"], "23:59:59")

    # 2) open_date+1 ~ today 까지 중간 날짜 row 생성 (마지막(today)은 열어둔다)
    d = open_date + timedelta(days=1)
    while d <= today:
        ds = d.strftime("%Y-%m-%d")
        if d == today:
            open_row(conn, steamid, session_userid, username, steam_name, ds, "00:00:00")
            break
        else:
            open_row(conn, steamid, session_userid, username, steam_name, ds, "00:00:00")
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            close_row(conn, new_id, "23:59:59")
        d += timedelta(days=1)

def handle_accepted_connection(steamid: str):
    """Accepted connection from {steamid} - 1단계"""
    pending_connections[steamid] = None  # 아직 session_userid 모름
    return {"ok": True, "action": "pending", "steamid": steamid}

def handle_connected_to_userid(steamid: str, session_userid: str):
    """Connected to userid:{session_userid} - 2단계"""
    if steamid in pending_connections:
        pending_connections[steamid] = session_userid
    # 아직 username을 모르므로 대기
    return {"ok": True, "action": "mapped", "steamid": steamid, "session_userid": session_userid}

async def handle_player_connected(conn, session_userid: str, username: str, now_dt: datetime):
    """[userid:{session_userid}] player {username} connected - 3단계 (실제 connect)"""
    # session_userid로 steamid 찾기
    steamid = None
    for sid, sess_id in pending_connections.items():
        if sess_id == session_userid:
            steamid = sid
            break
    
    if not steamid:
        # pending에 없으면 이미 처리됐거나 순서가 꼬인 경우
        # session_to_steam에서 찾기 (재시도 시나리오)
        if session_userid in session_to_steam:
            steamid, _, _ = session_to_steam[session_userid]
        else:
            return {"ok": False, "error": "steamid not found for session", "session_userid": session_userid}
    
    # Steam 프로필 이름 가져오기 (비동기로 await)
    steam_name = await fetch_steam_name(steamid)
    
    # 메모리에 매핑 저장
    session_to_steam[session_userid] = (steamid, username, steam_name)
    
    # pending에서 제거
    if steamid in pending_connections:
        del pending_connections[steamid]
    
    # split 처리
    split_if_needed(conn, steamid, session_userid, username, steam_name, now_dt)
    
    # 오늘 이미 열린 세션 있는지 확인
    today = now_dt.strftime("%Y-%m-%d")
    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE session_userid=? AND date=? AND disconnect_time IS NULL LIMIT 1",
        (session_userid, today),
    ).fetchone()
    if existing:
        return {"ok": True, "note": "already connected", "session_userid": session_userid}
    
    # 새 세션 생성
    open_row(conn, steamid, session_userid, username, steam_name, today, now_dt.strftime("%H:%M:%S"))
    
    # Discord 알림용 표시 이름
    display_name = f"{username}({steam_name})" if steam_name else username
    
    # Discord 알림 전송 (비동기)
    asyncio.create_task(send_discord_notification(
        message=None,
        embed={
            "title": "🎮 플레이어 접속",
            "description": f"**{display_name}** 님이 서버에 접속했습니다!",
            "color": 0x00FF00,  # 초록색
            "fields": [
                {"name": "Steam ID", "value": f"`{steamid}`", "inline": True},
                {"name": "접속 시간", "value": now_dt.strftime("%H:%M:%S"), "inline": True}
            ],
            "timestamp": now_dt.isoformat()
        }
    ))
    
    return {"ok": True, "action": "connected", "steamid": steamid, "session_userid": session_userid}

def handle_disconnect(conn, session_userid: str, now_dt: datetime):
    """Disconnected from userid:{session_userid}"""
    # session_userid로 steamid, username, steam_name 찾기
    if session_userid not in session_to_steam:
        return {"ok": True, "note": "session not found in memory", "session_userid": session_userid}
    
    steamid, username, steam_name = session_to_steam[session_userid]
    
    # split 처리
    split_if_needed(conn, steamid, session_userid, username, steam_name, now_dt)
    
    # 열린 세션 찾아서 닫기
    row = get_open_row(conn, session_userid)
    if not row:
        return {"ok": True, "note": "no open session", "session_userid": session_userid}
    
    close_row(conn, row["id"], now_dt.strftime("%H:%M:%S"))
    
    # 플레이타임 계산 (방금 닫은 세션)
    updated_row = conn.execute("SELECT * FROM sessions WHERE id=?", (row["id"],)).fetchone()
    playtime_seconds = updated_row["playtime"] if updated_row else 0
    hours = playtime_seconds // 3600
    minutes = (playtime_seconds % 3600) // 60
    
    # Discord 알림용 표시 이름
    display_name = f"{username}({steam_name})" if steam_name else username
    
    # Discord 알림 전송 (비동기)
    asyncio.create_task(send_discord_notification(
        message=None,
        embed={
            "title": "👋 플레이어 퇴장",
            "description": f"**{display_name}** 님이 서버에서 나갔습니다.",
            "color": 0xFF0000,  # 빨간색
            "fields": [
                {"name": "Steam ID", "value": f"`{steamid}`", "inline": True},
                {"name": "퇴장 시간", "value": now_dt.strftime("%H:%M:%S"), "inline": True},
                {"name": "플레이 시간", "value": f"{hours}시간 {minutes}분", "inline": False}
            ],
            "timestamp": now_dt.isoformat()
        }
    ))
    
    # 메모리에서 제거
    del session_to_steam[session_userid]
    
    return {"ok": True, "action": "disconnected", "steamid": steamid, "session_userid": session_userid}

@app.post("/ingest")
async def ingest(req: Request):
    payload = await req.json()
    
    # Fluent Bit은 배열로 전송할 수 있음
    if isinstance(payload, list):
        results = []
        for item in payload:
            result = await process_single_log(item)
            results.append(result)
        return {"ok": True, "processed": len(results), "results": results}
    else:
        # 단일 객체 (수동 테스트용)
        return await process_single_log(payload)

async def process_single_log(payload: dict):
    """단일 로그 처리"""
    raw = payload.get("log", "")
    log_id = payload.get("log_id")
    
    # 로그 ID가 없으면 생성 (fallback)
    if not log_id:
        log_id = hashlib.md5(f"{raw}{datetime.now().isoformat()}".encode()).hexdigest()
    
    now_dt = datetime.now()
    
    # 1) Accepted connection from {steamid}
    steamid = parse_steamid(raw)
    if steamid:
        result = handle_accepted_connection(steamid)
        return {**result, "log_id": log_id}
    
    # 2) Connected to userid:{session_userid}
    session_userid = parse_session_userid_connect(raw)
    if session_userid:
        # pending_connections에서 가장 최근 steamid 찾기
        if pending_connections:
            latest_steamid = list(pending_connections.keys())[-1]
            result = handle_connected_to_userid(latest_steamid, session_userid)
            return {**result, "log_id": log_id}
        else:
            return {"ok": True, "note": "no pending connection", "log_id": log_id}
    
    # 3) [userid:{session_userid}] player {username} connected
    session_userid, username = parse_player_connected(raw)
    if session_userid and username:
        with db_transaction() as conn:
            # 이미 처리된 로그인지 확인
            existing = conn.execute(
                "SELECT 1 FROM processed_logs WHERE log_id=?",
                (log_id,)
            ).fetchone()
            
            if existing:
                return {"ok": True, "note": "already processed", "log_id": log_id}
            
            result = await handle_player_connected(conn, session_userid, username, now_dt)
            
            # 처리 완료 기록
            conn.execute(
                "INSERT INTO processed_logs(log_id, processed_at, steamid, session_userid, action) VALUES(?,?,?,?,?)",
                (log_id, now_dt.isoformat(), result.get("steamid"), session_userid, "connected")
            )
            
            return {**result, "log_id": log_id}
    
    # 4) Disconnected from userid:{session_userid}
    session_userid = parse_disconnect(raw)
    if session_userid:
        with db_transaction() as conn:
            # 이미 처리된 로그인지 확인
            existing = conn.execute(
                "SELECT 1 FROM processed_logs WHERE log_id=?",
                (log_id,)
            ).fetchone()
            
            if existing:
                return {"ok": True, "note": "already processed", "log_id": log_id}
            
            result = handle_disconnect(conn, session_userid, now_dt)
            
            # 처리 완료 기록
            conn.execute(
                "INSERT INTO processed_logs(log_id, processed_at, steamid, session_userid, action) VALUES(?,?,?,?,?)",
                (log_id, now_dt.isoformat(), result.get("steamid"), session_userid, "disconnected")
            )
            
            return {**result, "log_id": log_id}
    
    # 매칭되지 않은 로그
    return {"ok": True, "skip": "no pattern matched", "log_id": log_id}

@app.get("/health")
def health_check():
    """시스템 상태 확인"""
    try:
        conn = db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        
        return {
            "status": "healthy",
            "pending_connections": len(pending_connections),
            "active_sessions": len(session_to_steam),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@app.get("/stats")
def get_stats():
    """간단한 통계 조회"""
    try:
        conn = db()
        
        # 전체 플레이타임 (steamid별)
        total_playtime = conn.execute("""
            SELECT 
                steamid,
                MAX(username) as username,
                SUM(playtime) / 3600.0 as total_hours
            FROM sessions
            GROUP BY steamid
            ORDER BY total_hours DESC
        """).fetchall()
        
        # 오늘 접속 중인 유저
        today = datetime.now().strftime("%Y-%m-%d")
        active_today = conn.execute("""
            SELECT steamid, username, connect_time
            FROM sessions
            WHERE date=? AND disconnect_time IS NULL
        """, (today,)).fetchall()
        
        conn.close()
        
        return {
            "total_playtime": [dict(row) for row in total_playtime],
            "active_today": [dict(row) for row in active_today],
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }

@app.post("/send-daily-report")
async def send_daily_report():
    """일일 리포트를 Discord로 전송"""
    try:
        conn = db()
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        # 어제 통계
        yesterday_stats = conn.execute("""
            SELECT 
                steamid,
                MAX(username) as username,
                COUNT(*) as sessions,
                SUM(playtime) / 3600.0 as hours
            FROM sessions
            WHERE date = ?
            GROUP BY steamid
            ORDER BY hours DESC
        """, (yesterday,)).fetchall()
        
        # 전체 통계 (누적)
        total_stats = conn.execute("""
            SELECT 
                steamid,
                MAX(username) as username,
                SUM(playtime) / 3600.0 as total_hours
            FROM sessions
            GROUP BY steamid
            ORDER BY total_hours DESC
            LIMIT 5
        """).fetchall()
        
        conn.close()
        
        # Embed 생성
        fields = []
        
        # 어제 플레이타임
        if yesterday_stats:
            yesterday_text = "\n".join([
                f"**{row['username']}**: {row['hours']:.1f}시간 ({row['sessions']}세션)"
                for row in yesterday_stats
            ])
            fields.append({
                "name": f"📅 {yesterday} 플레이타임",
                "value": yesterday_text or "기록 없음",
                "inline": False
            })
        
        # 전체 순위 (Top 5)
        if total_stats:
            total_text = "\n".join([
                f"{i+1}. **{row['username']}**: {row['total_hours']:.1f}시간"
                for i, row in enumerate(total_stats)
            ])
            fields.append({
                "name": "🏆 전체 순위 (Top 5)",
                "value": total_text,
                "inline": False
            })
        
        # Discord 전송
        await send_discord_notification(
            message=None,
            embed={
                "title": "📊 Core Keeper 일일 리포트",
                "description": "어제 하루 활동 요약입니다!",
                "color": 0x0099FF,  # 파란색
                "fields": fields,
                "timestamp": datetime.now().isoformat(),
                "footer": {"text": "LogTrail Bot"}
            }
        )
        
        return {"ok": True, "message": "Daily report sent"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
