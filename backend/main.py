import os
import re
import hmac
import time
import json
import base64
import random
import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import libsql_client
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

# Lokalnie: TURSO_DATABASE_URL może wskazywać na plik (file:users.db)
# Na Render.com: libsql://impostor-antonithecodeprogrammer.aws-eu-west-1.turso.io
TURSO_URL   = os.environ.get("TURSO_DATABASE_URL") or "file:users.db"
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

# Sekret do założenia pierwszego konta admina. Dawna wartość domyślna była
# publiczna (repozytorium, README), więc jest traktowana jak jej brak.
_seed_secret = os.environ.get("ADMIN_SEED_SECRET", "")
ADMIN_SECRET = "" if _seed_secret == "impostor-secret" else _seed_secret

# Walidacja przy starcie – wyraźny komunikat zamiast cryptic 400
if TURSO_URL.startswith("libsql://") and not TURSO_TOKEN:
    raise RuntimeError(
        "TURSO_AUTH_TOKEN is not set! "
        "Add it in Render.com → Environment → Add Environment Variable."
    )


def _derive_token_key() -> bytes:
    explicit = os.environ.get("TOKEN_SECRET")
    if explicit:
        return explicit.encode()
    if TURSO_TOKEN:
        # Pochodna sekretu Turso – tokeny przeżywają restart serwera
        return hashlib.sha256(b"impostor-token-v1:" + TURSO_TOKEN.encode()).digest()
    # Lokalnie bez tokenu Turso: tokeny wygasają przy restarcie
    return secrets.token_bytes(32)


TOKEN_KEY = _derive_token_key()
ADMIN_TOKEN_TTL = 12 * 3600
USER_TOKEN_TTL = 3 * 24 * 3600

USERNAME_RE = re.compile(r"^[\w .\-]{1,24}$")

# Active WebSocket connections: username -> set of WebSockets (np. kilka kart)
active_connections: dict[str, set[WebSocket]] = {}
_last_ping: dict[str, float] = {}
# Połączenia panelu admina – dostają zmiany graczy i logów zamiast odpytywania
active_admin_connections: set[WebSocket] = set()


# ── DB helpers ────────────────────────────────────────────────────────────────

# Jedno współdzielone połączenie – otwieranie nowego przy każdym żądaniu
# (WebSocket do Turso) było głównym źródłem opóźnień.
_db: libsql_client.Client | None = None


@asynccontextmanager
async def get_client():
    yield _db


def row_to_dict(columns: list[str], row) -> dict:
    return dict(zip(columns, row))


async def init_db():
    async with get_client() as client:
        await client.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(id INTEGER PRIMARY KEY, username TEXT UNIQUE, "
            " password TEXT, role TEXT DEFAULT NULL, challenge_id INTEGER DEFAULT NULL)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS admins "
            "(id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS roles "
            "(id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, count INTEGER DEFAULT 1)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS quests "
            "(id INTEGER PRIMARY KEY, title TEXT, description TEXT, num_groups INTEGER DEFAULT 1)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS user_tasks "
            "(username TEXT PRIMARY KEY, quest_id INTEGER, quest_title TEXT, "
            " quest_description TEXT, group_num INTEGER)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS challenges "
            "(id INTEGER PRIMARY KEY, title TEXT, description TEXT, reward TEXT)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS user_rewards "
            "(id INTEGER PRIMARY KEY, username TEXT, challenge_title TEXT, reward TEXT, "
            " used INTEGER DEFAULT 0)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS user_pings "
            "(username TEXT PRIMARY KEY, pinged_at TEXT)"
        )
        await client.execute(
            "CREATE TABLE IF NOT EXISTS activity_log "
            "(id INTEGER PRIMARY KEY, created_at TEXT, username TEXT, action TEXT, detail TEXT)"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db
    _db = libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)
    try:
        await init_db()
        print(f"[DB] Connected to: {TURSO_URL}")
    except Exception as e:
        print(f"[DB] FATAL: Could not initialize database: {e}")
        await _db.close()
        raise
    try:
        yield
    finally:
        await _db.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

# Endpointy admina poza logowaniem i seedem wymagają tokenu admina.
# Middleware dodajemy przed CORS, żeby odpowiedzi 401 też miały nagłówki CORS.
PUBLIC_ADMIN_PATHS = {"/admin/login", "/admin/seed"}


@app.middleware("http")
async def require_admin_token(request: Request, call_next):
    path = request.url.path
    if (
        path.startswith("/admin/")
        and path not in PUBLIC_ADMIN_PATHS
        and request.method != "OPTIONS"
        and read_token(request.headers.get("x-admin-token", ""), "admin") is None
    ):
        return JSONResponse({"detail": "Brak dostępu – zaloguj się jako admin"}, status_code=401)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth helpers ──────────────────────────────────────────────────────────────

_PBKDF2_ITERATIONS = 200_000


def hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_pw(stored: str, password: str) -> tuple[bool, bool]:
    """Zwraca (hasło poprawne, czy hash wymaga aktualizacji do nowego formatu)."""
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt_hex, digest_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
            )
        except ValueError:
            return False, False
        return hmac.compare_digest(digest.hex(), digest_hex), False
    # Dawny format: SHA-256 bez soli
    legacy = hashlib.sha256(password.encode()).hexdigest()
    ok = hmac.compare_digest(legacy, stored or "")
    return ok, ok


async def hash_pw_async(password: str) -> str:
    return await asyncio.to_thread(hash_pw, password)


async def verify_pw_async(stored: str, password: str) -> tuple[bool, bool]:
    return await asyncio.to_thread(verify_pw, stored, password)


def _sign(payload: str) -> str:
    return hmac.new(TOKEN_KEY, payload.encode(), hashlib.sha256).hexdigest()


def make_token(kind: str, subject: str, ttl: int) -> str:
    body = {"k": kind, "s": subject, "e": int(time.time()) + ttl}
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def read_token(token: str, kind: str) -> str | None:
    """Zwraca podmiot tokenu (nazwę użytkownika/admina) albo None, gdy token jest nieprawidłowy."""
    try:
        payload, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if body["k"] != kind or body["e"] < time.time():
            return None
        return body["s"]
    except Exception:
        return None


async def current_user(request: Request) -> str:
    header = request.headers.get("authorization", "")
    username = read_token(header.removeprefix("Bearer ").strip(), "user")
    if username is None:
        raise HTTPException(status_code=401, detail="Sesja wygasła – zaloguj się ponownie")
    return username


# ── Models ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class RoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    count: int = Field(default=1, ge=1, le=500)

class ManualAssignBody(BaseModel):
    assignments: dict[str, str]

class QuestBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    num_groups: int = Field(default=1, ge=1, le=50)

class ChallengeBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    reward: str = Field(default="", max_length=200)

class ActivateTaskRequest(BaseModel):
    quest_id: int

class DrawChallengesRequest(BaseModel):
    challenge_ids: list[int]
    count: int = Field(ge=1)

class SeedRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    secret: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _drop_connection(username: str, ws: WebSocket):
    connections = active_connections.get(username)
    if connections:
        connections.discard(ws)
        if not connections:
            active_connections.pop(username, None)


async def send_to(username: str, payload: dict):
    text = json.dumps(payload)
    for ws in list(active_connections.get(username, ())):
        try:
            await ws.send_text(text)
        except Exception:
            _drop_connection(username, ws)


async def broadcast_admin(payload: dict):
    text = json.dumps(payload)
    for ws in list(active_admin_connections):
        try:
            await ws.send_text(text)
        except Exception:
            active_admin_connections.discard(ws)


def _in_clause(column: str, usernames: list[str] | None) -> tuple[str, list]:
    if usernames is None:
        return "", []
    return f" WHERE {column} IN ({','.join('?' * len(usernames))})", list(usernames)


async def admin_user_rows(client, usernames: list[str] | None = None) -> list[dict]:
    """Gracze w formacie panelu admina; usernames=None oznacza wszystkich graczy."""
    if usernames is not None and not usernames:
        return []
    u_where, u_args = _in_clause("u.username", usernames)
    r_where, r_args = _in_clause("username", usernames)
    result, rewards_result, pings_result = await client.batch([
        ("SELECT u.username, u.role, u.challenge_id, ut.group_num, c.title as challenge_title "
         "FROM users u "
         "LEFT JOIN user_tasks ut ON u.username = ut.username "
         "LEFT JOIN challenges c ON u.challenge_id = c.id" + u_where, u_args),
        ("SELECT username, reward, used FROM user_rewards" + r_where + " ORDER BY id", r_args),
        ("SELECT username FROM user_pings" + r_where, r_args),
    ])
    pinged = {row[0] for row in pings_result.rows}
    rewards_by_user: dict[str, list[dict]] = {}
    for row in rewards_result.rows:
        rw = row_to_dict(rewards_result.columns, row)
        rewards_by_user.setdefault(rw["username"], []).append(
            {"reward": rw["reward"], "used": bool(rw["used"])}
        )
    users = []
    for row in result.rows:
        r = row_to_dict(result.columns, row)
        users.append({
            "username": r["username"],
            "role": r["role"],
            "online": r["username"] in active_connections,
            "group_num": r["group_num"],
            "challenge": r["challenge_title"],
            "rewards": rewards_by_user.get(r["username"], []),
            "pinged": r["username"] in pinged,
        })
    return users


async def publish_users(usernames: list[str]):
    """Wysyła adminom aktualny stan wskazanych graczy. Gracze, których już nie ma, trafiają do users_removed."""
    usernames = list(dict.fromkeys(usernames))
    if not usernames or not active_admin_connections:
        return
    async with get_client() as client:
        users = await admin_user_rows(client, usernames)
    if users:
        await broadcast_admin({"type": "users_upsert", "users": users})
    found = {u["username"] for u in users}
    missing = [name for name in usernames if name not in found]
    if missing:
        await broadcast_admin({"type": "users_removed", "usernames": missing})


async def publish_log(log_id: int, created_at: str, username: str, action: str, detail: str):
    await broadcast_admin({"type": "log", "log": {
        "id": log_id, "created_at": created_at, "username": username,
        "action": action, "detail": detail,
    }})


async def build_state(client, username: str) -> dict | None:
    """Stan gracza (rola, zadanie, wyzwanie bez nagrody, nagrody) jednym zapytaniem."""
    user_rs, task_rs, challenge_rs, rewards_rs = await client.batch([
        ("SELECT role FROM users WHERE username = ?", [username]),
        ("SELECT * FROM user_tasks WHERE username = ?", [username]),
        # Nagroda jest ujawniana dopiero po zatwierdzeniu wykonania przez admina
        ("SELECT c.id, c.title, c.description FROM users u "
         "JOIN challenges c ON c.id = u.challenge_id WHERE u.username = ?", [username]),
        ("SELECT id, challenge_title, reward FROM user_rewards "
         "WHERE username = ? AND used = 0 ORDER BY id", [username]),
    ])
    if not user_rs.rows:
        return None
    return {
        "role": user_rs.rows[0][0],
        "task": row_to_dict(task_rs.columns, task_rs.rows[0]) if task_rs.rows else None,
        "challenge": row_to_dict(challenge_rs.columns, challenge_rs.rows[0]) if challenge_rs.rows else None,
        "rewards": [row_to_dict(rewards_rs.columns, r) for r in rewards_rs.rows],
    }


def public_challenge(challenge: dict) -> dict:
    return {k: v for k, v in challenge.items() if k != "reward"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def add_log(client, username: str, action: str, detail: str = ""):
    created_at = now_iso()
    result = await client.execute(
        "INSERT INTO activity_log (created_at, username, action, detail) VALUES (?, ?, ?, ?)",
        [created_at, username, action, detail],
    )
    await publish_log(result.last_insert_rowid, created_at, username, action, detail)


# ── User endpoints ────────────────────────────────────────────────────────────

@app.post("/login")
async def login(data: LoginRequest):
    username = data.username.strip()

    async with get_client() as client:
        result = await client.execute(
            "SELECT password FROM users WHERE username = ?", [username]
        )

        if not result.rows:
            if not USERNAME_RE.match(username):
                raise HTTPException(
                    status_code=400,
                    detail="Nazwa użytkownika: 1–24 znaki (litery, cyfry, spacja, _ . -)",
                )
            try:
                await client.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    [username, await hash_pw_async(data.password)],
                )
            except Exception as e:
                if "UNIQUE" in str(e):
                    raise HTTPException(status_code=409, detail="Nazwa jest już zajęta")
                raise
            status = "registered"
            await publish_users([username])
        else:
            ok, needs_upgrade = await verify_pw_async(result.rows[0][0], data.password)
            if not ok:
                raise HTTPException(status_code=401, detail="Złe hasło")
            if needs_upgrade:
                await client.execute(
                    "UPDATE users SET password = ? WHERE username = ?",
                    [await hash_pw_async(data.password), username],
                )
            status = "logged_in"

        state = await build_state(client, username)

    return {
        "status": status,
        "username": username,
        "token": make_token("user", username, USER_TOKEN_TTL),
        **state,
    }


@app.get("/me")
async def me(username: str = Depends(current_user)):
    async with get_client() as client:
        state = await build_state(client, username)
    if state is None:
        raise HTTPException(status_code=401, detail="Konto zostało usunięte")
    return {"username": username, **state}


@app.post("/rewards/{reward_id}/use")
async def use_reward(reward_id: int, username: str = Depends(current_user)):
    async with get_client() as client:
        found = await client.execute(
            "SELECT reward FROM user_rewards WHERE id = ? AND username = ? AND used = 0",
            [reward_id, username],
        )
        if not found.rows:
            raise HTTPException(status_code=404, detail="Nagroda nie istnieje lub została już użyta")
        # Warunek used = 0 zapobiega podwójnemu użyciu przy równoległych żądaniach
        updated = await client.execute(
            "UPDATE user_rewards SET used = 1 WHERE id = ? AND used = 0", [reward_id]
        )
        if updated.rows_affected == 0:
            raise HTTPException(status_code=404, detail="Nagroda nie istnieje lub została już użyta")
        await add_log(client, username, "reward_used", found.rows[0][0])
    await publish_users([username])
    return {"status": "used", "id": reward_id}


@app.post("/ping")
async def ping(username: str = Depends(current_user)):
    now = time.monotonic()
    if now - _last_ping.get(username, float("-inf")) < 2:
        raise HTTPException(status_code=429, detail="Za szybko – poczekaj chwilę")
    _last_ping[username] = now

    async with get_client() as client:
        exists = await client.execute(
            "SELECT 1 FROM users WHERE username = ?", [username]
        )
        if not exists.rows:
            raise HTTPException(status_code=401, detail="Konto zostało usunięte")
        ts = now_iso()
        _, log_result = await client.batch([
            ("INSERT INTO user_pings (username, pinged_at) VALUES (?, ?) "
             "ON CONFLICT(username) DO UPDATE SET pinged_at = excluded.pinged_at",
             [username, ts]),
            ("INSERT INTO activity_log (created_at, username, action, detail) VALUES (?, ?, 'ping', '')",
             [ts, username]),
        ])
    await publish_log(log_result.last_insert_rowid, ts, username, "ping", "")
    await publish_users([username])
    return {"status": "pinged"}


@app.get("/")
async def health():
    return {"status": "ok"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    await websocket.accept()

    username = read_token(token, "user")
    if username is not None:
        async with get_client() as client:
            exists = await client.execute("SELECT 1 FROM users WHERE username = ?", [username])
        if not exists.rows:
            username = None
    if username is None:
        await websocket.close(code=4401)
        return

    went_online = username not in active_connections
    active_connections.setdefault(username, set()).add(websocket)
    if went_online:
        await publish_users([username])
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _drop_connection(username, websocket)
        if username not in active_connections:
            await publish_users([username])


@app.websocket("/ws/admin")
async def admin_websocket_endpoint(websocket: WebSocket, token: str = ""):
    await websocket.accept()
    if read_token(token, "admin") is None:
        await websocket.close(code=4401)
        return

    active_admin_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_text(json.dumps({"type": "pong"}))
    except Exception:
        pass
    finally:
        active_admin_connections.discard(websocket)


# ── Admin – login ─────────────────────────────────────────────────────────────

@app.post("/admin/login")
async def admin_login(data: LoginRequest):
    async with get_client() as client:
        result = await client.execute(
            "SELECT password FROM admins WHERE username = ?", [data.username]
        )
        stored = result.rows[0][0] if result.rows else ""
        ok, needs_upgrade = await verify_pw_async(stored, data.password)
        if not result.rows or not ok:
            raise HTTPException(status_code=401, detail="Zły login lub hasło")
        if needs_upgrade:
            await client.execute(
                "UPDATE admins SET password = ? WHERE username = ?",
                [await hash_pw_async(data.password), data.username],
            )
    return {
        "status": "logged_in",
        "username": data.username,
        "token": make_token("admin", data.username, ADMIN_TOKEN_TTL),
    }


# ── Admin – users ─────────────────────────────────────────────────────────────

@app.get("/admin/users")
async def list_users():
    async with get_client() as client:
        users = await admin_user_rows(client)
    return {"users": users}


@app.post("/admin/users/{username}/clear-ping")
async def clear_ping(username: str):
    async with get_client() as client:
        await client.execute("DELETE FROM user_pings WHERE username = ?", [username])
    await publish_users([username])
    return {"status": "cleared"}


@app.get("/admin/logs")
async def list_logs(limit: int = 200):
    limit = max(1, min(limit, 1000))
    async with get_client() as client:
        result = await client.execute(
            "SELECT id, created_at, username, action, detail FROM activity_log "
            "ORDER BY id DESC LIMIT ?",
            [limit],
        )
    return {"logs": [row_to_dict(result.columns, r) for r in result.rows]}


@app.delete("/admin/users/{username}")
async def delete_user(username: str):
    async with get_client() as client:
        results = await client.batch([
            ("DELETE FROM users WHERE username = ?", [username]),
            ("DELETE FROM user_tasks WHERE username = ?", [username]),
            ("DELETE FROM user_rewards WHERE username = ?", [username]),
            ("DELETE FROM user_pings WHERE username = ?", [username]),
        ])

    if results[0].rows_affected == 0:
        raise HTTPException(status_code=404, detail="Użytkownik nie istnieje")

    _last_ping.pop(username, None)
    for ws in list(active_connections.pop(username, ())):
        try:
            await ws.send_text(json.dumps({"type": "kicked"}))
            await ws.close()
        except Exception:
            pass

    await publish_users([username])
    return {"status": "deleted", "username": username}


@app.delete("/admin/users/{username}/challenge")
async def delete_user_challenge(username: str):
    async with get_client() as client:
        result = await client.execute(
            "UPDATE users SET challenge_id = NULL WHERE username = ?", [username]
        )
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Użytkownik nie istnieje")

    await send_to(username, {"type": "challenge_removed"})
    await publish_users([username])
    return {"status": "deleted"}


@app.post("/admin/users/{username}/complete-challenge")
async def complete_user_challenge(username: str):
    async with get_client() as client:
        # Jedna transakcja: dwa równoległe kliknięcia nie przyznają nagrody dwa razy
        inserted, _ = await client.batch([
            ("INSERT INTO user_rewards (username, challenge_title, reward) "
             "SELECT u.username, c.title, c.reward FROM users u "
             "JOIN challenges c ON c.id = u.challenge_id WHERE u.username = ?", [username]),
            ("UPDATE users SET challenge_id = NULL WHERE username = ?", [username]),
        ])
        if inserted.rows_affected == 0:
            raise HTTPException(status_code=404, detail="Gracz nie ma aktywnego wyzwania")
        created = await client.execute(
            "SELECT id, challenge_title, reward FROM user_rewards WHERE id = ?",
            [inserted.last_insert_rowid],
        )

    reward = row_to_dict(created.columns, created.rows[0])
    await send_to(username, {"type": "challenge_removed"})
    await send_to(username, {"type": "reward", "reward": reward})
    await publish_users([username])
    return {"status": "completed", "reward": reward}


# ── Admin – roles ─────────────────────────────────────────────────────────────

@app.get("/admin/roles")
async def list_roles():
    async with get_client() as client:
        result = await client.execute("SELECT * FROM roles")
    return {"roles": [row_to_dict(result.columns, r) for r in result.rows]}


@app.post("/admin/roles")
async def create_role(data: RoleBody):
    async with get_client() as client:
        try:
            result = await client.execute(
                "INSERT INTO roles (name, description, count) VALUES (?, ?, ?)",
                [data.name, data.description, data.count],
            )
            return {"id": result.last_insert_rowid, **data.model_dump()}
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=409, detail="Rola już istnieje")
            raise


@app.put("/admin/roles/{role_id}")
async def update_role(role_id: int, data: RoleBody):
    async with get_client() as client:
        result = await client.execute(
            "UPDATE roles SET name=?, description=?, count=? WHERE id=?",
            [data.name, data.description, data.count, role_id],
        )
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Rola nie istnieje")
    return {"id": role_id, **data.model_dump()}


@app.delete("/admin/roles/{role_id}")
async def delete_role(role_id: int):
    async with get_client() as client:
        result = await client.execute("DELETE FROM roles WHERE id=?", [role_id])
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Rola nie istnieje")
    return {"status": "deleted"}


# ── Admin – assign roles ──────────────────────────────────────────────────────

@app.post("/admin/assign-roles")
async def assign_roles():
    async with get_client() as client:
        users_result, roles_result = await client.batch([
            "SELECT username FROM users",
            "SELECT * FROM roles",
        ])

    users = [row[0] for row in users_result.rows]
    roles_rows = [row_to_dict(roles_result.columns, r) for r in roles_result.rows]

    if not users:
        raise HTTPException(status_code=400, detail="Brak graczy")

    pool: list[str] = []
    for r in roles_rows:
        pool.extend([r["name"]] * r["count"])

    if len(pool) > len(users):
        raise HTTPException(
            status_code=400,
            detail=f"Za dużo ról ({len(pool)}) dla {len(users)} graczy",
        )

    shuffled = users[:]
    random.shuffle(shuffled)
    assignments: dict[str, str] = {}
    for i, username in enumerate(shuffled):
        assignments[username] = pool[i] if i < len(pool) else "Cywil"

    await save_roles(assignments)
    return {"assignments": assignments}


async def save_roles(assignments: dict[str, str]):
    """Zapisuje role wszystkim naraz (jedna transakcja) i powiadamia graczy."""
    async with get_client() as client:
        await client.batch([
            ("UPDATE users SET role=? WHERE username=?", [role, username])
            for username, role in assignments.items()
        ])

    for username, role in assignments.items():
        await send_to(username, {"type": "role", "role": role})
    await publish_users(list(assignments))


@app.post("/admin/assign-roles-manual")
async def assign_roles_manual(data: ManualAssignBody):
    async with get_client() as client:
        users_result, roles_result = await client.batch([
            "SELECT username FROM users",
            "SELECT name, count FROM roles",
        ])

    users = [row[0] for row in users_result.rows]
    role_counts = {row[0]: row[1] for row in roles_result.rows}

    for username, role in data.assignments.items():
        if username not in users:
            raise HTTPException(status_code=404, detail=f"Użytkownik {username} nie istnieje")
        if role not in role_counts:
            raise HTTPException(status_code=404, detail=f"Rola {role} nie istnieje")

    for role, limit in role_counts.items():
        used = sum(1 for r in data.assignments.values() if r == role)
        if used > limit:
            raise HTTPException(
                status_code=400, detail=f"Za dużo graczy w roli {role} ({used}/{limit})"
            )

    # Gracze bez wybranej roli dostają "Cywil" – tak samo jak przy losowaniu
    assignments = {u: data.assignments.get(u, "Cywil") for u in users}

    await save_roles(assignments)
    return {"assignments": assignments}


# ── Admin – quests CRUD ───────────────────────────────────────────────────────

@app.get("/admin/quests")
async def list_quests():
    async with get_client() as client:
        result = await client.execute("SELECT * FROM quests")
    return {"quests": [row_to_dict(result.columns, r) for r in result.rows]}


@app.post("/admin/quests")
async def create_quest(data: QuestBody):
    async with get_client() as client:
        result = await client.execute(
            "INSERT INTO quests (title, description, num_groups) VALUES (?, ?, ?)",
            [data.title, data.description, data.num_groups],
        )
        return {"id": result.last_insert_rowid, **data.model_dump()}


@app.put("/admin/quests/{quest_id}")
async def update_quest(quest_id: int, data: QuestBody):
    async with get_client() as client:
        result = await client.execute(
            "UPDATE quests SET title=?, description=?, num_groups=? WHERE id=?",
            [data.title, data.description, data.num_groups, quest_id],
        )
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje")
    return {"id": quest_id, **data.model_dump()}


@app.delete("/admin/quests/{quest_id}")
async def delete_quest(quest_id: int):
    async with get_client() as client:
        result = await client.execute("DELETE FROM quests WHERE id=?", [quest_id])
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje")
    return {"status": "deleted"}


# ── Admin – challenges CRUD ───────────────────────────────────────────────────

@app.get("/admin/challenges")
async def list_challenges():
    async with get_client() as client:
        result = await client.execute("SELECT * FROM challenges")
    return {"challenges": [row_to_dict(result.columns, r) for r in result.rows]}


@app.post("/admin/challenges")
async def create_challenge(data: ChallengeBody):
    async with get_client() as client:
        result = await client.execute(
            "INSERT INTO challenges (title, description, reward) VALUES (?, ?, ?)",
            [data.title, data.description, data.reward],
        )
        return {"id": result.last_insert_rowid, **data.model_dump()}


@app.put("/admin/challenges/{challenge_id}")
async def update_challenge(challenge_id: int, data: ChallengeBody):
    async with get_client() as client:
        result = await client.execute(
            "UPDATE challenges SET title=?, description=?, reward=? WHERE id=?",
            [data.title, data.description, data.reward, challenge_id],
        )
    if result.rows_affected == 0:
        raise HTTPException(status_code=404, detail="Wyzwanie nie istnieje")
    async with get_client() as client:
        holders = await client.execute("SELECT username FROM users WHERE challenge_id=?", [challenge_id])
    await publish_users([row[0] for row in holders.rows])
    return {"id": challenge_id, **data.model_dump()}


@app.delete("/admin/challenges/{challenge_id}")
async def delete_challenge(challenge_id: int):
    async with get_client() as client:
        holders = await client.execute(
            "SELECT username FROM users WHERE challenge_id=?", [challenge_id]
        )
        results = await client.batch([
            ("DELETE FROM challenges WHERE id=?", [challenge_id]),
            ("UPDATE users SET challenge_id=NULL WHERE challenge_id=?", [challenge_id]),
        ])
    if results[0].rows_affected == 0:
        raise HTTPException(status_code=404, detail="Wyzwanie nie istnieje")

    # Gracze, którzy mieli to wyzwanie, od razu przestają je widzieć
    for row in holders.rows:
        await send_to(row[0], {"type": "challenge_removed"})
    await publish_users([row[0] for row in holders.rows])
    return {"status": "deleted"}


# ── Admin – activate task ─────────────────────────────────────────────────────

@app.post("/admin/activate-task")
async def activate_task(req: ActivateTaskRequest):
    async with get_client() as client:
        users_result, quest_result = await client.batch([
            "SELECT username FROM users",
            ("SELECT * FROM quests WHERE id=?", [req.quest_id]),
        ])
    users = [row[0] for row in users_result.rows]
    if not users:
        raise HTTPException(status_code=400, detail="Brak graczy")
    if not quest_result.rows:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje")

    quest = row_to_dict(quest_result.columns, quest_result.rows[0])
    num_groups = quest["num_groups"] or 1
    shuffled = users[:]
    random.shuffle(shuffled)

    assignments = []
    for i, username in enumerate(shuffled):
        group_num = (i % num_groups) + 1
        assignments.append({"username": username, "group_num": group_num})

    # Jedna transakcja: w razie błędu gracze nie zostają bez zadania
    async with get_client() as client:
        await client.batch([
            "DELETE FROM user_tasks",
            *[
                ("INSERT INTO user_tasks (username, quest_id, quest_title, quest_description, group_num) "
                 "VALUES (?, ?, ?, ?, ?)",
                 [a["username"], quest["id"], quest["title"], quest["description"], a["group_num"]])
                for a in assignments
            ],
        ])

    for a in assignments:
        await send_to(a["username"], {
            "type": "task",
            "title": quest["title"],
            "description": quest["description"],
            "group": a["group_num"],
            "num_groups": num_groups,
        })

    await publish_users(users)
    return {
        "status": "ok",
        "quest": quest["title"],
        "assignments": [{"username": a["username"], "group": a["group_num"]} for a in assignments],
    }


# ── Admin – draw challenges ───────────────────────────────────────────────────

@app.post("/admin/draw-challenges")
async def draw_challenges(req: DrawChallengesRequest):
    if not req.challenge_ids:
        raise HTTPException(status_code=400, detail="Brak wybranych wyzwań")

    placeholders = ",".join("?" * len(req.challenge_ids))
    async with get_client() as client:
        eligible_result, challenges_result = await client.batch([
            "SELECT username FROM users WHERE challenge_id IS NULL",
            (f"SELECT * FROM challenges WHERE id IN ({placeholders})", list(req.challenge_ids)),
        ])
    eligible_users = [row[0] for row in eligible_result.rows]
    challenges = [row_to_dict(challenges_result.columns, r) for r in challenges_result.rows]

    if not challenges:
        raise HTTPException(status_code=404, detail="Wybrane wyzwania nie istnieją")
    if not eligible_users:
        raise HTTPException(status_code=400, detail="Wszyscy gracze mają już wyzwania")

    num_to_assign = min(req.count, len(eligible_users))
    chosen_users = random.sample(eligible_users, num_to_assign)
    assignments = [
        {"username": u, "challenge": random.choice(challenges)} for u in chosen_users
    ]

    async with get_client() as client:
        await client.batch([
            ("UPDATE users SET challenge_id=? WHERE username=?", [a["challenge"]["id"], a["username"]])
            for a in assignments
        ])

    for a in assignments:
        await send_to(a["username"], {"type": "challenge", "challenge": public_challenge(a["challenge"])})
    await publish_users([a["username"] for a in assignments])

    return {
        "status": "ok",
        "assignments": [
            {"username": a["username"], "challenge": a["challenge"]["title"]}
            for a in assignments
        ],
    }


# ── Admin seed ────────────────────────────────────────────────────────────────

@app.post("/admin/seed")
async def seed_admin(data: SeedRequest):
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Tworzenie admina wyłączone – ustaw ADMIN_SEED_SECRET (inny niż domyślny)",
        )
    if not hmac.compare_digest(data.secret.encode(), ADMIN_SECRET.encode()):
        raise HTTPException(status_code=403, detail="Brak dostępu")
    async with get_client() as client:
        try:
            await client.execute(
                "INSERT INTO admins (username, password) VALUES (?, ?)",
                [data.username, await hash_pw_async(data.password)],
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=409, detail="Admin już istnieje")
            raise
    return {"status": "created", "username": data.username}
