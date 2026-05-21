========================================
  SALTPAPI BACKEND — SETUP GUIDE
========================================

STEP 1 — Install Python
  Make sure Python 3.10+ is installed.
  Download: https://www.python.org/downloads/

STEP 2 — Edit main.py
  Open main.py and fill in these values at the top:

    SECRET_KEY     = any random string (keep it secret)
    TELEGRAM_TOKEN = your bot token from @BotFather on Telegram
    ADMIN_CHAT_ID  = your Telegram chat ID (message @userinfobot to get it)
    AI_API_KEY     = your AI provider's API key
    AI_API_URL     = your AI provider's endpoint URL
    AI_MODEL       = the model name you want to use

STEP 3 — Run the server
  Windows:  double-click start.bat
  Mac/Linux: run ./start.sh

  Server will start at: http://localhost:8000

STEP 4 — Edit the frontend HTML
  Open saltpapi-frontend.html and set:
    const BACKEND_URL = "http://localhost:8000"
  (or your server's IP/domain when hosted online)

STEP 5 — Test it
  Open your browser and go to: http://localhost:8000
  You should see: {"status": "SALTPAPI backend is running!"}

========================================
  DEFAULT ADMIN LOGIN
========================================
  Username: admin
  Password: admin123
  ⚠️ Change this password after first login!

========================================
  TELEGRAM SETUP
========================================
  1. Message @BotFather on Telegram → /newbot
  2. Copy the token it gives you → paste in main.py
  3. Message @userinfobot on Telegram → copy your ID → paste in main.py
  4. Start your bot by messaging it once on Telegram

========================================
  API ENDPOINTS (for reference)
========================================
  POST /register        - create account
  POST /login           - login, returns token
  GET  /status/{user}   - check approval status
  POST /chat            - send message to AI
  GET  /sessions        - get chat history list
  POST /sessions        - create new chat session
  GET  /sessions/{id}/messages - get messages in a session
  DELETE /sessions/{id} - delete a session
  GET  /admin/pending   - list pending users (admin only)
  POST /admin/approve   - approve or reject a user (admin only)
