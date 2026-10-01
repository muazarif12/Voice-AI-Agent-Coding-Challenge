# Patient Intake Voice Agent

Call **+1 (484) 317-4139**. An AI agent collects your registration details, reads them
back, saves them, and hangs up.

- **Dashboard:** https://patient-intake-voice.fly.dev/dashboard
- **API:** https://patient-intake-voice.fly.dev/patients
- **API docs:** https://patient-intake-voice.fly.dev/docs

## How it works

```
Caller ──phone──> LiveKit Cloud ──> Voice worker ──> SQLite database <── REST API
                                  (speech → LLM → speech)
```

The voice worker and the API run in one container on Fly.io and share one database.

| File | Purpose |
|---|---|
| `app/worker.py` | Answers calls; sets up speech-to-text, the LLM and text-to-speech |
| `app/flow.py` | The conversation and the LLM prompts |
| `app/schema.py` | Validation rules, shared by the agent and the API |
| `app/store.py` | Database tables (`patients`, `call_transcripts`) |
| `app/web.py` | REST API |
| `app/dashboard.html` | Web page listing patients and their call transcripts |

## The conversation

The call moves through five short stages: **Welcome → Identity → Contact → Extras → Review**.

- **One small prompt per stage.** Each stage has a short prompt for its own job and only
  the tools it needs. That keeps the LLM focused. The prompts are in `app/flow.py`,
  explained at the top of the file.
- **The LLM never writes data directly.** It calls tools, and the tools validate the data.
  If a value is invalid, such as a future date of birth, the agent re-asks for just that
  field.
- **Fixed lines are spoken by code, not the LLM.** That covers the readback and the final
  "You're all set" or error line, so they are always accurate.
- **Returning callers.** If the phone number is already on file, the agent asks whether to
  update that record or create a new one.

## What happens when things go wrong

| Situation | What the agent does |
|---|---|
| Invalid answer (bad date, short phone number) | Says what's wrong and re-asks for that field only |
| Caller corrects an earlier answer | Updates it at any point in the call |
| Caller wants to start over | Clears everything and starts again from their name |
| Database write fails | Apologises, asks them to call back, logs the details |
| Call drops before saving | Logs everything collected so far |

## Tech stack

| Choice | Why |
|---|---|
| **LiveKit** (Agents SDK, Cloud, phone number) | Telephony, real-time audio and turn-taking in one place |
| **Deepgram Nova-3, GPT-4.1, Cartesia Sonic-3** via LiveKit Inference | Fast, accurate speech and reasoning with a single API key |
| **FastAPI + Pydantic** | Validated API and auto-generated docs with little code |
| **SQLite + SQLAlchemy** | No setup. Switch to Postgres by changing `INTAKE_DB_URL` |
| **Fly.io** | Always-on hosting with a persistent disk for the database |

## Setup

| Variable | Required | Description |
|---|---|---|
| `LIVEKIT_URL` | Yes | e.g. `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | Yes | LiveKit dashboard → Settings → API keys |
| `INTAKE_DB_URL` | No | Database URL. Defaults to `records.db` in the project folder |

**Run locally** (needs Python 3.10+ and [uv](https://docs.astral.sh/uv/)). Put the variables
in `.env.local` (copy `.env.example`), then:

```bash
uv sync
uv run python -m livekit.agents download-files   # one-time model download
uv run python -m app.worker console               # talk to the agent with your mic
uv run uvicorn app.web:app --reload               # API at http://localhost:8000/docs
uv run python -m app.seed                         # optional: 2 demo patients
uv run pytest
```

**Deploy to Fly.io:**

```bash
fly apps create patient-intake-voice
fly secrets set LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
fly deploy --ha=false     # one machine = one database
```

## API

Responses look like `{"data": ..., "error": null}`, or `{"data": null, "error": {...}}` on
failure.

| Method | Path | Description |
|---|---|---|
| GET | `/patients` | List patients. Filters: `?last_name=`, `?date_of_birth=`, `?phone_number=` |
| GET | `/patients/{id}` | One patient |
| POST | `/patients` | Create a patient |
| PUT | `/patients/{id}` | Update only the fields sent |
| DELETE | `/patients/{id}` | Soft delete (sets `deleted_at`) |
| GET | `/patients/{id}/transcripts` | Transcripts of this patient's calls |

## Transcripts and logs

Each call that saves a record stores its full transcript in `call_transcripts`, linked to
the patient. Every turn and the final record are also logged to stdout (`fly logs`).


