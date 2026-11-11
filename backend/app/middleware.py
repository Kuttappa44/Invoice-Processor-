"""ASGI middleware registered from the composition root."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import get_settings


def register_cors(app: FastAPI) -> None:
    """Same origin rules as the previous api_server.py CORS block."""
    cors_origins = get_settings().cors_allow_origins.split(",")
    if cors_origins == ["*"]:
        # Default: allow all origins (for localhost development)
        allowed_origins = ["*"]
    else:
        # Use specified origins
        allowed_origins = [origin.strip() for origin in cors_origins]
        # Always add localhost origins for local development
        localhost_origins = [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ]
        allowed_origins = list(set(allowed_origins + localhost_origins))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
