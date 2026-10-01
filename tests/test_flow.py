"""Flow-level tests: the IntakeState -> schema/store contract used by save_record.

These exercise the data assembly without a live LiveKit session (the stage tools that read
`self.session.userdata` are covered indirectly here through the same state object).
"""

import datetime as dt

from app.flow import (
    ContactStage,
    ExtrasStage,
    IdentityStage,
    IntakeState,
    ReviewStage,
    WelcomeStage,
    _spoken_lines,
    _validate,
)
from app.schema import NewPatient, PatientChanges


def _full_state() -> IntakeState:
    return IntakeState(
        first_name="Katherine",
        last_name="Johnson",
        date_of_birth=dt.date(1918, 8, 26),
        sex="Female",
        phone_number="2127365000",
        address_line_1="100 Main St",
        city="Hampton",
        state="VA",
        zip_code="23666",
    )


def test_collected_omits_unset_optional_fields():
    collected = _full_state().collected()
    assert "email" not in collected
    assert "insurance_provider" not in collected
    # Language is only sent when the caller gave one, so an update never overwrites the
    # language on file with a default.
    assert "preferred_language" not in collected


def test_collected_state_builds_valid_new_patient():
    record = NewPatient(**_full_state().collected())
    assert record.first_name == "Katherine"
    assert record.state == "VA"
    assert record.phone_number == "2127365000"
    assert record.preferred_language == "English"  # schema default for new records


def test_collected_state_builds_partial_changes():
    state = _full_state()
    state.city = "Newport News"
    changes = PatientChanges(**state.collected())
    dumped = changes.model_dump(exclude_unset=True)
    assert dumped["city"] == "Newport News"
    # An unset optional like insurance_member_id must not appear in the update.
    assert "insurance_member_id" not in dumped


def test_validate_reports_problems_per_field():
    values, problems = _validate(phone_number="123", city="Reston", email=None)
    assert values == {}
    assert set(problems) == {"phone_number"}

    values, problems = _validate(phone_number="(212) 736-5000", email=None)
    assert problems == {}
    assert values == {"phone_number": "2127365000"}  # None means "not given"


def test_spoken_lines_are_separate_sentences_formatted_for_speech():
    # _spoken_lines feeds the paced, line-by-line TTS readback in ReviewStage.on_enter —
    # each fact must be its own sentence (not one comma-joined line) so the caller hears a
    # real pause after each one instead of a single run-on readback.
    lines = _spoken_lines(_full_state())
    joined = " ".join(lines)
    assert "Katherine Johnson" in joined
    assert "August 26, 1918" in joined
    assert "2 1 2, 7 3 6, 5 0 0 0" in joined
    assert "Hampton, VA, zip code 2 3 6 6 6" in joined
    assert all(line.endswith(".") for line in lines)


def test_review_stage_can_end_the_call():
    # A caller who wants to stop without saving can hang up from the review; save_record
    # ends the call itself after a successful (or failed) save.
    tool_ids = {getattr(tool, "id", None) for tool in ReviewStage().tools}
    assert "end_call" in tool_ids


def test_every_stage_can_correct_details_and_start_over():
    # Corrections and restarts must work at any point in the call, not only at the review.
    for stage in (WelcomeStage(), IdentityStage(), ContactStage(), ExtrasStage(), ReviewStage()):
        names = {tool.info.name for tool in stage.tools if hasattr(tool, "info")}
        assert {"amend", "start_over"} <= names, type(stage).__name__
        assert "start_over" in stage.instructions  # shared rules are in every prompt


def test_reset_clears_everything_for_start_over():
    state = _full_state()
    state.updating = True
    state.reset()
    assert state == IntakeState()
