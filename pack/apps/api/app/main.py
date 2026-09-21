import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.missions import router as missions_router
from app.integrations.cci.mock_fixture_source import MockFixtureSource
from app.integrations.cci.protocol import CciMissionSource

_DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]


def create_app(source: CciMissionSource | None = None) -> FastAPI:
    if source is None:
        requested = os.environ.get("CCI_SOURCE", "mock").strip().lower()
        if requested != "mock":
            raise RuntimeError(
                "CCI_SOURCE must be 'mock'. Live CCI is not connected in this build."
            )
        source = MockFixtureSource()

    app = FastAPI(title="CCI The Pack", version="0.1.0")
    app.state.mission_source = source
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(missions_router)
    return app


app = create_app()
