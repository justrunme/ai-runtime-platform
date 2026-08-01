#!/usr/bin/env bash
# Production-profile OIDC/JWKS e2e.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
A="${GATEWAY_A:-http://127.0.0.1:18080}"
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

echo "OK: OIDC production-profile e2e passed"
