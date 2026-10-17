"""The controlled test service for the REAL_API_TEST environment.

A small, versioned API that WE control, served by a real HTTP server (uvicorn on a loopback TCP socket) with
an authoritative OpenAPI document. ADAPTIX calls it over real HTTP through the same guarded client used for
production; only this server's exact address is exempt from the host allowlist. No third-party API is
modified, and nothing here is a mock: requests, responses, status codes and side effects are real.

Areas
  /orders/...  orders API with two schema versions
               v1: POST /orders {customer_id, sku, quantity}
               v2: POST /orders {client_id, sku, quantity}   (rename documented in the field description)
               GET /orders/{order_id} reads back what was stored (independent business verification)
               mode "drop_writes": v2 answers 201 but does not store the order (a verification failure)
  /m/{name}/...  scenario-matrix APIs built from the shared scenario catalogue (one app per run)
  /admin/...   version switching and stats, protected by a per-process admin token (X-Admin-Token)
"""
import secrets
import socket
import threading
import time
import uuid
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, create_model

ORDER_SKUS = ["SKU-100", "SKU-200", "SKU-300"]


class _Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.orders: dict[str, dict] = {}
        self.idem: dict[str, dict] = {}
        self.version = "v1"
        self.mode = "normal"            # normal | drop_writes
        self.writes = 0                 # side effects that really happened
        self.calls = 0


def _orders_app(store: _Store, version: str) -> FastAPI:
    who = "customer_id" if version == "v1" else "client_id"
    desc = ("Identifier of the customer placing the order" if version == "v1" else
            "Identifier of the client placing the order (previously called customer_id)")

    OrderIn = create_model(f"OrderIn_{version}", __config__=ConfigDict(extra="forbid"), **{
        who: (str, Field(..., min_length=1, max_length=64, description=desc)),
        "sku": (Literal["SKU-100", "SKU-200", "SKU-300"], Field(description="Product SKU")),
        "quantity": (int, Field(ge=1, le=100, description="Number of units"))})
    Received = create_model(f"Received_{version}", **{who: (str, ...), "sku": (str, ...), "quantity": (int, ...)})
    OrderOut = create_model(f"OrderOut_{version}",
                            status=(Literal["created"], Field(description="Outcome of the operation")),
                            order_id=(str, Field(description="Identifier of the stored order")),
                            received=(Received, Field(description="The data the API applied")))
    OrderRecord = create_model(f"OrderRecord_{version}", order_id=(str, ...), sku=(str, ...), quantity=(int, ...),
                               placed_by=(str, Field(description=f"The {who} the order belongs to")),
                               schema_version=(str, ...))

    app = FastAPI(title="ADAPTIX controlled test service - orders", version="1.0.0" if version == "v1" else "2.0.0",
                  description="A test API controlled by ADAPTIX for reproducible recovery demonstrations.")

    @app.post("/orders", response_model=OrderOut, status_code=201)
    def create(order: OrderIn, idempotency_key: str | None = Header(default=None)):
        with store.lock:
            store.calls += 1
            if idempotency_key and idempotency_key in store.idem:
                return store.idem[idempotency_key]
            body = order.model_dump()
            oid = "ord_" + uuid.uuid4().hex[:10]
            if store.mode != "drop_writes":
                store.orders[oid] = {"order_id": oid, "sku": body["sku"], "quantity": body["quantity"],
                                     "placed_by": body[who], "schema_version": version}
                store.writes += 1
            out = {"status": "created", "order_id": oid, "received": body}
            if idempotency_key:
                store.idem[idempotency_key] = out
            return out

    @app.get("/orders/{order_id}", response_model=OrderRecord)
    def read(order_id: str):
        rec = store.orders.get(order_id)
        if rec is None:
            raise HTTPException(404, "order not found")
        return rec

    @app.get("/health")
    def health():
        return {"status": "ok", "schema_version": version}

    return app


class TestService:
    """Starts the controlled service on 127.0.0.1:<free port> in a background thread."""

    def __init__(self):
        self.admin_token = "adm-" + secrets.token_urlsafe(18)
        self.store = _Store()
        self._orders = {"v1": _orders_app(self.store, "v1"), "v2": _orders_app(self.store, "v2")}
        self._matrix: dict[str, object] = {}
        self._matrix_specs: dict[str, dict] = {}
        self.port: int | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.started_at: str | None = None

    # ---------------------------------------------------------------- ASGI dispatcher
    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            msg = await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            await send({"type": "lifespan.shutdown.complete"})
            return
        path = scope.get("path", "")
        if path.startswith("/admin"):
            return await self._admin(scope, receive, send)
        for prefix, app in (("/orders-api", self._orders[self.store.version]),):
            if path == prefix or path.startswith(prefix + "/"):
                return await app({**scope, "path": path[len(prefix):] or "/", "root_path": ""}, receive, send)
        if path.startswith("/m/"):
            name, _, rest = path[3:].partition("/")
            app = self._matrix.get(name)
            if app is not None:
                return await app({**scope, "path": "/" + rest, "root_path": ""}, receive, send)
        await JSONResponse({"detail": "not found"}, status_code=404)(scope, receive, send)

    async def _admin(self, scope, receive, send):
        req = Request(scope, receive)
        if not secrets.compare_digest(req.headers.get("x-admin-token", ""), self.admin_token):
            return await JSONResponse({"detail": "admin token required"}, status_code=401)(scope, receive, send)
        p = scope["path"]
        if p == "/admin/orders" and req.method == "POST":
            body = await req.json()
            with self.store.lock:
                if body.get("version") in ("v1", "v2"):
                    self.store.version = body["version"]
                if body.get("mode") in ("normal", "drop_writes"):
                    self.store.mode = body["mode"]
            return await JSONResponse(self.orders_state())(scope, receive, send)
        if p == "/admin/orders" and req.method == "GET":
            return await JSONResponse(self.orders_state())(scope, receive, send)
        if p.startswith("/admin/m/") and req.method == "POST":
            from .simulation import BY_ID, build_app
            name = p[len("/admin/m/"):]
            body = await req.json()
            if body.get("spec") is not None:                 # a Multi-Change Lab schema (validated again here)
                from .multichange import ChangeError, build_spec_app
                try:
                    self._matrix[name] = build_spec_app(body["spec"])
                except ChangeError as e:
                    return await JSONResponse({"detail": str(e)}, status_code=422)(scope, receive, send)
                self._matrix_specs[name] = {"spec": body["spec"]}
                return await JSONResponse({"installed": name, "custom": True})(scope, receive, send)
            s = BY_ID.get(body.get("scenario"))
            if s is None or body.get("phase") not in ("v1", "v2"):
                return await JSONResponse({"detail": "unknown scenario or phase"}, status_code=422)(scope, receive, send)
            self._matrix[name] = build_app(s, body["phase"], body.get("keys"))
            self._matrix_specs[name] = {"scenario": s.id, "phase": body["phase"]}
            return await JSONResponse({"installed": name, **self._matrix_specs[name]})(scope, receive, send)
        if p.startswith("/admin/m/") and req.method == "GET":
            name = p[len("/admin/m/"):]
            app = self._matrix.get(name)
            if app is None:
                return await JSONResponse({"detail": "unknown app"}, status_code=404)(scope, receive, send)
            return await JSONResponse({"calls": app.state.calls, "effects": app.state.effects,
                                       **self._matrix_specs[name]})(scope, receive, send)
        return await JSONResponse({"detail": "unknown admin route"}, status_code=404)(scope, receive, send)

    def clear_apps(self):
        """Forget every scenario app (used by the demo reset)."""
        self._matrix.clear()
        self._matrix_specs.clear()

    def orders_state(self) -> dict:
        return {"version": self.store.version, "mode": self.store.mode, "orders_stored": len(self.store.orders),
                "writes": self.store.writes, "calls": self.store.calls}

    # ---------------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def address(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def start(self, timeout: float = 10.0, port: int = 0) -> "TestService":
        """port=0 (default, used inside ADAPTIX): the OS picks a free port. A fixed port is for standalone use."""
        if self._thread and self._thread.is_alive():
            return self
        import uvicorn
        from .models import now_iso
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self, log_level="warning", lifespan="on", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]},
                                        name="adaptix-test-service", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.url + "/orders-api/health", timeout=1).status_code == 200:
                    self.started_at = now_iso()
                    return self
            except httpx.HTTPError:
                time.sleep(0.05)
        raise RuntimeError("the controlled test service did not start")

    def stop(self):
        if self._server:
            self._server.should_exit = True

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---------------------------------------------------------------- admin client (real HTTP)
    def admin(self, method: str, path: str, json: dict | None = None) -> dict:
        r = httpx.request(method, self.url + path, json=json, headers={"X-Admin-Token": self.admin_token}, timeout=5)
        r.raise_for_status()
        return r.json()

    def set_orders(self, version: str | None = None, mode: str | None = None) -> dict:
        return self.admin("POST", "/admin/orders", {"version": version, "mode": mode})

    on_install = None            # callback(name, scenario, phase, has_keys): lets ADAPTIX persist what is served

    def install(self, name: str, scenario: str, phase: str, keys: dict | None = None) -> dict:
        out = self.admin("POST", f"/admin/m/{name}", {"scenario": scenario, "phase": phase, "keys": keys})
        if self.on_install:
            self.on_install(name, scenario, phase, bool(keys), None)
        return out

    def install_spec(self, name: str, spec: dict) -> dict:
        """Serve a Multi-Change Lab schema at /m/<name> (spec = {"path", "fields"}, validated server-side)."""
        out = self.admin("POST", f"/admin/m/{name}", {"spec": spec})
        if self.on_install:
            self.on_install(name, None, "custom", False, spec)
        return out

    def remove(self, name: str):
        """Stop serving one app (a Multi-Change Lab reset affects only that experiment)."""
        self._matrix.pop(name, None)
        self._matrix_specs.pop(name, None)

    def restore(self, apps: dict) -> int:
        """Re-create scenario APIs after a restart (definitions are deterministic; keyed apps are not restored)."""
        from .simulation import BY_ID, build_app
        n = 0
        for name, spec in apps.items():
            if spec.get("spec"):
                from .multichange import ChangeError, build_spec_app
                try:
                    self._matrix[name] = build_spec_app(spec["spec"])
                    self._matrix_specs[name] = {"spec": spec["spec"]}
                    n += 1
                except ChangeError:
                    pass
                continue
            s = BY_ID.get(spec.get("scenario"))
            if s is not None and not spec.get("has_keys") and spec.get("phase") in ("v1", "v2"):
                self._matrix[name] = build_app(s, spec["phase"])
                self._matrix_specs[name] = {"scenario": s.id, "phase": spec["phase"]}
                n += 1
        return n

    def stats(self, name: str) -> dict:
        return self.admin("GET", f"/admin/m/{name}")


if __name__ == "__main__":       # standalone: python -m adaptix.testservice --port 8765
    import argparse
    ap = argparse.ArgumentParser(description="Run the ADAPTIX controlled test service on 127.0.0.1 by itself")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    svc = TestService().start(port=a.port)
    print(f"ADAPTIX test service listening on {svc.url}")
    print(f"  health:  {svc.url}/orders-api/health")
    print(f"  schema:  {svc.url}/orders-api/openapi.json")
    print("  admin token (for /admin routes, this process only): " + svc.admin_token, flush=True)
    try:
        while svc.running():
            time.sleep(1)
    except KeyboardInterrupt:
        svc.stop()
