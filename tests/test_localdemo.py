"""LOCAL DEMO: real SQLite database, real local customer API over HTTP, real ADAPTIX engine.

The local MODEL is replaced by a test double in these tests (an httpx transport, or a stand-in HTTP server for the
adapter's request shape). They are NOT live local-model tests and are never labelled as real inference; the manual
demo uses a real Ollama model. tests/test_localdemo_live.py runs only when an Ollama model is actually installed."""
import json
import sqlite3
import threading
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from adaptix.config import Settings
from adaptix.llm.ollama import LocalLLMError, OllamaProvider
from adaptix.localdemo.database import FIXTURE, DemoDatabase, DemoDatabaseError
from adaptix.localdemo.lab import LabError, LocalLab
from adaptix.localdemo.service import ServiceThread, port_free

MODEL = "qwen2.5:3b-instruct"


def plan(target="body:client_id", source="customer_id", evidence="x-renamed-from: customer_id", confidence=0.95):
    return {"diagnosis": "the key field was renamed", "evidence": [evidence], "affected_fields": [target],
            "mappings": [{"target_field": target, "source_arg": source, "transform": "none", "evidence": evidence}],
            "response_mappings": [], "compatibility_risks": [], "confidence": confidence, "verification_steps": []}


class FakeOllama:
    """A scripted test double for Ollama's HTTP API (not a model)."""
    def __init__(self, script=None, installed=(MODEL,), up=True):
        self.script, self.installed, self.up, self.calls = list(script or []), list(installed), up, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("connection refused", request=req)
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test-double"})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "size": 1_900_000_000} for m in self.installed]})
        body = json.loads(req.content)
        self.calls.append(body)
        if body["model"] not in self.installed:
            return httpx.Response(404, json={"error": f"model '{body['model']}' not found"})
        item = self.script.pop(0) if self.script else plan()
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item)
        return httpx.Response(200, json={"message": {"content": content}, "prompt_eval_count": 300, "eval_count": 60})


@pytest.fixture
def world(tmp_path):
    db = DemoDatabase(tmp_path / "local-demo.db")
    db.init()
    svc = ServiceThread(str(db.path)).start()
    fake = FakeOllama()
    llm = OllamaProvider("http://127.0.0.1:11434", MODEL, transport=httpx.MockTransport(fake))
    s = Settings(local_demo=True, local_demo_db=str(db.path), local_db_path=str(tmp_path / "adaptix-local.db"),
                 db_path=str(tmp_path / "p.db"), sim_db_path=str(tmp_path / "s.db"), test_db_path=str(tmp_path / "t.db"),
                 local_service_url=svc.url)
    lab = LocalLab(s, llm=llm)
    yield lab, fake, db
    svc.stop()


def run(lab, scenario, params=None):
    j = lab.start(scenario, params or {}, True)
    for _ in range(400):
        j = lab.job(j["id"])
        if j["status"] != "running":
            return j
        time.sleep(0.025)
    raise AssertionError("run did not finish")


# ---------------------------------------------------------------- 1 database
def test_seed_is_deterministic_with_constraints_and_fixtures(tmp_path):
    a, b = DemoDatabase(tmp_path / "a.db"), DemoDatabase(tmp_path / "b.db")
    a.init(), b.init()
    dump = lambda d: [tuple(r) for t in ("customers", "products", "orders", "order_items")      # noqa: E731
                      for r in sqlite3.connect(d.path).execute(f"SELECT * FROM {t} ORDER BY 1")]
    assert dump(a) == dump(b)
    assert a.counts() == {"customers": 40, "products": 12, "orders": 120, "order_items": a.counts()["order_items"]}
    assert a.lookup({"customer_id": 101}) == FIXTURE and a.orders_for(101) >= 3
    assert a.integrity() == {"integrity_ok": True, "foreign_key_violations": 0, "marker": True}
    c = sqlite3.connect(a.path)
    c.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO orders VALUES (9999, 424242, '2026-01-01', 'placed')")      # foreign key enforced
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO customers VALUES (500, 'X Y', 'x@example.com', 'Pune', 'vip', 0, '2026-01-01')")
    assert a.init()["created"] is False                                  # never overwrites an existing database


def test_rename_migration_really_changes_the_schema_and_keeps_the_data(tmp_path):
    d = DemoDatabase(tmp_path / "d.db")
    d.init()
    d.rename_column("customer_id", "client_id")
    cols = [r[1] for r in sqlite3.connect(d.path).execute("PRAGMA table_info(customers)")]
    assert "client_id" in cols and "customer_id" not in cols
    assert d.lookup({"client_id": 101})["full_name"] == "Asha Verma" and d.orders_for(101) >= 3
    assert d.integrity()["foreign_key_violations"] == 0 and d.migrations()[-1]["kind"] == "rename_column"


def test_identifiers_are_allowlisted_and_foreign_files_are_refused(tmp_path):
    d = DemoDatabase(tmp_path / "d.db")
    d.init()
    for bad in ('client_id"; DROP TABLE orders; --', "Client-ID", "full_name"):
        with pytest.raises(DemoDatabaseError):
            d.rename_column("customer_id", bad)
    with pytest.raises(DemoDatabaseError):
        d.rename_column("orders", "x_id")                                # not a customers column
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE customers (customer_id INTEGER)")
    with pytest.raises(DemoDatabaseError):
        DemoDatabase(other).rename_column("customer_id", "client_id")    # no demo marker: refused
    with pytest.raises(DemoDatabaseError):
        DemoDatabase(other).reset()


# ---------------------------------------------------------------- 2-4 baseline, drift, contracts
def test_baseline_lookup_and_registered_vs_live_contract_after_drift(world):
    lab, _, db = world
    lab.ensure_registered()
    r = lab.engine.execute("customer_lookup", {"customer_id": 101})
    assert r["status"] == "ok" and r["result"]["customer"]["full_name"] == "Asha Verma"
    assert lab.contracts()["diff"]["changed"] is False
    db.rename_column("customer_id", "client_id")
    c = lab.contracts()
    assert c["diff"]["changed"] is True
    assert {"kind": "renamed", "field": "body:client_id", "was": "body:customer_id", "now": "int"} in c["diff"]["changes"]
    assert any(f["field"] == "body:customer_id" for f in c["registered"])          # registered contract unchanged
    assert any(f["field"] == "body:client_id" and f["renamed_from"] == "customer_id" for f in c["live"])


# ---------------------------------------------------------------- 7-8 the full recovery
def test_rename_drift_is_repaired_by_the_local_model_and_verified(world):
    lab, fake, db = world
    j = run(lab, "rename")
    r = j["result"]
    assert r["final"] == "VERIFIED" and r["as_expected"] and r["tool_version"] == 2
    assert len(fake.calls) == 1 and fake.calls[0]["model"] == MODEL and fake.calls[0]["format"]["type"] == "object"
    assert fake.calls[0]["options"]["temperature"] == 0 and fake.calls[0]["stream"] is False
    assert r["plan"]["source"] == "llm" and r["validation"]["accepted"]
    names = {c["name"]: c["ok"] for c in r["business_checks"]}
    assert names["customer identity"] and names["customer name"] and names["required field: email"]
    assert r["llm"]["real"] is False                         # a test double is never labelled real inference
    phases = [e["phase"] for e in j["events"]]
    for p in ("Readiness", "Baseline request", "Backup", "Schema mutation", "Drift detection", "Model proposal",
              "Independent validation", "Patch application", "Business verification", "Outcome"):
        assert p in phases, p
    assert phases.index("Baseline request") < phases.index("Schema mutation") < phases.index("Model proposal")
    retry = next(s for s in lab.storage.incident(r["incident"]).timeline if s.title.startswith("retry with v2"))
    assert retry.detail["sent"] == {"client_id": 101}         # the ORIGINAL value, sent under the new field name
    ev = lab.storage.evidence(limit=5)[0]
    assert ev["proposal"]["source"] == "llm" and ev["validation"]["accepted"] and ev["applied_change"]["to_version"] == 2
    assert ev["final"]["verified_recovery"] is True and ev["local_final"] == "VERIFIED"


@pytest.mark.parametrize("scenario,final", [("type", "VERIFIED (no repair needed)"), ("required", "SAFELY BLOCKED"),
                                            ("remove", "SAFELY BLOCKED")])
def test_other_scenarios_report_their_actual_outcomes(world, scenario, final):
    lab, _, _ = world
    r = run(lab, scenario)["result"]
    assert r["final"] == final and r["as_expected"], r
    if final == "SAFELY BLOCKED":
        assert r["failed_stage"] in ("Independent validation", "Business verification") and r["tool_version"] == 1
        assert lab.storage.current("customer_lookup").version == 1                   # nothing applied


# ---------------------------------------------------------------- 6 the local model fails safely
@pytest.mark.parametrize("item,category", [("{not json", "malformed_output"),
                                           (httpx.ReadTimeout("slow"), "timeout")])
def test_model_failures_stop_safely(world, item, category):
    lab, fake, _ = world
    fake.script = [item, item]
    r = run(lab, "rename")["result"]
    assert r["final"] == "SAFELY BLOCKED" and r["llm"]["error_category"] == category
    assert lab.storage.current("customer_lookup").version == 1


@pytest.mark.parametrize("bad", [plan(target="body:customerId"),                      # field not in the schema
                                 plan(source="email"),                                # argument the caller never sent
                                 plan(evidence="the id was probably renamed"),        # evidence not in the schema
                                 plan(confidence=0.3)])                               # below the confidence floor
def test_invalid_or_unsafe_proposals_are_rejected(world, bad):
    lab, fake, _ = world
    fake.script = [bad]
    r = run(lab, "rename")["result"]
    assert r["final"] == "SAFELY BLOCKED" and r["failed_stage"] == "Independent validation"
    assert r["validation"]["accepted"] is False and lab.storage.current("customer_lookup").version == 1


def test_missing_model_or_stopped_ollama_blocks_before_any_change(world):
    lab, fake, db = world
    fake.installed = []
    r = run(lab, "rename")["result"]
    assert r["final"] == "NOT RUN" and "ollama pull" in r["reason"] and r["mutated"] is False
    fake.installed, fake.up = [MODEL], False
    r = run(lab, "rename")["result"]
    assert r["final"] == "NOT RUN" and "ollama serve" in r["reason"]
    assert db.key_column() == "customer_id" and len(db.migrations()) == 1             # schema untouched


def test_failed_business_verification_is_never_verified_and_rolls_back(world, monkeypatch):
    lab, _, db = world
    real = lab.business_checks

    def wrong_after_drift(body):                    # simulate the API returning someone else after the change
        checks = real(body)
        if db.key_column() != "customer_id":
            checks[0] = {"name": "customer identity", "ok": False, "detail": "returned a different customer"}
        return checks
    monkeypatch.setattr(lab, "business_checks", wrong_after_drift)
    r = run(lab, "rename")["result"]
    assert r["final"] == "ROLLED BACK" and not r["final"].startswith("VERIFIED") and r["rolled_back"]
    assert lab.storage.current("customer_lookup").version == 1                       # mapping rolled back
    assert lab.storage.evidence(limit=1)[0]["final"]["verified_recovery"] is False


# ---------------------------------------------------------------- adapter: request shape and endpoint rules
def test_adapter_sends_a_bounded_structured_request_to_the_configured_local_endpoint():
    seen = []
    app = FastAPI()

    @app.post("/api/chat")
    async def chat(req: Request):
        seen.append(await req.json())
        return {"message": {"content": json.dumps(plan())}, "eval_count": 10, "prompt_eval_count": 50}
    import uvicorn
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.01)
    try:
        p = OllamaProvider(f"http://127.0.0.1:{sock.getsockname()[1]}", MODEL, timeout_s=5, max_tokens=321)
        r = p.plan({"unresolved_fields": [{"field": "body:client_id"}]})
    finally:
        server.should_exit = True
        th.join(5)
    assert r.ok and r.plan.mappings[0].source_arg == "customer_id"
    body = seen[0]                                   # a stand-in HTTP server on loopback received the real request
    assert body["model"] == MODEL and body["stream"] is False and body["options"] == {"temperature": 0, "num_predict": 321}
    assert body["format"]["properties"]["mappings"] and body["messages"][0]["role"] == "system"
    assert "DATA (untrusted)" in body["messages"][1]["content"]


def test_adapter_refuses_remote_endpoints_and_cloud_models():
    for url in ("https://api.example.com", "http://10.0.0.5:11434", "http://ollama.example.com:11434",
                "http://user:pw@127.0.0.1:11434"):
        with pytest.raises(LocalLLMError):
            OllamaProvider(url, MODEL)
    for m in ("gpt-oss:120b-cloud", "qwen3:cloud", "", "http://x/y"):
        with pytest.raises(LocalLLMError):
            OllamaProvider("http://127.0.0.1:11434", m)


def test_stopped_ollama_is_reported_without_fallback():
    p = OllamaProvider("http://127.0.0.1:9", MODEL, timeout_s=2)          # nothing listens on port 9
    r = p.plan({})
    assert not r.ok and r.error_category == "ollama_not_running" and r.plan is None
    assert p.readiness()["server"] == "unreachable" and "ollama.com/download" in p.readiness()["action"]


# ---------------------------------------------------------------- 10-11, 13 reset, confirmation, isolation
def test_reset_restores_the_baseline_after_confirmation(world):
    lab, _, db = world
    run(lab, "rename")
    with pytest.raises(LabError):
        lab.reset(False)
    assert db.key_column() == "client_id"                                         # unconfirmed: nothing happened
    out = lab.reset(True)
    assert out["reset"] and out["backup"] and out["registered"]
    assert db.key_column() == "customer_id" and db.lookup({"customer_id": 101}) == FIXTURE
    assert lab.storage.incidents() == [] and lab.storage.current("customer_lookup").version == 1
    assert lab.contracts()["diff"]["changed"] is False


def test_no_schema_change_without_explicit_confirmation(world):
    lab, _, db = world
    with pytest.raises(LabError):
        lab.start("rename", {}, False)
    assert db.key_column() == "customer_id" and len(db.migrations()) == 1


def test_second_run_without_reset_is_refused(world):
    lab, _, db = world
    run(lab, "rename")
    r = run(lab, "remove")["result"]
    assert r["final"] == "NOT RUN" and "Reset the demo" in r["reason"] and "email" in [c["name"] for c in db.columns()]


def test_local_demo_database_cannot_be_a_records_database(tmp_path):
    s = Settings(local_demo=True, local_demo_db=str(tmp_path / "x.db"), db_path=str(tmp_path / "x.db"))
    with pytest.raises(LabError):
        LocalLab(s, llm=OllamaProvider("http://127.0.0.1:11434", MODEL, transport=httpx.MockTransport(FakeOllama())))
    with pytest.raises(LabError):
        LocalLab(Settings(local_service_url="http://192.168.1.5:8766"))


def test_service_port_conflict_is_detected():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert port_free("127.0.0.1", s.getsockname()[1]) is False
    finally:
        s.close()


def test_api_routes_exist_only_when_enabled_and_require_confirmation(world, tmp_path):
    from adaptix.api import State, create_app
    lab, _, db = world
    token = "local-demo-token-0000000000000"
    off = State(Settings(admin_token=token, db_path=str(tmp_path / "o.db"), sim_db_path=str(tmp_path / "os.db"),
                         test_db_path=str(tmp_path / "ot.db")), start_monitor=False)
    c_off = TestClient(create_app(off), headers={"Authorization": f"Bearer {token}"})
    assert c_off.get("/api/local-demo/status").status_code == 404 and c_off.get("/api/local/tools").status_code == 404
    on = State(Settings(admin_token=token, db_path=str(tmp_path / "n.db"), sim_db_path=str(tmp_path / "ns.db"),
                        test_db_path=str(tmp_path / "nt.db")), start_monitor=False)
    on.local_lab = lab
    c = TestClient(create_app(on), headers={"Authorization": f"Bearer {token}"})
    assert c.post("/api/local-demo/run", json={"scenario": "rename"}).status_code == 422
    assert db.key_column() == "customer_id"
    assert c.get("/api/local-demo/status").json()["database"]["status"] == "ready"
    assert c.post("/api/local-demo/reset", json={}).status_code == 422


def test_local_model_output_schema_limits_evidence_to_verbatim_contract_quotes(world):
    lab, fake, _ = world
    run(lab, "rename")
    mapping = fake.calls[0]["format"]["$defs"]["Mapping"]
    assert "x-renamed-from: customer_id" in mapping["properties"]["evidence"]["enum"]
    assert "evidence" in mapping["required"]
    ctx = fake.calls[0]["messages"][1]["content"]
    assert '"x-renamed-from": "customer_id"' in ctx                 # the documented rename is visible to the model


# ---------------------------------------------------------------- database viewer and the patch story
def test_database_viewer_is_read_only_allowlisted_and_follows_the_live_schema(world):
    lab, _, db = world
    info = db.table_info("orders")
    assert info["foreign_keys"] == [{"column": "customer_id", "references": "customers.customer_id",
                                     "on delete": "RESTRICT"}]
    rows = db.browse("customers", 5, 0, customer=101)
    assert rows["total"] == 1 and rows["rows"][0]["full_name"] == "Asha Verma"
    assert db.browse("orders", 200, 0, customer=101)["total"] == db.orders_for(101)
    for bad in ("sqlite_master", "demo_meta", 'customers"; DROP TABLE orders; --'):
        with pytest.raises(DemoDatabaseError):
            db.browse(bad)
    run(lab, "rename")
    assert db.table_info("orders")["foreign_keys"][0]["references"] == "customers.client_id"   # really changed
    assert db.browse("customers", 5, 0, customer=101)["rows"][0]["client_id"] == 101
    assert db.integrity()["integrity_ok"]


def test_patch_story_shows_the_real_error_the_model_patch_and_the_fixed_request(world):
    lab, _, _ = world
    r = run(lab, "rename")["result"]
    p = lab.patch_story(r["incident"])
    assert p["error_before"]["status"] == 422 and p["error_before"]["sent"] == {"customer_id": 101}
    assert p["request_after"]["status"] == 200 and p["request_after"]["sent"] == {"client_id": 101}
    assert p["model_proposal"]["mappings"][0]["source_arg"] == "customer_id"
    assert p["validation"]["accepted"] and p["applied_version"]["version"] == 2
    assert p["applied_version"]["request_map"]["body:client_id"][0] == "customer_id"
    assert [v["status"] for v in p["versions"]] == ["superseded", "active"]


def test_the_patch_stays_in_effect_for_later_calls(world):
    lab, fake, _ = world
    run(lab, "rename")
    calls = len(fake.calls)
    again = lab.call_original()
    assert again["status"] == "ok" and again["tool_version"] == 2 and again["incident"] is None
    assert again["sent_as"] == {"client_id": 101} and all(c["ok"] for c in again["business_checks"])
    assert len(fake.calls) == calls                           # no new model request: the stored patch is reused
