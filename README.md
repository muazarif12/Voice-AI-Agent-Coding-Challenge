# Patient Intake Voice Agent

Call **+1 (484) 317-4139**. A voice agent answers, collects your registration details,
reads them back for confirmation, saves them to a database, and hangs up. A REST API lets
you view and manage the saved records.

- **Live API:** https://patient-intake-voice.fly.dev/patients
- **API docs:** https://patient-intake-voice.fly.dev/docs

## How it works

```
Caller ──phone──> LiveKit Cloud ──> Voice worker (app/worker.py)
                                        │  speech-to-text → LLM → text-to-speech
                                        ▼
                                    SQLite database  <── REST API (app/web.py)
```

The voice worker and the API run side by side in one container and share one database.

| File | What it does |
|---|---|
| `app/worker.py` | Connects to LiveKit, answers calls, sets up the voice pipeline |
| `app/flow.py` | The conversation: Welcome → Identity → Contact → Extras → Review |
| `app/schema.py` | Validation rules, shared by the voice agent and the API |
| `app/store.py` | Database tables (`patients`, `call_transcripts`) and queries |
| `app/web.py` | REST API |
| `app/seed.py` | Adds two demo patients to an empty database |

Each stage of the conversation has its own short prompt and only the tools it needs. That
keeps the LLM focused and makes the flow easy to follow and change.

## Tech stack

| Choice | Why |
|---|---|
| **LiveKit** (Agents SDK, Cloud, phone number) | Handles telephony, real-time audio and turn-taking in one place |
| **LiveKit Inference**: Deepgram Nova-3, GPT-4.1, Cartesia Sonic-3 | Fast, accurate speech and reasoning, all with one LiveKit API key |
| **FastAPI + Pydantic** | Request validation and API docs with very little code |
| **SQLite + SQLAlchemy** | Zero setup. Swap to Postgres by changing `INTAKE_DB_URL` |
| **Fly.io** | Simple always-on hosting with a persistent volume for the database |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `LIVEKIT_URL` | Yes | e.g. `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | Yes | From the LiveKit dashboard: Settings → API keys |
| `LIVEKIT_API_SECRET` | Yes | Same place as the key |
| `INTAKE_DB_URL` | No | Database URL. Defaults to `records.db` in the project folder |

Locally, put them in `.env.local` (copy `.env.example`). On Fly.io, use `fly secrets set`.

## Run locally

Needs Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m livekit.agents download-files   # one-time model download

uv run python -m app.worker console               # talk to the agent with your mic
uv run python -m app.worker dev                   # or: answer real calls to the number
uv run uvicorn app.web:app --reload               # API at http://localhost:8000/docs

uv run python -m app.seed                         # optional: add two demo patients
uv run pytest                                     # tests
```

## Deploy to Fly.io

```bash
fly apps create patient-intake-voice      # the name used in fly.toml
fly secrets set LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
fly deploy --ha=false                      # one machine, so there is one database
```

`--ha=false` matters: a second machine would get its own volume and its own separate
database.

## API

Every response is `{"data": ..., "error": null}`, or `{"data": null, "error": {...}}` on
failure. Interactive docs: `/docs`.

| Method | Path | Description |
|---|---|---|
| GET | `/patients` | List patients. Filters: `?last_name=`, `?date_of_birth=`, `?phone_number=` |
| GET | `/patients/{id}` | One patient |
| POST | `/patients` | Create a patient (201) |
| PUT | `/patients/{id}` | Update only the fields you send |
| DELETE | `/patients/{id}` | Soft delete (sets `deleted_at`) |
| GET | `/patients/{id}/transcripts` | Transcripts of the calls that registered or updated this patient |

```bash
curl "https://patient-intake-voice.fly.dev/patients?last_name=doe"
```

## Transcripts and logs

When a call saves a patient record, the full conversation is stored in the
`call_transcripts` table, linked to that patient. Read it with
`GET /patients/{id}/transcripts`.

Every turn is also logged to stdout, plus the full record when it is saved. If a save
fails, that log line still holds what the caller said. View logs with `fly logs`.

## Known limitations and trade-offs

- **No API authentication.** Anyone with the URL can read and change records. A real
  deployment needs auth and HTTPS-only access for staff.
- **Patient data is in the logs**, which is required here but would need restricted log
  access in production.
- **One machine, one SQLite file.** Simple and enough for a demo, but it doesn't scale out.
  Use Postgres via `INTAKE_DB_URL` to run more machines.
- **Returning callers are matched by phone number only**, and the agent asks before
  updating an existing record.
- **US only:** phone numbers, states and ZIP codes are validated as US formats.
