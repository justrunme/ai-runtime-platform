#!/usr/bin/env python3
"""Generate docs/results.json and docs/EVIDENCE.md from the budget-guard test run.

Honest scope: claims are on this test suite / this setup only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
RESULTS_PARTIAL = DOCS / "budget_guard_results.json"
RESULTS = DOCS / "results.json"
EVIDENCE = DOCS / "EVIDENCE.md"


def run_tests() -> None:
    env = os.environ.copy()
    env["BUDGET_EVIDENCE_DIR"] = str(DOCS)
    env.setdefault(
        "GITHUB_SHA",
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    )
    # Remove stale partial so a failed run cannot leave old envelopes.
    if RESULTS_PARTIAL.exists():
        RESULTS_PARTIAL.unlink()
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "app/gateway/tests/test_budget_guard.py",
        "-q",
        "--tb=short",
    ]
    subprocess.check_call(cmd, cwd=ROOT, env=env)


def load_partial() -> dict:
    if not RESULTS_PARTIAL.exists():
        raise SystemExit(f"missing {RESULTS_PARTIAL}; tests did not write evidence")
    return json.loads(RESULTS_PARTIAL.read_text(encoding="utf-8"))


def merge_results(partial: dict) -> dict:
    existing: dict = {}
    if RESULTS.exists():
        try:
            existing = json.loads(RESULTS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    run_url = "(local)"
    if os.environ.get("GITHUB_RUN_ID"):
        repo = os.environ.get("GITHUB_REPOSITORY", "justrunme/ai-runtime-platform")
        run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    concurrency = partial["concurrency"]
    existing["budget_guard"] = {
        "suite": partial["suite"],
        "scope": partial["scope"],
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_run": run_url,
        "commit_sha": partial.get("commit_sha"),
        "policy_version": partial["policy_version"],
        "policy_sha256": partial["policy_sha256"],
        "scenario_count": len(partial["envelopes"]),
        "false_deny_count_legitimate": partial["false_deny_count_legitimate"],
        "concurrency": {
            "rounds_per_backend": concurrency["rounds_per_backend"],
            "parallel_requests": concurrency["parallel_requests"],
            "overspend_events": concurrency["overspend_events"],
            "max_total_usd_observed": concurrency["max_total_usd_observed"],
            "budget_usd": concurrency["budget_usd"],
        },
        "limits": partial["limits"],
        "envelopes": partial["envelopes"],
    }
    if "repo" not in existing:
        existing["repo"] = "ai-runtime-platform"
    if "metrics" not in existing:
        existing["metrics"] = {}
    existing["metrics"]["budget_guard_false_deny"] = partial["false_deny_count_legitimate"]
    existing["metrics"]["budget_guard_overspend_events"] = concurrency["overspend_events"]
    existing["metrics"]["budget_guard_concurrency_rounds"] = concurrency["rounds_per_backend"] * 2
    return existing


def render_evidence(merged: dict) -> str:
    bg = merged["budget_guard"]
    conc = bg["concurrency"]
    envelopes = bg["envelopes"]
    # Deduplicate scenario rows (keep last).
    by_id: dict[str, dict] = {}
    for row in envelopes:
        by_id[row["scenario_id"]] = row

    lines = [
        "# Evidence",
        "",
        "Maps execution-plane claims to CI proof. Status vocabulary: **Tested in CI** /",
        "**Reference implementation** / **Simulated** / **Not yet**.",
        "",
        "Numbers are copied only from [docs/results.md](results.md), [docs/results.json](results.json),",
        "and [docs/compatibility.md](compatibility.md) plus the linked runs. No invented timings.",
        "",
        "## Claim table",
        "",
        "| Claim | Status | Scenario + environment | Real limits | Proof link | Negative tests |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    # Preserve prior non-budget claims from the previous EVIDENCE.md when present.
    prior = EVIDENCE.read_text(encoding="utf-8") if EVIDENCE.exists() else ""
    prior_rows = []
    if "| Claim | Status |" in prior:
        in_table = False
        for line in prior.splitlines():
            if line.startswith("| Claim | Status |"):
                in_table = True
                continue
            if in_table:
                if not line.startswith("|"):
                    break
                if line.startswith("| ---"):
                    continue
                if "Inference budget guard" in line or "budget guard" in line.lower():
                    continue
                prior_rows.append(line)
    lines.extend(prior_rows)

    run = bg.get("source_run") or "(local)"
    sha = bg.get("commit_sha") or "unknown"
    lines.append(
        "| Inference budget guard: atomic reserve ≤ budget under concurrency | "
        "Tested in CI | "
        f"`test_budget_guard` · {conc['parallel_requests']} parallel × "
        f"{conc['rounds_per_backend']} rounds × memory+redis/fakeredis | "
        "On this test suite / fakeredis or in-process memory — not multi-region billing; "
        "estimate may differ from actual usage; settlement can lag | "
        f"{run} · SHA `{sha}` | "
        f"overspend_events={conc['overspend_events']}; "
        f"max_total_usd_observed={conc['max_total_usd_observed']} ≤ budget "
        f"{conc['budget_usd']} |"
    )
    lines.append(
        "| Inference budget guard: fail-closed on store down/timeout/corrupt + malformed policy | "
        "Tested in CI | Same suite | Denies with 503; no silent allow on this setup | "
        f"{run} · SHA `{sha}` | store_down, store_timeout, store_corrupt, malformed_policy |"
    )
    lines.append(
        "| Inference budget guard: over-budget deny, settlement refund, expired release | "
        "Tested in CI | Same suite | Reservation TTL reclaim; refund = reserved − actual | "
        f"{run} · SHA `{sha}` | over_budget, settlement_refunds_unused, expired_reservation |"
    )
    lines.append(
        f"| Inference budget guard: legitimate within-budget allow (false-deny={bg['false_deny_count_legitimate']}) | "
        "Tested in CI | Same suite | On this test suite only | "
        f"{run} · SHA `{sha}` | false_deny_count_legitimate |"
    )

    lines += [
        "",
        "## Inference budget guard (this suite)",
        "",
        f"- Scope: **{bg['scope']}**",
        f"- Policy version: `{bg['policy_version']}`",
        f"- Policy sha256: `{bg['policy_sha256']}`",
        f"- Scenario envelopes: `{bg['scenario_count']}`",
        f"- False-deny count (legitimate): `{bg['false_deny_count_legitimate']}`",
        f"- Concurrency overspend events: `{conc['overspend_events']}` "
        f"(max total USD observed `{conc['max_total_usd_observed']}` vs budget `{conc['budget_usd']}`)",
        "",
        "### Decision envelopes",
        "",
        "| Scenario | Decision | Reason |",
        "| --- | --- | --- |",
    ]
    for scenario_id, row in sorted(by_id.items()):
        lines.append(f"| `{scenario_id}` | {row['decision']} | `{row['reason']}` |")

    lines += [
        "",
        "### Honest limits",
        "",
    ]
    for item in bg["limits"]:
        lines.append(f"- {item}")

    lines += [
        "",
        "## Not proven / out of scope",
        "",
        "- Multi-region gateway mesh / cross-region global admission (fleet Redis admission is compose/CI-proved only)",
        "- Production GPU capacity planning (vLLM chart is a template)",
        "- Billing / chargeback ledger (budget guard is an admission control, not an invoice system)",
        "- Treating compose golden-path seconds as production p99 latency",
        "- Blanket €0 overspend outside this test suite",
        "",
        "## How to reproduce",
        "",
        "```bash",
        "make validate",
        "python scripts/generate_budget_evidence.py",
        "# golden paths (see docs/demo-flow.md and deploy/e2e/):",
        "bash deploy/e2e/run_e2e.sh",
        "```",
        "",
        "CI entrypoints: `.github/workflows/ci.yaml`, `.github/workflows/budget-guard.yml`.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    run_tests()
    partial = load_partial()
    merged = merge_results(partial)
    RESULTS.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    EVIDENCE.write_text(render_evidence(merged), encoding="utf-8")
    print(f"wrote {RESULTS}")
    print(f"wrote {EVIDENCE}")
    print(
        json.dumps(
            {
                "scenarios": merged["budget_guard"]["scenario_count"],
                "false_deny": merged["budget_guard"]["false_deny_count_legitimate"],
                "overspend_events": merged["budget_guard"]["concurrency"]["overspend_events"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
