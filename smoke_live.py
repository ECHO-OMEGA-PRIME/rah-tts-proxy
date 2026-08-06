#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

import httpx


REQUIRED_HEADERS = (
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
    "cache-control",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="No-spend RAH TTS proxy smoke")
    parser.add_argument("--base", required=True)
    parser.add_argument("--expect-build")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base, timeout=10, follow_redirects=False)
    checks: list[tuple[str, bool]] = []

    health = client.get("/health")
    body = health.json() if health.headers.get("content-type", "").startswith("application/json") else {}
    checks.append(("health", health.status_code == 200 and body.get("status") == "ok"))
    checks.append(("build", not args.expect_build or body.get("build") == args.expect_build))
    checks.append(("security_headers", all(health.headers.get(name) for name in REQUIRED_HEADERS)))
    checks.append(("voices_auth", client.get("/voices").status_code == 401))
    checks.append(("tts_auth", client.post("/tts", json={"text": "smoke"}).status_code == 401))
    checks.append(("clone_auth", client.post("/clone", json={}).status_code in {403, 422}))
    checks.append(("real_404", client.get("/not-a-route").status_code == 404))
    checks.append(("method_405", client.post("/health").status_code == 405))
    checks.append(("cors_reject", client.options("/tts", headers={"Origin": "https://invalid.example"}).status_code == 403))
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
