"""Inspect a recorded gateway routing decision."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from app.gateway.decisions import format_decision_tree


def fetch_decision(gateway_url: str, request_id: str) -> dict[str, object]:
    url = f"{gateway_url.rstrip('/')}/v1/decisions/{request_id}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SystemExit(f"routing decision not found for request_id={request_id}") from error
        raise SystemExit(f"gateway returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"failed to reach gateway at {gateway_url}: {error.reason}") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arp",
        description="AI Runtime Platform operator tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser(
        "replay",
        help="Print the routing decision tree for a request ID.",
    )
    replay.add_argument(
        "--request-id",
        required=True,
        help="X-Request-ID value recorded by the gateway.",
    )
    replay.add_argument(
        "--gateway",
        default="http://localhost:8080",
        help="Gateway base URL (default: http://localhost:8080).",
    )
    replay.add_argument(
        "--json",
        action="store_true",
        help="Print the raw decision JSON instead of the tree view.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command != "replay":
        raise SystemExit(f"unsupported command: {args.command}")

    decision = fetch_decision(args.gateway, args.request_id)
    if args.json:
        print(json.dumps(decision, indent=2, sort_keys=True))
    else:
        print(format_decision_tree(decision))


if __name__ == "__main__":
    main(sys.argv[1:])
