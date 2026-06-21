# Architecture decisions

## Runtime first

The repository focuses on data-plane reliability: inference admission, GPU scheduling, traffic routing, performance signals, and delivery. Model governance and catalogue management are separate control-plane concerns.

## Queue pressure is the first scaling signal

CPU is weak as an LLM capacity signal: model execution may be GPU-bound while incoming work waits in the scheduler. The KEDA example therefore uses the vLLM waiting-request gauge. Production thresholds must be calibrated against a measured GPU/model/context profile and bounded by available GPU capacity.

## Cost is request attribution, not billing

The gateway attaches an estimated USD value from `usage.prompt_tokens` and `usage.completion_tokens`. It is useful for chargeback experiments and request-level telemetry. It does not include GPU reservation time, idle capacity, model loading, storage, network, or provider discounts, so it must not be reconciled as billing.

## Canary safety

Canaries must compare like-for-like: prompt mix, model revision, GPU SKU, tokenizer, context size, and runtime flags. A percentage traffic shift without SLO-based analysis is only a traffic split, not a safe rollout policy.
