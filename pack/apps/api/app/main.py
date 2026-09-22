import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.engagement import router as engagement_router
from app.api.health import router as health_router
from app.api.missions import router as missions_router
from app.api.progression import router as progression_router
from app.api.webhook import router as webhook_router
from app.bot.runtime import ensure_bot_optional
from app.db.session import session_scope
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.integrations.cci.protocol import CciMissionSource
from app.services.sync import seed_reference, sync_missions
from app.settings import load_settings

_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]


def create_app(source: CciMissionSource | None = None) -> FastAPI:
    settings = load_settings()
    ensure_bot_optional()
    if settings.live_cci:
        raise RuntimeError("LIVE_CCI must stay false. Live CCI is not connected in this build.")
    if source is None:
        requested = os.environ.get("CCI_SOURCE", "mock").strip().lower()
        if requested != "mock":
            raise RuntimeError(
                "CCI_SOURCE must be 'mock'. Live CCI is not connected in this build."
            )
        source = MockFixtureSource()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            with session_scope() as db:
                seed_reference(db)
                sync_missions(db, app.state.mission_source)
        except Exception:
            # Missions still serve from fixtures when Postgres is down.
            pass
        yield

    app = FastAPI(title="CCI The Pack", version="0.2.0", lifespan=lifespan)
    app.state.mission_source = source
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(missions_router)
    app.include_router(auth_router)
    app.include_router(progression_router)
    app.include_router(engagement_router)
    app.include_router(webhook_router)
    return app


app = create_app()
