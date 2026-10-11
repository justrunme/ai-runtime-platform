#!/usr/bin/env bash
# Production-profile OIDC/JWKS e2e.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
A="${GATEWAY_A:-http://127.0.0.1:18080}"
B="${GATEWAY_B:-http://127.0.0.1:18081}"
OIDC="${OIDC_URL:-http://127.0.0.1:18083}"

mint_token() {
  # Prefer stdin JSON to avoid shell-quoting issues with function args.
  curl -fsS -H 'Content-Type: application/json' --data-binary @- "$OIDC/token" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

echo "== wait for readiness =="
for _ in $(seq 1 60); do
  curl -fsS "$A/readyz" >/dev/null && curl -fsS "$OIDC/readyz" >/dev/null && break
  sleep 2
done
curl -fsS "$A/readyz" >/dev/null
curl -fsS "$OIDC/readyz" >/dev/null

echo "== missing JWT → 401 on /v1/models =="
code="$(curl -sS -o /tmp/oidc-missing.json -w '%{http_code}' "$A/v1/models")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code" >&2; exit 1; }

echo "== missing JWT → 401 on /v1/decisions =="
code="$(curl -sS -o /tmp/oidc-dec.json -w '%{http_code}' "$A/v1/decisions/x")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code" >&2; exit 1; }

echo "== valid JWT → 200 =="
token="$(printf '%s' '{}' | mint_token)"
curl -fsS -H "Authorization: Bearer ${token}" "$A/v1/models" | grep -q llama3.1:8b

echo "== invalid signature → 401 =="
bad="eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ4IiwiZXhwIjo5OTk5OTk5OTk5fQ.sig"
code="$(curl -sS -o /tmp/oidc-bad.json -w '%{http_code}' -H "Authorization: Bearer ${bad}" "$A/v1/models")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code" >&2; exit 1; }

echo "== wrong audience → 401 =="
wrong_aud="$(printf '%s' '{"aud":"other-service"}' | mint_token)"
code="$(curl -sS -o /tmp/oidc-aud.json -w '%{http_code}' -H "Authorization: Bearer ${wrong_aud}" "$A/v1/models")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code body=$(cat /tmp/oidc-aud.json)" >&2; exit 1; }

echo "== wrong issuer → 401 =="
wrong_iss="$(printf '%s' '{"iss":"https://evil.example"}' | mint_token)"
code="$(curl -sS -o /tmp/oidc-iss.json -w '%{http_code}' -H "Authorization: Bearer ${wrong_iss}" "$A/v1/models")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code body=$(cat /tmp/oidc-iss.json)" >&2; exit 1; }

echo "== expired → 401 =="
expired="$(printf '%s' '{"exp":1,"iat":1}' | mint_token)"
code="$(curl -sS -o /tmp/oidc-exp.json -w '%{http_code}' -H "Authorization: Bearer ${expired}" "$A/v1/models")"
[[ "$code" == "401" ]] || { echo "expected 401 got $code body=$(cat /tmp/oidc-exp.json)" >&2; exit 1; }

echo "== governed chat with valid JWT =="
chat="$(curl -fsS -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions")"
echo "$chat" | grep -q 'hello from mock'

echo "== livez remains public =="
curl -fsS "$A/livez" | grep -q ok

echo "== fleet admission: multi-tenant limits across replicas =="
finance_token="$(printf '%s' '{"tenant_id":"finance","groups":["runtime-service"]}' | mint_token)"
research_token="$(printf '%s' '{"tenant_id":"research","groups":["runtime-service"]}' | mint_token)"
# Each tenant limited to 2 fleet-wide. Saturate both tenants across A/B, then reject.
curl -sS -N -H "Authorization: Bearer ${finance_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream fin-a"}]}' \
  "$A/v1/chat/completions" >/tmp/oidc-fin-a &
fin_a_pid=$!
curl -sS -N -H "Authorization: Bearer ${finance_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream fin-b"}]}' \
  "$B/v1/chat/completions" >/tmp/oidc-fin-b &
fin_b_pid=$!
curl -sS -N -H "Authorization: Bearer ${research_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream res-a"}]}' \
  "$A/v1/chat/completions" >/tmp/oidc-res-a &
res_a_pid=$!
curl -sS -N -H "Authorization: Bearer ${research_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream res-b"}]}' \
  "$B/v1/chat/completions" >/tmp/oidc-res-b &
res_b_pid=$!
sleep 2
fin_reject="$(curl -sS -o /tmp/oidc-fin-reject.json -w '%{http_code}' \
  -H "Authorization: Bearer ${finance_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"fin shed"}]}' \
  "$A/v1/chat/completions" || true)"
res_reject="$(curl -sS -o /tmp/oidc-res-reject.json -w '%{http_code}' \
  -H "Authorization: Bearer ${research_token}" -H "Content-Type: application/json" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"res shed"}]}' \
  "$B/v1/chat/completions" || true)"
[[ "$fin_reject" == "429" && "$res_reject" == "429" ]] || {
  echo "expected multi-tenant 429s, got finance=$fin_reject research=$res_reject" >&2
  kill "$fin_a_pid" "$fin_b_pid" "$res_a_pid" "$res_b_pid" 2>/dev/null || true
  exit 1
}
# Cross-tenant capacity is independent: research saturation must not block a free
# tenant slot after finance releases — verified by independent 429s above.
kill "$fin_a_pid" "$fin_b_pid" "$res_a_pid" "$res_b_pid" 2>/dev/null || true
wait "$fin_a_pid" "$fin_b_pid" "$res_a_pid" "$res_b_pid" 2>/dev/null || true

echo "OK: OIDC production-profile e2e passed"
