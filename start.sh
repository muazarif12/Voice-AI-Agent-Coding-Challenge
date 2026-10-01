#!/bin/bash
# Container entrypoint: runs the REST API and the voice worker side by side, so both use the
# same SQLite database on the mounted volume.

# On shutdown (Fly sends SIGINT), pass the signal on so both processes stop cleanly.
trap 'kill -TERM "$API_PID" "$WORKER_PID" 2>/dev/null' INT TERM

# --no-access-log: the API logs every request itself, with a request id (app/obs.py).
uvicorn app.web:app --host 0.0.0.0 --port 8000 --no-access-log &
API_PID=$!
python -m app.worker start &
WORKER_PID=$!

# If either process exits, stop the other and exit too. Fly then restarts the machine,
# instead of leaving it running with only half of the app.
wait -n
EXIT_CODE=$?
kill -TERM "$API_PID" "$WORKER_PID" 2>/dev/null
wait
exit "$EXIT_CODE"
