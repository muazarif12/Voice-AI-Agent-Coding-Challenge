"""Shared observability wiring for the FastAPI service.

The LiveKit worker gets structured JSON logging and a per-call correlation id ("room") for
free from the LiveKit CLI (see app/worker.py). The FastAPI service has neither by default —
plain-text uvicorn access logs with no way to tie a log line back to the request that produced
it — so this module gives it the same shape: JSON log lines, and a request id attached to every
log emitted while handling a given request, echoed back as `X-Request-ID` for the caller to
quote when reporting an issue.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_request_id: ContextVar[str | None] = ContextVar("_request_id", default=None)

# Successful requests to these paths are logged at DEBUG instead of INFO (see dispatch).
_QUIET_PATHS = frozenset({"/health"})

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
    "message",
    "asctime",
}


class JSONFormatter(logging.Formatter):
    """Renders one log record per line as JSON, matching the shape of the worker's own
    JSON logs closely enough that both can be searched the same way (by `level`, `logger`,
    `message`, plus whatever structured fields the call site passed via `extra=`)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if (rid := _request_id.get()) is not None:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Route root + uvicorn loggers through JSONFormatter. Idempotent — safe to call from
    both the app module and a test fixture without duplicating handlers."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers on these; replace rather than double-log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = [handler]
        uv_logger.propagate = False


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assigns a request id (reusing an inbound `X-Request-ID` if the caller sent one, e.g. a
    load balancer or another internal service), makes it available to every log line emitted
    while handling the request via the contextvar above, and logs one structured access-log
    line per request — method, path, status, duration — since uvicorn's default access log is
    plain text and carries none of this as structured fields."""

    def __init__(self, app: object) -> None:
        super().__init__(app)
        self._log = logging.getLogger("api.access")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = _request_id.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            self._log.exception(
                "request failed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            # Fly calls /health every 30 seconds; logging each success would bury the call
            # logs. A failing health check is still logged.
            quiet = request.url.path in _QUIET_PATHS and response.status_code < 400
            log = self._log.debug if quiet else self._log.info
            log(
                "request handled",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            _request_id.reset(token)
