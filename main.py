import hashlib
import jwt
import httpx
import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ================================================================
# SETTINGS
# ================================================================
SECRET_KEY     = "Salt123"
TELEGRAM_TOKEN = "8912112986:AAELKPR-F7lP_pw_Q2iLxjF2UOYaHS5r8O4"
ADMIN_CHAT_ID  = "5943498178"
API_URL        = "https://salt-wormgpt.wasmer.app/index.php"
API_MODEL      = "salt-2.0"
DATABASE_URL   = os.environ.get("DATABASE_URL", "postgresql://postgres.fuhfodxrcmngwidqezlg:Salt092990%3F%21@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres")
# ================================================================

app = FastAPI(title="SALTPAPI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()


# ================================================================
# HELPERS
# ================================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


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
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception:
        pass


async def answer_callback(callback_query_id: str, text: str):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/answerCallbackQuery"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"callback_query_id": callback_query_id, "text": text})
    except Exception:
        pass


async def call_ai(message: str, history: str = "") -> str:
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                API_URL,
                data={"message": message, "model": API_MODEL, "history": history}
            )
            raw = response.text.strip()
            try:
                result = response.json()
                for key in ["response", "reply", "message", "content"]:
                    if key in result:
                        return result[key]
                if "choices" in result:
                    return result["choices"][0]["message"]["content"]
                return str(result)
            except Exception:
                return raw if raw else "No response from AI."
    except Exception as e:
        return "AI error: " + str(e)


# ================================================================
# DATABASE SETUP
# ================================================================
def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            rejected INTEGER DEFAULT 0,
            banned INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (NOW()::text)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'New chat',
            created_at TEXT DEFAULT (NOW()::text)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT (NOW()::text)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id SERIAL PRIMARY KEY,
            message TEXT NOT NULL,
            created_at TEXT DEFAULT (NOW()::text)
        )
    """)
    # Default admin
    pw = hash_password("admin123")
    cur.execute("""
        INSERT INTO users (username, password, approved, is_admin)
        VALUES (%s, %s, 1, 1)
        ON CONFLICT (username) DO NOTHING
    """, ("admin", pw))
    conn.commit()
    cur.close()
    conn.close()


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
# TELEGRAM WEBHOOK
# ================================================================
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    # Handle button taps
    if "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        cq_data = cq.get("data", "")
        from_id = str(cq["from"]["id"])

        if from_id != str(ADMIN_CHAT_ID):
            await answer_callback(cq_id, "Not authorized.")
            return {"ok": True}

        if ":" not in cq_data:
            await answer_callback(cq_id, "Invalid data.")
            return {"ok": True}

        action, username = cq_data.split(":", 1)

        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()

        if not user:
            cur.execute("SELECT username FROM users")
            all_users = cur.fetchall()
            names = ", ".join([u["username"] for u in all_users]) or "none"
            cur.close()
            conn.close()
            await answer_callback(cq_id, "User not found.")
            await send_telegram(ADMIN_CHAT_ID, "Could not find: " + username + ". DB has: " + names)
            return {"ok": True}

        if action == "approve":
            cur.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=%s", (username,))
            reply_msg = username + " approved!"
        elif action == "reject":
            cur.execute("UPDATE users SET rejected=1, approved=0 WHERE username=%s", (username,))
            reply_msg = username + " rejected."
        elif action == "ban":
            cur.execute("UPDATE users SET banned=1, approved=0 WHERE username=%s", (username,))
            reply_msg = username + " banned."
        else:
            cur.close()
            conn.close()
            await answer_callback(cq_id, "Unknown action.")
            return {"ok": True}

        conn.commit()
        cur.close()
        conn.close()
        await answer_callback(cq_id, reply_msg)
        await send_telegram(ADMIN_CHAT_ID, reply_msg)
        return {"ok": True}

    # Handle text commands
    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    if chat_id != str(ADMIN_CHAT_ID):
        await send_telegram(chat_id, "Not authorized.")
        return {"ok": True}

    conn = get_db()
    cur = conn.cursor()

    if text in ("/start", "/help"):
        await send_telegram(chat_id,
            "<b>SALTPAPI Admin Bot</b>\n\n"
            "/pending - users waiting approval\n"
            "/approve username\n"
            "/reject username\n"
            "/ban username\n"
            "/unban username\n"
            "/users - list all users\n"
            "/info username\n"
            "/announce your message\n"
            "/stats"
        )

    elif text == "/pending":
        cur.execute("SELECT username, created_at FROM users WHERE approved=0 AND rejected=0 AND banned=0")
        users = cur.fetchall()
        if not users:
            await send_telegram(chat_id, "No pending users.")
        else:
            lines = ["Pending (" + str(len(users)) + "):\n"]
            for u in users:
                lines.append(u["username"] + " - " + str(u["created_at"]))
            lines.append("\nUse /approve username")
            await send_telegram(chat_id, "\n".join(lines))

    elif text.startswith("/approve "):
        username = text.split(" ", 1)[1].strip()
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            await send_telegram(chat_id, "User not found: " + username)
        else:
            cur.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=%s", (username,))
            conn.commit()
            await send_telegram(chat_id, username + " approved!")

    elif text.startswith("/reject "):
        username = text.split(" ", 1)[1].strip()
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            await send_telegram(chat_id, "User not found: " + username)
        else:
            cur.execute("UPDATE users SET rejected=1, approved=0 WHERE username=%s", (username,))
            conn.commit()
            await send_telegram(chat_id, username + " rejected.")

    elif text.startswith("/ban "):
        username = text.split(" ", 1)[1].strip()
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            await send_telegram(chat_id, "User not found: " + username)
        elif user["is_admin"]:
            await send_telegram(chat_id, "Cannot ban an admin.")
        else:
            cur.execute("UPDATE users SET banned=1, approved=0 WHERE username=%s", (username,))
            conn.commit()
            await send_telegram(chat_id, username + " banned.")

    elif text.startswith("/unban "):
        username = text.split(" ", 1)[1].strip()
        cur.execute("SELECT id FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            await send_telegram(chat_id, "User not found: " + username)
        else:
            cur.execute("UPDATE users SET banned=0, approved=1, rejected=0 WHERE username=%s", (username,))
            conn.commit()
            await send_telegram(chat_id, username + " unbanned!")

    elif text == "/users":
        cur.execute("SELECT username, approved, rejected, banned, is_admin FROM users ORDER BY id DESC")
        users = cur.fetchall()
        if not users:
            await send_telegram(chat_id, "No users yet.")
        else:
            lines = ["All users (" + str(len(users)) + "):\n"]
            for u in users:
                if u["is_admin"]:     s = "Admin"
                elif u["banned"]:     s = "Banned"
                elif u["approved"]:   s = "Active"
                elif u["rejected"]:   s = "Rejected"
                else:                 s = "Pending"
                lines.append(u["username"] + " - " + s)
            await send_telegram(chat_id, "\n".join(lines))

    elif text.startswith("/info "):
        username = text.split(" ", 1)[1].strip()
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        if not user:
            await send_telegram(chat_id, "User not found: " + username)
        else:
            cur.execute("SELECT COUNT(*) as c FROM sessions WHERE user_id=%s", (user["id"],))
            sc = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) as c FROM messages m JOIN sessions s ON m.session_id=s.id WHERE s.user_id=%s", (user["id"],))
            mc = cur.fetchone()["c"]
            if user["is_admin"]:   s = "Admin"
            elif user["banned"]:   s = "Banned"
            elif user["approved"]: s = "Active"
            elif user["rejected"]: s = "Rejected"
            else:                  s = "Pending"
            await send_telegram(chat_id,
                "User: " + username + "\n"
                "Status: " + s + "\n"
                "Registered: " + str(user["created_at"]) + "\n"
                "Chats: " + str(sc) + "\n"
                "Messages: " + str(mc)
            )

    elif text.startswith("/announce "):
        announcement = text.split(" ", 1)[1].strip()
        cur.execute("INSERT INTO announcements (message) VALUES (%s)", (announcement,))
        conn.commit()
        await send_telegram(chat_id, "Announced: " + announcement)

    elif text == "/stats":
        cur.execute("SELECT COUNT(*) as c FROM users WHERE is_admin=0")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM users WHERE approved=1 AND banned=0 AND is_admin=0")
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM users WHERE approved=0 AND rejected=0 AND banned=0")
        pending = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM users WHERE banned=1")
        banned = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM sessions")
        chats = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM messages")
        msgs = cur.fetchone()["c"]
        await send_telegram(chat_id,
            "SALTPAPI Stats\n\n"
            "Total users: " + str(total) + "\n"
            "Active: " + str(active) + "\n"
            "Pending: " + str(pending) + "\n"
            "Banned: " + str(banned) + "\n"
            "Total chats: " + str(chats) + "\n"
            "Total messages: " + str(msgs)
        )

    else:
        await send_telegram(chat_id, "Unknown command. Type /help")

    cur.close()
    conn.close()
    return {"ok": True}


@app.on_event("startup")
async def set_webhook():
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if base_url:
        webhook_url = base_url + "/telegram/webhook"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.get(
                    "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/setWebhook",
                    params={"url": webhook_url}
                )
        except Exception:
            pass


# ================================================================
# AUTH
# ================================================================
@app.post("/register")
async def register(data: AuthRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username=%s", (data.username,))
    if cur.fetchone():
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Username already taken")
    pw = hash_password(data.password)
    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (data.username, pw))
    conn.commit()
    cur.close()
    conn.close()
    await send_telegram(
        ADMIN_CHAT_ID,
        "New registration!\n\nUsername: " + data.username + "\nTime: " + datetime.now().strftime("%Y-%m-%d %H:%M"),
        reply_markup={
            "inline_keyboard": [[
                {"text": "Approve", "callback_data": "approve:" + data.username},
                {"text": "Reject",  "callback_data": "reject:"  + data.username},
                {"text": "Ban",     "callback_data": "ban:"     + data.username}
            ]]
        }
    )
    return {"message": "Registered! Waiting for admin approval."}


@app.post("/login")
def login(data: AuthRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username=%s", (data.username,))
    user = cur.fetchone()
    cur.close()
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
    cur = conn.cursor()
    cur.execute("SELECT approved, rejected, banned FROM users WHERE username=%s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user["banned"]:   return {"status": "banned"}
    if user["rejected"]: return {"status": "rejected"}
    if user["approved"]: return {"status": "approved"}
    return {"status": "pending"}


# ================================================================
# ANNOUNCEMENTS
# ================================================================
@app.get("/announcements/latest")
def get_latest_announcement():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT message, created_at FROM announcements ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return {"announcement": None}
    return {"announcement": row["message"], "created_at": str(row["created_at"])}


# ================================================================
# ADMIN
# ================================================================
@app.post("/admin/approve")
async def approve_user(data: ApprovalRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE id=%s", (current_user["user_id"],))
    admin = cur.fetchone()
    if not admin or not admin["is_admin"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")
    if data.action == "approve":
        cur.execute("UPDATE users SET approved=1, rejected=0, banned=0 WHERE username=%s", (data.username,))
    elif data.action == "reject":
        cur.execute("UPDATE users SET rejected=1, approved=0 WHERE username=%s", (data.username,))
    elif data.action == "ban":
        cur.execute("UPDATE users SET banned=1, approved=0 WHERE username=%s", (data.username,))
    elif data.action == "unban":
        cur.execute("UPDATE users SET banned=0, approved=1, rejected=0 WHERE username=%s", (data.username,))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Done"}


@app.get("/admin/pending")
def get_pending_users(current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT is_admin FROM users WHERE id=%s", (current_user["user_id"],))
    admin = cur.fetchone()
    if not admin or not admin["is_admin"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Admins only")
    cur.execute("SELECT username, created_at FROM users WHERE approved=0 AND rejected=0 AND banned=0")
    users = cur.fetchall()
    cur.close()
    conn.close()
    return [{"username": u["username"], "created_at": str(u["created_at"])} for u in users]


# ================================================================
# SESSIONS
# ================================================================
@app.get("/sessions")
def get_sessions(current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, title, created_at FROM sessions WHERE user_id=%s ORDER BY created_at DESC", (current_user["user_id"],))
    sessions = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id": s["id"], "title": s["title"], "created_at": str(s["created_at"])} for s in sessions]


@app.post("/sessions")
def create_session(data: NewSessionRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (user_id, title) VALUES (%s, %s) RETURNING id", (current_user["user_id"], data.title))
    session_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return {"session_id": session_id, "title": data.title}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE id=%s", (session_id,))
    session = cur.fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    cur.execute("DELETE FROM messages WHERE session_id=%s", (session_id,))
    cur.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Deleted"}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: int, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM sessions WHERE id=%s", (session_id,))
    session = cur.fetchone()
    if not session or session["user_id"] != current_user["user_id"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")
    cur.execute("SELECT role, content, created_at FROM messages WHERE session_id=%s ORDER BY id ASC", (session_id,))
    msgs = cur.fetchall()
    cur.close()
    conn.close()
    return [{"role": m["role"], "content": m["content"], "created_at": str(m["created_at"])} for m in msgs]


# ================================================================
# CHAT
# ================================================================
@app.post("/chat")
async def chat(data: ChatRequest, current_user=Depends(get_current_user)):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT banned FROM users WHERE id=%s", (current_user["user_id"],))
    user = cur.fetchone()
    if user and user["banned"]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="banned")

    cur.execute("SELECT id, title FROM sessions WHERE id=%s AND user_id=%s", (data.session_id, current_user["user_id"]))
    session = cur.fetchone()
    if not session:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    cur.execute("SELECT role, content FROM messages WHERE session_id=%s ORDER BY id ASC", (data.session_id,))
    history_rows = cur.fetchall()
    conversation = ""
    for m in history_rows:
        role = "User" if m["role"] == "user" else "Assistant"
        conversation += role + ": " + m["content"] + "\n"

    cur.execute("INSERT INTO messages (session_id, role, content) VALUES (%s, 'user', %s)", (data.session_id, data.message))

    if session["title"] == "New chat":
        short_title = data.message[:40] + ("..." if len(data.message) > 40 else "")
        cur.execute("UPDATE sessions SET title=%s WHERE id=%s", (short_title, data.session_id))

    conn.commit()

    ai_reply = await call_ai(data.message, conversation)

    cur.execute("INSERT INTO messages (session_id, role, content) VALUES (%s, 'assistant', %s)", (data.session_id, ai_reply))
    conn.commit()
    cur.close()
    conn.close()

    return {"reply": ai_reply, "session_id": data.session_id}


# ================================================================
# FILE UPLOAD
# ================================================================
@app.post("/upload")
async def upload_file(
    session_id: int = Form(...),
    file: UploadFile = File(...),
    current_user=Depends(get_current_user)
):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM sessions WHERE id=%s AND user_id=%s", (session_id, current_user["user_id"]))
    session = cur.fetchone()
    if not session:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Session not found")

    file_bytes = await file.read()
    filename = file.filename
    mime = file.content_type or "application/octet-stream"
    is_image = mime.startswith("image/")

    if is_image:
        user_msg = "[Image: " + filename + "]"
        import base64
        file_b64 = base64.b64encode(file_bytes).decode("utf-8")
        ai_reply = await call_ai("The user sent an image named " + filename + ". Please analyze it.")
    else:
        try:
            text_content = file_bytes.decode("utf-8", errors="ignore")[:3000]
        except Exception:
            text_content = ""
        user_msg = "[File: " + filename + "]\n\n" + text_content if text_content else "[File: " + filename + "]"
        ai_reply = await call_ai("The user uploaded a file named " + filename + (". Content:\n\n" + text_content if text_content else "."))

    cur.execute("INSERT INTO messages (session_id, role, content) VALUES (%s, 'user', %s)", (session_id, user_msg))
    cur.execute("INSERT INTO messages (session_id, role, content) VALUES (%s, 'assistant', %s)", (session_id, ai_reply))
    conn.commit()
    cur.close()
    conn.close()

    return {"reply": ai_reply, "user_msg": user_msg, "session_id": session_id}


@app.get("/")
def root():
    return {"status": "SALTPAPI backend is running!"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
