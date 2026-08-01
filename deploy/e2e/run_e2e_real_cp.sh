#!/usr/bin/env bash
# Release/nightly proof against published Control Plane + OIDC production profile.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE=(
  docker compose
  -f "$ROOT/docker-compose.yaml"
  -f "$ROOT/docker-compose.oidc.yaml"
  -f "$ROOT/docker-compose.real-cp.yaml"
)
A="${GATEWAY_A:-http://127.0.0.1:18080}"
B="${GATEWAY_B:-http://127.0.0.1:18081}"
OIDC="${OIDC_URL:-http://127.0.0.1:18083}"
CP="${CONTROL_PLANE_URL:-http://127.0.0.1:18084}"

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

echo "== wait for stack =="
for _ in $(seq 1 90); do
  if curl -fsS "$A/readyz" >/dev/null && curl -fsS "$CP/healthz" >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS "$A/readyz" >/dev/null
curl -fsS "$CP/healthz" >/dev/null

dev_token="$(printf '%s' '{"environment":"development","tool_access":false,"write_permission":false}' | mint_token)"
prod_token="$(printf '%s' '{"environment":"production","tool_access":true,"write_permission":true,"namespace":"ai-prod","groups":["platform"]}' | mint_token)"
# Approver token for Control Plane approve/reject when OIDC is enabled on CP.
# Published CP image defaults to demo mode (no JWT verify); still send identity.
approver_token="$(printf '%s' '{"sub":"secops","preferred_username":"secops","groups":["ai-approvers","secops"]}' | mint_token)"

echo "== real CP allow (development) =="
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions" | grep -q 'hello from mock'

echo "== real CP approval_required (production + tools) =="
apr_code="$(curl -sS -o /tmp/real-apr.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr_code" == "409" ]] || fail_http "approval_required" "$apr_code" /tmp/real-apr.json
python3 - <<'PY'
import json, sys
body = json.load(open("/tmp/real-apr.json"))
assert body.get("final_verdict") == "approval_required", body
assert body.get("approval_id"), body
assert body.get("decision_id"), body
assert body.get("policy_digest"), body
# request_digest may be absent on evaluate response; verify via CP decision API.
open("/tmp/real-approval-id.txt", "w").write(body["approval_id"])
open("/tmp/real-decision-id.txt", "w").write(body["decision_id"])
print("approval_id", body["approval_id"])
print("decision_id", body["decision_id"])
print("policy_digest", body["policy_digest"])
PY
approval_id="$(cat /tmp/real-approval-id.txt)"
decision_id="$(cat /tmp/real-decision-id.txt)"

echo "== durable decision has request_digest on Control Plane =="
decision="$(curl -fsS "$CP/governance/decisions/${decision_id}")"
echo "$decision" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("request_digest"), d; print(d["request_digest"][:16], "...")'

echo "== approve at real Control Plane =="
approve_code="$(curl -sS -o /tmp/real-approve.json -w '%{http_code}' \
  -H "Authorization: Bearer ${approver_token}" \
  -H 'Content-Type: application/json' \
  -d '{"reviewer":"secops","comment":"e2e ok"}' \
  "$CP/approvals/${approval_id}/approve")"
[[ "$approve_code" == "200" ]] || fail_http "approve" "$approve_code" /tmp/real-approve.json

echo "== retry with approval → inference =="
curl -fsS -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -H "x-ai-approval-id: ${approval_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval"}]}' \
  "$A/v1/chat/completions" | grep -q 'hello from mock'

echo "== one-time consumption / replay rejected =="
replay_code="$(curl -sS -o /tmp/real-replay.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -H "x-ai-approval-id: ${approval_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval"}]}' \
  "$A/v1/chat/completions")"
[[ "$replay_code" != "200" ]] || fail_http "replay unexpectedly allowed" "$replay_code" /tmp/real-replay.json

echo "== body change after approval rejected =="
apr2_code="$(curl -sS -o /tmp/real-apr2.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval again"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr2_code" == "409" ]] || fail_http "second approval_required" "$apr2_code" /tmp/real-apr2.json
approval_id2="$(python3 -c 'import json; print(json.load(open("/tmp/real-apr2.json"))["approval_id"])')"
approve2_code="$(curl -sS -o /tmp/real-approve2.json -w '%{http_code}' \
  -H "Authorization: Bearer ${approver_token}" \
  -H 'Content-Type: application/json' \
  -d '{"reviewer":"secops","comment":"e2e"}' \
  "$CP/approvals/${approval_id2}/approve")"
[[ "$approve2_code" == "200" ]] || fail_http "approve2" "$approve2_code" /tmp/real-approve2.json
mismatch_code="$(curl -sS -o /tmp/real-mismatch.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -H "x-ai-approval-id: ${approval_id2}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"CHANGED BODY"}]}' \
  "$A/v1/chat/completions")"
[[ "$mismatch_code" != "200" ]] || fail_http "changed body unexpectedly allowed" "$mismatch_code" /tmp/real-mismatch.json

echo "== runtime pod restart keeps shared decision =="
req_id="real-dec-$(date +%s)"
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -H "x-request-id: ${req_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions" >/dev/null
"${COMPOSE[@]}" restart gateway-a
for _ in $(seq 1 60); do
  curl -fsS "$A/readyz" >/dev/null && break
  sleep 2
done
curl -fsS -H "Authorization: Bearer ${dev_token}" "$B/v1/decisions/${req_id}" | grep -q 'llama3.1:8b'

echo "== Control Plane restart =="
"${COMPOSE[@]}" restart control-plane
for _ in $(seq 1 60); do
  curl -fsS "$CP/healthz" >/dev/null && break
  sleep 2
done
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi after cp restart"}]}' \
  "$A/v1/chat/completions" | grep -q 'hello from mock'

echo "OK: real Control Plane compatibility e2e passed"
