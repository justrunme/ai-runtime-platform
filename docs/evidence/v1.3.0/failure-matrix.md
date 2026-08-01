# Failure matrix — v1.3.0

| Scenario | Expected | Evidence |
| --- | --- | --- |
| Missing JWT on `/v1/models` | 401 | `run_e2e_oidc.sh` |
| Invalid / expired / wrong iss/aud JWT | 401 | `run_e2e_oidc.sh` |
| Governance block | 403, backend not called | `run_e2e.sh` (`/stats`) |
| Approval required → approve → retry | 409 then 200 | mock + real CP e2e |
| Approval replay | rejected | mock + real CP e2e |
| Approval body change | rejected | mock + real CP e2e |
| Redis down | `/readyz` 503 | `run_e2e.sh` |
| Control Plane down | chat 503 fail-closed | `run_e2e.sh` |
| Gateway replica stop | other replica serves | `run_e2e.sh` |
| Runtime restart | shared decision readable | `run_e2e_real_cp.sh` |
| Control Plane restart | inference resumes | `run_e2e_real_cp.sh` |

Attach CI run URLs for the release tag here after promotion.
