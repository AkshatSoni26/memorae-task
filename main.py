import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from app.core.config import settings
from app.core.logger import setup_logging
from app.api.v1.router import api_router
from app.api.v1.endpoints.webhooks import router as webhook_router

# Setup application logging
setup_logging(settings.log_level)

app = FastAPI(
    title="Memorae - Google Meet AI Agent",
    description="FastAPI service to deploy and control real-time conversational AI meeting participants",
    version="1.0.0",
)

@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "healthy",
        "api_key_configured": bool(settings.meeting_baas_api_key),
        "webhook_secret_configured": bool(settings.meeting_baas_webhook_secret),
        "llm_model": settings.llm_model
    }

@app.get("/", response_class=HTMLResponse, tags=["UI"])
def serve_ui():
    template_path = os.path.join(os.path.dirname(__file__), "app", "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Memorae Meeting Bot API</h1><p>Visit <a href='/docs'>/docs</a> for Swagger UI.</p>")

# Mount /webhook directly at root (matches: http://<domain>/webhook)
app.include_router(webhook_router)

# Mount /api/v1 prefix
app.include_router(api_router, prefix="/api/v1")
