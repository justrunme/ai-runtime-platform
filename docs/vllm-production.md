# Production vLLM serving

Primary supported inference path: Helm chart `charts/vllm-runtime`.

## Features

- Model cache PVC and `/dev/shm`
- Startup probe with large load budget
- Recreate strategy for GPU workloads
- Optional HF token secret, revision/digest annotations
- PDB, security contexts, tensor-parallel and quantization knobs
- `values-production.yaml` profile

```bash
helm upgrade --install qwen charts/vllm-runtime \
  -f charts/vllm-runtime/values-production.yaml \
  --set model.revision=main \
  --set model.artifactDigest=sha256:example \
  --set huggingface.existingSecret=hf-token
```

## Maturity

| Component | Status |
| --- | --- |
| vLLM Helm chart | Supported |
| KServe examples | Reference |
| KEDA ScaledObject | Reference |
| Argo Rollouts canary | Experimental |
