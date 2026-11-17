from fastapi import APIRouter
from app.api.v1.endpoints import bots, webhooks, streaming

api_router = APIRouter()
api_router.include_router(bots.router)
api_router.include_router(webhooks.router)
api_router.include_router(streaming.router)

