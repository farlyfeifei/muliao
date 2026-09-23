"""FastAPI router for the standalone voice subsystem.

Integration intentionally happens elsewhere with::

    app.include_router(voice.api.router, prefix="/api/voice")

The routes do not import or mutate chat state.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .service import VoicePermissionDenied, VoiceService


@asynccontextmanager
async def _voice_lifespan(_app):
    try:
        yield
    finally:
        shutdown_voice_service()


router = APIRouter(tags=["voice"], lifespan=_voice_lifespan)

_service: VoiceService | None = None
_service_lock = threading.Lock()


class VoiceCommandRequest(BaseModel):
    text: str


def get_voice_service() -> VoiceService:
    """Return the process singleton without constructing voice hardware eagerly."""

    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = VoiceService()
    return _service


def set_voice_service(service: VoiceService | None) -> None:
    """Replace the singleton, stopping the previous process-owned instance first."""

    global _service
    with _service_lock:
        previous = _service
        _service = service
    if previous is not None and previous is not service:
        previous.stop()


def shutdown_voice_service() -> None:
    """Release microphone and runtime resources with the containing FastAPI app."""

    global _service
    with _service_lock:
        service = _service
        _service = None
    if service is not None:
        service.stop()


VoiceServiceDependency = Annotated[VoiceService, Depends(get_voice_service)]


@router.get("/status")
def voice_status(service: VoiceServiceDependency) -> dict[str, Any]:
    return {"ok": True, **service.status()}


@router.post("/start")
def voice_start(service: VoiceServiceDependency) -> dict[str, Any]:
    try:
        return {"ok": True, **service.start()}
    except VoicePermissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "permission_denied",
                "message": str(exc),
                "status": service.status(),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "voice_start_failed", "message": f"{type(exc).__name__}: {exc}"},
        ) from exc


@router.post("/stop")
def voice_stop(service: VoiceServiceDependency) -> dict[str, Any]:
    try:
        return {"ok": True, **service.stop()}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "voice_stop_failed", "message": f"{type(exc).__name__}: {exc}"},
        ) from exc


@router.post("/command/test")
def voice_test_command(
    request: VoiceCommandRequest,
    service: VoiceServiceDependency,
) -> dict[str, Any]:
    try:
        return service.test_command(request.text)
    except VoicePermissionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "permission_denied", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_command", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "voice_test_failed", "message": f"{type(exc).__name__}: {exc}"},
        ) from exc


@router.get("/events")
async def voice_events(
    request: Request,
    service: VoiceServiceDependency,
) -> StreamingResponse:
    async def stream():
        try:
            async for event in service.stream_events(request.is_disconnected):
                event_type = str(event.get("type", ""))
                if not event_type.startswith("voice."):
                    continue
                yield _encode_sse(event_type, event)
        except asyncio.CancelledError:
            # StreamingResponse closes the generator; VoiceService then removes the
            # subscriber in its own ``finally`` block.
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _encode_sse(event_type: str, envelope: dict[str, Any]) -> str:
    """Serialize exactly one namespaced voice event."""

    if not event_type.startswith("voice."):
        raise ValueError("SSE event type must use the voice.* namespace")
    body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_type}\ndata: {body}\n\n"


__all__ = [
    "VoiceCommandRequest",
    "get_voice_service",
    "router",
    "set_voice_service",
]
