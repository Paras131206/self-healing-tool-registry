"""Multi-Change Lab: several schema changes in one confirmed experiment, then automatic recovery.

Service target = ADAPTIX's controlled test service over real HTTP (the deployed site's target).
Local target = the LOCAL DEMO SQLite database + customer API. Model providers here are TEST DOUBLES (a fake SDK
client / an httpx transport); they are never labelled real. Live model runs are in the *_live test files."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import adaptix.api as api_mod
import adaptix.multichange as mc_mod
from adaptix.api import State, create_app
from adaptix.config import Settings
from adaptix.llm.gemini import GeminiProvider
from adaptix.llm.ollama import OllamaProvider
from adaptix.localdemo.database import DemoDatabase
from adaptix.localdemo.lab import LocalLab
from adaptix.localdemo.service import ServiceThread
from adaptix.multichange import GUIDED, LOCAL_GUIDED, ChangeError, build_spec_app
from tests.test_gemini import FakeClient, creds

TOKEN = "multichange-test-token-000000000"
KEY = "AIzaFAKE-primary-key-000001"


@pytest.fixture
def world(tmp_path):
    st = State(Settings(admin_token=TOKEN, db_path=str(tmp_path / "p.db"), sim_db_path=str(tmp_path / "s.db"),
                        test_db_path=str(tmp_path / "t.db")), start_monitor=False)
    c = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    yield c, st
    if st.test_service:
        st.test_service.stop()


def run(c, changes, engine="none", target="service", session="sessaaa111", partial=True):
    r = c.post("/api/multichange/run", json={"target": target, "changes": changes, "engine": engine,
                                              "confirm": True, "session": session, "partial": partial})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    for _ in range(400):
        j = c.get(f"/api/multichange/jobs/{jid}", params={"session": session}).json()
        if j["status"] != "running":
            return j
        time.sleep(0.025)
    raise AssertionError("experiment did not finish")


def outcome(j):
    return {ch["label"]: ch["status"] for ch in j["result"]["changes"]}


# ---------------------------------------------------------------- combined changes on the controlled test API
def test_multiple_simultaneous_renames_are_repaired_and_verified(world):
    c, st = world
    j = run(c, GUIDED["two_renames"]["changes"])
    r = j["result"]
    assert r["final"] == "VERIFIED RECOVERY" and r["verified"]
    assert set(outcome(j).values()) == {"repaired"}
    assert r["retry"]["sent"]["client_id"] == 101 and r["retry"]["sent"]["contact_email"] == "asha.verma@example.com"
    assert r["failure"]["status"] == 422 and r["retry"]["status"] == 200
    assert [v["status"] for v in r["versions"]] == ["superseded", "active"]
    phases = [e["phase"] for e in j["events"]]
    for p in ("Selected changes", "Baseline", "Changes applied", "Schema retrieval", "Drift detection",
              "Repair proposal", "Independent validation", "Patch applied", "Business verification", "Outcome"):
        assert p in phases, p


def test_rename_plus_type_change_uses_a_lossless_conversion(world):
    c, _ = world
    r = run(c, GUIDED["rename_retype"]["changes"])["result"]
    assert r["final"] == "VERIFIED RECOVERY"
    assert r["retry"]["sent"]["client_id"] == "101"                       # int -> str, lossless
    assert any(m["transform"] == "to_str" for m in r["plan"]["mappings"])


def test_rename_plus_required_field_is_partially_repairable_but_nothing_is_applied(world):
    c, st = world
    j = run(c, GUIDED["rename_required"]["changes"], partial=False)
    r = j["result"]
    assert r["final"] == "PARTIAL - SAFELY REFUSED" and not r["verified"] and r["first_attempt"] is None
    o = outcome(j)
    assert o["rename customer_id -> client_id"] == "proposed, not applied"
    assert o["add required region_code (string)"] == "refused"
    assert st.test_storage.current(j["result"]["tool"]).version == 1     # no patch was applied


def test_removing_an_unused_field_plus_a_rename_recovers(world):
    c, _ = world
    j = run(c, GUIDED["remove_optional_rename"]["changes"])
    assert j["result"]["final"] == "VERIFIED RECOVERY"
    assert outcome(j) == {"remove notes": "compatible", "rename customer_id -> client_id": "repaired"}


def test_nested_rename_plus_enum_change_is_safely_refused(world):
    c, _ = world
    j = run(c, GUIDED["nested_enum"]["changes"], partial=False)
    assert j["result"]["final"] == "PARTIAL - SAFELY REFUSED"
    assert outcome(j)["tier: allowed values -> standard, GOLD, platinum"] == "refused"


@pytest.mark.parametrize("changes,problem", [
    ([], "at least one"),
    ([{"kind": "drop_table", "field": "email"}], "unknown change kind"),
    ([{"kind": "rename", "field": "email", "new": "Bad-Name"}], "not a valid new name"),
    ([{"kind": "rename", "field": "email; DROP TABLE x", "new": "mail"}], "not a valid field name"),
    ([{"kind": "remove", "field": "email"}, {"kind": "rename", "field": "email", "new": "mail"}], "contradictory"),
    ([{"kind": "rename", "field": "email", "new": "tier"}], "two fields would be called tier"),
    ([{"kind": "rename", "field": "email", "new": "a1"}, {"kind": "rename", "field": "email", "new": "a2"}],
     "more than one"),
    ([{"kind": "retype", "field": "tier", "to": "str"}], "is an enum"),
    ([{"kind": "retype", "field": "email", "to": "str"}], "already string"),
    ([{"kind": "enum", "field": "email", "values": "a|b"}], "not an enum"),
    ([{"kind": "add_optional", "field": "email", "to": "str"}], "already exists"),
    ([{"kind": "remove", "field": "nope"}], "not a field"),
    ([{"kind": "remove", "field": "notes"}] * 2, "selected twice"),
    ([{"kind": "add_optional", "field": f"f{i}x", "to": "str"} for i in range(7)], "at most 6"),
    ([{"kind": "rename", "field": "email", "new": "mail", "url": "https://evil.example"}], "each change is"),
])
def test_invalid_or_conflicting_change_sets_are_rejected_before_anything_changes(world, changes, problem):
    c, st = world
    pv = c.post("/api/multichange/preview", json={"target": "service", "changes": changes}).json()
    assert pv["valid"] is False and problem in pv["problems"][0], pv
    r = c.post("/api/multichange/run", json={"target": "service", "changes": changes, "engine": "none",
                                              "confirm": True, "session": "sessaaa111"})
    assert r.status_code == 409
    assert st.test_storage.tools() == []                                   # nothing registered or mutated


def test_no_experiment_without_confirmation(world):
    c, st = world
    r = c.post("/api/multichange/run", json={"target": "service", "changes": GUIDED["two_renames"]["changes"],
                                              "engine": "none", "confirm": False, "session": "sessaaa111"})
    assert r.status_code == 422 and st.test_storage.tools() == []


# ---------------------------------------------------------------- the target is fixed: no arbitrary hosts
def test_cannot_target_arbitrary_hosts_or_paths(world):
    c, st = world
    for extra in ({"url": "https://api.example.com"}, {"host": "10.0.0.1"}, {"base_url": "http://169.254.169.254"}):
        r = c.post("/api/multichange/run", json={"target": "service", "changes": GUIDED["two_renames"]["changes"],
                                                  "engine": "none", "confirm": True, "session": "sessaaa111", **extra})
        assert r.status_code == 422                                         # unknown fields are refused
    assert c.post("/api/multichange/run", json={"target": "production", "changes": [], "engine": "none",
                                                 "confirm": True, "session": "sessaaa111"}).status_code == 422
    with pytest.raises(ChangeError):
        build_spec_app({"path": "/admin/evil", "fields": {"a": {"type": "str"}}})
    with pytest.raises(ChangeError):
        build_spec_app({"path": mc_mod.SERVICE_PATH, "fields": {"a": {"type": "__import__('os')"}}})
    with pytest.raises(ChangeError):
        build_spec_app({"path": mc_mod.SERVICE_PATH, "fields": {"../x": {"type": "str"}}})
    j = run(c, GUIDED["two_renames"]["changes"])
    spec = st.test_storage.tool(j["result"]["tool"])
    assert spec.base_url.startswith("http://127.0.0.1:") and "/m/mc-" in spec.base_url   # only the test service
    assert st.storage.tools() == []                                                       # production untouched
    assert c.post("/api/multichange/preview", json={"target": "local", "changes": []}).status_code == 404


# ---------------------------------------------------------------- sessions are isolated; reset is scoped
def test_sessions_are_isolated_and_reset_removes_only_that_experiment(world):
    c, st = world
    a = run(c, GUIDED["two_renames"]["changes"], session="sessionaaa")
    b = run(c, GUIDED["remove_optional_rename"]["changes"], session="sessionbbb")
    assert c.get(f"/api/multichange/jobs/{a['id']}", params={"session": "sessionbbb"}).status_code == 404
    r = c.post("/api/multichange/reset", json={"target": "service", "job_id": a["id"], "session": "sessionbbb",
                                                "confirm": True})
    assert r.status_code == 409                                            # B cannot reset A's experiment
    assert c.post("/api/multichange/reset", json={"target": "service", "job_id": a["id"],
                                                   "session": "sessionaaa"}).status_code == 409   # needs confirm
    out = c.post("/api/multichange/reset", json={"target": "service", "job_id": a["id"], "session": "sessionaaa",
                                                  "confirm": True}).json()
    assert out["reset"] and a["result"]["tool"] in out["scope"]
    tools = {t["spec"].name for t in st.test_storage.tools()}
    assert a["result"]["tool"] not in tools and b["result"]["tool"] in tools
    assert a["result"]["tool"] not in st.test_service._matrix and b["result"]["tool"] in st.test_service._matrix


# ---------------------------------------------------------------- the real provider path (test double)
@pytest.fixture
def gemini_world(tmp_path, monkeypatch):
    script, calls = [], []
    gem = GeminiProvider(creds(False), "gemini-3.5-flash-lite",
                         client_factory=lambda k: FakeClient(script, k, calls), sleep=lambda s: None)
    monkeypatch.setattr(mc_mod, "is_live", lambda p: p is gem)
    monkeypatch.setattr(api_mod, "is_live", lambda p: p is gem)
    st = State(Settings(admin_token=TOKEN, db_path=str(tmp_path / "p.db"), sim_db_path=str(tmp_path / "s.db"),
                        test_db_path=str(tmp_path / "t.db")), creds=creds(False), llm=gem, start_monitor=False)
    c = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    yield c, st, script, calls
    if st.test_service:
        st.test_service.stop()


def gem_plan():
    ev = "x-renamed-from: customer_id"
    return json.dumps({"diagnosis": "customer_id was renamed to client_id", "evidence": [ev],
                       "affected_fields": ["body:client_id"],
                       "mappings": [{"target_field": "body:client_id", "source_arg": "customer_id", "transform": "none",
                                     "evidence": ev}],
                       "response_mappings": [], "compatibility_risks": [], "confidence": 0.97,
                       "verification_steps": []})


def test_selected_gemini_proposes_the_repair_and_its_work_is_shown_without_secrets(gemini_world):
    c, st, script, calls = gemini_world
    script.append(gem_plan())
    j = run(c, [{"kind": "rename", "field": "customer_id", "new": "client_id"}], engine="gemini")
    r = j["result"]
    assert len(calls) == 1 and r["final"] == "VERIFIED RECOVERY"
    assert r["llm"]["provider"] == "gemini" and r["llm"]["real"] is False          # a test double is never "real"
    assert r["llm_step"]["diagnosis"] == "customer_id was renamed to client_id"
    ctx = r["llm_step"]["context_sent"]
    assert ctx["unresolved_fields"][0]["field"] == "body:client_id"
    assert "101" not in json.dumps(ctx) and "asha" not in json.dumps(ctx)           # names and types only, no values
    blob = json.dumps(j) + c.get("/api/test/audit?limit=500").text + c.get("/api/test/evidence").text
    assert KEY not in blob and TOKEN not in blob


@pytest.mark.parametrize("reply,category", [("{not json", "malformed_output"),
                                            (RuntimeError("Service Unavailable"), "unknown")])
def test_gemini_failure_is_reported_and_nothing_is_applied(gemini_world, reply, category):
    c, st, script, calls = gemini_world
    script.extend([reply] * 6)
    j = run(c, [{"kind": "rename", "field": "customer_id", "new": "client_id"}], engine="gemini")
    r = j["result"]
    assert r["final"] == "SAFELY REFUSED" and not r["verified"]
    assert r["llm"]["ok"] is False and r["llm"]["error_category"]
    assert st.test_storage.current(r["tool"]).version == 1


def test_gemini_not_configured_starts_nothing(world):
    c, st = world
    r = c.post("/api/multichange/run", json={"target": "service", "changes": GUIDED["two_renames"]["changes"],
                                              "engine": "gemini", "confirm": True, "session": "sessaaa111"})
    assert r.status_code == 409 and "GEMINI_API_KEY" in r.json()["detail"]["detail"]
    assert st.test_storage.tools() == []
    assert [e["key"] for e in c.get("/api/multichange/catalog").json()["engines"]["service"]] == ["none"]


# ---------------------------------------------------------------- local demo target
class FakeOllama:
    def __init__(self):
        self.calls = []

    def __call__(self, req):
        if req.url.path == "/api/version":
            return httpx.Response(200, json={"version": "test-double"})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b-instruct", "size": 1}]})
        body = json.loads(req.content)
        self.calls.append(body)
        return httpx.Response(200, json={"message": {"content": gem_plan()}, "eval_count": 9, "prompt_eval_count": 90})


@pytest.fixture
def local_world(tmp_path):
    db = DemoDatabase(tmp_path / "ld.db")
    db.init()
    svc = ServiceThread(str(db.path)).start()
    fake = FakeOllama()
    s = Settings(admin_token=TOKEN, db_path=str(tmp_path / "p.db"), sim_db_path=str(tmp_path / "s.db"),
                 test_db_path=str(tmp_path / "t.db"), local_demo=True, local_demo_db=str(db.path),
                 local_db_path=str(tmp_path / "l.db"), local_service_url=svc.url)
    st = State(s, start_monitor=False)
    st.local_lab = LocalLab(s, llm=OllamaProvider("http://127.0.0.1:11434", "qwen2.5:3b-instruct",
                                                   transport=httpx.MockTransport(fake)))
    c = TestClient(create_app(st), headers={"Authorization": f"Bearer {TOKEN}"})
    yield c, st, db, fake
    svc.stop()
    if st.test_service:
        st.test_service.stop()


def test_local_target_uses_the_local_service_and_local_model_only(local_world):
    c, st, db, fake = local_world
    cat = c.get("/api/multichange/catalog").json()
    assert [e["key"] for e in cat["engines"]["local"]] == ["none", "local"]          # no cloud model offered
    r = c.post("/api/multichange/run", json={"target": "local", "changes": LOCAL_GUIDED["key_and_city"]["changes"],
                                              "engine": "gemini", "confirm": True, "session": "sessloc111"})
    assert r.status_code == 409 and "never a cloud model" in r.json()["detail"]["detail"]
    j = run(c, LOCAL_GUIDED["key_and_city"]["changes"], engine="local", target="local", session="sessloc111")
    rr = j["result"]
    assert rr["final"] == "VERIFIED RECOVERY" and len(fake.calls) == 1
    assert rr["llm"]["provider"] == "ollama" and st.local_lab.storage.tool("customer_lookup").base_url == \
        st.local_lab.service_url                                                    # the intended local service
    assert {"client_id", "billing_city"} <= {col["name"] for col in db.columns()}  # real migrations


def test_local_multi_change_with_type_widening_and_optional_column(local_world):
    c, _, db, _ = local_world
    j = run(c, LOCAL_GUIDED["rename_retype_optional"]["changes"], target="local", session="sessloc111")
    assert j["result"]["final"] == "VERIFIED RECOVERY"
    assert outcome(j)["add optional nickname (string)"] == "compatible"
    assert next(col for col in db.columns() if col["name"] == "loyalty_points")["type"] == "REAL"


def test_failed_business_verification_rolls_the_repair_back(local_world):
    c, st, db, _ = local_world
    j = run(c, LOCAL_GUIDED["remove_email_rename"]["changes"], target="local", session="sessloc111", partial=False)
    r = j["result"]
    assert r["final"] == "FAILED (rolled back)" and not r["verified"] and r["rolled_back"]
    o = outcome(j)
    assert o["remove email"] == "refused" and o["rename customer_id -> client_id"] == "applied, then rolled back"
    assert st.local_lab.storage.current("customer_lookup").version == 1             # rollback restored v1


def test_local_reset_restores_the_baseline_and_needs_a_clean_start(local_world):
    c, st, db, _ = local_world
    j = run(c, LOCAL_GUIDED["key_and_city"]["changes"], target="local", session="sessloc111")
    same = c.post("/api/multichange/run", json={"target": "local", "changes": LOCAL_GUIDED["key_and_city"]["changes"],
                                                 "engine": "none", "confirm": True, "session": "sessloc111"})
    assert same.status_code == 409 and "not a field of the current schema" in same.json()["detail"]["detail"]
    again = run(c, [{"kind": "remove", "field": "created_on"}], target="local", session="sessloc111")
    assert again["result"]["final"] == "NOT RUN" and "reset it first" in again["result"]["reason"]
    assert "created_on" in [col["name"] for col in db.columns()]                     # refused before any change
    out = c.post("/api/multichange/reset", json={"target": "local", "job_id": j["id"], "session": "sessloc111",
                                                  "confirm": True}).json()
    assert out["reset"] and db.key_column() == "customer_id" and "city" in [col["name"] for col in db.columns()]


def test_local_changes_outside_the_allowlist_are_refused(local_world):
    c, _, db, _ = local_world
    for bad in ([{"kind": "rename", "field": "full_name", "new": "name"}], [{"kind": "remove", "field": "tier"}],
                [{"kind": "enum", "field": "tier", "values": "a|b"}],
                [{"kind": "retype", "field": "email", "to": "int"}]):
        pv = c.post("/api/multichange/preview", json={"target": "local", "changes": bad}).json()
        assert pv["valid"] is False, bad
    assert db.migrations()[-1]["kind"] == "seed"                                     # nothing touched


# ---------------------------------------------------------------- partial recovery: safe fixes kept, unsafe reverted
def test_partial_recovery_repairs_the_safe_change_and_reverts_the_unsafe_one(world):
    c, st = world
    j = run(c, GUIDED["rename_required"]["changes"])
    r = j["result"]
    assert r["final"] == "PARTIAL RECOVERY" and r["verified"]
    assert r["first_attempt"]["final"] == "PARTIAL - SAFELY REFUSED"               # the honest first result is kept
    assert outcome(j) == {"rename customer_id -> client_id": "repaired", "add required region_code (string)": "reverted"}
    assert r["reverted"] == ["add required region_code (string)"]
    assert "region_code" not in [f["field"] for f in r["after"]] and "client_id" in [f["field"] for f in r["after"]]
    assert r["retry"]["sent"]["client_id"] == 101 and "region_code" not in r["retry"]["sent"]   # nothing invented
    assert "Unsafe changes reverted" in [e["phase"] for e in j["events"]]
    assert st.test_storage.current(r["tool"]).request_map["body:client_id"][0] == "customer_id"


def test_partial_recovery_with_an_enum_change(world):
    c, _ = world
    j = run(c, GUIDED["nested_enum"]["changes"])
    assert j["result"]["final"] == "PARTIAL RECOVERY"
    assert outcome(j)["rename address.city -> town"] == "repaired"
    assert j["result"]["retry"]["sent"]["tier"] == "gold"                            # the old value, never rewritten


def test_partial_recovery_is_not_attempted_when_nothing_is_safe(world):
    c, st = world
    j = run(c, [{"kind": "add_required", "field": "region_code", "to": "str"},
                {"kind": "enum", "field": "tier", "values": "standard|GOLD|platinum"}])
    assert j["result"]["final"] == "SAFELY REFUSED" and j["result"]["first_attempt"] is None
    assert st.test_storage.current(j["result"]["tool"]).version == 1


def test_local_partial_recovery_restores_the_removed_column_and_keeps_the_rename(local_world):
    c, st, db, fake = local_world
    j = run(c, LOCAL_GUIDED["remove_email_rename"]["changes"], engine="local", target="local", session="sessloc111")
    r = j["result"]
    assert r["final"] == "PARTIAL RECOVERY" and r["first_attempt"]["final"] == "FAILED (rolled back)"
    assert outcome(j) == {"remove email": "reverted", "rename customer_id -> client_id": "repaired"}
    cols = [col["name"] for col in db.columns()]
    assert "email" in cols and "client_id" in cols and "customer_id" not in cols    # real database state
    assert db.lookup({"client_id": 101})["email"] == "asha.verma@example.com"      # data restored from the backup
    assert {c["name"]: c["ok"] for c in r["business_checks"]}["required field: email"]
    assert len(fake.calls) == 2                                                      # the local model, both attempts
