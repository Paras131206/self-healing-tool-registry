"""LOCAL DEMO customer API: a real HTTP service on loopback whose contract is generated from the live database.

  GET  /contract/openapi.json   OpenAPI 3.1, built on every request from the customers table (PRAGMA): request
                                fields = the lookup index columns, response = every column, descriptions and
                                x-renamed-from from column_docs. A database migration therefore changes the contract.
  POST /customers/lookup        {<lookup columns>} -> {"status": "found", "query": {...}, "customer": {...}}
                                Unknown or missing fields -> 422 (FastAPI-style detail list); not found -> 404.
  GET  /health

Run:  python -m adaptix.localdemo service       (binds 127.0.0.1 only; refuses a port that is already in use)
"""
from __future__ import annotations

import socket

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .database import DemoDatabase, DemoDatabaseError

JSON_TYPE = {"INTEGER": "integer", "REAL": "number", "TEXT": "string"}
PATH = "/customers/lookup"
CONTRACT = "/contract/openapi.json"


def _prop(col: dict) -> dict:
    p = {"type": JSON_TYPE.get(col["type"], "string")}
    if col["description"]:
        p["description"] = col["description"]
    if col["renamed_from"]:
        p["x-renamed-from"] = col["renamed_from"]
    return p


def contract(db: DemoDatabase) -> dict:
    cols = {c["name"]: c for c in db.columns()}
    keys = db.lookup_columns()
    request = {"type": "object", "additionalProperties": False, "required": keys,
               "properties": {k: _prop(cols[k]) for k in keys}}
    customer = {"type": "object", "required": [n for n, c in cols.items() if c["not_null"]],
                "properties": {n: _prop(c) for n, c in cols.items()}}
    response = {"type": "object", "required": ["status", "query", "customer"], "properties": {
        "status": {"type": "string", "enum": ["found"], "description": "Lookup outcome."},
        "query": {"type": "object", "description": "The lookup values the service applied."},
        "customer": customer}}
    return {"openapi": "3.1.0", "info": {"title": "ADAPTIX local demo - customer API", "version": str(len(db.migrations()))},
            "paths": {PATH: {"post": {"operationId": "lookup_customer", "summary": "Look up one customer",
                                      "requestBody": {"required": True, "content": {"application/json": {"schema": request}}},
                                      "responses": {"200": {"description": "Customer found",
                                                            "content": {"application/json": {"schema": response}}},
                                                    "404": {"description": "No such customer"},
                                                    "422": {"description": "Invalid request"}}}}}}


def _type_ok(value, sql_type: str) -> bool:
    if sql_type == "INTEGER":
        return isinstance(value, int) and not isinstance(value, bool)
    if sql_type == "REAL":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def create_service(db_path: str) -> FastAPI:
    db = DemoDatabase(db_path)
    app = FastAPI(title="ADAPTIX local demo customer API", openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health")
    def health():
        return {"status": "ok" if db.is_demo() else "database_not_initialised"}

    @app.get(CONTRACT)
    def openapi():
        try:
            return contract(db)
        except DemoDatabaseError as e:
            return JSONResponse(status_code=503, content={"detail": str(e)})

    @app.post(PATH)
    async def lookup(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": [{"type": "json_invalid", "loc": ["body"],
                                                                      "msg": "JSON object expected"}]})
        if not isinstance(body, dict):
            return JSONResponse(status_code=422, content={"detail": [{"type": "dict_type", "loc": ["body"],
                                                                      "msg": "JSON object expected"}]})
        try:
            cols = {c["name"]: c for c in db.columns()}
            keys = db.lookup_columns()
        except DemoDatabaseError as e:
            return JSONResponse(status_code=503, content={"detail": str(e)})
        errors = [{"type": "missing", "loc": ["body", k], "msg": "Field required"} for k in keys if k not in body]
        errors += [{"type": "extra_forbidden", "loc": ["body", k], "msg": "Extra inputs are not permitted"}
                   for k in body if k not in keys]
        errors += [{"type": f"{JSON_TYPE[cols[k]['type']]}_type", "loc": ["body", k],
                    "msg": f"Input should be a valid {JSON_TYPE[cols[k]['type']]}"}
                   for k in keys if k in body and not _type_ok(body[k], cols[k]["type"])]
        if errors:
            return JSONResponse(status_code=422, content={"detail": errors})
        row = db.lookup({k: body[k] for k in keys})
        if row is None:
            return JSONResponse(status_code=404, content={"status": "not_found", "query": body})
        return {"status": "found", "query": {k: body[k] for k in keys}, "customer": row}

    return app


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # like uvicorn: TIME_WAIT is not "in use"
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


class ServiceThread:
    """Run the customer API in a background thread on loopback (tests and the single-process launcher)."""

    def __init__(self, db_path: str, port: int = 0, host: str = "127.0.0.1"):
        self.db_path, self.host, self.port = db_path, host, port
        self._server = self._thread = None

    def start(self, timeout: float = 10.0) -> "ServiceThread":
        import threading
        import time
        import uvicorn
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        self._server = uvicorn.Server(uvicorn.Config(create_service(self.db_path), log_level="warning",
                                                     access_log=False))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True,
                                        name="adaptix-local-demo-service")
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        return self

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self):
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=5)
