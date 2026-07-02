"""Pytest hooks shared by gateway tests."""

from __future__ import annotations

import os

# Prevent BatchSpanProcessor from flushing to a closed stdout after pytest exits.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
