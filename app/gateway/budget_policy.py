"""Configurable inference budget policy: budget, window, prices, soft warning."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class ModelPrice(BaseModel):
    input_per_million: float = Field(ge=0, alias="inputPerMillion")
    output_per_million: float = Field(ge=0, alias="outputPerMillion")

    model_config = {"populate_by_name": True}


class ScopeBudget(BaseModel):
    budget_usd: float = Field(gt=0, alias="budgetUsd")
    window_seconds: int = Field(default=3600, gt=0, alias="windowSeconds")
    soft_warning_ratio: float = Field(default=0.8, gt=0, le=1.0, alias="softWarningRatio")
    reservation_ttl_seconds: float = Field(default=120.0, gt=0, alias="reservationTtlSeconds")

    model_config = {"populate_by_name": True}


class TenantBudgetPolicy(ScopeBudget):
    api_keys: dict[str, ScopeBudget] = Field(default_factory=dict, alias="apiKeys")

    model_config = {"populate_by_name": True}


class InferenceBudgetPolicy(BaseModel):
    """Fail-closed policy document for the inference budget guard."""

    version: str = Field(min_length=1)
    enabled: bool = True
    default: ScopeBudget
    tenants: dict[str, TenantBudgetPolicy] = Field(default_factory=dict)
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    # When true and Redis is configured, Redis outages deny requests (always fail-closed).
    require_store: bool = Field(default=True, alias="requireStore")

    model_config = {"populate_by_name": True}

    @field_validator("prices")
    @classmethod
    def _require_default_price(cls, value: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
        if "*" not in value and "default" not in value:
            raise ValueError("prices must include '*' or 'default' fallback entry")
        return value

    @model_validator(mode="after")
    def _non_empty_version(self) -> InferenceBudgetPolicy:
        if not self.version.strip():
            raise ValueError("version must be non-empty")
        return self

    def scope_for(self, tenant_id: str, api_key: str | None = None) -> tuple[str, ScopeBudget]:
        tenant = self.tenants.get(tenant_id)
        if tenant is None:
            return f"tenant:{tenant_id}", self.default
        if api_key and api_key in tenant.api_keys:
            return f"key:{tenant_id}:{api_key}", tenant.api_keys[api_key]
        return f"tenant:{tenant_id}", ScopeBudget(
            budgetUsd=tenant.budget_usd,
            windowSeconds=tenant.window_seconds,
            softWarningRatio=tenant.soft_warning_ratio,
            reservationTtlSeconds=tenant.reservation_ttl_seconds,
        )

    def price_for(self, model: str) -> ModelPrice:
        if model in self.prices:
            return self.prices[model]
        if "*" in self.prices:
            return self.prices["*"]
        return self.prices["default"]

    def estimate_cost_usd(self, model: str, *, input_tokens: int, output_tokens: int) -> float:
        price = self.price_for(model)
        return round(
            (
                max(0, input_tokens) * price.input_per_million
                + max(0, output_tokens) * price.output_per_million
            )
            / 1_000_000,
            8,
        )

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(by_alias=True, mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class BudgetPolicyError(Exception):
    """Raised when the configured budget policy is missing or malformed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _load_raw_policy() -> Any:
    path = os.getenv("INFERENCE_BUDGET_POLICY_FILE", "").strip()
    raw = os.getenv("INFERENCE_BUDGET_POLICY", "").strip()
    if path:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    if raw:
        return json.loads(raw)
    return None


@lru_cache(maxsize=1)
def load_inference_budget_policy() -> InferenceBudgetPolicy | None:
    """Load policy when the guard is enabled.

    Returns None when the feature flag is off.
    Raises BudgetPolicyError when enabled but the document is missing/corrupt
    (callers must fail closed).
    """
    enabled = os.getenv("INFERENCE_BUDGET_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not enabled:
        return None
    try:
        data = _load_raw_policy()
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise BudgetPolicyError(f"malformed inference budget policy: {error}") from error
    if data is None:
        raise BudgetPolicyError(
            "INFERENCE_BUDGET_ENABLED is set but INFERENCE_BUDGET_POLICY "
            "or INFERENCE_BUDGET_POLICY_FILE is missing"
        )
    try:
        return InferenceBudgetPolicy.model_validate(data)
    except Exception as error:  # noqa: BLE001 - pydantic + custom validators
        raise BudgetPolicyError(f"malformed inference budget policy: {error}") from error


def reset_inference_budget_policy_cache() -> None:
    load_inference_budget_policy.cache_clear()
