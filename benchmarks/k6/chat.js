import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  vus: 10,
  duration: "30s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<2000"],
  },
};

const BASE = __ENV.BASE_URL || "http://127.0.0.1:8080";
const KEY = __ENV.GATEWAY_API_KEY || "";

export default function () {
  const headers = { "Content-Type": "application/json" };
  if (KEY) {
    headers.Authorization = `Bearer ${KEY}`;
  }
  const res = http.post(
    `${BASE}/v1/chat/completions`,
    JSON.stringify({
      model: __ENV.MODEL || "qwen",
      messages: [{ role: "user", content: "ping" }],
    }),
    { headers },
  );
  check(res, { "status is 200": (r) => r.status === 200 });
  sleep(0.1);
}
