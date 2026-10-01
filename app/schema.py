"""Validation and normalization for patient demographics.

This module is the single source of truth for what a valid patient looks like. Both entry
points use it: the REST API (app/web.py) validates request bodies with these models, and the
voice agent (app/flow.py) runs everything the caller says through the same models before
storing it. Field names match the spec's data model (first_name, phone_number, zip_code, ...).

The approach here is composition rather than inheritance: each field carries its own
reusable ``Annotated`` type (with the normalizer baked in), and the record models are
assembled from those types. That keeps the create and patch shapes small and avoids a
tall class hierarchy.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import Annotated, Any

import phonenumbers
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.us_states import STATE_CODES

# --- Accepted vocabularies -------------------------------------------------

SEX_OPTIONS: tuple[str, ...] = ("Male", "Female", "Other", "Decline to Answer")

# Spoken variants a caller might use, mapped onto the canonical option.
_SEX_SYNONYMS = {
    "m": "Male",
    "male": "Male",
    "man": "Male",
    "f": "Female",
    "female": "Female",
    "woman": "Female",
    "other": "Other",
    "nonbinary": "Other",
    "non-binary": "Other",
    "x": "Other",
    "decline": "Decline to Answer",
    "declined": "Decline to Answer",
    "decline to answer": "Decline to Answer",
    "prefer not to say": "Decline to Answer",
    "prefer not to answer": "Decline to Answer",
    "no answer": "Decline to Answer",
}

NAME_MAX_LENGTH = 50
# Letters plus hyphens and apostrophes, per the spec. Spaces are also allowed so multi-part
# names like "Mary Ann" or "De La Cruz" aren't rejected.
_NAME_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z '\-]*$")
_ZIP_SHAPE = re.compile(r"^\d{5}(?:-\d{4})?$")
_MEMBER_ID_SEPARATORS = re.compile(r"[\s\-]")
_BIRTH_FORMATS = (
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m-%d-%Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%b %d, %Y",
)


# --- Normalizers -----------------------------------------------------------


def tidy_name(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        raise ValueError("name cannot be empty")
    if len(text) > NAME_MAX_LENGTH:
        raise ValueError(f"name must be at most {NAME_MAX_LENGTH} characters")
    if not _NAME_ALLOWED.fullmatch(text):
        raise ValueError("name may only contain letters, spaces, hyphens, or apostrophes")
    return text


def to_national_phone(raw: str) -> str:
    """Parse any US-dialable form into its 10-digit national number."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("phone number cannot be empty")
    try:
        parsed = phonenumbers.parse(text, "US")
    except phonenumbers.NumberParseException:
        raise ValueError("phone number could not be understood") from None
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("that is not a valid US phone number")
    return phonenumbers.national_significant_number(parsed)


def parse_birth_date(raw: object) -> date:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        candidate = raw.strip()
        for fmt in _BIRTH_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
        raise ValueError("date of birth must look like 05/01/1990, 1990-05-01, or May 1 1990")
    raise ValueError("date of birth is not a recognizable date")


def reject_future_date(value: date) -> date:
    if value > date.today():
        raise ValueError("date of birth cannot be in the future")
    return value


def canonical_state(raw: str) -> str:
    code = (raw or "").strip().upper()
    if code not in STATE_CODES:
        raise ValueError("state must be a valid two-letter US code, e.g. CA")
    return code


def canonical_zip(raw: str) -> str:
    text = (raw or "").strip()
    if not _ZIP_SHAPE.fullmatch(text):
        raise ValueError("postal code must be 12345 or 12345-6789")
    return text


_EmailAdapter = TypeAdapter(EmailStr)


def canonical_email(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("email cannot be empty")
    try:
        return _EmailAdapter.validate_python(text)
    except ValidationError:
        raise ValueError("that doesn't look like a valid email address") from None


def canonical_sex(raw: str) -> str:
    key = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if key in _SEX_SYNONYMS:
        return _SEX_SYNONYMS[key]
    # Allow the caller to say the canonical value directly, any casing.
    for option in SEX_OPTIONS:
        if key == option.lower():
            return option
    raise ValueError(f"sex must be one of: {', '.join(SEX_OPTIONS)}")


def canonical_member_id(raw: str) -> str:
    """Member IDs are alphanumeric. Callers often read them out with dashes or pauses
    ("ABC dash 123"), so spaces and hyphens are dropped rather than rejected."""
    text = _MEMBER_ID_SEPARATORS.sub("", raw or "").upper()
    if not text:
        raise ValueError("member id cannot be empty")
    if len(text) > 64:
        raise ValueError("member id must be at most 64 characters")
    if not (text.isascii() and text.isalnum()):
        raise ValueError("member id may only contain letters and digits")
    return text


def field_errors(errors: Iterable[Any]) -> dict[str, str]:
    """Flatten Pydantic error dicts into ``{field: reason}``.

    Used for the API's 422 response body and for the voice agent's re-prompts, so both say
    which field was wrong and why (e.g. ``{"phone_number": "that is not a valid US phone
    number"}``).
    """
    problems: dict[str, str] = {}
    for err in errors:
        # FastAPI prefixes the location with where the value came from; drop that.
        loc = [str(part) for part in err["loc"] if part not in ("body", "query", "path")]
        field = ".".join(loc) or "body"
        problems.setdefault(field, err["msg"].removeprefix("Value error, "))
    return problems


# --- Reusable field types --------------------------------------------------
# Validators run after Pydantic's own str check (AfterValidator), so a wrong JSON type such as
# a number for phone_number is a clean 422 instead of crashing the normalizer.


def _text(max_length: int) -> Any:
    """A trimmed, non-empty string no longer than its database column."""
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)
    ]


PersonName = Annotated[str, AfterValidator(tidy_name)]
NationalPhone = Annotated[str, AfterValidator(to_national_phone)]
BirthDate = Annotated[date, BeforeValidator(parse_birth_date), AfterValidator(reject_future_date)]
StateCode = Annotated[str, AfterValidator(canonical_state)]
PostalCode = Annotated[str, AfterValidator(canonical_zip)]
SexValue = Annotated[str, AfterValidator(canonical_sex)]
EmailAddress = Annotated[str, AfterValidator(canonical_email)]
MemberId = Annotated[str, AfterValidator(canonical_member_id)]
AddressLine = _text(255)
CityName = _text(100)
InsurerName = _text(255)
ContactName = _text(255)
LanguageName = _text(60)


# --- Record models ---------------------------------------------------------


class NewPatient(BaseModel):
    """Everything required to register a patient. Used by the API POST and the save tool."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    first_name: PersonName
    last_name: PersonName
    date_of_birth: BirthDate
    sex: SexValue
    phone_number: NationalPhone
    email: EmailAddress | None = None
    address_line_1: AddressLine
    address_line_2: AddressLine | None = None
    city: CityName
    state: StateCode
    zip_code: PostalCode
    insurance_provider: InsurerName | None = None
    insurance_member_id: MemberId | None = None
    preferred_language: LanguageName = "English"
    emergency_contact_name: ContactName | None = None
    emergency_contact_phone: NationalPhone | None = None


# Columns a stored patient must always have. An update may leave these out, but must not set
# them to null (that would violate the database's NOT NULL constraint and surface as a 500).
_NOT_NULL_FIELDS = (
    "first_name",
    "last_name",
    "date_of_birth",
    "sex",
    "phone_number",
    "address_line_1",
    "city",
    "state",
    "zip_code",
    "preferred_language",
)


class PatientChanges(BaseModel):
    """A partial update. Only the fields actually supplied are applied downstream."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    first_name: PersonName | None = None
    last_name: PersonName | None = None
    date_of_birth: BirthDate | None = None
    sex: SexValue | None = None
    phone_number: NationalPhone | None = None
    email: EmailAddress | None = None
    address_line_1: AddressLine | None = None
    address_line_2: AddressLine | None = None
    city: CityName | None = None
    state: StateCode | None = None
    zip_code: PostalCode | None = None
    insurance_provider: InsurerName | None = None
    insurance_member_id: MemberId | None = None
    preferred_language: LanguageName | None = None
    emergency_contact_name: ContactName | None = None
    emergency_contact_phone: NationalPhone | None = None

    @field_validator(*_NOT_NULL_FIELDS, mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: object) -> object:
        # Only runs for fields present in the request, so omitting a field is still fine.
        if value is None:
            raise ValueError("this field is required and cannot be null")
        return value
