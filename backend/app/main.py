"""FastAPI composition root: middleware, routers, no product rules."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app.middleware import register_cors
from backend.app.modules.invoices.router import router as invoices_router
from backend.app.modules.mailbox.router import router as mailbox_router
from backend.app.modules.stats.router import router as stats_router
from backend.app.modules.tasks.router import router as tasks_router
from backend.app.spa import FRONTEND_DIR, router as spa_router
from backend.app.system import router as system_router


def create_app() -> FastAPI:
    application = FastAPI(
        title="Invoice Processor API",
        description="REST API for invoice processing automation: Gmail → OpenAI PDF extraction → Excel matching → API push",
        version="1.1.0",
    )
    register_cors(application)
    if FRONTEND_DIR.exists():
        application.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    application.include_router(tasks_router)
    application.include_router(invoices_router)
    application.include_router(stats_router)
    application.include_router(mailbox_router)
    application.include_router(system_router)
    # Catch-all SPA route must be last so /api/* is not swallowed.
    application.include_router(spa_router)
    return application


app = create_app()
