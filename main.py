"""
World Cup 2026 Private Draft — Backend
FastAPI + WebSocket, in-memory state (SQLite fallback on Railway).
Serves the frontend HTML and handles all tournament logic.
"""

import json
import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST_PASSWORD = os.getenv("HOST_PASSWORD", "admin2026")
DB_PATH = os.getenv("DB_PATH", "tournament.db")
PORT = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------------------
# Tier data
# ---------------------------------------------------------------------------

TIERS = {
    "tier1": ["Brazil","France","England","Argentina","Spain","Germany","Portugal","Belgium"],
    "tier2": ["Netherlands","Italy","Croatia","Uruguay","Denmark","Mexico","USA","Japan"],
    "tier3": ["Colombia","Morocco","Senegal","Switzerland","Poland","Sweden","Serbia","Iran"],
    "tier4": ["South Korea","Australia","Egypt","Nigeria","Cameroon","Ghana","Tunisia","Algeria"],
    "tier5": ["Peru","Chile","Ecuador","Paraguay","Venezuela","Bolivia","Costa Rica","Panama"],
    "tier6": ["Canada","Jamaica","Honduras","Saudi Arabia","Uzbekistan","Iraq","Qatar","New Zealand"],
}

AVATAR_COLORS = ["#00ff41","#00d4ff","#ffd700","#cc44ff","#ff8800","#ff2244","#00ff88","#ff44cc"]

# ---------------------------------------------------------------------------
# Database — SQLite for persistence across Railway restarts
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS players (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                avatar_color TEXT,
                avatar_initial TEXT,
                registered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS squads (
                player_id TEXT PRIMARY KEY,
                squad_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tournament (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Ensure draw_done key exists
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('draw_done', '0')")
        conn.commit()

def db_get_players():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM players ORDER BY registered_at").fetchall()
        return [dict(r) for r in rows]

def db_add_player(player):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO players (id,name,email,avatar_color,avatar_initial,registered_at) VALUES (?,?,?,?,?,?)",
            (player["id"], player["name"], player["email"],
             player["avatar_color"], player["avatar_initial"], player["registered_at"])
        )
        conn.commit()

def db_get_squads():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM squads").fetchall()
        return {r["player_id"]: json.loads(r["squad_json"]) for r in rows}

def db_save_squads(squads):
    with get_db() as conn:
        for pid, squad in squads.items():
            conn.execute(
                "INSERT OR REPLACE INTO squads (player_id, squad_json) VALUES (?,?)",
                (pid, json.dumps(squad))
            )
        conn.commit()

def db_is_draw_done():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM tournament WHERE key='draw_done'").fetchone()
        return row and row["value"] == "1"

def db_set_draw_done():
    with get_db() as conn:
        conn.execute("UPDATE tournament SET value='1' WHERE key='draw_done'")
        conn.commit()

def db_reset():
    with get_db() as conn:
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM squads")
        conn.execute("UPDATE tournament SET value='0' WHERE key='draw_done'")
        conn.commit()

# ---------------------------------------------------------------------------
# Draw algorithm — fair, random, no duplicates
# ---------------------------------------------------------------------------

def run_draw(players):
    squads = {p["id"]: {} for p in players}
    for tier, teams in TIERS.items():
        shuffled = teams[:]
        random.shuffle(shuffled)
        for i, player in enumerate(players):
            squads[player["id"]][tier] = shuffled[i]
    return squads

# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------

class WSManager:
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        msg = json.dumps(data)
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self.clients -= dead

ws_manager = WSManager()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="WC2026 Draft", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    name: str
    email: str

class LoginRequest(BaseModel):
    email: str

class HostRequest(BaseModel):
    password: str

# ---------------------------------------------------------------------------
# Helper — build full state snapshot
# ---------------------------------------------------------------------------

def full_state():
    players = db_get_players()
    squads = db_get_squads() if db_is_draw_done() else {}
    return {
        "players": players,
        "squads": squads,
        "draw_done": db_is_draw_done(),
        "count": len(players),
    }

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    return full_state()


@app.post("/api/register")
async def api_register(req: RegisterRequest):
    name = req.name.strip()
    email = req.email.strip().lower()

    if not name or not email or "@" not in email:
        raise HTTPException(400, "Invalid name or email")

    players = db_get_players()
    if any(p["email"] == email for p in players):
        raise HTTPException(409, "Email already registered")
    if len(players) >= 8:
        raise HTTPException(403, "Tournament is full (8/8)")

    player = {
        "id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "avatar_color": AVATAR_COLORS[len(players) % len(AVATAR_COLORS)],
        "avatar_initial": name[0].upper(),
        "registered_at": datetime.utcnow().isoformat(),
    }

    db_add_player(player)
    await ws_manager.broadcast({"type": "state_update", **full_state()})
    return {"player": player}


@app.post("/api/login")
def api_login(req: LoginRequest):
    email = req.email.strip().lower()
    players = db_get_players()
    player = next((p for p in players if p["email"] == email), None)
    if not player:
        raise HTTPException(404, "Email not found")
    return {"player": player}


@app.post("/api/draw")
async def api_draw(req: HostRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")

    players = db_get_players()
    if len(players) < 8:
        raise HTTPException(400, f"Need 8 players, only {len(players)} registered")
    if db_is_draw_done():
        raise HTTPException(409, "Draw already completed")

    squads = run_draw(players)
    db_save_squads(squads)
    db_set_draw_done()

    state = full_state()
    await ws_manager.broadcast({"type": "draw_complete", **state})
    return state


@app.post("/api/reset")
async def api_reset(req: HostRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    db_reset()
    state = full_state()
    await ws_manager.broadcast({"type": "state_update", **state})
    return {"ok": True}


@app.get("/api/health")
def api_health():
    players = db_get_players()
    return {"ok": True, "players": len(players), "draw_done": db_is_draw_done()}

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    # Send current state immediately on connect
    try:
        await ws.send_text(json.dumps({"type": "state_update", **full_state()}))
    except Exception:
        pass
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/")
def serve_frontend():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return FileResponse(html_path, media_type="text/html", headers={
            "Content-Security-Policy": (
                "default-src 'self' 'unsafe-inline' 'unsafe-eval' "
                "https://cdn.tailwindcss.com https://fonts.googleapis.com "
                "https://fonts.gstatic.com; "
                "img-src 'self' data:; "
                "connect-src 'self' wss: ws: https:;"
            )
        })
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
