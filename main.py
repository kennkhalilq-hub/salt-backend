from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, hashlib, jwt, httpx, os
from datetime import datetime, timedelta

app = FastAPI(title="SALTPAPI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change to your frontend domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# ✏️  EDIT THESE — your settings
# ================================================================
SECRET_KEY     = "saltpapi-super-secret-change-this"   # any random string

TELEGRAM_TOKEN = "8912112986:AAELKPR-F7lP_pw_Q2iLxjF2UOYaHS5r8O4"                 # from @BotFather on Telegram
ADMIN_CHAT_ID  = "5943498178"          # your Telegram ID from @userinfobot

# ── Your AI API — only edit these 2 lines ──
API_URL   = "https://salt-wormgpt.wasmer.app/index.php"    # paste your API site URL here e.g. https://yoursite.com/api/chat
API_MODEL = "salt-2.0"    # the model name e.g. gpt-4o
# ================================================================

DB_PATH = "saltpapi.db"
security = HTTPBearer()


# ── Database ────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            rejected INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'New chat',
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );
    """)
    # Default admin account — change password after first login!
    pw = hash_password("admin123")
    conn.execute("INSERT OR IGNORE INTO users (username, password, approved, is_admin) VALUES (?, ?, 1, 1)", ("admin", pw))
    conn.commit()
    conn.close()




# ── Helpers ─────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def make_token(user_id: int, username: str) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    return decode_token(creds.credentials)

async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": ADMIN_CHAT_ID, "text": message, "parse_mode": "HTML"})


init_db()


# ── Models ──────────────────────────────────────────────────────
class AuthRequest(BaseModel):
    username: str
    password: str

class ChatRequest(BaseModel):
    session_id: int
    message: str

class NewSessionRequest(BaseModel):
    title: Optional[str] = "New chat"

class ApprovalRequest(BaseModel):
    username: str
    action: str  # "approve" or "reject"


# ================================================================
# AUTH ROUTES
# ================================================================

@app.post("/register")
async def register(data: AuthRequest):
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (data.username,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already taken")

    pw = hash_password(data.password)
    conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (data.username, pw))
    conn.commit()
    conn.close()

    # Notify you on Telegram
    await send_telegram(
        f"🆕 <b>New user registration!</b>\n\n"
        f"👤 Username: <b>{data.username}</b>\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Reply with /approve_{data.username} or /reject_{data.username}"
    )

    return {"message": "Registered! Waiting for admin approval."}


@app.post("/login")
def login(data: AuthRequest):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (data.username,)).fetchone()
    conn.close()

    if not user or user["password"] != hash_password(data.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if user["rejected"]:
        raise HTTPException(status_code=403, detail="Your account was rejected")
    if not user["approved"]:
        raise HTTPException(status_code=403, detail="pending")

    token = make_token(user["id"], user["username"])
    return {
        "token": token,
        "username": user["username"],
        "is_admin": bool(user["is_admin"])
    }


@app.get("/status/{username}")
def check_status(username: str):
    conn = get_db()
    user = conn.execute("SELECT approved, rejected FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["rejected"]:
        return {"status": "rejected"}
    if user["approved"]:
        return {"status": "approved"}
    return {"status": "pending"}


# ================================================================
# ADMIN ROUTES
# ================================================================

@app.post("/admin/approve")
async def approve_user(data: ApprovalRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    admin = conn.execute("SELECT is_admin FROM users WHERE id = ?", (current_user["user_id"],)).fetchone()
    if not admin or not admin["is_admin"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")

    if data.action == "approve":
        conn.execute("UPDATE users SET approved = 1, rejected = 0 WHERE username = ?", (data.username,))
        msg = f"✅ <b>{data.username}</b> has been approved and can now log in."
    elif data.action == "reject":
        conn.execute("UPDATE users SET rejected = 1, approved = 0 WHERE username = ?", (data.username,))
        msg = f"❌ <b>{data.username}</b> has been rejected."
    else:
        conn.close()
        raise HTTPException(status_code=400, detail="Action must be approve or reject")

    conn.commit()
    conn.close()

    # Notify the user via Telegram (you can expand this to notify the user directly if they have a chat id)
    await send_telegram(msg)
    return {"message": f"User {data.action}d successfully"}


@app.get("/admin/pending")
def get_pending_users(current_user=Depends(get_current_user)):
    conn = get_db()
    admin = conn.execute("SELECT is_admin FROM users WHERE id = ?", (current_user["user_id"],)).fetchone()
    if not admin or not admin["is_admin"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")
    users = conn.execute("SELECT username, created_at FROM users WHERE approved = 0 AND rejected = 0").fetchall()
    conn.close()
    return [{"username": u["username"], "created_at": u["created_at"]} for u in users]


# ================================================================
# CHAT SESSIONS
# ================================================================

@app.get("/sessions")
def get_sessions(current_user=Depends(get_current_user)):
    conn = get_db()
    sessions = conn.execute(
        "SELECT id, title, created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC",
        (current_user["user_id"],)
    ).fetchall()
    conn.close()
    return [{"id": s["id"], "title": s["title"], "created_at": s["created_at"]} for s in sessions]


@app.post("/sessions")
def create_session(data: NewSessionRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO sessions (user_id, title) VALUES (?, ?)",
        (current_user["user_id"], data.title)
    )
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return {"session_id": session_id, "title": data.title}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    session = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return {"message": "Session deleted"}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    session = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    msgs = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,)
    ).fetchall()
    conn.close()
    return [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]} for m in msgs]


# ================================================================
# AI CHAT — this is where your API key is used (safe, server-side)
# ================================================================

@app.post("/chat")
async def chat(data: ChatRequest, current_user=Depends(get_current_user)):
    conn = get_db()

    # Verify session belongs to this user
    session = conn.execute(
        "SELECT id, title FROM sessions WHERE id = ? AND user_id = ?",
        (data.session_id, current_user["user_id"])
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    # Load conversation history for context
    history = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id ASC",
        (data.session_id,)
    ).fetchall()

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": data.message})

    # Save user message
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)", (data.session_id, data.message))

    # Auto-title the session from the first message
    if session["title"] == "New chat":
        short_title = data.message[:40] + ("…" if len(data.message) > 40 else "")
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (short_title, data.session_id))

    conn.commit()

    # ── Call your AI API ──────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                API_URL,
                headers={
                    "Content-Type": "application/json"
                },
                json={
                    "model": API_MODEL,
                    "messages": messages,
                    "max_tokens": 2048
                }
            )
            result = response.json()
            ai_reply = result["choices"][0]["message"]["content"]
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"AI API error: {str(e)}")
    # ─────────────────────────────────────────────────────────

    # Save AI reply
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)", (data.session_id, ai_reply))
    conn.commit()
    conn.close()

    return {"reply": ai_reply, "session_id": data.session_id}


@app.get("/")
def root():
    return {"status": "SALTPAPI backend is running!"}
