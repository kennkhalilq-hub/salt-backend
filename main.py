from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional
import sqlite3, hashlib, jwt, httpx, os
from datetime import datetime, timedelta

app = FastAPI(title="SALTPAPI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ================================================================
# ✏️  EDIT THESE — your settings
# ================================================================
SECRET_KEY     = "Salt123"
TELEGRAM_TOKEN = "8912112986:AAELKPR-F7lP_pw_Q2iLxjF2UOYaHS5r8O4"
ADMIN_CHAT_ID  = "5943498178"
API_URL        = "https://salt-wormgpt.wasmer.app/index.php"
API_MODEL      = "salt-2.0"
# ================================================================

DB_PATH = "saltpapi.db"
security = HTTPBearer()


# ================================================================
# DATABASE
# ================================================================
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
            banned INTEGER DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    pw = hash_password("admin123")
    conn.execute("INSERT OR IGNORE INTO users (username, password, approved, is_admin) VALUES (?, ?, 1, 1)", ("admin", pw))
    conn.commit()
    conn.close()


# ================================================================
# HELPERS
# ================================================================
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

async def send_telegram(chat_id: str, message: str, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        await client.post(url, json=payload)

async def answer_callback(callback_query_id: str, text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"callback_query_id": callback_query_id, "text": text})

init_db()


# ================================================================
# MODELS
# ================================================================
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
    action: str


# ================================================================
# TELEGRAM BOT WEBHOOK
# ================================================================
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # ── Handle inline button presses (approve/reject/ban) ──
    if "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        cq_data = cq.get("data", "")
        chat_id = str(cq["from"]["id"])

        if str(chat_id) != str(ADMIN_CHAT_ID):
            await answer_callback(cq_id, "⛔ Not authorized.")
            return {"ok": True}

        parts = cq_data.split(":")
        action = parts[0]
        username = parts[1] if len(parts) > 1 else ""

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if not user:
            await answer_callback(cq_id, "❌ User not found.")
            conn.close()
            return {"ok": True}

        if action == "approve":
            conn.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=?", (username,))
            conn.commit()
            conn.close()
            await answer_callback(cq_id, f"✅ {username} approved!")
            await send_telegram(ADMIN_CHAT_ID, f"✅ <b>{username}</b> has been <b>approved</b> and can now log in.")

        elif action == "reject":
            conn.execute("UPDATE users SET rejected=1, approved=0 WHERE username=?", (username,))
            conn.commit()
            conn.close()
            await answer_callback(cq_id, f"❌ {username} rejected!")
            await send_telegram(ADMIN_CHAT_ID, f"❌ <b>{username}</b> has been <b>rejected</b>.")

        elif action == "ban":
            conn.execute("UPDATE users SET banned=1, approved=0 WHERE username=?", (username,))
            conn.commit()
            conn.close()
            await answer_callback(cq_id, f"🔨 {username} banned!")
            await send_telegram(ADMIN_CHAT_ID, f"🔨 <b>{username}</b> has been <b>banned</b>.")

        return {"ok": True}

    # ── Handle text commands ──
    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    # Only respond to admin
    if str(chat_id) != str(ADMIN_CHAT_ID):
        await send_telegram(chat_id, "⛔ You are not authorized to use this bot.")
        return {"ok": True}

    conn = get_db()

    # /start — show all commands
    if text == "/start" or text == "/help":
        await send_telegram(chat_id,
            "⚡ <b>SALTPAPI Admin Bot</b>\n\n"
            "Here are all the commands:\n\n"
            "👤 <b>User Management</b>\n"
            "/pending — list users waiting for approval\n"
            "/approve username — approve a user\n"
            "/reject username — reject a user\n"
            "/ban username — ban a user\n"
            "/unban username — unban a user\n"
            "/users — list all users\n"
            "/info username — get info about a user\n\n"
            "📢 <b>Announcements</b>\n"
            "/announce your message — send announcement to all users on the site\n\n"
            "📊 <b>Stats</b>\n"
            "/stats — see site stats\n"
        )

    # /pending — list pending users
    elif text == "/pending":
        users = conn.execute("SELECT username, created_at FROM users WHERE approved=0 AND rejected=0 AND banned=0").fetchall()
        if not users:
            await send_telegram(chat_id, "✅ No pending users right now!")
        else:
            msg_text = f"🕐 <b>Pending users ({len(users)}):</b>\n\n"
            for u in users:
                msg_text += f"👤 <b>{u['username']}</b> — registered {u['created_at']}\n"
            msg_text += "\nUse /approve username or /reject username"
            await send_telegram(chat_id, msg_text)

    # /approve username
    elif text.startswith("/approve "):
        username = text.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await send_telegram(chat_id, f"❌ User <b>{username}</b> not found.")
        else:
            conn.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=?", (username,))
            conn.commit()
            await send_telegram(chat_id, f"✅ <b>{username}</b> has been approved and can now log in!")

    # /reject username
    elif text.startswith("/reject "):
        username = text.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await send_telegram(chat_id, f"❌ User <b>{username}</b> not found.")
        else:
            conn.execute("UPDATE users SET rejected=1, approved=0 WHERE username=?", (username,))
            conn.commit()
            await send_telegram(chat_id, f"❌ <b>{username}</b> has been rejected.")

    # /ban username
    elif text.startswith("/ban "):
        username = text.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await send_telegram(chat_id, f"❌ User <b>{username}</b> not found.")
        elif user["is_admin"]:
            await send_telegram(chat_id, f"⛔ Cannot ban an admin account.")
        else:
            conn.execute("UPDATE users SET banned=1, approved=0 WHERE username=?", (username,))
            conn.commit()
            await send_telegram(chat_id, f"🔨 <b>{username}</b> has been banned.")

    # /unban username
    elif text.startswith("/unban "):
        username = text.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await send_telegram(chat_id, f"❌ User <b>{username}</b> not found.")
        else:
            conn.execute("UPDATE users SET banned=0, approved=1, rejected=0 WHERE username=?", (username,))
            conn.commit()
            await send_telegram(chat_id, f"✅ <b>{username}</b> has been unbanned and can log in again.")

    # /users — list all users
    elif text == "/users":
        users = conn.execute("SELECT username, approved, rejected, banned, is_admin, created_at FROM users ORDER BY created_at DESC").fetchall()
        if not users:
            await send_telegram(chat_id, "No users yet.")
        else:
            msg_text = f"👥 <b>All users ({len(users)}):</b>\n\n"
            for u in users:
                if u["is_admin"]:
                    status = "👑 Admin"
                elif u["banned"]:
                    status = "🔨 Banned"
                elif u["approved"]:
                    status = "✅ Active"
                elif u["rejected"]:
                    status = "❌ Rejected"
                else:
                    status = "🕐 Pending"
                msg_text += f"• <b>{u['username']}</b> — {status}\n"
            await send_telegram(chat_id, msg_text)

    # /info username
    elif text.startswith("/info "):
        username = text.split(" ", 1)[1].strip()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await send_telegram(chat_id, f"❌ User <b>{username}</b> not found.")
        else:
            session_count = conn.execute("SELECT COUNT(*) as c FROM sessions WHERE user_id=?", (user["id"],)).fetchone()["c"]
            msg_count = conn.execute(
                "SELECT COUNT(*) as c FROM messages m JOIN sessions s ON m.session_id=s.id WHERE s.user_id=?",
                (user["id"],)
            ).fetchone()["c"]
            if user["is_admin"]: status = "👑 Admin"
            elif user["banned"]: status = "🔨 Banned"
            elif user["approved"]: status = "✅ Active"
            elif user["rejected"]: status = "❌ Rejected"
            else: status = "🕐 Pending"
            await send_telegram(chat_id,
                f"👤 <b>User Info: {username}</b>\n\n"
                f"Status: {status}\n"
                f"Registered: {user['created_at']}\n"
                f"Total chats: {session_count}\n"
                f"Total messages: {msg_count}\n"
            )

    # /announce message — saves to DB, frontend polls it
    elif text.startswith("/announce "):
        announcement = text.split(" ", 1)[1].strip()
        conn.execute("INSERT INTO announcements (message) VALUES (?)", (announcement,))
        conn.commit()
        await send_telegram(chat_id, f"📢 Announcement sent to site:\n\n<i>{announcement}</i>")

    # /stats
    elif text == "/stats":
        total = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_admin=0").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM users WHERE approved=1 AND banned=0 AND is_admin=0").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) as c FROM users WHERE approved=0 AND rejected=0 AND banned=0").fetchone()["c"]
        banned = conn.execute("SELECT COUNT(*) as c FROM users WHERE banned=1").fetchone()["c"]
        chats = conn.execute("SELECT COUNT(*) as c FROM sessions").fetchone()["c"]
        msgs = conn.execute("SELECT COUNT(*) as c FROM messages").fetchone()["c"]
        await send_telegram(chat_id,
            f"📊 <b>SALTPAPI Stats</b>\n\n"
            f"👥 Total users: {total}\n"
            f"✅ Active: {active}\n"
            f"🕐 Pending: {pending}\n"
            f"🔨 Banned: {banned}\n\n"
            f"💬 Total chats: {chats}\n"
            f"📨 Total messages: {msgs}\n"
        )

    else:
        await send_telegram(chat_id, "❓ Unknown command. Type /help to see all commands.")

    conn.close()
    return {"ok": True}


# ── Register the webhook automatically on startup ────────────────
@app.on_event("startup")
async def set_webhook():
    # Render sets RENDER_EXTERNAL_URL automatically
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if base_url:
        webhook_url = f"{base_url}/telegram/webhook"
        async with httpx.AsyncClient() as client:
            await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                params={"url": webhook_url}
            )


# ================================================================
# AUTH ROUTES
# ================================================================
@app.post("/register")
async def register(data: AuthRequest):
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE username=?", (data.username,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already taken")
    pw = hash_password(data.password)
    conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (data.username, pw))
    conn.commit()
    conn.close()

    # Send Telegram notification with inline approve/reject/ban buttons
    await send_telegram(
        ADMIN_CHAT_ID,
        f"🆕 <b>New Registration!</b>\n\n"
        f"👤 Username: <b>{data.username}</b>\n"
        f"⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Tap a button to respond:",
        reply_markup={
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{data.username}"},
                {"text": "❌ Reject",  "callback_data": f"reject:{data.username}"},
                {"text": "🔨 Ban",     "callback_data": f"ban:{data.username}"}
            ]]
        }
    )
    return {"message": "Registered! Waiting for admin approval."}


@app.post("/login")
def login(data: AuthRequest):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (data.username,)).fetchone()
    conn.close()
    if not user or user["password"] != hash_password(data.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if user["banned"]:
        raise HTTPException(status_code=403, detail="banned")
    if user["rejected"]:
        raise HTTPException(status_code=403, detail="Your account was rejected")
    if not user["approved"]:
        raise HTTPException(status_code=403, detail="pending")
    token = make_token(user["id"], user["username"])
    return {"token": token, "username": user["username"], "is_admin": bool(user["is_admin"])}


@app.get("/status/{username}")
def check_status(username: str):
    conn = get_db()
    user = conn.execute("SELECT approved, rejected, banned FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["banned"]:    return {"status": "banned"}
    if user["rejected"]:  return {"status": "rejected"}
    if user["approved"]:  return {"status": "approved"}
    return {"status": "pending"}


# ── Announcements — frontend polls this ─────────────────────────
@app.get("/announcements/latest")
def get_latest_announcement():
    conn = get_db()
    row = conn.execute("SELECT message, created_at FROM announcements ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if not row:
        return {"announcement": None}
    return {"announcement": row["message"], "created_at": row["created_at"]}


# ================================================================
# ADMIN ROUTES
# ================================================================
@app.post("/admin/approve")
async def approve_user(data: ApprovalRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    admin = conn.execute("SELECT is_admin FROM users WHERE id=?", (current_user["user_id"],)).fetchone()
    if not admin or not admin["is_admin"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")
    if data.action == "approve":
        conn.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=?", (data.username,))
    elif data.action == "reject":
        conn.execute("UPDATE users SET rejected=1, approved=0 WHERE username=?", (data.username,))
    elif data.action == "ban":
        conn.execute("UPDATE users SET banned=1, approved=0 WHERE username=?", (data.username,))
    elif data.action == "unban":
        conn.execute("UPDATE users SET banned=0, approved=1, rejected=0 WHERE username=?", (data.username,))
    conn.commit()
    conn.close()
    return {"message": f"User {data.action}d successfully"}


@app.get("/admin/pending")
def get_pending_users(current_user=Depends(get_current_user)):
    conn = get_db()
    admin = conn.execute("SELECT is_admin FROM users WHERE id=?", (current_user["user_id"],)).fetchone()
    if not admin or not admin["is_admin"]:
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")
    users = conn.execute("SELECT username, created_at FROM users WHERE approved=0 AND rejected=0 AND banned=0").fetchall()
    conn.close()
    return [{"username": u["username"], "created_at": u["created_at"]} for u in users]


# ================================================================
# CHAT SESSIONS
# ================================================================
@app.get("/sessions")
def get_sessions(current_user=Depends(get_current_user)):
    conn = get_db()
    sessions = conn.execute(
        "SELECT id, title, created_at FROM sessions WHERE user_id=? ORDER BY created_at DESC",
        (current_user["user_id"],)
    ).fetchall()
    conn.close()
    return [{"id": s["id"], "title": s["title"], "created_at": s["created_at"]} for s in sessions]


@app.post("/sessions")
def create_session(data: NewSessionRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.execute("INSERT INTO sessions (user_id, title) VALUES (?, ?)", (current_user["user_id"], data.title))
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return {"session_id": session_id, "title": data.title}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    session = conn.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    return {"message": "Session deleted"}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    session = conn.execute("SELECT user_id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    msgs = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id ASC",
        (session_id,)
    ).fetchall()
    conn.close()
    return [{"role": m["role"], "content": m["content"], "created_at": m["created_at"]} for m in msgs]


# ================================================================
# AI CHAT
# ================================================================
@app.post("/chat")
async def chat(data: ChatRequest, current_user=Depends(get_current_user)):
    conn = get_db()

    # Check if user is banned
    user = conn.execute("SELECT banned FROM users WHERE id=?", (current_user["user_id"],)).fetchone()
    if user and user["banned"]:
        conn.close()
        raise HTTPException(status_code=403, detail="banned")

    session = conn.execute(
        "SELECT id, title FROM sessions WHERE id=? AND user_id=?",
        (data.session_id, current_user["user_id"])
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    history = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC",
        (data.session_id,)
    ).fetchall()

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": data.message})

    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)", (data.session_id, data.message))

    if session["title"] == "New chat":
        short_title = data.message[:40] + ("…" if len(data.message) > 40 else "")
        conn.execute("UPDATE sessions SET title=? WHERE id=?", (short_title, data.session_id))

    conn.commit()

    # ── Call your AI API ──────────────────────────────────────
    try:
        conversation = ""
        for m in messages:
            role = "User" if m["role"] == "user" else "Assistant"
            conversation += f"{role}: {m['content']}\n"

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                API_URL,
                data={
                    "message": data.message,
                    "model": API_MODEL,
                    "history": conversation
                }
            )
            raw = response.text.strip()
            try:
                result = response.json()
                if "response" in result:
                    ai_reply = result["response"]
                elif "reply" in result:
                    ai_reply = result["reply"]
                elif "message" in result:
                    ai_reply = result["message"]
                elif "content" in result:
                    ai_reply = result["content"]
                elif "choices" in result:
                    ai_reply = result["choices"][0]["message"]["content"]
                else:
                    ai_reply = str(result)
            except Exception:
                ai_reply = raw if raw else "No response from AI."
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"AI API error: {str(e)}")
    # ─────────────────────────────────────────────────────────

    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)", (data.session_id, ai_reply))
    conn.commit()
    conn.close()

    return {"reply": ai_reply, "session_id": data.session_id}


@app.get("/")
def root():
    return {"status": "SALTPAPI backend is running!"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)


# ================================================================
# FILE UPLOAD
# ================================================================
from fastapi import UploadFile, File, Form
import base64, mimetypes

@app.post("/upload")
async def upload_file(
    session_id: int = Form(...),
    file: UploadFile = File(...),
    current_user=Depends(get_current_user)
):
    conn = get_db()
    session = conn.execute(
        "SELECT id FROM sessions WHERE id=? AND user_id=?",
        (session_id, current_user["user_id"])
    ).fetchone()
    if not session:
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    # Read file and convert to base64
    file_bytes = await file.read()
    file_b64 = base64.b64encode(file_bytes).decode("utf-8")
    mime = file.content_type or "application/octet-stream"
    filename = file.filename

    # Build message to send to AI
    is_image = mime.startswith("image/")

    if is_image:
        user_msg = f"[Image uploaded: {filename}]"
        # Send image + message to AI API
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    API_URL,
                    data={
                        "message": f"The user sent an image named {filename}. Please analyze it.",
                        "model": API_MODEL,
                        "image": file_b64,
                        "image_mime": mime
                    }
                )
                raw = response.text.strip()
                try:
                    result = response.json()
                    if "response" in result:   ai_reply = result["response"]
                    elif "reply" in result:    ai_reply = result["reply"]
                    elif "message" in result:  ai_reply = result["message"]
                    elif "content" in result:  ai_reply = result["content"]
                    elif "choices" in result:  ai_reply = result["choices"][0]["message"]["content"]
                    else:                      ai_reply = str(result)
                except Exception:
                    ai_reply = raw if raw else "I received your image but couldn't process it."
        except Exception as e:
            ai_reply = f"Sorry, I couldn't process the image: {str(e)}"
    else:
        # For non-image files, extract text if possible
        try:
            text_content = file_bytes.decode("utf-8", errors="ignore")[:3000]
            user_msg = f"[File uploaded: {filename}]\n\n{text_content}"
        except:
            text_content = ""
            user_msg = f"[File uploaded: {filename}]"

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    API_URL,
                    data={
                        "message": f"The user uploaded a file named {filename}. Here is its content:\n\n{text_content}" if text_content else f"The user uploaded a file named {filename}.",
                        "model": API_MODEL,
                    }
                )
                raw = response.text.strip()
                try:
                    result = response.json()
                    if "response" in result:   ai_reply = result["response"]
                    elif "reply" in result:    ai_reply = result["reply"]
                    elif "message" in result:  ai_reply = result["message"]
                    elif "content" in result:  ai_reply = result["content"]
                    elif "choices" in result:  ai_reply = result["choices"][0]["message"]["content"]
                    else:                      ai_reply = str(result)
                except Exception:
                    ai_reply = raw if raw else "I received your file but couldn't process it."
        except Exception as e:
            ai_reply = f"Sorry, I couldn't process the file: {str(e)}"

    # Save both messages to DB
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)", (session_id, user_msg))
    conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'assistant', ?)", (session_id, ai_reply))
    conn.commit()
    conn.close()

    return {"reply": ai_reply, "user_msg": user_msg, "session_id": session_id}
