"""
World Cup 2026 Private Draft — Backend
FastAPI + WebSocket, in-memory state (SQLite fallback on Railway).
Serves the frontend HTML and handles all tournament logic.
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

log = logging.getLogger("wc2026")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HOST_PASSWORD = os.getenv("HOST_PASSWORD", "")
if not HOST_PASSWORD:
    import warnings
    warnings.warn("HOST_PASSWORD env var not set — using insecure default. Set it on Railway.", stacklevel=1)
    HOST_PASSWORD = "admin2026"
DB_PATH = os.getenv("DB_PATH", "/data/tournament.db" if os.path.isdir("/data") else "tournament.db")
PORT = int(os.getenv("PORT", "8000"))
FD_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
POLL_INTERVAL = int(os.getenv("BRACKET_POLL_SECONDS", "180"))  # 3 min default

# ---------------------------------------------------------------------------
# Tier data
# ---------------------------------------------------------------------------

TIERS = {
    "tier1": ["France","Spain","Argentina","England","Portugal","Brazil","Germany","Netherlands","Belgium","Morocco","Uruguay","Colombia"],
    "tier2": ["Croatia","Japan","Senegal","Switzerland","USA","Mexico","Norway","Sweden","Austria","Turkey","Ecuador","South Korea"],
    "tier3": ["Iran","Australia","Egypt","Ivory Coast","Scotland","Bosnia and Herzegovina","Czechia","Algeria","Tunisia","Ghana","South Africa","DR Congo"],
    "tier4": ["Paraguay","Canada","Panama","Uzbekistan","Qatar","Iraq","Saudi Arabia","Jordan","Cape Verde","New Zealand","Curaçao","Haiti"],
}

AVATAR_COLORS = ["#00ff41","#00d4ff","#ffd700","#cc44ff","#ff8800","#ff2244","#00ff88","#ff44cc",
                 "#44ffff","#ff6622","#88ff00","#ff0088"]

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
            CREATE TABLE IF NOT EXISTS chat (
                id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                avatar_color TEXT NOT NULL,
                message TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
        """)
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('draw_done', '0')")
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('finalist_1', '')")
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('finalist_2', '')")
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('champion', '')")
        conn.execute("INSERT OR IGNORE INTO tournament VALUES ('bracket', '')")
        conn.commit()

def db_get_players():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM players ORDER BY registered_at").fetchall()
        return [dict(r) for r in rows]

def db_add_player(player):
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO players (id,name,email,avatar_color,avatar_initial,registered_at) VALUES (?,?,?,?,?,?)",
                (player["id"], player["name"], player["email"],
                 player["avatar_color"], player["avatar_initial"], player["registered_at"])
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Email already registered")

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

def db_get_finals():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM tournament WHERE key IN ('finalist_1','finalist_2','champion')").fetchall()
        data = {r["key"]: r["value"] for r in rows}
        return {
            "finalist_1": data.get("finalist_1", ""),
            "finalist_2": data.get("finalist_2", ""),
            "champion":   data.get("champion", ""),
        }

def db_set_finals(f1: str, f2: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tournament VALUES ('finalist_1', ?)", (f1,))
        conn.execute("INSERT OR REPLACE INTO tournament VALUES ('finalist_2', ?)", (f2,))
        conn.execute("INSERT OR REPLACE INTO tournament VALUES ('champion', '')")
        conn.commit()

def db_set_champion(team: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tournament VALUES ('champion', ?)", (team,))
        conn.commit()

def _empty_bracket():
    def matches(n): return [{"home":"","away":"","winner":""} for _ in range(n)]
    return {"r32": matches(16), "r16": matches(8), "qf": matches(4), "sf": matches(2), "final": matches(1)}

# Maps (round, idx) → (next_round, next_idx, "home"|"away")
_ADVANCE = {}
for _i in range(16): _ADVANCE[("r32",_i)] = ("r16", _i//2, "home" if _i%2==0 else "away")
for _i in range(8):  _ADVANCE[("r16",_i)] = ("qf",  _i//2, "home" if _i%2==0 else "away")
for _i in range(4):  _ADVANCE[("qf", _i)] = ("sf",  _i//2, "home" if _i%2==0 else "away")
for _i in range(2):  _ADVANCE[("sf", _i)] = ("final",   0, "home" if _i%2==0 else "away")

def db_get_bracket():
    with get_db() as conn:
        row = conn.execute("SELECT value FROM tournament WHERE key='bracket'").fetchone()
        if row and row["value"]:
            return json.loads(row["value"])
        return _empty_bracket()

def db_save_bracket(bracket):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tournament VALUES ('bracket', ?)", (json.dumps(bracket),))
        conn.commit()

def db_reset():
    with get_db() as conn:
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM squads")
        conn.execute("UPDATE tournament SET value='0' WHERE key='draw_done'")
        conn.execute("UPDATE tournament SET value='' WHERE key IN ('finalist_1','finalist_2','champion')")
        conn.execute("UPDATE tournament SET value='' WHERE key='bracket'")
        conn.commit()


def db_reset_draw_only():
    """Clear squads + draw flag, keep player registrations intact."""
    with get_db() as conn:
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

# ---------------------------------------------------------------------------
# Live bracket polling — football-data.org
# ---------------------------------------------------------------------------

# football-data.org team name → our internal name
_FD_TEAM = {
    "Korea Republic":           "South Korea",
    "United States":            "USA",
    "IR Iran":                  "Iran",
    "Côte d'Ivoire":            "Ivory Coast",
    "Congo DR":                 "DR Congo",
    "Democratic Republic of Congo": "DR Congo",
    "Czech Republic":           "Czechia",
    "Cabo Verde":               "Cape Verde",
    "Türkiye":                  "Turkey",
    "China PR":                 "China",
    "Trinidad and Tobago":      "Trinidad & Tobago",
}

# football-data.org stage → our bracket round key
_FD_STAGE = {
    "LAST_32":       "r32",
    "LAST_16":       "r16",
    "QUARTER_FINALS":"qf",
    "SEMI_FINALS":   "sf",
    "FINAL":         "final",
}

# Cache for all WC matches — refreshed each poll cycle
_match_cache: list = []
_match_cache_ts: str = ""

def _fd_name(raw: str) -> str:
    return _FD_TEAM.get(raw, raw)

async def _fetch_wc_matches() -> list:
    url = "https://api.football-data.org/v4/competitions/WC/matches"
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.get(url, headers={"X-Auth-Token": FD_API_KEY})
        r.raise_for_status()
        return r.json().get("matches", [])

async def _apply_fd_matches(matches: list) -> bool:
    """Update bracket from football-data.org match list. Returns True if anything changed."""
    if not db_is_draw_done():
        return False

    bracket = db_get_bracket()
    changed = False

    for m in matches:
        stage = m.get("stage", "")
        rk = _FD_STAGE.get(stage)
        if not rk:
            continue

        status    = m.get("status", "")
        home_raw  = (m.get("homeTeam") or {}).get("name", "")
        away_raw  = (m.get("awayTeam") or {}).get("name", "")
        home      = _fd_name(home_raw)
        away      = _fd_name(away_raw)

        # Skip if both teams still TBD (match not yet seeded)
        if not home or not away or home == "TBD" or away == "TBD":
            continue

        round_matches = bracket[rk]

        # Find existing slot for this fixture, or claim the next empty one
        slot_idx = None
        for i, slot in enumerate(round_matches):
            if slot["home"] == home and slot["away"] == away:
                slot_idx = i
                break
        if slot_idx is None:
            # Claim next empty slot (preserve any host-assigned ones)
            for i, slot in enumerate(round_matches):
                if not slot["home"] and not slot["away"]:
                    slot_idx = i
                    break
        if slot_idx is None:
            continue

        slot = bracket[rk][slot_idx]
        old  = (slot["home"], slot["away"], slot["winner"])

        slot["home"] = home
        slot["away"] = away

        if status == "FINISHED":
            fd_winner = (m.get("score") or {}).get("winner")
            if fd_winner == "HOME_TEAM":
                slot["winner"] = home
            elif fd_winner == "AWAY_TEAM":
                slot["winner"] = away
            # DRAW can happen in group stage; in knockout it means penalties —
            # football-data.org sets winner correctly after extra time / penalties

        if (slot["home"], slot["away"], slot["winner"]) != old:
            changed = True
            if slot["winner"]:
                key = (rk, slot_idx)
                if key in _ADVANCE:
                    nxt_r, nxt_i, side = _ADVANCE[key]
                    bracket[nxt_r][nxt_i][side] = slot["winner"]

    if changed:
        db_save_bracket(bracket)
        f1 = bracket["sf"][0].get("winner", "")
        f2 = bracket["sf"][1].get("winner", "")
        if f1 and f2:
            db_set_finals(f1, f2)
        champ = bracket["final"][0].get("winner", "")
        if champ:
            db_set_champion(champ)

    return changed

async def _poll_loop():
    global _match_cache, _match_cache_ts
    log.info("Bracket polling started (interval=%ds)", POLL_INTERVAL)
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            matches  = await _fetch_wc_matches()
            _match_cache = matches
            _match_cache_ts = datetime.utcnow().isoformat()
            changed  = await _apply_fd_matches(matches)
            if changed:
                state = full_state()
                await ws_manager.broadcast({"type": "bracket_update", **state})
                log.info("Bracket auto-updated from live data")
        except Exception as exc:
            log.warning("Bracket poll failed: %s", exc)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Chat helpers
# ---------------------------------------------------------------------------

CHAT_MAX_MESSAGES = 100
CHAT_MESSAGE_MAX_LEN = 280

def db_get_chat(limit: int = 50) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))

def db_add_chat(player_id: str, player_name: str, avatar_color: str, message: str) -> dict:
    record = {
        "id": str(uuid.uuid4()),
        "player_id": player_id,
        "player_name": player_name,
        "avatar_color": avatar_color,
        "message": message,
        "sent_at": datetime.utcnow().isoformat(),
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat (id,player_id,player_name,avatar_color,message,sent_at) VALUES (?,?,?,?,?,?)",
            (record["id"], record["player_id"], record["player_name"],
             record["avatar_color"], record["message"], record["sent_at"]),
        )
        # Keep only last CHAT_MAX_MESSAGES rows
        conn.execute(
            "DELETE FROM chat WHERE id NOT IN (SELECT id FROM chat ORDER BY sent_at DESC LIMIT ?)",
            (CHAT_MAX_MESSAGES,),
        )
        conn.commit()
    return record

# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    poll_task = asyncio.create_task(_poll_loop()) if FD_API_KEY else None
    yield
    if poll_task:
        poll_task.cancel()

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

class UpdateProfileRequest(BaseModel):
    email: str
    name: str | None = None
    avatar_initial: str | None = None
    avatar_color: str | None = None

    @field_validator("name")
    @classmethod
    def _chk_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v or len(v) > 40:
            raise ValueError("name must be 1–40 chars")
        return v

    @field_validator("avatar_color")
    @classmethod
    def _chk_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        import re
        if not re.match(r'^#[0-9a-fA-F]{6}$', v.strip()):
            raise ValueError("avatar_color must be a hex color")
        return v.strip()

class RegisterRequest(BaseModel):
    name: str
    email: str

class LoginRequest(BaseModel):
    email: str

class HostRequest(BaseModel):
    password: str

class FinalistsRequest(BaseModel):
    password: str
    finalist_1: str
    finalist_2: str

class ChampionRequest(BaseModel):
    password: str
    champion: str

class SetMatchRequest(BaseModel):
    password: str
    round: str   # r32 | r16 | qf | sf | final
    idx: int     # 0-based
    home: str = ""
    away: str = ""
    winner: str = ""  # must equal home, away, or ""

# ---------------------------------------------------------------------------
# Helper — build full state snapshot
# ---------------------------------------------------------------------------

def full_state():
    players = db_get_players()
    draw_done = db_is_draw_done()
    squads = db_get_squads() if draw_done else {}
    finals = db_get_finals() if draw_done else {"finalist_1": "", "finalist_2": "", "champion": ""}
    bracket = db_get_bracket() if draw_done else _empty_bracket()
    return {
        "players": players,
        "squads": squads,
        "draw_done": draw_done,
        "count": len(players),
        "finalist_1": finals["finalist_1"],
        "finalist_2": finals["finalist_2"],
        "champion":   finals["champion"],
        "bracket":    bracket,
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
    if len(players) >= 12:
        raise HTTPException(403, "Tournament is full (12/12)")

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
    if len(players) < 12:
        raise HTTPException(400, f"Need 12 players, only {len(players)} registered")
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


class SetSquadRequest(BaseModel):
    password: str
    email: str
    tier1: str
    tier2: str
    tier3: str
    tier4: str

@app.post("/api/set-squad")
async def api_set_squad(req: SetSquadRequest):
    """Host-only: manually assign a player's squad (restores draw without re-randomising)."""
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    players = db_get_players()
    player = next((p for p in players if p["email"] == req.email.strip().lower()), None)
    if not player:
        raise HTTPException(404, "Player not found")
    squad = {"tier1": req.tier1, "tier2": req.tier2, "tier3": req.tier3, "tier4": req.tier4}
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO squads (player_id, squad_json) VALUES (?,?)",
            (player["id"], json.dumps(squad))
        )
        # Mark draw as done once at least one squad is set
        conn.execute("UPDATE tournament SET value='1' WHERE key='draw_done'")
        conn.commit()
    state = full_state()
    await ws_manager.broadcast({"type": "state_update", **state})
    return {"ok": True, "player": player["name"], "squad": squad}


@app.post("/api/reset-draw")
async def api_reset_draw(req: HostRequest):
    """Undo only the draw — keeps all player registrations intact."""
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    db_reset_draw_only()
    state = full_state()
    await ws_manager.broadcast({"type": "state_update", **state})
    return {"ok": True, "players": len(state.get("players", []))}


@app.post("/api/set-finalists")
async def api_set_finalists(req: FinalistsRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    if not db_is_draw_done():
        raise HTTPException(400, "Draw not done yet")
    f1, f2 = req.finalist_1.strip(), req.finalist_2.strip()
    if not f1 or not f2:
        raise HTTPException(400, "Both finalists required")
    if f1 == f2:
        raise HTTPException(400, "Finalists must be different teams")
    db_set_finals(f1, f2)
    state = full_state()
    await ws_manager.broadcast({"type": "finals_set", **state})
    return state


@app.post("/api/set-champion")
async def api_set_champion(req: ChampionRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    finals = db_get_finals()
    if req.champion not in (finals["finalist_1"], finals["finalist_2"]):
        raise HTTPException(400, "Champion must be one of the two finalists")
    db_set_champion(req.champion)
    state = full_state()
    await ws_manager.broadcast({"type": "champion_set", **state})
    return state


@app.post("/api/set-match")
async def api_set_match(req: SetMatchRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    if not db_is_draw_done():
        raise HTTPException(400, "Draw not done yet")
    valid_rounds = ("r32", "r16", "qf", "sf", "final")
    round_sizes  = {"r32": 16, "r16": 8, "qf": 4, "sf": 2, "final": 1}
    if req.round not in valid_rounds:
        raise HTTPException(400, "Invalid round")
    if req.idx < 0 or req.idx >= round_sizes[req.round]:
        raise HTTPException(400, "Invalid match index")
    if req.winner and req.winner not in (req.home, req.away):
        raise HTTPException(400, "Winner must be home or away team")

    bracket = db_get_bracket()
    match = bracket[req.round][req.idx]
    match["home"]   = req.home
    match["away"]   = req.away
    match["winner"] = req.winner

    # Auto-advance winner to next round
    if req.winner:
        key = (req.round, req.idx)
        if key in _ADVANCE:
            nxt_round, nxt_idx, slot = _ADVANCE[key]
            bracket[nxt_round][nxt_idx][slot] = req.winner

        # Sync sf winners → finalist_1 / finalist_2
        if req.round == "sf":
            f1 = bracket["sf"][0].get("winner", "")
            f2 = bracket["sf"][1].get("winner", "")
            if f1 and f2:
                db_set_finals(f1, f2)
        # Sync final winner → champion
        if req.round == "final":
            w = bracket["final"][0].get("winner", "")
            if w:
                db_set_champion(w)

    db_save_bracket(bracket)
    state = full_state()
    event = "champion_set" if req.round=="final" and req.winner else "bracket_update"
    await ws_manager.broadcast({"type": event, **state})
    return state


@app.post("/api/verify-host")
def api_verify_host(req: HostRequest):
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    return {"ok": True}


@app.post("/api/sync-bracket")
async def api_sync_bracket(req: HostRequest):
    """Force an immediate poll from football-data.org (host only)."""
    if req.password != HOST_PASSWORD:
        raise HTTPException(403, "Wrong password")
    if not FD_API_KEY:
        raise HTTPException(503, "FOOTBALL_DATA_API_KEY not configured")
    try:
        matches = await _fetch_wc_matches()
        changed = await _apply_fd_matches(matches)
        if changed:
            state = full_state()
            await ws_manager.broadcast({"type": "bracket_update", **state})
        return {"ok": True, "changed": changed, "matches_fetched": len(matches)}
    except httpx.HTTPError as e:
        raise HTTPException(502, f"football-data.org error: {e}")


@app.get("/api/matches")
def api_matches():
    """Return cached WC matches for the next 7 days / last 48h (group+knockout)."""
    if not FD_API_KEY or not _match_cache:
        return {"matches": [], "live_sync": bool(FD_API_KEY), "cached_at": _match_cache_ts}

    from datetime import timezone, timedelta
    now = datetime.now(timezone.utc)
    cutoff_future = now + timedelta(days=7)
    cutoff_past   = now - timedelta(hours=48)

    out = []
    for m in _match_cache:
        status   = m.get("status", "")
        utc_date = m.get("utcDate", "")
        try:
            match_dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        except Exception:
            continue

        if status in ("IN_PLAY", "PAUSED"):
            pass  # always include live
        elif status == "FINISHED" and match_dt >= cutoff_past:
            pass
        elif status in ("SCHEDULED", "TIMED") and match_dt <= cutoff_future:
            pass
        else:
            continue

        home  = _fd_name((m.get("homeTeam") or {}).get("name", ""))
        away  = _fd_name((m.get("awayTeam") or {}).get("name", ""))
        score = m.get("score") or {}
        ft    = score.get("fullTime") or {}
        ht    = score.get("halfTime") or {}
        stage = _FD_STAGE.get(m.get("stage", ""), "group")

        out.append({
            "home":       home,
            "away":       away,
            "status":     status,
            "utcDate":    utc_date,
            "stage":      stage,
            "homeScore":  ft.get("home"),
            "awayScore":  ft.get("away"),
            "homeHT":     ht.get("home"),
            "awayHT":     ht.get("away"),
        })

    out.sort(key=lambda x: x["utcDate"])
    return {"matches": out, "live_sync": True, "cached_at": _match_cache_ts}


@app.get("/api/health")
def api_health():
    players = db_get_players()
    return {
        "ok": True,
        "players": len(players),
        "draw_done": db_is_draw_done(),
        "live_sync": bool(FD_API_KEY),
        "poll_interval_s": POLL_INTERVAL if FD_API_KEY else None,
    }

# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

class ChatSendRequest(BaseModel):
    email: str
    message: str

@app.post("/api/update-profile")
async def api_update_profile(req: UpdateProfileRequest):
    """Let a registered player update their display name and/or avatar."""
    email = req.email.strip().lower()
    players = db_get_players()
    player = next((p for p in players if p["email"] == email), None)
    if not player:
        raise HTTPException(403, "Not a registered player")

    updates: list[str] = []
    values: list = []
    if req.name is not None:
        updates.append("name = ?")
        values.append(req.name)
    if req.avatar_initial is not None:
        updates.append("avatar_initial = ?")
        values.append(req.avatar_initial)
    if req.avatar_color is not None:
        updates.append("avatar_color = ?")
        values.append(req.avatar_color)

    if not updates:
        raise HTTPException(400, "Nothing to update")

    values.append(player["id"])
    with get_db() as conn:
        conn.execute(f"UPDATE players SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()

    state = full_state()
    await ws_manager.broadcast({"type": "state_update", **state})
    updated = next((p for p in state.get("players", []) if p["id"] == player["id"]), None)
    return {"ok": True, "player": updated}


@app.get("/api/chat")
def api_chat_get(limit: int = 50):
    bounded = max(1, min(limit, 100))
    return {"messages": db_get_chat(bounded)}

@app.post("/api/chat")
async def api_chat_post(req: ChatSendRequest):
    email = req.email.strip().lower()
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Message cannot be empty")
    if len(message) > CHAT_MESSAGE_MAX_LEN:
        raise HTTPException(400, f"Message too long (max {CHAT_MESSAGE_MAX_LEN} chars)")
    players = db_get_players()
    player = next((p for p in players if p["email"] == email), None)
    if not player:
        raise HTTPException(403, "Only registered players can chat")
    record = db_add_chat(player["id"], player["name"], player["avatar_color"] or "#00ff41", message)
    await ws_manager.broadcast({"type": "chat_message", "message": record})
    return {"ok": True, "message": record}

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
        return FileResponse(html_path, media_type="text/html")
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

@app.get("/manifest.json")
def serve_manifest():
    p = Path(__file__).parent / "manifest.json"
    return FileResponse(p, media_type="application/manifest+json")

@app.get("/sw.js")
def serve_sw():
    p = Path(__file__).parent / "sw.js"
    return FileResponse(p, media_type="application/javascript")

@app.get("/icons/{filename}")
def serve_icon(filename: str):
    p = Path(__file__).parent / "icons" / filename
    if not p.exists() or not p.suffix == ".png":
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
