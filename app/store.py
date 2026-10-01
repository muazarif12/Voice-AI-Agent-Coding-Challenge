"""Persistence for patient records.

A single ``PatientStore`` object owns a SQLAlchemy session and exposes the handful of
operations the rest of the app needs. The database URL is read from the environment so the
same code runs against a local SQLite file or a managed database without edits; the SQLite
default resolves to an absolute path next to the package rather than the process directory.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    String,
    Uuid,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from app.schema import NAME_MAX_LENGTH, SEX_OPTIONS, NewPatient, PatientChanges


def _normalize_db_url(url: str) -> str:
    """Managed-Postgres providers (Railway, Heroku, ...) hand out a bare `postgres://` or
    `postgresql://` URL with no driver suffix. SQLAlchemy's default resolution for that tries
    to import psycopg2, which isn't installed here (we use psycopg v3) — normalize to the
    driver we actually have rather than requiring every deployment target to set the URL just
    right. Anything already specifying a driver (`postgresql+psycopg://`, `sqlite://`, ...) is
    left untouched.
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


_DEFAULT_DB_FILE = Path(__file__).resolve().parent.parent / "records.db"
DATABASE_URL = _normalize_db_url(os.environ.get("INTAKE_DB_URL", f"sqlite:///{_DEFAULT_DB_FILE}"))

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
_engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    # Recycle connections that a proxy/firewall/DB restart silently dropped instead of
    # surfacing "server closed the connection unexpectedly" mid-call. A no-op for SQLite.
    pool_pre_ping=True,
)
_new_session = sessionmaker(bind=_engine, expire_on_commit=False)


class ORMBase(DeclarativeBase):
    pass


_SEX_VALUES_SQL = ", ".join(f"'{option}'" for option in SEX_OPTIONS)


class PatientRow(ORMBase):
    # Column names follow the spec's data model. The table is `patients` rather than the
    # previous `patient_records`, so an existing database with the old column layout gets a
    # fresh table instead of failing on columns that no longer exist.
    __tablename__ = "patients"
    # The database's own backstop for the data model. app/schema.py does the precise
    # validation (exact state codes, ZIP pattern, phone validity); these checks use only
    # length() and IN, so they behave the same on SQLite and Postgres.
    __table_args__ = (
        CheckConstraint(f"sex IN ({_SEX_VALUES_SQL})", name="ck_patients_sex"),
        CheckConstraint(
            f"length(first_name) BETWEEN 1 AND {NAME_MAX_LENGTH}", name="ck_patients_first_name"
        ),
        CheckConstraint(
            f"length(last_name) BETWEEN 1 AND {NAME_MAX_LENGTH}", name="ck_patients_last_name"
        ),
        CheckConstraint("length(phone_number) = 10", name="ck_patients_phone_number"),
        CheckConstraint("length(address_line_1) >= 1", name="ck_patients_address_line_1"),
        CheckConstraint("length(city) BETWEEN 1 AND 100", name="ck_patients_city"),
        CheckConstraint("length(state) = 2", name="ck_patients_state"),
        CheckConstraint("length(zip_code) IN (5, 10)", name="ck_patients_zip_code"),
        CheckConstraint(
            "emergency_contact_phone IS NULL OR length(emergency_contact_phone) = 10",
            name="ck_patients_emergency_contact_phone",
        ),
    )

    # Native UUID on Postgres; SQLite stores it as 32 hex characters. Either way the API
    # returns the standard hyphenated form (see as_public_dict).
    patient_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    first_name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    last_name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    sex: Mapped[str] = mapped_column(String(20), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255))
    address_line_1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line_2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    zip_code: Mapped[str] = mapped_column(String(10), nullable=False)
    insurance_provider: Mapped[str | None] = mapped_column(String(255))
    insurance_member_id: Mapped[str | None] = mapped_column(String(64))
    preferred_language: Mapped[str] = mapped_column(String(60), nullable=False, default="English")
    emergency_contact_name: Mapped[str | None] = mapped_column(String(255))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # Soft delete: set instead of removing the row. NULL means the patient is active.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def create_schema() -> None:
    """Create tables if they do not yet exist. Safe to call on every startup."""
    ORMBase.metadata.create_all(_engine)


def _utc_iso(value: datetime | None) -> str | None:
    """ISO-8601 timestamp with an explicit UTC offset.

    SQLite returns naive datetimes (its CURRENT_TIMESTAMP is already UTC); Postgres returns
    aware ones in the session's time zone. Normalize both so clients always see `+00:00`.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def as_public_dict(row: PatientRow) -> dict:
    """Serialize a row to the JSON shape the API returns."""
    return {
        "patient_id": str(row.patient_id),
        "first_name": row.first_name,
        "last_name": row.last_name,
        "date_of_birth": row.date_of_birth.strftime("%m/%d/%Y"),
        "sex": row.sex,
        "phone_number": row.phone_number,
        "email": row.email,
        "address_line_1": row.address_line_1,
        "address_line_2": row.address_line_2,
        "city": row.city,
        "state": row.state,
        "zip_code": row.zip_code,
        "insurance_provider": row.insurance_provider,
        "insurance_member_id": row.insurance_member_id,
        "preferred_language": row.preferred_language,
        "emergency_contact_name": row.emergency_contact_name,
        "emergency_contact_phone": row.emergency_contact_phone,
        "created_at": _utc_iso(row.created_at),
        "updated_at": _utc_iso(row.updated_at),
        "deleted_at": _utc_iso(row.deleted_at),
    }


class PatientStore:
    """Thin session wrapper. Prefer using it as a context manager."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @classmethod
    def open(cls) -> PatientStore:
        return cls(_new_session())

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> PatientStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def ping(self) -> None:
        """Round-trip the database. Raises on failure — used by /readyz, which unlike the
        static /health endpoint is meant to actually fail when the DB is unreachable."""
        self._session.execute(select(1))

    # -- reads (soft-deleted patients are never returned) ----------------------

    def get(self, patient_id: uuid.UUID) -> PatientRow | None:
        stmt = select(PatientRow).where(
            PatientRow.patient_id == patient_id,
            PatientRow.deleted_at.is_(None),
        )
        return self._session.scalar(stmt)

    def find_by_phone(self, phone_number: str) -> PatientRow | None:
        stmt = (
            select(PatientRow)
            .where(PatientRow.phone_number == phone_number, PatientRow.deleted_at.is_(None))
            .order_by(PatientRow.created_at.desc())
        )
        return self._session.scalar(stmt)

    def search(
        self,
        *,
        last_name: str | None = None,
        date_of_birth: date | None = None,
        phone_number: str | None = None,
    ) -> list[PatientRow]:
        stmt = select(PatientRow).where(PatientRow.deleted_at.is_(None))
        if last_name:
            # Case-insensitive, so ?last_name=doe finds "Doe".
            stmt = stmt.where(func.lower(PatientRow.last_name) == last_name.strip().lower())
        if date_of_birth:
            stmt = stmt.where(PatientRow.date_of_birth == date_of_birth)
        if phone_number:
            stmt = stmt.where(PatientRow.phone_number == phone_number)
        stmt = stmt.order_by(PatientRow.created_at.desc())
        return list(self._session.scalars(stmt).all())

    def count(self) -> int:
        """Non-deleted patient count — a real SQL COUNT, not fetching rows just to len() them.
        Backs the patient_records_total gauge in app/web.py."""
        stmt = select(func.count()).select_from(PatientRow).where(PatientRow.deleted_at.is_(None))
        return self._session.scalar(stmt) or 0

    # -- writes -------------------------------------------------------------

    def add(self, data: NewPatient) -> PatientRow:
        row = PatientRow(**data.model_dump())
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def apply_changes(self, row: PatientRow, changes: PatientChanges) -> PatientRow:
        # Only fields the caller actually supplied are touched — absent fields are left as-is.
        for field, value in changes.model_dump(exclude_unset=True).items():
            setattr(row, field, value)
        self._session.commit()
        self._session.refresh(row)
        return row

    def soft_delete(self, row: PatientRow) -> PatientRow:
        row.deleted_at = datetime.now(tz=timezone.utc)
        self._session.commit()
        self._session.refresh(row)
        return row
