# Changelog

## 1.0.0

Stable Execution Plane boundary for AI Infrastructure OS with Control Plane 1.x.

- Trust boundary: fail-closed JWT, trusted-proxy mode, server-derived governance attributes
- Control Plane v1 approval contract (`x-ai-approval-id` forward + structured 409)
- HA profiles with Redis-required multi-replica readiness
- Observed streaming lifecycle and frozen OpenAI-compatible API
- Production vLLM Helm chart

## 0.6.0

Production vLLM Helm hardening.

## 0.5.0

Frozen OpenAPI, clean OpenAI bodies, SDK compatibility tests.

## 0.4.0

Streaming lifecycle correctness.

## 0.3.0

HA Redis profiles and readiness probes.

## 0.2.0

Trust boundary and Control Plane approval contract.

## 0.1.x

Initial Execution Plane gateway and demos.
