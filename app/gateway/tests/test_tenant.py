from app.gateway.tenant import TenantAttributionStore


def test_tenant_store_records_requests_and_tokens() -> None:
    store = TenantAttributionStore()
    store.record_request("finance", input_tokens=100, output_tokens=50)
    rpm, tokens = store.usage_snapshot("finance")
    assert rpm == 1
    assert tokens == 150


def test_resolve_team_prefers_header() -> None:
    store = TenantAttributionStore()
    request = type(
        "Request",
        (),
        {"headers": {"x-ai-team": "search", "x-ai-tenant": "ignored"}},
    )()
    assert store.resolve_team(request) == "search"
