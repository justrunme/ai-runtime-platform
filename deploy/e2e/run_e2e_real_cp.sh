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
prod_token="$(printf '%s' '{"environment":"production","tool_access":true,"write_permission":true,"namespace":"ai-prod"}' | mint_token)"

echo "== real CP allow (development) =="
curl -fsS -H "Authorization: Bearer ${dev_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions" | grep -q 'hello from mock'

echo "== real CP approval_required (production + tools) =="
apr_code="$(curl -sS -o /tmp/real-apr.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr_code" == "409" ]] || { echo "expected 409 got $apr_code" >&2; cat /tmp/real-apr.json; exit 1; }
approval_id="$(python3 -c 'import json; print(json.load(open("/tmp/real-apr.json"))["approval_id"])')"
policy_digest="$(python3 -c 'import json; print(json.load(open("/tmp/real-apr.json")).get("policy_digest") or "")')"
request_digest="$(python3 -c 'import json; print(json.load(open("/tmp/real-apr.json")).get("request_digest") or "")')"
[[ -n "$approval_id" ]]
[[ -n "$policy_digest" ]]
[[ -n "$request_digest" ]]

echo "== approve at real Control Plane =="
curl -fsS -H 'Content-Type: application/json' \
  -d '{"reviewer":"secops","comment":"e2e ok"}' \
  "$CP/approvals/${approval_id}/approve" >/dev/null

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
# After consumption CP should not allow again with same approval id.
[[ "$replay_code" != "200" ]] || { echo "replay unexpectedly allowed" >&2; cat /tmp/real-replay.json; exit 1; }

echo "== body change after approval rejected =="
apr2_code="$(curl -sS -o /tmp/real-apr2.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"need approval again"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr2_code" == "409" ]] || { echo "expected 409 got $apr2_code" >&2; exit 1; }
approval_id2="$(python3 -c 'import json; print(json.load(open("/tmp/real-apr2.json"))["approval_id"])')"
curl -fsS -H 'Content-Type: application/json' \
  -d '{"reviewer":"secops","comment":"e2e"}' \
  "$CP/approvals/${approval_id2}/approve" >/dev/null
mismatch_code="$(curl -sS -o /tmp/real-mismatch.json -w '%{http_code}' \
  -H "Authorization: Bearer ${prod_token}" -H "Content-Type: application/json" \
  -H "x-ai-approval-id: ${approval_id2}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"CHANGED BODY"}]}' \
  "$A/v1/chat/completions")"
[[ "$mismatch_code" != "200" ]] || { echo "changed body unexpectedly allowed" >&2; exit 1; }

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
curl -fsS -H "Authorization: Bearer ${dev_token}" "$B/v1/decisions/${req_id}" | grep -q llama3.1:8b

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
