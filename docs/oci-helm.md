# OCI Helm chart publication

The `charts/vllm-runtime` chart is versioned independently of the gateway image.

## Local package

```bash
helm package charts/vllm-runtime
helm lint charts/vllm-runtime
```

## OCI publish (release pipeline)

The release workflow packages and pushes:

```text
oci://ghcr.io/justrunme/charts/vllm-runtime:<semver>
```

Manual:

```bash
helm package charts/vllm-runtime --version 1.3.0 --app-version 1.3.0
helm push vllm-runtime-1.3.0.tgz oci://ghcr.io/justrunme/charts
```

Gateway image remains:

```text
ghcr.io/justrunme/ai-runtime-platform:<semver>
```

Prefer pinning vLLM with `image.digest` for production:

```yaml
image:
  repository: vllm/vllm-openai
  digest: sha256:...
```

Profiles:

- `values-production.yaml`
- `values-single-gpu.yaml`
- `values-tensor-parallel.yaml`
