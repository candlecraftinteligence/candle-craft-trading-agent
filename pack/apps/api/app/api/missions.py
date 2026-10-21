from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.integrations.cci.errors import SetupNotFoundError

router = APIRouter()


def _source(request: Request) -> Any:
    return request.app.state.mission_source


@router.get("/api/missions")
def list_missions(request: Request) -> dict[str, Any]:
    source = _source(request)
    return {
        "source": getattr(source, "source_name", "mock"),
        "missions": [setup.model_dump() for setup in source.list_setups()],
    }


@router.get("/api/missions/{cci_setup_id}")
def get_mission(cci_setup_id: str, request: Request) -> dict[str, Any]:
    source = _source(request)
    try:
        setup = source.get_setup(cci_setup_id)
    except SetupNotFoundError:
        raise HTTPException(status_code=404, detail="Mission not found") from None
    return setup.model_dump()
