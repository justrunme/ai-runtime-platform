import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    inference: {
      executor: "ramping-arrival-rate",
      startRate: 1,
      timeUnit: "1s",
      preAllocatedVUs: 5,
      stages: [
        { target: 5, duration: "1m" },
        { target: 20, duration: "3m" },
        { target: 0, duration: "1m" },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<10000"],
  },
};

const endpoint = __ENV.GATEWAY_URL || "http://localhost:8080";
const model = __ENV.MODEL || "qwen2.5-7b-instruct";

export default function () {
  const response = http.post(
    `${endpoint}/v1/chat/completions`,
    JSON.stringify({
      model,
      messages: [{ role: "user", content: "Explain Kubernetes in one sentence." }],
      max_tokens: 64,
    }),
    { headers: { "Content-Type": "application/json" } },
  );
  check(response, { "returns a completion": (r) => r.status === 200 });
  sleep(1);
}
