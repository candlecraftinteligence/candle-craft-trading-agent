from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    source = request.app.state.mission_source
    name = getattr(source, "source_name", "unknown")
    return {"status": "ok", "service": "cci-the-pack", "source": str(name), "live_cci": "false"}
