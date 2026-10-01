"""API tests exercising the full create/read/filter/update/delete flow."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.store import PatientStore
from app.web import app, store_dependency

NEW_PATIENT = dict(
    first_name="Grace",
    last_name="Hopper",
    date_of_birth="12/09/1906",
    sex="female",
    phone_number="(212) 736-5000",
    address_line_1="1 Navy Yard",
    city="Arlington",
    state="va",
    zip_code="22202",
)


@pytest.fixture()
def client():
    # The `with` block runs the lifespan, which creates the schema.
    with TestClient(app) as c:
        yield c


def _create(client) -> dict:
    return client.post("/patients", json=NEW_PATIENT).json()["data"]


def test_health(client):
    assert client.get("/health").json() == {"data": {"status": "up"}, "error": None}


def test_dashboard_page_is_served(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Registered Patients" in resp.text


def test_readyz(client):
    assert client.get("/readyz").json()["data"] == {"status": "ready"}


def _patient_records_total(metrics_text: str) -> float:
    for line in metrics_text.splitlines():
        if line.startswith("patient_records_total "):
            return float(line.split()[1])
    raise AssertionError("patient_records_total not found in /metrics output")


def test_metrics_reflects_real_patient_count(client):
    # patient_records_total is computed fresh on each scrape (Gauge.set_function), straight
    # from the database. Deleted rows must not count. Other tests in this file share the
    # same DB, so assert on the *delta* rather than an absolute count.
    before = _patient_records_total(client.get("/metrics").text)

    created = _create(client)
    assert _patient_records_total(client.get("/metrics").text) == before + 1

    client.delete(f"/patients/{created['patient_id']}")
    assert _patient_records_total(client.get("/metrics").text) == before


def test_create_normalizes_and_returns_201_in_envelope(client):
    resp = client.post("/patients", json=NEW_PATIENT)
    assert resp.status_code == 201
    body = resp.json()
    assert body["error"] is None
    patient = body["data"]
    assert patient["phone_number"] == "2127365000"
    assert patient["state"] == "VA"
    assert patient["sex"] == "Female"
    assert patient["date_of_birth"] == "12/09/1906"
    assert uuid.UUID(patient["patient_id"])  # standard UUID string
    assert patient["created_at"].endswith("+00:00")
    assert patient["deleted_at"] is None


def test_create_rejects_bad_phone_with_422_naming_the_field(client):
    resp = client.post("/patients", json=dict(NEW_PATIENT, phone_number="12"))
    assert resp.status_code == 422
    body = resp.json()
    assert body["data"] is None
    assert "phone_number" in body["error"]["fields"]


def test_create_rejects_future_birth_date(client):
    resp = client.post("/patients", json=dict(NEW_PATIENT, date_of_birth="01/01/2999"))
    assert resp.status_code == 422
    assert "date_of_birth" in resp.json()["error"]["fields"]


def test_get_404_and_400(client):
    created = _create(client)
    got = client.get(f"/patients/{created['patient_id']}")
    assert got.status_code == 200
    assert got.json()["data"]["last_name"] == "Hopper"

    missing = client.get(f"/patients/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json() == {"data": None, "error": {"message": "No patient with that id"}}

    assert client.get("/patients/not-a-uuid").status_code == 400


def test_filter_by_phone_last_name_and_birth_date(client):
    _create(client)
    # Filters accept human formatting and match the normalized stored values.
    for params in (
        {"phone_number": "212-736-5000"},
        {"last_name": "hopper"},  # case-insensitive
        {"date_of_birth": "12/09/1906"},
    ):
        found = client.get("/patients", params=params).json()["data"]
        assert any(p["last_name"] == "Hopper" for p in found), params


def test_unparseable_filter_is_400(client):
    assert client.get("/patients", params={"phone_number": "123"}).status_code == 400
    assert client.get("/patients", params={"date_of_birth": "someday"}).status_code == 400


def test_put_only_changes_supplied_fields(client):
    pid = _create(client)["patient_id"]
    for method in ("put", "patch"):
        resp = getattr(client, method)(f"/patients/{pid}", json={"city": "Reston"})
        assert resp.status_code == 200
        patient = resp.json()["data"]
        # The one supplied field changed; everything else is preserved (no null-out).
        assert patient["city"] == "Reston"
        assert patient["first_name"] == "Grace"
        assert patient["zip_code"] == "22202"


def test_put_rejects_null_for_required_field_with_422(client):
    pid = _create(client)["patient_id"]
    resp = client.put(f"/patients/{pid}", json={"first_name": None})
    assert resp.status_code == 422
    assert "first_name" in resp.json()["error"]["fields"]


def test_delete_is_soft_and_hides_from_reads(client):
    pid = _create(client)["patient_id"]
    deleted = client.delete(f"/patients/{pid}").json()["data"]
    assert deleted["deleted_at"] is not None
    assert client.get(f"/patients/{pid}").status_code == 404
    remaining = client.get("/patients", params={"last_name": "Hopper"}).json()["data"]
    assert all(p["patient_id"] != pid for p in remaining)


def test_unexpected_error_is_500_in_envelope():
    class BrokenStore:
        def search(self, **_filters):
            raise RuntimeError("database exploded")

    app.dependency_overrides[store_dependency] = lambda: BrokenStore()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/patients")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 500
    # The internal error text must not leak to the client.
    assert resp.json() == {"data": None, "error": {"message": "Internal server error"}}


def test_transcripts_are_linked_to_the_patient(client):
    pid = _create(client)["patient_id"]
    assert client.get(f"/patients/{pid}/transcripts").json()["data"] == []

    turns = [
        {"speaker": "agent", "text": "Hello, are you ready to begin?"},
        {"speaker": "caller", "text": "Yes."},
    ]
    with PatientStore.open() as store:  # what the worker does when a call ends
        store.add_transcript(uuid.UUID(pid), "call-room-1", turns)

    transcripts = client.get(f"/patients/{pid}/transcripts").json()["data"]
    assert len(transcripts) == 1
    assert transcripts[0]["patient_id"] == pid
    assert transcripts[0]["room_name"] == "call-room-1"
    assert transcripts[0]["turns"] == turns

    assert client.get(f"/patients/{uuid.uuid4()}/transcripts").status_code == 404
