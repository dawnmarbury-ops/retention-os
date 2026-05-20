"""Shared utilities for chain connectors: fail-loud die, HTTP with retry+backoff."""
from __future__ import annotations

import sys
import time

import requests

HTTP_RETRIES = 3
HTTP_TIMEOUT = 30


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def retryable_status(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def http_post_json(url: str, payload: dict, label: str) -> dict:
    backoff = 1.0
    last_err = "unknown"
    for _ in range(HTTP_RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if retryable_status(resp.status_code):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                die(f"{label} RPC error: {data['error']}")
            return data
        except requests.RequestException as exc:
            last_err = f"request failed: {exc}"
            time.sleep(backoff)
            backoff *= 2
    die(f"{label} failed after {HTTP_RETRIES} attempts: {last_err}")
    return {}


def http_get_json(url: str, label: str, params: dict | None = None) -> dict:
    backoff = 1.0
    last_err = "unknown"
    for _ in range(HTTP_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if retryable_status(resp.status_code):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = f"request failed: {exc}"
            time.sleep(backoff)
            backoff *= 2
    die(f"{label} failed after {HTTP_RETRIES} attempts: {last_err}")
    return {}
