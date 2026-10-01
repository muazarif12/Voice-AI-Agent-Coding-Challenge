"""Point the store at a throwaway SQLite file before any app module is imported."""

import os
import pathlib
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="intake-tests-")
os.environ["INTAKE_DB_URL"] = f"sqlite:///{pathlib.Path(_tmp_dir) / 'test.db'}"
