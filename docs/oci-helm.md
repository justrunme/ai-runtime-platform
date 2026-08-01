# OCI Helm chart publication

The `charts/vllm-runtime` chart is versioned independently of the gateway image.

## Local package

```bash
helm package charts/vllm-runtime
helm lint charts/vllm-runtime
```

## Intended OCI publish (release pipeline)

```bash
helm push vllm-runtime-<version>.tgz oci://ghcr.io/justrunme/charts
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
