import os
import hashlib
import random
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import libsql_client
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

# Lokalnie: TURSO_DATABASE_URL może wskazywać na plik (file:users.db)
# Na Render.com: libsql://impostor-antonithecodeprogrammer.aws-eu-west-1.turso.io
TURSO_URL   = os.environ.get("TURSO_DATABASE_URL", "file:users.db")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
ADMIN_SECRET = os.environ.get("ADMIN_SEED_SECRET", "inpostor-secret")

# Active WebSocket connections: username -> WebSocket
active_connections: dict[str, WebSocket] = {}


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_client() -> libsql_client.Client:
    return libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── Models ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str

class RoleBody(BaseModel):
    name: str
    description: str
    count: int = 1

class QuestBody(BaseModel):
    title: str
    description: str
    num_groups: int = 1

class ChallengeBody(BaseModel):
    title: str
    description: str
    reward: str

class ActivateTaskRequest(BaseModel):
    quest_id: int

class DrawChallengesRequest(BaseModel):
    challenge_ids: list[int]
    count: int

class SeedRequest(BaseModel):
    username: str
    password: str
    secret: str


# ── Helpers ───────────────────────────────────────────────────────────────────

async def send_to(username: str, payload: dict):
    ws = active_connections.get(username)
    if ws:
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            active_connections.pop(username, None)


async def get_all_usernames() -> list[str]:
    async with get_client() as client:
        result = await client.execute("SELECT username FROM users")
        return [row[0] for row in result.rows]


# ── User endpoints ────────────────────────────────────────────────────────────

@app.post("/login")
async def login(data: LoginRequest):
    async with get_client() as client:
        result = await client.execute(
            "SELECT id, username, password, role, challenge_id FROM users WHERE username = ?",
            [data.username],
        )

        if not result.rows:
            await client.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                [data.username, hash_pw(data.password)],
            )
            return {"status": "registered", "username": data.username, "role": None, "task": None, "challenge": None}

        user = row_to_dict(result.columns, result.rows[0])

        if user["password"] != hash_pw(data.password):
            raise HTTPException(status_code=401, detail="Złe hasło")

        task_result = await client.execute(
            "SELECT * FROM user_tasks WHERE username = ?", [data.username]
        )
        task = row_to_dict(task_result.columns, task_result.rows[0]) if task_result.rows else None

        challenge = None
        if user.get("challenge_id"):
            c_result = await client.execute(
                "SELECT * FROM challenges WHERE id = ?", [user["challenge_id"]]
            )
            if c_result.rows:
                challenge = row_to_dict(c_result.columns, c_result.rows[0])

        return {
            "status": "logged_in",
            "username": data.username,
            "role": user["role"],
            "task": task,
            "challenge": challenge,
        }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await websocket.accept()
    active_connections[username] = websocket
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.pop(username, None)


# ── Admin – login ─────────────────────────────────────────────────────────────

@app.post("/admin/login")
async def admin_login(data: LoginRequest):
    async with get_client() as client:
        result = await client.execute(
            "SELECT * FROM admins WHERE username = ?", [data.username]
        )
        if not result.rows:
            raise HTTPException(status_code=404, detail="Admin nie istnieje")
        admin = row_to_dict(result.columns, result.rows[0])
        if admin["password"] != hash_pw(data.password):
            raise HTTPException(status_code=401, detail="Złe hasło")
    return {"status": "logged_in", "username": data.username}


# ── Admin – users ─────────────────────────────────────────────────────────────

@app.get("/admin/users")
async def list_users():
    async with get_client() as client:
        result = await client.execute("""
            SELECT u.username, u.role, u.challenge_id,
                   ut.group_num, c.title as challenge_title
            FROM users u
            LEFT JOIN user_tasks ut ON u.username = ut.username
            LEFT JOIN challenges c ON u.challenge_id = c.id
        """)
    online = list(active_connections.keys())
    users = []
    for row in result.rows:
        r = row_to_dict(result.columns, row)
        users.append({
            "username": r["username"],
            "role": r["role"],
            "online": r["username"] in online,
            "group_num": r["group_num"],
            "challenge": r["challenge_title"],
        })
    return {"users": users}


@app.delete("/admin/users/{username}")
async def delete_user(username: str):
    async with get_client() as client:
        del_result = await client.execute(
            "DELETE FROM users WHERE username = ?", [username]
        )
        rows_affected = del_result.rows_affected
        await client.execute("DELETE FROM user_tasks WHERE username = ?", [username])

    if rows_affected == 0:
        raise HTTPException(status_code=404, detail="Użytkownik nie istnieje")

    ws = active_connections.pop(username, None)
    if ws:
        try:
            await ws.send_text(json.dumps({"type": "kicked"}))
            await ws.close()
        except Exception:
            pass

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
    return {"status": "deleted"}


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
        users_result = await client.execute("SELECT username FROM users")
        roles_result = await client.execute("SELECT * FROM roles")

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

    async with get_client() as client:
        for username, role in assignments.items():
            await client.execute(
                "UPDATE users SET role=? WHERE username=?", [role, username]
            )

    for username, role in assignments.items():
        await send_to(username, {"type": "role", "role": role})

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
    return {"id": challenge_id, **data.model_dump()}


@app.delete("/admin/challenges/{challenge_id}")
async def delete_challenge(challenge_id: int):
    async with get_client() as client:
        result = await client.execute(
            "DELETE FROM challenges WHERE id=?", [challenge_id]
        )
        rows_affected = result.rows_affected
        await client.execute(
            "UPDATE users SET challenge_id=NULL WHERE challenge_id=?", [challenge_id]
        )
    if rows_affected == 0:
        raise HTTPException(status_code=404, detail="Wyzwanie nie istnieje")
    return {"status": "deleted"}


# ── Admin – activate task ─────────────────────────────────────────────────────

@app.post("/admin/activate-task")
async def activate_task(req: ActivateTaskRequest):
    users = await get_all_usernames()
    if not users:
        raise HTTPException(status_code=400, detail="Brak graczy")

    async with get_client() as client:
        result = await client.execute(
            "SELECT * FROM quests WHERE id=?", [req.quest_id]
        )
    if not result.rows:
        raise HTTPException(status_code=404, detail="Zadanie nie istnieje")

    quest = row_to_dict(result.columns, result.rows[0])
    num_groups = quest["num_groups"] or 1
    shuffled = users[:]
    random.shuffle(shuffled)

    assignments = []
    for i, username in enumerate(shuffled):
        group_num = (i % num_groups) + 1
        assignments.append({"username": username, "group_num": group_num})

    async with get_client() as client:
        await client.execute("DELETE FROM user_tasks")
        for a in assignments:
            await client.execute(
                "INSERT INTO user_tasks (username, quest_id, quest_title, quest_description, group_num) "
                "VALUES (?, ?, ?, ?, ?)",
                [a["username"], quest["id"], quest["title"], quest["description"], a["group_num"]],
            )

    for a in assignments:
        await send_to(a["username"], {
            "type": "task",
            "title": quest["title"],
            "description": quest["description"],
            "group": a["group_num"],
            "num_groups": num_groups,
        })

    return {
        "status": "ok",
        "quest": quest["title"],
        "assignments": [{"username": a["username"], "group": a["group_num"]} for a in assignments],
    }


# ── Admin – draw challenges ───────────────────────────────────────────────────

@app.post("/admin/draw-challenges")
async def draw_challenges(req: DrawChallengesRequest):
    if req.count < 1:
        raise HTTPException(status_code=400, detail="Count musi być większe od 0")
    if not req.challenge_ids:
        raise HTTPException(status_code=400, detail="Brak wybranych wyzwań")

    async with get_client() as client:
        eligible_result = await client.execute(
            "SELECT username FROM users WHERE challenge_id IS NULL"
        )
        eligible_users = [row[0] for row in eligible_result.rows]

        if not eligible_users:
            raise HTTPException(status_code=400, detail="Wszyscy gracze mają już wyzwania")

        chosen_challenge_ids = random.choices(req.challenge_ids, k=req.count)
        num_to_assign = min(req.count, len(eligible_users))
        chosen_users = random.sample(eligible_users, num_to_assign)

        assignments = []
        for i in range(num_to_assign):
            c_id = chosen_challenge_ids[i]
            u = chosen_users[i]
            c_result = await client.execute(
                "SELECT * FROM challenges WHERE id=?", [c_id]
            )
            if not c_result.rows:
                continue
            c = row_to_dict(c_result.columns, c_result.rows[0])
            await client.execute(
                "UPDATE users SET challenge_id=? WHERE username=?", [c["id"], u]
            )
            assignments.append({"username": u, "challenge": c})

    for a in assignments:
        await send_to(a["username"], {"type": "challenge", "challenge": a["challenge"]})

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
    if data.secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Brak dostępu")
    async with get_client() as client:
        try:
            await client.execute(
                "INSERT INTO admins (username, password) VALUES (?, ?)",
                [data.username, hash_pw(data.password)],
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=409, detail="Admin już istnieje")
            raise
    return {"status": "created", "username": data.username}
