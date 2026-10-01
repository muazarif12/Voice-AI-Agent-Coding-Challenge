-- Postgres schema for the patient intake voice agent.
--
-- You do NOT need to run this by hand for the app to work: app/worker.py calls
-- create_schema() (app/store.py -> ORMBase.metadata.create_all()) once per worker process at
-- startup, and app/web.py does the same in its lifespan handler — the table is created
-- automatically the first time either service connects to an empty database.
--
-- This file exists so the schema can be inspected or run manually (e.g. in a Postgres query
-- console) without spinning up the app first, and as documentation. It's kept in sync by
-- hand with the SQLAlchemy model in app/store.py (PatientRow) — that model is the source of
-- truth; if the two ever disagree, the ORM wins because that's what actually runs.

CREATE TABLE IF NOT EXISTS patients (
    patient_id              UUID          PRIMARY KEY, -- generated app-side (uuid4), no DB default
    first_name              VARCHAR(50)   NOT NULL,
    last_name               VARCHAR(50)   NOT NULL,
    date_of_birth           DATE          NOT NULL,
    sex                     VARCHAR(20)   NOT NULL,
    phone_number            VARCHAR(10)   NOT NULL,
    email                   VARCHAR(255),
    address_line_1          VARCHAR(255)  NOT NULL,
    address_line_2          VARCHAR(255),
    city                    VARCHAR(100)  NOT NULL,
    state                   VARCHAR(2)    NOT NULL,
    zip_code                VARCHAR(10)   NOT NULL,
    insurance_provider      VARCHAR(255),
    insurance_member_id     VARCHAR(64),
    preferred_language      VARCHAR(60)   NOT NULL, -- app-side default 'English', not a DB default
    emergency_contact_name  VARCHAR(255),
    emergency_contact_phone VARCHAR(10),
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ,              -- soft delete marker; NULL = active

    -- Backstop checks; app/schema.py does the precise validation (state list, ZIP pattern, ...).
    CONSTRAINT ck_patients_sex CHECK (sex IN ('Male', 'Female', 'Other', 'Decline to Answer')),
    CONSTRAINT ck_patients_first_name CHECK (length(first_name) BETWEEN 1 AND 50),
    CONSTRAINT ck_patients_last_name CHECK (length(last_name) BETWEEN 1 AND 50),
    CONSTRAINT ck_patients_phone_number CHECK (length(phone_number) = 10),
    CONSTRAINT ck_patients_address_line_1 CHECK (length(address_line_1) >= 1),
    CONSTRAINT ck_patients_city CHECK (length(city) BETWEEN 1 AND 100),
    CONSTRAINT ck_patients_state CHECK (length(state) = 2),
    CONSTRAINT ck_patients_zip_code CHECK (length(zip_code) IN (5, 10)),
    CONSTRAINT ck_patients_emergency_contact_phone
        CHECK (emergency_contact_phone IS NULL OR length(emergency_contact_phone) = 10)
);

-- Matches the ORM's phone lookup (find_by_phone / check_returning in app/flow.py) and
-- SQLAlchemy's own default index-naming convention for index=True on this column.
CREATE INDEX IF NOT EXISTS ix_patients_phone_number ON patients (phone_number);

-- One row per phone call that registered or updated a patient (see app/worker.py).
CREATE TABLE IF NOT EXISTS call_transcripts (
    transcript_id UUID          PRIMARY KEY,       -- generated app-side (uuid4)
    patient_id    UUID          NOT NULL REFERENCES patients (patient_id),
    room_name     VARCHAR(255)  NOT NULL,          -- LiveKit room; also tags that call's logs
    turns         JSON          NOT NULL,          -- [{"speaker": "agent"|"caller", "text": ...}]
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_call_transcripts_patient_id ON call_transcripts (patient_id);

-- Optional, for parity with the ORM's onupdate=func.now() on `updated_at`: SQLAlchemy already
-- sets updated_at correctly whenever the app itself does an UPDATE (it issues now() as part of
-- that statement), so this trigger only matters if someone updates a row directly in psql
-- rather than through the app.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_patients_updated_at ON patients;
CREATE TRIGGER trg_patients_updated_at
    BEFORE UPDATE ON patients
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();
