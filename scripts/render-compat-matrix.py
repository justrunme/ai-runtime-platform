#!/usr/bin/env python3
"""Render Control Plane compatibility results as Markdown."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


PREFERRED_ORDER = ("2.4.0", "main", "2.0.0", "1.3.0")


def load_results(source: Path) -> list[dict]:
    files = [source] if source.is_file() else sorted(source.rglob("*.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in files]


def sort_key(row: dict) -> tuple[int, str]:
    ref = str(row.get("control_plane", ""))
    try:
        return PREFERRED_ORDER.index(ref), ref
    except ValueError:
        return len(PREFERRED_ORDER), ref


def cell(value: str) -> str:
    return value or "unknown"


def render(rows: list[dict], *, run_url: str = "", generated_at: str = "") -> str:
    rows = sorted(rows, key=sort_key)
    stamp = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Compatibility",
        "",
        "Control Plane × Runtime results from the real Control Plane e2e.",
        "Each image is resolved to a digest and checked with keyless "
        "`cosign verify` (identity = the Control Plane **Release** workflow) "
        "before the stack starts.",
        "",
        f"Last updated: `{stamp}`",
    ]
    if run_url:
        lines.append(f"Workflow run: {run_url}")
    lines += [
        "",
        "| Control Plane | Runtime | Image digest | Signature | e2e |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        digest = row.get("digest") or "unresolved"
        lines.append(
            "| {ref} | {runtime} | `{digest}` | {sig} | {e2e} |".format(
                ref=row.get("control_plane") or "",
                runtime=row.get("runtime") or "2.3.x",
                digest=digest,
                sig=cell(str(row.get("signature", "unknown"))),
                e2e=cell(str(row.get("e2e", "unknown"))),
            )
        )
    pins = [row for row in rows if row.get("image")]
    if pins:
        lines += ["", "## Pins", ""]
        for row in pins:
            lines.append(f"- `{row.get('control_plane')}` → `{row.get('image')}`")
    lines += [
        "",
        "## Verify locally",
        "",
        "```sh",
        "cosign verify \\",
        "  --certificate-identity-regexp \\",
        "  '^https://github.com/justrunme/ai-infra-control-plane/\\.github/workflows/release\\.ya?ml@refs/(heads/main|tags/v.+)$' \\",
        "  --certificate-oidc-issuer https://token.actions.githubusercontent.com \\",
        "  ghcr.io/justrunme/ai-infra-control-plane@<digest>",
        "```",
        "",
        "Supported-pair policy (beyond the last CI run) lives in",
        "[compatibility-matrix.md](compatibility-matrix.md).",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="JSON file or directory of result files")
    parser.add_argument("-o", "--output", type=Path, help="Write Markdown to this path")
    parser.add_argument("--run-url", default="", help="Workflow run URL to embed")
    parser.add_argument("--generated-at", default="", help="Override timestamp")
    args = parser.parse_args()
    markdown = render(
        load_results(args.source),
        run_url=args.run_url,
        generated_at=args.generated_at,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
