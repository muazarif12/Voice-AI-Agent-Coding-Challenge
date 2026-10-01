"""Load two demo patients into an empty database.

    uv run python -m app.seed

Does nothing if the database already holds patients, so it's safe to run more than once.
Records go through the same NewPatient validation as the API. The phone numbers use the
reserved 555-01xx range, so they don't belong to anyone.
"""

from __future__ import annotations

from app.schema import NewPatient
from app.store import PatientStore, create_schema

DEMO_PATIENTS = (
    {
        "first_name": "John",
        "last_name": "Smith",
        "date_of_birth": "04/12/1985",
        "sex": "Male",
        "phone_number": "(202) 555-0143",
        "email": "john.smith@example.com",
        "address_line_1": "1600 Pennsylvania Ave NW",
        "city": "Washington",
        "state": "DC",
        "zip_code": "20500",
        "insurance_provider": "Blue Cross Blue Shield",
        "insurance_member_id": "XYZ123456789",
    },
    {
        "first_name": "Maria",
        "last_name": "Garcia",
        "date_of_birth": "09/30/1972",
        "sex": "Female",
        "phone_number": "(312) 555-0187",
        "address_line_1": "233 S Wacker Dr",
        "address_line_2": "Apt 4B",
        "city": "Chicago",
        "state": "IL",
        "zip_code": "60606",
        "preferred_language": "Spanish",
        "emergency_contact_name": "Luis Garcia",
        "emergency_contact_phone": "(312) 555-0199",
    },
)


def seed() -> int:
    """Insert the demo patients if there are none yet. Returns how many were added."""
    create_schema()
    with PatientStore.open() as store:
        if store.count() > 0:
            return 0
        for patient in DEMO_PATIENTS:
            store.add(NewPatient(**patient))
    return len(DEMO_PATIENTS)


if __name__ == "__main__":
    added = seed()
    print(f"Added {added} demo patients." if added else "Database already has patients; skipped.")
