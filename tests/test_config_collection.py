"""QDRANT_COLLECTION env override (fresh interpreter per case: settings are read at import)."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _resolved_collection(value: str) -> str:
    # The variable is always present in the child env, so load_dotenv() can't override it
    # from a local .env (it never overwrites variables that are already set, even to "").
    env = {**os.environ, "PYTHONPATH": str(ROOT), "QDRANT_COLLECTION": value}
    result = subprocess.run(
        [sys.executable, "-c", "from app.config import settings; print(settings.QDRANT_COLLECTION)"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_env_var_overrides_collection_name():
    assert _resolved_collection("enterprise_rag_v2") == "enterprise_rag_v2"


def test_empty_value_falls_back_to_default():
    assert _resolved_collection("") == "enterprise_rag"
