#!/usr/bin/env bash
# Resolve a Control Plane image to an immutable digest, then require a
# keyless cosign signature from the Control Plane Release workflow.
set -euo pipefail

IMAGE="${1:?usage: verify-control-plane-image.sh <image-ref>}"
IDENTITY_REGEXP="${COSIGN_CERTIFICATE_IDENTITY_REGEXP:-^https://github.com/justrunme/ai-infra-control-plane/\.github/workflows/release\.ya?ml@refs/(heads/main|tags/v.+)$}"
OIDC_ISSUER="${COSIGN_CERTIFICATE_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"
RESULT_FILE="${COMPAT_RESULT_FILE:-}"

repo_from_ref() {
  local ref="$1"
  if [[ "$ref" == *@sha256:* ]]; then
    printf '%s\n' "${ref%@*}"
    return
  fi
  # Strip a single trailing :tag (registry host may contain a port).
  printf '%s\n' "${ref%:*}"
}

resolve_digest() {
  local ref="$1"
  local digest=""
  if [[ "$ref" == *@sha256:* ]]; then
    printf '%s\n' "${ref##*@}"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    digest="$(docker buildx imagetools inspect --format '{{json .Manifest.Digest}}' "$ref" 2>/dev/null | tr -d '"' || true)"
    if [[ -z "$digest" || "$digest" == "null" ]]; then
      digest="$(docker buildx imagetools inspect "$ref" 2>/dev/null | awk '/^Digest:/{print $2; exit}' || true)"
    fi
  fi
  if [[ -z "$digest" && "$(command -v crane || true)" ]]; then
    digest="$(crane digest "$ref")"
  fi
  if [[ -z "$digest" ]]; then
    echo "failed to resolve digest for ${ref}" >&2
    return 1
  fi
  printf '%s\n' "$digest"
}

write_result() {
  local signature="$1"
  local e2e="${2:-skipped}"
  [[ -z "$RESULT_FILE" ]] && return 0
  python3 - "$RESULT_FILE" "$CONTROL_PLANE_REF" "$PINNED" "$DIGEST" "$signature" "$e2e" <<'PY'
import json, sys
path, ref, pinned, digest, signature, e2e = sys.argv[1:]
payload = {
    "control_plane": ref,
    "runtime": "2.3.x",
    "image": pinned,
    "digest": digest,
    "signature": signature,
    "e2e": e2e,
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY
}

CONTROL_PLANE_REF="${CONTROL_PLANE_REF:-$IMAGE}"
REPO="$(repo_from_ref "$IMAGE")"
if DIGEST="$(resolve_digest "$IMAGE")"; then
  PINNED="${REPO}@${DIGEST}"
else
  echo "digest inspect unavailable; will verify ${IMAGE} and pin afterwards" >&2
  PINNED="$IMAGE"
  DIGEST=""
fi

echo "Resolved ${IMAGE} -> ${PINNED}"

if ! command -v cosign >/dev/null 2>&1; then
  echo "cosign is required" >&2
  write_result "fail" "skipped"
  exit 1
fi

VERIFY_LOG="$(mktemp)"
verify_signed() {
  # Released tags use classic cosign signatures; current :main uses the
  # Sigstore bundle / OCI 1.1 packaging produced by recent Release runs.
  if cosign verify \
    --certificate-identity-regexp="${IDENTITY_REGEXP}" \
    --certificate-oidc-issuer="${OIDC_ISSUER}" \
    "${PINNED}" >"$VERIFY_LOG" 2>&1; then
    cat "$VERIFY_LOG"
    return 0
  fi
  echo "classic signature not found; trying Sigstore bundle format" >&2
  if cosign verify --new-bundle-format \
    --certificate-identity-regexp="${IDENTITY_REGEXP}" \
    --certificate-oidc-issuer="${OIDC_ISSUER}" \
    "${PINNED}" >"$VERIFY_LOG" 2>&1; then
    cat "$VERIFY_LOG"
    return 0
  fi
  cat "$VERIFY_LOG" >&2
  return 1
}

if ! verify_signed; then
  echo "cosign verify failed for ${PINNED}" >&2
  echo "expected keyless identity: Control Plane Release workflow" >&2
  write_result "fail" "skipped"
  exit 1
fi

if [[ -z "$DIGEST" || "$DIGEST" == "unresolved" ]]; then
  DIGEST="$(python3 - "$VERIFY_LOG" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
start = raw.rfind("[")
if start < 0:
    raise SystemExit(0)
data = json.loads(raw[start:])
print(data[0]["critical"]["image"]["docker-manifest-digest"])
PY
)"
fi
if [[ -n "$DIGEST" ]]; then
  PINNED="${REPO}@${DIGEST}"
fi

write_result "pass" "pending"
echo "CONTROL_PLANE_IMAGE=${PINNED}"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CONTROL_PLANE_IMAGE=${PINNED}" >> "$GITHUB_ENV"
fi
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "image=${PINNED}" >> "$GITHUB_OUTPUT"
  echo "digest=${DIGEST}" >> "$GITHUB_OUTPUT"
fi
