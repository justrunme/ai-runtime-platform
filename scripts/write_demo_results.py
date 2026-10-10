#!/usr/bin/env python3
import json, os, time
from pathlib import Path
payload = {
    "repo": "ai-runtime-platform",
    "source_run": "https://github.com/%s/actions/runs/%s"
    % (os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")),
    "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "metrics": {"suite": "platform-e2e", "script": "deploy/e2e/run_e2e.sh"},
}
Path("docs/results.json").write_text(json.dumps(payload, indent=2) + "\n")
print(json.dumps(payload, indent=2))
