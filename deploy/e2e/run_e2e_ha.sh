#!/usr/bin/env bash
# Nightly HA closed-loop proof: Postgres + 2 CP + 2 Runtime + Redis + OIDC + signed verify.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
KEY_FILE="${RUNTIME_VERIFY_KEY_FILE:-/tmp/runtime-verify-e2e.pem}"
if [[ -z "${RUNTIME_VERIFY_PRIVATE_KEY_B64:-}" ]]; then
  if [[ ! -f "$KEY_FILE" ]]; then
    openssl genrsa -out "$KEY_FILE" 2048 >/dev/null 2>&1
  fi
  export RUNTIME_VERIFY_PRIVATE_KEY_B64
  RUNTIME_VERIFY_PRIVATE_KEY_B64="$(python3 - <<PY
import base64
from pathlib import Path
print(base64.b64encode(Path("$KEY_FILE").read_bytes()).decode())
PY
)"
fi
python3 -m pip install --quiet 'PyJWT[crypto]' cryptography >/dev/null

COMPOSE=(
  docker compose
  -f "$ROOT/docker-compose.yaml"
  -f "$ROOT/docker-compose.oidc.yaml"
  -f "$ROOT/docker-compose.real-cp.yaml"
  -f "$ROOT/docker-compose.ha.yaml"
)
A="${GATEWAY_A:-http://127.0.0.1:18080}"
B="${GATEWAY_B:-http://127.0.0.1:18081}"
OIDC="${OIDC_URL:-http://127.0.0.1:18083}"
CP_A="${CONTROL_PLANE_URL:-http://127.0.0.1:18084}"
CP_B="${CONTROL_PLANE_B_URL:-http://127.0.0.1:18085}"

mint_token() {
  curl -fsS -H 'Content-Type: application/json' --data-binary @- "$OIDC/token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

fail_http() {
  local label="$1" code="$2" file="$3"
  echo "${label}: unexpected HTTP ${code}" >&2
  cat "$file" >&2 || true
  exit 1
}

echo "== wait for HA stack =="
for _ in $(seq 1 120); do
  if curl -fsS "$A/readyz" >/dev/null \
    && curl -fsS "$B/readyz" >/dev/null \
    && curl -fsS "$CP_A/healthz" >/dev/null \
    && curl -fsS "$CP_B/healthz" >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS "$A/readyz" >/dev/null
curl -fsS "$B/readyz" >/dev/null
curl -fsS "$CP_A/healthz" >/dev/null
curl -fsS "$CP_B/healthz" >/dev/null

dev_token="$(printf '%s' '{"environment":"development","tenant_id":"finance","groups":["runtime-service"]}' | mint_token)"
other_token="$(printf '%s' '{"environment":"development","tenant_id":"hr","sub":"hr-user","groups":["runtime-service"]}' | mint_token)"
svc_token="$(printf '%s' '{"environment":"development","groups":["runtime-service","platform-admin"],"tenant_id":"platform"}' | mint_token)"
approver_token="$(printf '%s' '{"sub":"secops","preferred_username":"secops","groups":["ai-approvers","secops"]}' | mint_token)"

echo "== CP-A durable approval redeemed via Runtime→CP-B path =="
prod_token="$(printf '%s' '{"environment":"production","tool_access":true,"write_permission":true,"namespace":"ai-prod","groups":["platform","runtime-service"],"tenant_id":"finance"}' | mint_token)"
apr_code="$(curl -sS -o /tmp/ha-apr.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval ha"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr_code" == "409" ]] || fail_http "approval_required" "$apr_code" /tmp/ha-apr.json
approval_id="$(python3 -c 'import json; print(json.load(open("/tmp/ha-apr.json"))["approval_id"])')"
# Approve on CP-A (gateway-a path); redeem through gateway-b → CP-B (shared Postgres).
approve_code="$(curl -sS -o /tmp/ha-approve.json -w '%{http_code}' \
  -H "Authorization: Bearer ${approver_token}" \
  -H 'Content-Type: application/json' \
  -d '{"reviewer":"secops","comment":"ha e2e"}' \
  "$CP_A/approvals/${approval_id}/approve")"
[[ "$approve_code" == "200" ]] || fail_http "approve" "$approve_code" /tmp/ha-approve.json
curl -fsS -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -H "x-ai-approval-id: ${approval_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval ha"}]}' \
  "$B/v1/chat/completions" | grep -q 'hello from mock'

echo "== signed runtime verify (Control Plane closed-loop call shape) =="
status="$(curl -fsS -H "Authorization: Bearer ${svc_token}" "$A/v1/runtime/status")"
digest="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["configuration"]["observed_digest"])' <<<"$status")"
generation="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["configuration"]["generation"])' <<<"$status")"
[[ "$generation" == "7" ]] || { echo "expected generation 7, got $generation" >&2; exit 1; }
jwks="$(curl -fsS "$A/v1/runtime/jwks")"
verify="$(curl -fsS -H "Authorization: Bearer ${svc_token}" -H "Content-Type: application/json" \
  -d "{\"expected\":{\"config_digest\":\"${digest}\",\"generation\":7},\"remediation_id\":\"rem-ha-1\",\"correlation_id\":\"corr-ha-1\"}" \
  "$A/v1/runtime/verify")"
printf '%s' "$verify" > /tmp/ha-verify.json
printf '%s' "$jwks" > /tmp/ha-jwks.json
python3 - <<'PY'
import json

import jwt
from jwt.algorithms import RSAAlgorithm

body = json.load(open("/tmp/ha-verify.json"))
jwks = json.load(open("/tmp/ha-jwks.json"))
assert body["verified"] is True, body
assert body["correlation"]["config_generation"] == 7, body
assert body.get("verification_token"), body
assert body.get("remediation_id") == "rem-ha-1", body
key = RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
claims = jwt.decode(
    body["verification_token"],
    key=key,
    algorithms=["RS256"],
    audience="ai-control-plane",
    issuer="ai-runtime",
)
assert claims["verified"] is True
assert claims["correlation"]["config_generation"] == 7
assert claims["remediation_id"] == "rem-ha-1"
print("signed verify ok", claims["typ"])
PY

# Drift detection via the other replica (same generation, different instance_id).
drift="$(curl -fsS -H "Authorization: Bearer ${svc_token}" -H "Content-Type: application/json" \
  -d '{"expected":{"generation":999},"correlation_id":"corr-drift"}' \
  "$B/v1/runtime/verify")"
python3 -c 'import json,sys; b=json.loads(sys.argv[1]); assert b["verified"] is False and b["differences"][0]["field"]=="generation"' "$drift"

echo "== tenant isolation across replicas =="
req_id="ha-tenant-$(date +%s)"
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -H "x-request-id: ${req_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi finance"}]}' \
  "$A/v1/chat/completions" >/dev/null
ok_code="$(curl -sS -o /tmp/ha-dec-ok.json -w '%{http_code}' \
  -H "Authorization: Bearer ${dev_token}" "$B/v1/decisions/${req_id}")"
[[ "$ok_code" == "200" ]] || fail_http "same-tenant decision" "$ok_code" /tmp/ha-dec-ok.json
deny_code="$(curl -sS -o /tmp/ha-dec-deny.json -w '%{http_code}' \
  -H "Authorization: Bearer ${other_token}" "$B/v1/decisions/${req_id}")"
[[ "$deny_code" == "404" ]] || fail_http "cross-tenant decision leak" "$deny_code" /tmp/ha-dec-deny.json

echo "== pod termination during stream =="
rm -f /tmp/ha-stream.out /tmp/ha-stream.err
set +e
curl -sS -N --max-time 25 \
  -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream"}]}' \
  "$A/v1/chat/completions" > /tmp/ha-stream.out 2>/tmp/ha-stream.err &
stream_pid=$!
sleep 2
"${COMPOSE[@]}" kill -s SIGTERM gateway-a >/dev/null
wait "$stream_pid"
stream_rc=$?
set -e
# Client must not hang forever; incomplete terminal [DONE] is acceptable after SIGTERM.
if grep -q '\[DONE\]' /tmp/ha-stream.out; then
  echo "stream completed before terminate (acceptable race)"
else
  [[ "$stream_rc" -ne 0 ]] || [[ -s /tmp/ha-stream.out ]] || {
    echo "expected stream interrupt without clean DONE" >&2
    cat /tmp/ha-stream.out /tmp/ha-stream.err >&2 || true
    exit 1
  }
  echo "stream interrupted by gateway SIGTERM (rc=${stream_rc})"
fi
# Peer replica still serves after termination.
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi after kill"}]}' \
  "$B/v1/chat/completions" | grep -q 'hello from mock'
"${COMPOSE[@]}" start gateway-a >/dev/null
for _ in $(seq 1 60); do
  curl -fsS "$A/readyz" >/dev/null && break
  sleep 2
done

echo "OK: HA closed-loop e2e passed"
