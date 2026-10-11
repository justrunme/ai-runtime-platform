#!/usr/bin/env bash
# Golden-path checks against deploy/e2e/docker-compose.yaml (mock Control Plane).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
COMPOSE=(
  docker compose
  -f "$ROOT/docker-compose.yaml"
  -f "$ROOT/docker-compose.admission.yaml"
)
A="${GATEWAY_A:-http://127.0.0.1:18080}"
B="${GATEWAY_B:-http://127.0.0.1:18081}"
OPENAI_STATS="${OPENAI_STATS:-http://127.0.0.1:18082/stats}"
KEY="${GATEWAY_API_KEYS:-e2e-key}"
AUTH=(-H "Authorization: Bearer ${KEY}" -H "Content-Type: application/json")

echo "== wait for readiness =="
for url in "$A" "$B"; do
  ok=0
  for _ in $(seq 1 60); do
    if curl -fsS "$url/readyz" >/dev/null; then
      ok=1
      break
    fi
    sleep 2
  done
  [[ "$ok" == "1" ]] || { echo "readyz timeout for $url" >&2; exit 1; }
done

curl -fsS -X POST http://127.0.0.1:18082/stats/reset >/dev/null

echo "== allow → inference =="
before="$(curl -fsS "$OPENAI_STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat_completions"])')"
allow="$(curl -fsS "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions")"
echo "$allow" | grep -q 'hello from mock'
after="$(curl -fsS "$OPENAI_STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat_completions"])')"
[[ "$after" -gt "$before" ]] || { echo "backend was not called on allow" >&2; exit 1; }

echo "== block → backend not called (403) =="
before="$(curl -fsS "$OPENAI_STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat_completions"])')"
block_code="$(curl -sS -o /tmp/e2e-block.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-block please"}]}' \
  "$A/v1/chat/completions")"
[[ "$block_code" == "403" ]] || { echo "expected 403 got $block_code" >&2; cat /tmp/e2e-block.json; exit 1; }
after="$(curl -fsS "$OPENAI_STATS" | python3 -c 'import json,sys; print(json.load(sys.stdin)["chat_completions"])')"
[[ "$after" == "$before" ]] || { echo "backend was called on block" >&2; exit 1; }
python3 -c 'import json; b=json.load(open("/tmp/e2e-block.json")); assert "detail" not in b and b["error"]["code"]=="governance_blocked"'

echo "== approval_required → approve → retry =="
apr_code="$(curl -sS -o /tmp/e2e-apr.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-approve please"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr_code" == "409" ]] || { echo "expected 409 got $apr_code" >&2; cat /tmp/e2e-apr.json; exit 1; }
approval_id="$(python3 -c 'import json; print(json.load(open("/tmp/e2e-apr.json"))["approval_id"])')"
"${COMPOSE[@]}" exec -T mock-control-plane \
  python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/approvals/${approval_id}/approve', method='POST'))"
retry="$(curl -fsS "${AUTH[@]}" -H "x-ai-approval-id: ${approval_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-approve please"}]}' \
  "$A/v1/chat/completions")"
echo "$retry" | grep -q 'hello from mock'

echo "== approval replay rejected =="
replay_code="$(curl -sS -o /tmp/e2e-replay.json -w '%{http_code}' "${AUTH[@]}" \
  -H "x-ai-approval-id: ${approval_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-approve please"}]}' \
  "$A/v1/chat/completions")"
[[ "$replay_code" == "409" ]] || { echo "expected replay 409 got $replay_code" >&2; cat /tmp/e2e-replay.json; exit 1; }

echo "== approval body mismatch rejected =="
apr2_code="$(curl -sS -o /tmp/e2e-apr2.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-approve please"}]}' \
  "$A/v1/chat/completions")"
[[ "$apr2_code" == "409" ]] || { echo "expected 409 got $apr2_code" >&2; exit 1; }
approval_id2="$(python3 -c 'import json; print(json.load(open("/tmp/e2e-apr2.json"))["approval_id"])')"
"${COMPOSE[@]}" exec -T mock-control-plane \
  python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8080/approvals/${approval_id2}/approve', method='POST'))"
mismatch_code="$(curl -sS -o /tmp/e2e-mismatch.json -w '%{http_code}' "${AUTH[@]}" \
  -H "x-ai-approval-id: ${approval_id2}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e-approve CHANGED"}]}' \
  "$A/v1/chat/completions")"
[[ "$mismatch_code" == "409" ]] || { echo "expected mismatch 409 got $mismatch_code" >&2; cat /tmp/e2e-mismatch.json; exit 1; }

echo "== streaming [DONE] =="
stream="$(curl -fsS "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions")"
echo "$stream" | grep -q '\[DONE\]'

echo "== decision readable from other replica =="
req_id="e2e-decision-$(date +%s)"
curl -fsS "${AUTH[@]}" -H "x-request-id: ${req_id}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$A/v1/chat/completions" >/dev/null
decision="$(curl -fsS "${AUTH[@]}" "$B/v1/decisions/${req_id}")"
echo "$decision" | grep -q 'llama3.1:8b'

echo "== gateway-a stop → traffic via gateway-b =="
"${COMPOSE[@]}" stop gateway-a
curl -fsS "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$B/v1/chat/completions" | grep -q 'hello from mock'
"${COMPOSE[@]}" start gateway-a
for _ in $(seq 1 30); do
  curl -fsS "$A/readyz" >/dev/null && break
  sleep 2
done

echo "== fleet admission: global limit across replicas =="
# Limit is 2 fleet-wide for tenant "platform". Hold two slow streams, then expect 429.
rm -f /tmp/e2e-adm-hold-a /tmp/e2e-adm-hold-b /tmp/e2e-adm-reject.json
curl -sS -N "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream hold-a"}]}' \
  "$A/v1/chat/completions" >/tmp/e2e-adm-hold-a &
hold_a_pid=$!
curl -sS -N "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream hold-b"}]}' \
  "$B/v1/chat/completions" >/tmp/e2e-adm-hold-b &
hold_b_pid=$!
sleep 2
reject_code="$(curl -sS -o /tmp/e2e-adm-reject.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"should shed"}]}' \
  "$A/v1/chat/completions" || true)"
[[ "$reject_code" == "429" ]] || {
  echo "expected fleet 429 got $reject_code" >&2
  cat /tmp/e2e-adm-reject.json >&2 || true
  kill "$hold_a_pid" "$hold_b_pid" 2>/dev/null || true
  exit 1
}
python3 -c 'import json; b=json.load(open("/tmp/e2e-adm-reject.json")); assert b["error"]["code"]=="tenant_concurrency_exceeded"'
kill "$hold_a_pid" "$hold_b_pid" 2>/dev/null || true
wait "$hold_a_pid" "$hold_b_pid" 2>/dev/null || true
# Allow disconnect finalizers or lease TTL to free capacity.
sleep 4

echo "== fleet admission: SIGKILL mid-stream recovers after lease TTL =="
curl -sS -N "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream kill-me"}]}' \
  "$A/v1/chat/completions" >/tmp/e2e-adm-kill-stream &
kill_stream_pid=$!
sleep 1
"${COMPOSE[@]}" kill -s SIGKILL gateway-a
kill "$kill_stream_pid" 2>/dev/null || true
wait "$kill_stream_pid" 2>/dev/null || true
# Lease TTL is 3s; wait for expiry then fill the fleet limit again from gateway-b.
sleep 4
curl -sS -N "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream after-kill-1"}]}' \
  "$B/v1/chat/completions" >/tmp/e2e-adm-after-1 &
after1_pid=$!
curl -sS -N "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"e2e-slow-stream after-kill-2"}]}' \
  "$B/v1/chat/completions" >/tmp/e2e-adm-after-2 &
after2_pid=$!
sleep 2
over_code="$(curl -sS -o /tmp/e2e-adm-over.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"over limit"}]}' \
  "$B/v1/chat/completions" || true)"
[[ "$over_code" == "429" ]] || {
  echo "expected 429 after recovery fill, got $over_code" >&2
  cat /tmp/e2e-adm-over.json >&2 || true
  kill "$after1_pid" "$after2_pid" 2>/dev/null || true
  exit 1
}
kill "$after1_pid" "$after2_pid" 2>/dev/null || true
wait "$after1_pid" "$after2_pid" 2>/dev/null || true
"${COMPOSE[@]}" start gateway-a
for _ in $(seq 1 30); do
  curl -fsS "$A/readyz" >/dev/null && break
  sleep 2
done
# Reclaim any leases left by SIGKILL / client abort.
sleep 4

echo "== Redis unavailable → /readyz 503 + admission fail-closed 503 =="
"${COMPOSE[@]}" stop redis
sleep 2
ready_code="$(curl -sS -o /tmp/e2e-ready-down.json -w '%{http_code}' "$B/readyz" || true)"
adm_code="$(curl -sS -o /tmp/e2e-adm-redis-down.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$B/v1/chat/completions" || true)"
"${COMPOSE[@]}" start redis
for _ in $(seq 1 30); do
  curl -fsS "$B/readyz" >/dev/null && break
  sleep 2
done
[[ "$ready_code" == "503" ]] || { echo "expected readyz 503, got $ready_code" >&2; exit 1; }
[[ "$adm_code" == "503" ]] || {
  echo "expected admission 503 when Redis down, got $adm_code" >&2
  cat /tmp/e2e-adm-redis-down.json >&2 || true
  exit 1
}
python3 -c 'import json; b=json.load(open("/tmp/e2e-adm-redis-down.json")); assert b["error"]["code"]=="admission_store_unavailable", b'

echo "== Control Plane unavailable → fail-closed 503 =="
"${COMPOSE[@]}" stop mock-control-plane
sleep 1
cp_code="$(curl -sS -o /tmp/e2e-cp-down.json -w '%{http_code}' "${AUTH[@]}" \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}' \
  "$B/v1/chat/completions" || true)"
"${COMPOSE[@]}" start mock-control-plane
for _ in $(seq 1 30); do
  curl -fsS "$B/readyz" >/dev/null && break
  sleep 2
done
[[ "$cp_code" == "503" ]] || { echo "expected chat 503 when CP down, got $cp_code" >&2; exit 1; }

echo "OK: platform e2e golden paths passed"
