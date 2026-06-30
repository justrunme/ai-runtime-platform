"""In-memory tenant attribution counters for workload identity prototype."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from prometheus_client import Counter

TENANT_REQUESTS = Counter(
    "gateway_tenant_requests_total",
    "Chat completion requests attributed to a tenant team.",
    ["team"],
)
TENANT_TOKENS = Counter(
    "gateway_tenant_tokens_total",
    "Estimated tokens attributed to a tenant team.",
    ["team"],
)


@dataclass
class TenantUsage:
    minute_window_start: float = field(default_factory=time.monotonic)
    requests_last_minute: int = 0
    tokens_today: int = 0


class TenantAttributionStore:
    """Track per-team usage for governance quota signals."""

    def __init__(self) -> None:
        self._usage: dict[str, TenantUsage] = {}

    @staticmethod
    def enabled_from_environment() -> bool:
        return os.getenv("TENANT_ATTRIBUTION_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def resolve_team(self, request) -> str:
        return request.headers.get("x-ai-team") or request.headers.get("x-ai-tenant") or "platform"

    def _get(self, team: str) -> TenantUsage:
        usage = self._usage.setdefault(team, TenantUsage())
        now = time.monotonic()
        if now - usage.minute_window_start >= 60:
            usage.minute_window_start = now
            usage.requests_last_minute = 0
        return usage

    def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None:
        usage = self._get(team)
        usage.requests_last_minute += 1
        usage.tokens_today += input_tokens + output_tokens
        TENANT_REQUESTS.labels(team=team).inc()
        TENANT_TOKENS.labels(team=team).inc(input_tokens + output_tokens)

    def usage_snapshot(self, team: str) -> tuple[int, int]:
        usage = self._get(team)
        return usage.requests_last_minute, usage.tokens_today
