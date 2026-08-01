"""Ensure the published OpenAPI snapshot stays frozen."""

from __future__ import annotations

import json
from pathlib import Path

from app.gateway.main import app

SNAPSHOT = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"


def test_openapi_snapshot_is_current() -> None:
    assert SNAPSHOT.exists(), "run python scripts/export_openapi.py"
    current = app.openapi()
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert json.dumps(current, sort_keys=True) == json.dumps(frozen, sort_keys=True)
