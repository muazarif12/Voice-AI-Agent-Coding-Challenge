"""REST service for reading and maintaining patient records.

Every JSON response uses the same envelope, so clients can handle them uniformly:

    success:  {"data": <patient or list of patients>, "error": null}
    failure:  {"data": null, "error": {"message": "...", "fields": {...}}}  # "fields": 422 only

Status codes:
    200  OK (201 on create)
    400  a malformed URL value: a patient id that isn't a UUID, or an unparseable query filter
    404  no active (non-deleted) patient with that id
    422  the JSON body failed validation; error.fields says which field and why
    500  unexpected server error; details go to the logs, never to the client
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.obs import RequestIDMiddleware, configure_logging
from app.schema import (
    NewPatient,
    PatientChanges,
    field_errors,
    parse_birth_date,
    to_national_phone,
)
from app.store import (
    PatientRow,
    PatientStore,
    as_public_dict,
    as_transcript_dict,
    create_schema,
)

configure_logging()
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_schema()
    yield


app = FastAPI(title="Patient Intake Records", version="0.1.0", lifespan=lifespan)
app.add_middleware(RequestIDMiddleware)

# /metrics (Prometheus format): request count, latency, and in-progress requests by
# method/path/status, plus the patient count below.
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# How many active patients are on file. set_function() re-runs the COUNT query on every
# scrape, so the number is always current.
_patients_total_gauge = Gauge(
    "patient_records_total", "Non-deleted patient records currently in the database"
)


def _count_patients() -> float:
    with PatientStore.open() as store:
        return float(store.count())


_patients_total_gauge.set_function(_count_patients)


# --- Response envelope -----------------------------------------------------


def _envelope(data: Any = None, error: dict | None = None) -> dict:
    return {"data": data, "error": error}


def _error_response(
    status_code: int,
    message: str,
    fields: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"message": message}
    if fields:
        error["fields"] = fields
    return JSONResponse(status_code=status_code, content=_envelope(error=error), headers=headers)


# These three handlers replace FastAPI's default error bodies ({"detail": ...}, and a
# plain-text "Internal Server Error" for crashes) so errors use the envelope too.


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Covers our own 400/404s plus framework ones like 404 for unknown paths and 405.
    return _error_response(exc.status_code, str(exc.detail), headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY, "Validation failed", field_errors(exc.errors())
    )


@app.exception_handler(Exception)
async def _unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
    # The traceback is already logged (with the request id) by RequestIDMiddleware.
    return _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal server error")


# --- Helpers ---------------------------------------------------------------


def store_dependency() -> Iterator[PatientStore]:
    store = PatientStore.open()
    try:
        yield store
    finally:
        store.close()


def _find_patient(store: PatientStore, patient_id: str) -> PatientRow:
    """Look up an active patient by the {patient_id} path segment: 400 if it isn't a UUID,
    404 if no active patient has it."""
    try:
        parsed_id = uuid.UUID(patient_id)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "patient_id must be a UUID") from None
    row = store.get(parsed_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No patient with that id")
    return row


# --- Endpoints -------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Liveness only: is the process up and serving requests at all. Deliberately does not
    touch the database — a slow/unreachable DB should show up as a /readyz failure, not take
    down the liveness probe and cause an unnecessary restart-loop of an otherwise-fine process."""
    return _envelope({"status": "up"})


@app.get("/readyz")
def ready(store: PatientStore = Depends(store_dependency)) -> dict:
    """Readiness: can this instance actually serve traffic right now. Runs a real round-trip
    to the database, unlike /health — a dead/unreachable DB fails this, which is what should
    pull the instance out of a load balancer's rotation."""
    try:
        store.ping()
    except Exception as exc:
        # The real error is logged; the response stays generic so it can't leak connection
        # details to whoever is probing the endpoint.
        log.exception("readiness check failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unreachable"
        ) from exc
    return _envelope({"status": "ready"})


@app.get("/patients")
def list_patients(
    last_name: str | None = None,
    date_of_birth: str | None = None,
    phone_number: str | None = None,
    store: PatientStore = Depends(store_dependency),
) -> dict:
    """List active patients, optionally filtered. Filters accept the same formats as the
    body (e.g. date_of_birth=05/01/1990, phone_number=(212) 736-5000); an unparseable
    filter is a 400 rather than silently matching nothing."""
    try:
        dob = parse_birth_date(date_of_birth) if date_of_birth else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"date_of_birth: {exc}") from None
    try:
        phone = to_national_phone(phone_number) if phone_number else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"phone_number: {exc}") from None

    rows = store.search(last_name=last_name, date_of_birth=dob, phone_number=phone)
    return _envelope([as_public_dict(row) for row in rows])


@app.get("/patients/{patient_id}")
def read_patient(patient_id: str, store: PatientStore = Depends(store_dependency)) -> dict:
    return _envelope(as_public_dict(_find_patient(store, patient_id)))


@app.get("/patients/{patient_id}/transcripts")
def list_transcripts(patient_id: str, store: PatientStore = Depends(store_dependency)) -> dict:
    """Transcripts of the phone calls that registered or updated this patient, newest first."""
    row = _find_patient(store, patient_id)
    return _envelope([as_transcript_dict(t) for t in store.transcripts_for(row.patient_id)])


@app.post("/patients", status_code=status.HTTP_201_CREATED)
def create_patient(body: NewPatient, store: PatientStore = Depends(store_dependency)) -> dict:
    return _envelope(as_public_dict(store.add(body)))


# PUT is the method the spec asks for; PATCH is kept as an alias. Both are partial updates:
# only the fields present in the body change.
@app.api_route("/patients/{patient_id}", methods=["PUT", "PATCH"])
def update_patient(
    patient_id: str,
    body: PatientChanges,
    store: PatientStore = Depends(store_dependency),
) -> dict:
    row = _find_patient(store, patient_id)
    return _envelope(as_public_dict(store.apply_changes(row, body)))


@app.delete("/patients/{patient_id}")
def delete_patient(patient_id: str, store: PatientStore = Depends(store_dependency)) -> dict:
    """Soft delete: sets deleted_at and hides the patient from reads; the row is kept."""
    row = _find_patient(store, patient_id)
    return _envelope(as_public_dict(store.soft_delete(row)))
