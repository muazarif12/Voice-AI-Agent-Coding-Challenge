"""Load two demo patients into the database.

    uv run python -m app.seed

A demo patient is skipped if a patient with the same phone number already exists, so it's
safe to run more than once.
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
    """Insert any demo patients not already on file. Returns how many were added."""
    create_schema()
    added = 0
    with PatientStore.open() as store:
        for patient in DEMO_PATIENTS:
            record = NewPatient(**patient)
            if store.find_by_phone(record.phone_number) is None:
                store.add(record)
                added += 1
    return added


if __name__ == "__main__":
    added = seed()
    print(f"Added {added} demo patient(s).")
