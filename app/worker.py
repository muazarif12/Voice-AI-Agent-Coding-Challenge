"""LiveKit worker entrypoint.

Boots the real-time voice pipeline and starts the intake at the Welcome stage. The stages
themselves live in ``app.flow``; this module only assembles the models and session.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import (
    AgentServer,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    JobProcess,
    cli,
    inference,
    room_io,
)
from livekit.agents.llm import ChatMessage
from livekit.plugins import ai_coustics, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from app.flow import IntakeState, WelcomeStage
from app.store import PatientStore, create_schema

# Load credentials from the .env.local next to the project root, regardless of the
# directory the worker is launched from. On Fly.io there is no such file; the same variables
# come from `fly secrets` instead.
load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

log = logging.getLogger("intake-worker")

# Infrastructure binding: the LiveKit SIP dispatch rule for the provisioned number targets
# this exact agent name, so keeping it lets the existing phone number route here unchanged.
DISPATCH_AGENT_NAME = "my-agent"

# Model selection via LiveKit Inference (no separate provider keys needed). These ids follow
# the "provider/model" convention; check the LiveKit Inference catalog to swap them.
TRANSCRIBER = "deepgram/nova-3"
BRAIN = "openai/gpt-4.1"
VOICE_MODEL = "cartesia/sonic-3"
VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"

# Chat roles mapped to the labels used in stored transcripts.
_SPEAKERS = {"assistant": "agent", "user": "caller"}

# The worker makes an outbound connection to LiveKit Cloud and waits for calls; it serves no
# public traffic. (It still runs a small internal health server on port 8081.)
server = AgentServer(
    # Keep one call process pre-started (the default scales with CPU count). One is enough for
    # this app and leaves more memory free on the 2 GB Fly machine (fly.toml).
    num_idle_processes=1,
)


def _load_models(proc: JobProcess) -> None:
    # Load the voice-activity model once per worker process and reuse it across calls.
    proc.userdata["vad"] = silero.VAD.load()
    # Ensure the schema exists once per prewarmed process, not on every inbound call. Against
    # Postgres, create_all() still does a catalog round-trip per call it's asked to run in —
    # cheap once at process startup, wasteful (and needlessly racy under concurrent calls)
    # repeated on every job.
    create_schema()


server.setup_fnc = _load_models


@server.rtc_session(agent_name=DISPATCH_AGENT_NAME)
async def handle_call(ctx: JobContext) -> None:
    # Attached to every log line from this call, so one call's logs can be pulled out by room.
    ctx.log_context_fields = {"room": ctx.room.name}

    session = AgentSession[IntakeState](
        userdata=IntakeState(),
        stt=inference.STT(model=TRANSCRIBER, language="multi"),
        llm=inference.LLM(model=BRAIN),
        tts=inference.TTS(model=VOICE_MODEL, voice=VOICE_ID),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # Start drafting a reply before the caller's turn is fully finished, to cut latency.
        preemptive_generation=True,
    )

    # Every caller and agent turn is logged to stdout as it happens and collected here, so the
    # whole conversation can be saved as the call's transcript once it ends. The final
    # collected record is logged separately by save_record (app/flow.py).
    turns: list[dict] = []

    @session.on("conversation_item_added")
    def _record_turn(event: ConversationItemAddedEvent) -> None:
        item = event.item
        if not (isinstance(item, ChatMessage) and item.text_content):
            return  # skip tool calls and other non-speech items
        speaker = _SPEAKERS.get(item.role)
        if speaker is None:
            return  # skip system/developer prompts
        turns.append({"speaker": speaker, "text": item.text_content})
        log.info("conversation turn", extra={"speaker": speaker, "text": item.text_content})

    # Runs however the call ends: the agent hangs up after saving, the caller hangs up, or the
    # line drops. A transcript is only stored when a patient record was saved, since that's
    # what it's linked to.
    async def _on_call_end() -> None:
        state = session.userdata
        patient_id = state.saved_patient_id
        if patient_id is None:
            # Ended before a record was saved (e.g. the line dropped): log whatever was
            # collected, so the details aren't lost silently. The turns are logged above too.
            if state.collected():
                log.warning(
                    "call ended before the record was saved",
                    extra={"patient": state.as_log_payload()},
                )
            return
        if not turns:
            return
        try:
            with PatientStore.open() as store:
                store.add_transcript(patient_id, ctx.room.name, turns)
            log.info("call transcript saved", extra={"patient_id": str(patient_id)})
        except Exception:
            log.exception("saving call transcript failed", extra={"patient_id": str(patient_id)})

    ctx.add_shutdown_callback(_on_call_end)

    await ctx.connect()

    # The Welcome stage greets on entry, so no separate opening prompt is needed here.
    await session.start(
        agent=WelcomeStage(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            # Ending the session (session.shutdown() in app/flow.py) deletes the room, which
            # disconnects the caller — this is how the agent hangs up after saving.
            delete_room_on_close=True,
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_L
                ),
            ),
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
