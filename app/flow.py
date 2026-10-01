"""Staged intake conversation.

Instead of one agent holding a long prompt for the whole call, the intake is broken into
short stages that hand control to one another. Each stage knows only its own slice of the
form and carries just the tools it needs. Collected values accumulate in a single
``IntakeState`` object that rides along on the session's ``userdata``.

Stage order:  Welcome -> Identity -> Contact -> Extras -> Review

Every value the caller gives goes through the same validation the REST API uses
(``_validate`` runs it through ``app.schema.PatientChanges``). A record saved by phone is
therefore held to exactly the same rules as one created with ``POST /patients``.

Prompt design:
- Each stage's ``instructions`` is its system prompt: a few sentences about its own job only.
  Every stage also gets ``_SHARED_RULES`` (corrections and starting over work anywhere).
- The LLM never writes data directly. It calls tools, and the tools validate; a rejected
  value comes back as ``{"reprompt": {field: reason}}`` so the agent re-asks just that field.
- Prompts forbid claiming something was saved or changed unless the tool call succeeded.
- What must be exact is spoken by code, not the LLM: the readback (``_spoken_lines``) and
  the final "You're all set" / error lines.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, fields
from datetime import date

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.beta.tools import EndCallTool
from pydantic import ValidationError

from app.schema import NewPatient, PatientChanges, field_errors
from app.store import PatientStore

# The worker sets ctx.log_context_fields = {"room": ...} once per call (app/worker.py), which
# LiveKit injects into every log record emitted during that job regardless of which logger it
# came from — so a plain stdlib logger here still gets "room" attached automatically, which is
# what makes a saved record traceable back to the specific call that produced it (see
# save_record below) without needing a schema change or a bespoke correlation mechanism.
log = logging.getLogger("intake.flow")

# Spoken when the record can't be saved. Deliberately generic: the real error is logged, and
# a database message read aloud would mean nothing to the caller.
SAVE_FAILED_MESSAGE = (
    "I'm sorry, I ran into a technical problem and couldn't save your registration. "
    "Please call us back a little later. Goodbye."
)

# Appended to every stage's prompt (see IntakeStage), so a caller can fix an earlier answer or
# start over at any point in the call, not only at the final review.
_SHARED_RULES = (
    "At any point in the call: if the caller corrects something they told you earlier (for "
    'example "actually my last name is spelled D-A-V-I-S"), call amend with only the corrected '
    "fields and briefly confirm the change. If they clearly ask to start over, call start_over. "
    "Never say a detail was recorded or changed unless the tool call succeeded."
)

# --- Shared session state --------------------------------------------------


@dataclass
class IntakeState:
    # Field names match the database columns and the API (see app/schema.py).
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    sex: str | None = None
    phone_number: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    email: str | None = None
    insurance_provider: str | None = None
    insurance_member_id: str | None = None
    # None until the caller names one. A new record then gets the schema's "English" default,
    # and an update leaves the language already on file untouched.
    preferred_language: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None

    # Set by check_returning when the phone number matches a record we already hold.
    existing_patient_id: uuid.UUID | None = None
    # True only once the caller agrees to update that record (confirm_update). Until then a
    # save creates a new record, so someone sharing a family phone never overwrites another
    # person's record.
    updating: bool = False
    # Set once save_record succeeds. When the call ends, the worker saves the call transcript
    # linked to this patient (app/worker.py).
    saved_patient_id: uuid.UUID | None = None

    def collected(self) -> dict:
        """Non-empty patient fields, keyed as the schema/store expect them."""
        return {
            field: getattr(self, field)
            for field in PatientChanges.model_fields
            if getattr(self, field) is not None
        }

    def apply(self, values: dict) -> None:
        for field, value in values.items():
            setattr(self, field, value)

    def reset(self) -> None:
        """Back to a blank intake (used by start_over)."""
        for field in fields(self):
            setattr(self, field.name, field.default)

    def as_log_payload(self) -> dict:
        """collected(), with the date of birth as a string so it logs cleanly as JSON."""
        return {
            field: value.isoformat() if isinstance(value, date) else value
            for field, value in self.collected().items()
        }


def _validate(**supplied: object) -> tuple[dict, dict[str, str]]:
    """Check caller-supplied values against the API's rules.

    Returns ``(clean_values, problems)``. ``problems`` maps each rejected field to a short
    reason, so the agent can re-ask for just that field. ``None`` means "not given" and is
    skipped.
    """
    given = {field: value for field, value in supplied.items() if value is not None}
    try:
        changes = PatientChanges(**given)
    except ValidationError as exc:
        return {}, field_errors(exc.errors())
    return changes.model_dump(exclude_unset=True), {}


# --- Readback formatting ---------------------------------------------------
# Raw stored values don't speak well: TTS may read "2127365000" as one big number and
# "1918-08-26" as "1918 dash 08 dash 26". These helpers turn them into what a person says.


def _spell(*groups: str) -> str:
    """'212', '736', '5000' -> '2 1 2, 7 3 6, 5 0 0 0': one character at a time, with a
    short pause between groups."""
    return ", ".join(" ".join(group) for group in groups)


def _spoken_phone(phone: str | None) -> str:
    if not phone:
        return "missing"
    return _spell(phone[:3], phone[3:6], phone[6:])


def _spoken_date(value: date | None) -> str:
    if value is None:
        return "missing"
    return f"{value:%B} {value.day}, {value.year}"  # e.g. "August 26, 1918"


def _spoken_lines(state: IntakeState) -> list[str]:
    """Natural-language sentences for the review readback, one per collected field.

    Kept as short, separate sentences (rather than one comma-joined line) so each can be
    spoken as its own TTS utterance with a deliberate pause after it — see ReviewStage.on_enter.
    """
    address = ", ".join(
        part
        for part in (state.address_line_1, state.address_line_2, f"{state.city}, {state.state}")
        if part
    )
    zip_spoken = _spell(*state.zip_code.split("-")) if state.zip_code else "missing"
    lines = [
        f"Your name is {state.first_name} {state.last_name}.",
        f"Your date of birth is {_spoken_date(state.date_of_birth)}.",
        f"Your sex is {state.sex}.",
        f"Your phone number is {_spoken_phone(state.phone_number)}.",
        f"Your address is {address}, zip code {zip_spoken}.",
    ]
    if state.email:
        lines.append(f"Your email is {state.email}.")
    if state.insurance_provider:
        member = (
            f", member ID {_spell(state.insurance_member_id)}" if state.insurance_member_id else ""
        )
        lines.append(f"Your insurance is {state.insurance_provider}{member}.")
    if state.emergency_contact_name:
        lines.append(
            f"Your emergency contact is {state.emergency_contact_name} at "
            f"{_spoken_phone(state.emergency_contact_phone)}."
        )
    if state.preferred_language:
        lines.append(f"Your preferred language is {state.preferred_language}.")
    return lines


# --- Base stage ------------------------------------------------------------


class IntakeStage(Agent):
    """Base for every stage: shared state access, plus the amend and start_over tools, which
    are available in every stage."""

    def __init__(self, *, instructions: str, **kwargs) -> None:
        super().__init__(instructions=f"{instructions}\n\n{_SHARED_RULES}", **kwargs)

    @property
    def state(self) -> IntakeState:
        return self.session.userdata

    def _store_valid(self, **supplied: object) -> dict:
        """Validate the given fields and, only if all of them pass, save them on the state.
        Returns the tool result for the LLM: ``{"ok": True}`` or the per-field problems."""
        values, problems = _validate(**supplied)
        if problems:
            return {"ok": False, "reprompt": problems}
        self.state.apply(values)
        return {"ok": True}

    @function_tool()
    async def amend(
        self,
        context: RunContext,
        first_name: str | None = None,
        last_name: str | None = None,
        date_of_birth: str | None = None,
        sex: str | None = None,
        phone_number: str | None = None,
        email: str | None = None,
        address_line_1: str | None = None,
        address_line_2: str | None = None,
        city: str | None = None,
        state: str | None = None,
        zip_code: str | None = None,
        insurance_provider: str | None = None,
        insurance_member_id: str | None = None,
        preferred_language: str | None = None,
        emergency_contact_name: str | None = None,
        emergency_contact_phone: str | None = None,
    ) -> dict | None:
        """Correct details the caller already gave earlier in the call, such as a misspelled
        name. Pass only the fields that change."""
        result = self._store_valid(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            sex=sex,
            phone_number=phone_number,
            email=email,
            address_line_1=address_line_1,
            address_line_2=address_line_2,
            city=city,
            state=state,
            zip_code=zip_code,
            insurance_provider=insurance_provider,
            insurance_member_id=insurance_member_id,
            preferred_language=preferred_language,
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
        )
        if not result["ok"]:
            # Nothing is applied; the per-field reasons let the agent re-ask just those.
            return result
        return await self._after_amend()

    async def _after_amend(self) -> dict | None:
        """What happens after a successful correction. ReviewStage overrides this to read
        everything back again."""
        return {"ok": True}

    @function_tool()
    async def start_over(self, context: RunContext) -> Agent:
        """Erase everything collected so far and restart from the caller's name. Only call
        this when the caller clearly asks to start over."""
        self.state.reset()
        # No chat_ctx: the new stage starts with a clean history, so old answers can't leak in.
        return IdentityStage()


# --- 1. Welcome ------------------------------------------------------------


class WelcomeStage(IntakeStage):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You open a phone call for a medical front desk. Keep it to one or two short "
                "sentences: say hello, mention you'll take down some registration details, and "
                "ask if it's a good time to start. When the caller signals they're ready, call "
                "the begin_collection tool. Do not collect any details yourself."
            )
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions=(
                "Warmly greet the caller, say you'll gather a few registration details, and ask "
                "whether they're ready to begin."
            )
        )

    @function_tool()
    async def begin_collection(self, context: RunContext) -> Agent:
        """Move on to collecting the caller's identity once they're ready."""
        return IdentityStage(chat_ctx=self.chat_ctx)


# --- 2. Identity -----------------------------------------------------------


class IdentityStage(IntakeStage):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "Collect the caller's legal first name, last name, date of birth, and sex. Ask "
                "conversationally, one item at a time, and accept them in whatever order they "
                "come. When you have all four, call submit_identity. If a value is rejected, tell "
                "the caller what was wrong and ask again only for that one item."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Ask the caller for their first and last name."
        )

    @function_tool()
    async def submit_identity(
        self,
        context: RunContext,
        first_name: str,
        last_name: str,
        date_of_birth: str,
        sex: str,
    ) -> dict | Agent:
        """Validate and store the caller's name, date of birth, and sex, then continue."""
        result = self._store_valid(
            first_name=first_name, last_name=last_name, date_of_birth=date_of_birth, sex=sex
        )
        if not result["ok"]:
            return result
        return ContactStage(chat_ctx=self.chat_ctx)


# --- 3. Contact ------------------------------------------------------------


class ContactStage(IntakeStage):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "First collect a US phone number and immediately call check_returning with it. "
                'If it reports an existing record, ask: "It looks like we already have a record '
                "for <first name> <last name>. Would you like to update your information "
                'instead?" and call confirm_update with their answer. Then collect the mailing '
                "address: street address, optional apartment, suite, or unit, city, two-letter "
                "state, and ZIP. When the address is complete, call submit_address. Re-ask only "
                "for any field that fails validation."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Ask the caller for the best phone number to reach them."
        )

    @function_tool()
    async def check_returning(self, context: RunContext, phone_number: str) -> dict:
        """Store the caller's phone number and check whether a record already exists."""
        result = self._store_valid(phone_number=phone_number)
        if not result["ok"]:
            return result

        # A new or corrected number starts fresh: forget any match from an earlier number.
        self.state.existing_patient_id = None
        self.state.updating = False
        with PatientStore.open() as store:
            existing = store.find_by_phone(self.state.phone_number)
        if existing is None:
            return {"ok": True, "returning": False}

        self.state.existing_patient_id = existing.patient_id
        return {
            "ok": True,
            "returning": True,
            "first_name": existing.first_name,
            "last_name": existing.last_name,
        }

    @function_tool()
    async def confirm_update(self, context: RunContext, update_existing: bool) -> dict:
        """Record the caller's answer to "would you like to update your information instead?":
        True to update the existing record, False to register as a new patient."""
        if self.state.existing_patient_id is None:
            return {"ok": False, "message": "No existing record was found for this number."}
        self.state.updating = update_existing
        if not update_existing:
            self.state.existing_patient_id = None
        return {"ok": True}

    @function_tool()
    async def submit_address(
        self,
        context: RunContext,
        address_line_1: str,
        city: str,
        state: str,
        zip_code: str,
        address_line_2: str | None = None,
    ) -> dict | Agent:
        """Validate and store the mailing address, then move on to optional details.
        address_line_1 is the street address; address_line_2 is an apartment, suite, or unit."""
        if self.state.phone_number is None:
            return {"ok": False, "message": "Collect and check the phone number first."}

        result = self._store_valid(
            address_line_1=address_line_1,
            address_line_2=address_line_2,
            city=city,
            state=state,
            zip_code=zip_code,
        )
        if not result["ok"]:
            return result
        return ExtrasStage(chat_ctx=self.chat_ctx)


# --- 4. Extras (optional) --------------------------------------------------


class ExtrasStage(IntakeStage):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "Offer four optional groups, briefly: insurance, an emergency contact, an email "
                "address, and a preferred language. Record whichever the caller wants using "
                "add_insurance, add_emergency_contact, add_email, or set_language — call the "
                "matching tool immediately whenever the caller volunteers one of these, even in "
                "passing or bundled with something else. Never state that a detail has been "
                "recorded without having called its tool first. If they decline everything or "
                "once they're done, call optional_done. Never insist."
            ),
            chat_ctx=chat_ctx,
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions=(
                "Let the caller know you can also record insurance, an emergency contact, an "
                "email address, and a preferred language, and ask if they'd like to add any of "
                "those."
            )
        )

    @function_tool()
    async def add_insurance(
        self,
        context: RunContext,
        insurance_provider: str,
        insurance_member_id: str | None = None,
    ) -> dict:
        """Record the caller's insurance company and optional member/subscriber ID."""
        return self._store_valid(
            insurance_provider=insurance_provider, insurance_member_id=insurance_member_id
        )

    @function_tool()
    async def add_emergency_contact(
        self, context: RunContext, emergency_contact_name: str, emergency_contact_phone: str
    ) -> dict:
        """Record an emergency contact's full name and phone number."""
        return self._store_valid(
            emergency_contact_name=emergency_contact_name,
            emergency_contact_phone=emergency_contact_phone,
        )

    @function_tool()
    async def add_email(self, context: RunContext, email: str) -> dict:
        """Record the caller's email address."""
        return self._store_valid(email=email)

    @function_tool()
    async def set_language(self, context: RunContext, preferred_language: str) -> dict:
        """Record the caller's preferred language."""
        return self._store_valid(preferred_language=preferred_language)

    @function_tool()
    async def optional_done(self, context: RunContext) -> Agent:
        """Finish optional details and move to the final review."""
        return ReviewStage(chat_ctx=self.chat_ctx)


# --- 5. Review and save ----------------------------------------------------


class ReviewStage(IntakeStage):
    def __init__(self, *, chat_ctx=None) -> None:
        super().__init__(
            instructions=(
                "Read back the collected details and ask the caller to confirm they're correct. "
                "If they want a change — including something they mention was missed earlier in "
                "the call, like an email address — call amend with just the corrected fields, "
                "then read back again. Never state that a detail has been added, changed, or "
                "recorded unless you actually called amend with it first; do not just describe "
                "the correction in your reply. Only once they clearly confirm, call save_record. "
                "save_record tells the caller the outcome and ends the call itself, so say "
                "nothing further after calling it."
            ),
            chat_ctx=chat_ctx,
            # Only for a caller who wants to hang up without saving; a confirmed record ends
            # the call through save_record instead. Hidden during on_enter so the readback
            # can't end the call.
            tools=[
                EndCallTool(
                    extra_description=(
                        "Only call this if the caller explicitly asks to end the call without "
                        "saving. If they confirmed their details, call save_record instead."
                    ),
                    ignore_on_enter=True,
                )
            ],
        )

    async def on_enter(self) -> None:
        # Spoken directly line-by-line (rather than via generate_reply) so each field gets its
        # own TTS utterance with a real pause after it, instead of the LLM paraphrasing the
        # whole summary into one run-on, comma-chained sentence.
        await self.session.say("Let me read everything back to you.").wait_for_playout()
        await asyncio.sleep(0.4)
        for line in _spoken_lines(self.state):
            await self.session.say(line).wait_for_playout()
            await asyncio.sleep(0.4)

        await self.session.generate_reply(
            instructions=(
                "You already read every detail aloud in the prior turns. Do not repeat, list, or "
                "summarize any of those details again. Just ask one short question, like 'Does "
                "everything sound correct?'"
            )
        )

    async def _after_amend(self) -> None:
        # Same paced, line-by-line readback as on_enter (see the comment there) — a plain
        # dict return here would let the LLM paraphrase the correction into one run-on
        # sentence again, and returning None (rather than a dict) avoids the framework
        # firing a second, redundant auto-reply on top of the one triggered below.
        for line in _spoken_lines(self.state):
            await self.session.say(line).wait_for_playout()
            await asyncio.sleep(0.4)

        await self.session.generate_reply(
            instructions=(
                "You already read every detail aloud in the prior turns. Do not repeat, list, "
                "or summarize any of those details again. Just ask one short question, like "
                "'Does everything sound correct now?'"
            )
        )
        return None

    @function_tool()
    async def save_record(self, context: RunContext) -> None:
        """Persist the confirmed patient record (creating or updating as appropriate), tell
        the caller the outcome, and end the call."""
        state = self.state
        # The full collected record goes into the log line for both outcomes. If the save
        # fails, that log line is the only copy of what the caller said.
        payload = state.as_log_payload()
        try:
            if state.updating and state.existing_patient_id:
                changes = PatientChanges(**state.collected())
                with PatientStore.open() as store:
                    row = store.get(state.existing_patient_id)
                    if row is None:
                        # e.g. the record was deleted through the API during this call.
                        raise LookupError(f"existing patient {state.existing_patient_id} vanished")
                    row = store.apply_changes(row, changes)
                action = "updated"
            else:
                new_patient = NewPatient(**state.collected())
                with PatientStore.open() as store:
                    row = store.add(new_patient)
                action = "created"
        except Exception:
            # Lands in the room-tagged logs (see the `log` comment above), so "what happened
            # on call X" is answerable — e.g. a locked or unreachable database.
            log.exception(
                "save_record failed", extra={"updating": state.updating, "patient": payload}
            )
            await self._say_and_hang_up(SAVE_FAILED_MESSAGE)
            return None

        state.saved_patient_id = row.patient_id
        log.info(
            f"patient record {action}",
            extra={"patient_id": str(row.patient_id), "action": action, "patient": payload},
        )
        await self._say_and_hang_up(
            f"You're all set, {state.first_name}. Thank you for calling, and have a great day."
        )
        # None: the line above is the final word, so the LLM must not generate a reply.
        return None

    async def _say_and_hang_up(self, text: str) -> None:
        """Speak a final line, then end the call.

        Closing the session hangs up the phone: the worker starts the session with
        delete_room_on_close=True (app/worker.py), and deleting the LiveKit room disconnects
        the caller. Interruptions are off so a quick "thanks" can't cut the goodbye short.
        """
        await self.session.say(text, allow_interruptions=False).wait_for_playout()
        self.session.shutdown()
