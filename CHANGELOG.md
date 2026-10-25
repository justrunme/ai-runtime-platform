# Changelog

## [Unreleased]

## [2.5.1] - 2026-10-25

### Fixed

- A tenant claim made only of whitespace no longer resolves to an empty tenant id; resolution falls through to the next claim or the default bucket

### Security

- Release workflow and CI follow least-privilege token permissions; third-party actions and downloaded binaries are pinned and checksum-verified
- Release assets include a keyless cosign bundle for the SBOM
- Property-based fuzz tests (Hypothesis) for JWT, request digest, MCP transport, tenant and budget policy parsing

## [2.5.0] - 2026-10-19

### Added

- Inference Budget Guard on the chat admission path: atomic reserve (tokens × price)
  per tenant/API key, settle after usage, fail-closed on store/policy faults
  (`INFERENCE_BUDGET_*`); design in `docs/budget-guard.md`; CI evidence envelopes
- Fleet-wide tenant concurrency admission via Redis distributed semaphore (Lua acquire,
  lease TTL + renewal, fencing token, pod-death reclaim on expiry)
- Per-tenant `redisUnavailablePolicy`: `fail_closed` (default), `degraded`, `fail_open`
- Admission metrics (`gateway_admission_inflight`, acquire latency, rejections, expired leases)
- Admission status on `/readyz` and `/v1/runtime/status`
- Platform e2e proofs: multi-replica limit, SIGKILL lease recovery, Redis fail-closed `503`

### Changed

- Tenant policy prefers `maxConcurrentRequests` / `maxQueuedRequests` (legacy `*PerReplica` aliases kept)
- Docs no longer describe concurrency limits as per-replica-only when Redis is configured
- Explicit `GOVERNANCE_MODE` (`enforce`|`disabled`): default `enforce` fails readiness / returns `503`
  when `CONTROL_PLANE_URL` is missing instead of silently skipping governance; `disabled` must be set
  explicitly (local demo compose / single-node overlay). Mode is exposed on `/health` and
  `gateway_governance_mode`. Real-CP e2e asserts exact `409` + error envelope for replay/mismatch.

## [2.4.0] - 2026-10-10

Signed Control Plane compatibility matrix and CI hygiene.

- Dependency updates (GitHub Actions, Docker base image, openai)
- Remove failing nightly schedule from the real Control Plane e2e workflow
- Signed compatibility matrix against Control Plane `main` / `2.4.0` / `2.0.0` / `1.3.0` with keyless cosign verification; results in `docs/compatibility.md`
- Weekly schedule restored for the real Control Plane e2e

## 2.3.0

MCP supported path for Streamable HTTP sessions.

- `initialize` + `Mcp-Session-Id` lifecycle (`sessionMode` auto/always/never)
- SSE (`text/event-stream`) JSON-RPC parsing for tools/call responses
- Cancellation via `notifications/cancelled` (+ DELETE fallback)
- Optional capability digest binding on initialize
- Richer audit-tool payload (session, capability digest, mcp_request_id)
- Routes: `POST /mcp/servers/{name}/initialize`, `POST /mcp/servers/{name}/cancel`

## 2.2.0

Full closed-loop integration surface.

- Signed `verification_token` on `POST /v1/runtime/verify` + public `GET /v1/runtime/jwks`
- Verify response includes `observed` / `correlation` (generation + digests) and remediation IDs
- Nightly HA e2e: Postgres-backed dual Control Plane, dual Runtime, Redis, OIDC
- HA proofs: cross-CP approval redemption, tenant isolation across replicas, SIGTERM during stream

## 2.1.0

Reliable usage evidence delivery.

- Background webhook retry worker with exponential backoff and buffer metrics
- Deterministic idempotent `event_id` from request/outcome/backend
- Shutdown drain for pending usage events
- Streaming usage extraction from SSE (`stream_options.include_usage`) + usage events in stream finalizer

## 2.0.1

Correctness and contract honesty.

- Fix admission inflight leak on cancellation while waiting for a permit
- Cache decision-token JWKS client; compare signed `request_digest` to local canonical digest
- MCP execution requires Control Plane unless `MCP_ALLOW_UNGOVERNED=true`
- RBAC for `/v1/runtime/status`, `/v1/runtime/verify`, `/mcp/servers` when JWT verify / explicit flag is on
- Release gates against Control Plane `2.0.0` and `1.3.0`; cosign-sign OCI Helm chart
- Docs: maturity/README/tenant limits honesty (`PerReplica`, reserved credential ref)

## 2.0.0

Stable closed-loop Execution Plane contract.

- Declares frozen enforcement / verification / evidence / usage / MCP surfaces
- Contract tests guard required routes and OpenAPI major version
- Compatible with Control Plane closed-loop remediation + PolicyBundle correlation
- Semver major marks the stable platform boundary (no intentional OpenAI body break from 1.9)

## 1.9.0

Usage and cost event emission (no billing database).

- Structured usage events: tenant, tokens, cost, latency, outcome, decision id
- OTEL span attributes `ai.runtime.usage.*`
- Optional webhook sink via `USAGE_EVENTS_WEBHOOK_URL` with idempotency key + bounded buffer

## 1.8.0

Real MCP execution transport.

- `MCP_SERVER_REGISTRY` for Streamable HTTP servers + `env:` credential refs
- Governed `POST /mcp/tools/{tool}/call` executes `tools/call` when `mcp_server` is set
- Result size limit / truncation, optional CP audit post
- `GET /mcp/servers` lists registered servers (no secret values)

## 1.7.0

Resilience and process-wide admission control.

- Global inflight/queue limits with load-shed `503 gateway_load_shed`
- Per-backend circuit breaker + retry budget for fallbacks
- Graceful drain on SIGTERM/SIGINT: `/readyz` → draining, new chat rejected
- Complements tenant admission from 1.6

## 1.6.0

Tenant isolation on the execution plane.

- Decision records store `tenant_id`; Redis keys `arp:{tenant}:decision:{id}`
- `GET /v1/decisions/{id}` requires matching JWT tenant (global auditor groups bypass)
- `TENANT_RUNTIME_POLICY` allowlists + per-tenant concurrency/queue limits
- `429` with `Retry-After` / rate-limit headers on admission overflow
- Tenant attribution counters use JWT tenant (not spoofable headers when JWT is on)

## 1.5.0

Runtime status, config digest, and verification API.

- `GET /v1/runtime/status` — instance id, config digest/generation, routes, backends, last-seen policy
- `POST /v1/runtime/verify` — compare expected config/policy/models digests (GitOps remediation close-loop)
- Immutable `RuntimeConfigSnapshot` at process start with last-known-good markers
- `GATEWAY_CONFIG_GENERATION` / `GATEWAY_INSTANCE_ID` for GitOps correlation

## 1.4.0

Enforcement evidence and Control Plane decision correlation.

- Decision records store `control_plane_decision_id`, policy/request digests, versions, and `enforcement_outcome`
- Response headers (OpenAI body unchanged): `x-ai-control-decision-id`, `x-ai-policy-bundle-id`, `x-ai-policy-digest`, `x-ai-request-digest`, `x-ai-runtime-version`
- Optional signed decision token (`decision_token`) verification via JWKS; `GATEWAY_REQUIRE_SIGNED_DECISION` for hard binding
- Compatible with Control Plane evaluate responses that omit digests (token or durable decision can fill them)

## 1.3.1

Release integrity fix.

- Real Control Plane e2e no longer fails when evaluate response omits `request_digest` (verify via durable decision API)
- Approver JWT sent on approve/reject against real Control Plane
- Clearer HTTP failure dumps in real CP golden-path script
- Release workflow gates image/chart publish on reusable real CP e2e (`needs`)
- Tag no longer runs a parallel ungated real CP workflow

## 1.3.0

Real platform compatibility proof.

- Combined e2e against published Control Plane image (nightly/tag workflow)
- OIDC/JWKS production-profile e2e (missing/invalid/expired/iss/aud)
- Approval binding/replay/body-mismatch coverage (mock + real CP)
- Gateway restart + Control Plane restart paths
- OCI Helm chart publish in release workflow
- SLO evidence scaffolding under `docs/evidence/v1.3.0/`

## 1.2.1

Authentication boundary fix (shipped in the 1.3.0 train).

- Unified `AuthenticationMiddleware` protects all non-public routes
- JWT claims cached on `request.state.identity_claims` (no double JWKS decode)
- Production requires `OIDC_JWT_ISSUER` + `OIDC_JWT_AUDIENCE`
- JWKS lookup via `asyncio.to_thread` + metrics
- Broken Sentinel manifests removed; managed Redis docs only
- vLLM NetworkPolicy egress documented as permissive by default + hardened profile

## 1.2.0

Architecture and supply-chain hardening on the frozen OpenAPI surface.

- Router/service split: `routers/*`, `services/*`, `stores/health.py`, `config.py`, `metrics.py`
- Redis HA reference docs / demo Redis separation
- vLLM chart: image digest, NetworkPolicy, ServiceAccount, imagePullSecrets, PVC keep policy, GPU profiles
- Compatibility matrix and OCI Helm publication notes

## 1.1.0

Production proof release (included in 1.2.0 train).

- `GATEWAY_PROFILE` modes: `local` / `internal` / `production`
- External Redis via Secret; OpenAI error envelope; streaming interruption classification
- Combined platform e2e + CI job; SLO targets wording

## 1.0.0

Stable Execution Plane boundary for AI Infrastructure OS with Control Plane 1.x.

## 0.6.0

Production vLLM Helm hardening.

## 0.5.0

Frozen OpenAPI, clean OpenAI bodies, SDK compatibility tests.

## 0.4.0

Streaming lifecycle correctness.

## 0.3.0

HA Redis profiles and readiness probes.

## 0.2.0

Trust boundary and Control Plane approval contract.

## 0.1.x

Initial Execution Plane gateway and demos.

[Unreleased]: https://github.com/justrunme/ai-runtime-platform/compare/v2.5.0...HEAD
[2.5.0]: https://github.com/justrunme/ai-runtime-platform/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/justrunme/ai-runtime-platform/compare/v2.3.0...v2.4.0
