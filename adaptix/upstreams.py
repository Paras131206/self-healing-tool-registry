"""HTTP access to upstream APIs, split by mode and enforced here (not in the UI).

* production: REAL providers only, every request through the SSRF guard (allowlist, public IPs, DNS
  pinning, verified TLS, validated redirects, per-host pacing). sim:// endpoints are refused.
* real_api_test: the controlled test service ADAPTIX runs itself (a real HTTP server on loopback), through
  the same guard; only that exact address is exempt from the allowlist. sim:// is refused.
* local_demo: the LOCAL DEMO customer API on this machine (loopback), through the same guard; only its exact
  configured address is exempt. Never enabled on a deployment (ADAPTIX_LOCAL_DEMO is read from the OS env only).
* simulation: in-process simulated APIs (sim://name) only. Nothing leaves the process; any real URL is
  refused, so a simulated schema mutation can never be sent to a real provider.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx

from .netguard import EgressDenied, GuardedHTTP

MODES = ("production", "real_api_test", "simulation", "local_demo")


class Upstreams:
    def __init__(self, timeout_s: float = 10.0, mode: str = "simulation", guard: GuardedHTTP | None = None,
                 max_schema_bytes: int = 5_000_000):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        self.mode, self.timeout_s, self.max_schema_bytes = mode, timeout_s, max_schema_bytes
        self.guard = guard if guard is not None else (GuardedHTTP((), timeout_s=timeout_s) if mode != "simulation" else None)
        self._sim_clients: dict[str, object] = {}
        # REAL_API_TEST only: returns the controlled service's CURRENT base URL in this process (starting it if
        # needed). Loopback URLs recorded earlier (another port, another process) are re-pointed to it; the guard
        # still exempts only that exact address. Production never rewrites anything.
        self.resolve = None
        self._sim_apps: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def is_simulated(base_url: str) -> bool:
        return base_url.startswith("sim://")

    # ---------------------------------------------------------------- simulated APIs
    def set_simulated(self, name: str, app):
        """Install or replace (i.e. 'drift') the simulated API behind sim://<name>."""
        if self.mode != "simulation":
            raise EgressDenied("simulated_in_production", "simulated APIs cannot be installed in production mode")
        from fastapi.testclient import TestClient
        with self._lock:
            self._sim_apps[name] = app
            self._sim_clients[name] = TestClient(app, base_url=f"http://{name}.simulated")

    def sim_app(self, name: str):
        return self._sim_apps.get(name)

    def _sim_client(self, url: str):
        name = url[len("sim://"):].split("/", 1)[0]
        c = self._sim_clients.get(name)
        if c is None:
            raise KeyError(f"simulated API sim://{name} is not running")
        return c

    def _check_mode(self, url: str):
        sim = self.is_simulated(url)
        if self.mode != "simulation" and sim:
            raise EgressDenied("simulated_in_production", f"sim:// endpoints are not allowed in {self.mode} mode")
        if self.mode == "simulation" and not sim:
            raise EgressDenied("real_in_simulation", "the Simulation Lab never contacts real providers")

    # ---------------------------------------------------------------- requests
    LOOPBACK = ("127.0.0.1", "localhost", "::1")

    def rebase(self, url: str) -> str:
        if self.mode != "real_api_test" or self.resolve is None:
            return url
        from urllib.parse import urlsplit, urlunsplit
        p = urlsplit(url)
        if p.scheme != "http" or (p.hostname or "") not in self.LOOPBACK:
            return url
        cur = urlsplit(self.resolve())
        return urlunsplit((cur.scheme, cur.netloc, p.path, p.query, ""))

    def request(self, base_url: str, method: str, url: str, **kw) -> httpx.Response:
        self._check_mode(base_url)
        base_url = self.rebase(base_url)
        if self.is_simulated(base_url):
            kw.pop("timeout", None)            # in-process test client does not support per-request timeouts
            return self._sim_client(base_url).request(method, url, **kw)
        headers = {"Accept": "application/json", **(kw.pop("headers", None) or {})}
        return self.guard.request(method, base_url.rstrip("/") + url, headers=headers, **kw)

    def fetch_json(self, url: str) -> SimpleNamespace:
        """GET an (absolute) schema URL. Returns status and parsed JSON (None if not JSON)."""
        self._check_mode(url)
        url = self.rebase(url)
        if self.is_simulated(url):
            path = "/" + url[len("sim://"):].split("/", 1)[1] if "/" in url[len("sim://"):] else "/"
            r = self._sim_client(url).get(path)
            try:
                return SimpleNamespace(status=r.status_code, json=r.json())
            except ValueError:
                return SimpleNamespace(status=r.status_code, json=None)
        status, doc = self.guard.get_json(url, self.max_schema_bytes)
        return SimpleNamespace(status=status, json=doc)
