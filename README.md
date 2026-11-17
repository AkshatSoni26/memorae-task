# Memorae — Real-Time Google Meet AI Agent

A conversational AI meeting participant designed to join a Google Meet call, listen in real time, and respond when addressed.

---

## ⚡ Quick Start: Live Demo Runbook

Follow these exact steps whenever you restart or present the live demo:

### 1. Start the FastAPI Server
Open your primary terminal and run:
```bash
uv run uvicorn main:app --reload --port 8000
```
- Web UI: [http://localhost:8000/](http://localhost:8000/)
- Swagger Docs: [http://localhost:8000/docs](http://localhost:8000/docs)
- Health Check: [http://localhost:8000/health](http://localhost:8000/health)

---

### 2. Start ngrok Tunnel (For Webhooks)
Open a second terminal and run:
```bash
ngrok http 8000
```
> **✨ Zero-Config Auto-Discovery:**  
> You don't need to manually copy or paste your ngrok URL anywhere! The backend automatically detects your active ngrok tunnel via the local daemon API (`http://127.0.0.1:4040/api/tunnels`) on every dispatch and registers it dynamically with the bot engine.

---

### 3. Join the Google Meet Call

You can trigger the bot in either of two ways:

#### Option A: Web Interface (Recommended for Demo)
1. Open [http://localhost:8000/](http://localhost:8000/) in your browser.
2. Paste your Google Meet URL (e.g., `https://meet.google.com/xxx-yyyy-zzz`).
3. Click **"Send Agent to Call"**.
4. In Google Meet, click **"Admit"** when the agent requests to join.
5. Listen for the greeting, speak to the bot, and click **"Leave Call"** when finished.

#### Option B: Terminal CLI
```bash
uv run python cli.py https://meet.google.com/xxx-yyyy-zzz
```
- Press `s` to view live status and participant counts.
- Press `q` to disconnect the bot and exit.

---

## 🛠️ Configuration (`.env`)

Your `.env` file controls all keys and personas:

```env
# Meeting BaaS Credentials
MEETING_BAAS=your_meeting_baas_api_key
MEETING_BAAS_WEBHOOK=your_webhook_secret

# AI Brain & LLM (LiteLLM with Groq)
GROQ_API_KEY=gsk_your_groq_api_key
LLM_MODEL=groq/qwen/qwen3.8-27b
LLM_TEMPERATURE=0.7
LLM_MAX_TOKENS=150

# Agent Persona
DEFAULT_BOT_NAME=Akshat
DEFAULT_ENTRY_MESSAGE="Hey everyone, Akshat here. Glad to join."
DEFAULT_BOT_PROMPT="You are Akshat, a friendly software engineer participating in a Google Meet. You listen carefully and only speak when someone asks you a question or directly addresses you. Keep answers very concise, punchy, conversational, and direct (1-2 sentences). Do not introduce yourself repeatedly. Do not interrupt other participants."
DEFAULT_RECORDING_MODE=speaker_view
```

---

## 📂 Project Structure

```text
memorsa-task/
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── endpoints/
│   │       │   ├── bots.py          # /join, /status, /leave endpoints
│   │       │   └── webhooks.py      # /webhook real-time callback logger
│   │       └── router.py            # API router aggregation
│   ├── core/
│   │   ├── config.py                # Pydantic BaseSettings & environment loader
│   │   ├── exceptions.py            # Custom typed error hierarchy
│   │   └── logger.py                # Structured terminal logger
│   ├── helpers/
│   │   └── meeting.py               # Google Meet URL cleaner & code extractor
│   ├── models/
│   │   ├── bot.py                   # Domain models & BotStatusEnum
│   │   └── __init__.py
│   ├── schemas/
│   │   ├── bot.py                   # Request & response validation schemas
│   │   └── webhook.py               # Webhook payload & ACK schemas
│   ├── services/
│   │   ├── agent.py                 # LiteLLM Groq agent brain & turn-taking gate
│   │   ├── meeting_baas.py          # Speaking Bots & v2 API bridge
│   │   └── session_store.py         # In-memory live session state
│   └── templates/
│       └── index.html               # Single-page UI for live demo
├── cli.py                           # Standalone interactive CLI
├── main.py                          # FastAPI application entrypoint
├── pyproject.toml                   # UV dependencies & project configuration
└── README.md                        # Documentation & setup guide
```

---

## 🔍 Architecture & Live Demo Talking Points

If Pau asks how this was built and structured:
1. **Media Transport Layer (Google Meet WebRTC):**
   - Headless WebRTC browser execution is handled via Meeting BaaS Speaking Bots API.
2. **AI Agent Brain (FastAPI + LiteLLM + Groq):**
   - High-speed inference using `groq/qwen/qwen3.8-27b` for sub-250ms conversational response time.
   - Intelligent addressing filter prevents the bot from talking over other participants.
3. **Real-Time Observability (Webhooks):**
   - Direct callback ingestion at `/webhook` for live session tracking and terminal logs.
