"""HTTP client for the simulator.

Transport failures and 5xx are retried with backoff (every write we send is idempotent by
proposal_id, so a retry can never double-apply). 4xx are returned to the caller as ApiResult,
because they carry meaning (409 stale_base_version, 403 auto_apply_not_permitted, 402 credits).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("controller.api")


@dataclass
class ApiResult:
    status: int
    body: object

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def code(self) -> str | None:
        return self.body.get("error") if isinstance(self.body, dict) else None


class SimUnavailable(RuntimeError):
    pass


class Api:
    def __init__(self, base_url: str, timeout: float = 300.0, max_retries: int = 8):
        self.c = httpx.Client(base_url=base_url, timeout=timeout)
        self.max_retries = max_retries
        self.calls = 0

    def _req(self, method: str, path: str, **kw) -> ApiResult:
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                r = self.c.request(method, path, **kw)
                self.calls += 1
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise SimUnavailable(f"{method} {path}: {e}") from e
                log.warning("transport error on %s %s (%s); retry in %.1fs", method, path, e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 15)
                continue
            if r.status_code >= 500:
                if attempt == self.max_retries:
                    raise SimUnavailable(f"{method} {path}: HTTP {r.status_code}")
                log.warning("HTTP %s on %s %s; retry in %.1fs", r.status_code, method, path, delay)
                time.sleep(delay)
                delay = min(delay * 2, 15)
                continue
            try:
                body = r.json()
            except (json.JSONDecodeError, ValueError):
                body = r.text
            return ApiResult(r.status_code, body)
        raise SimUnavailable(path)

    def get(self, path: str, **params) -> ApiResult:
        return self._req("GET", path, params=params or None)

    def post(self, path: str, body: dict) -> ApiResult:
        return self._req("POST", path, json=body)

    def put(self, path: str, body: dict) -> ApiResult:
        return self._req("PUT", path, json=body)

    def chunk_lines(self, n: int):
        """Streams one NDJSON chunk; yields raw lines (bytes)."""
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                with self.c.stream("GET", f"/events/chunks/{n}") as r:
                    if r.status_code != 200:
                        raise httpx.TransportError(f"chunk {n}: HTTP {r.status_code}")
                    data = r.read()
                self.calls += 1
                return data.splitlines()
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise SimUnavailable(f"chunk {n}: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 15)
        raise SimUnavailable(f"chunk {n}")

    # convenience wrappers --------------------------------------------------------
    def must(self, res: ApiResult, what: str):
        if not res.ok:
            raise RuntimeError(f"{what}: HTTP {res.status} {res.body}")
        return res.body
