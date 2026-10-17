"""Simulation Lab: reproducible, SIMULATED schema-drift scenarios.

Runs only on a simulation-mode engine (separate database, sim:// upstreams only, its own credential
registry with throwaway keys). A simulated mutation can therefore never reach a real provider.

Each scenario installs a simulated API (sim://<name>), registers a tool against its v1 schema, applies a
mutation (v2), then calls the tool - with self-healing (ADAPTIX) or without (baseline). Every incident it
creates is marked simulated=True with the scenario id. Nothing here is a real production incident.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .llm import is_live
from .mock_api import make_app
from .models import ToolSpec

DOC = lambda desc, old=None, t="str": {"type": t, "description": desc, **({"renamed_from": old} if old else {})}


@dataclass
class Scenario:
    id: str
    title: str
    group: str
    description: str
    v1: dict
    v2: dict
    args: dict
    expect: str                         # recovered | stopped | rolled_back
    expect_category: str
    method: str = "POST"
    path: str = "/v1/resource"
    v1_headers: list = field(default_factory=list)
    v2_headers: list = field(default_factory=list)
    v2_response: dict | None = None
    v2_faults: dict | None = None
    v2_method: str | None = None
    v2_content_type: str = "application/json"
    auth: str | None = None            # None | "ok" | "expired_primary" | "invalid" | "forbidden"
    needs_llm: bool = False

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "group": self.group, "description": self.description,
                "expect": self.expect, "expect_category": self.expect_category, "needs_llm": self.needs_llm,
                "v1": self.v1, "v2": self.v2, "args": self.args, "method": self.method,
                "v2_headers": self.v2_headers, "v2_response": self.v2_response, "v2_faults": self.v2_faults,
                "v2_method": self.v2_method, "auth": self.auth}


U = {"user_id": "str", "email": "str"}
UA = {"user_id": "7", "email": "asha@example.com"}

SCENARIOS: list[Scenario] = [
    Scenario("rename-param", "Renamed parameter", "Request drift", "user_id is renamed to userId.",
             U, {"userId": "str", "email": "str"}, UA, "recovered", "schema_drift"),
    Scenario("documented-nested-rename", "Documented rename into a nested object + new header", "Request drift",
             "user_id becomes target.id (an integer); the schema documents the rename and now requires an API key.",
             {"user_id": "str"}, {"target.id": DOC("ID of the user to update (formerly user_id)", "user_id", "int")},
             {"user_id": "123"}, "recovered", "schema_drift", v2_headers=["x-api-key"]),
    Scenario("safe-type-change", "Safe type change", "Request drift", 'quantity changes from string to integer ("3" -> 3).',
             {"sku": "str", "quantity": "str"}, {"sku": "str", "quantity": "int"}, {"sku": "S1", "quantity": "3"},
             "recovered", "schema_drift"),
    Scenario("unsafe-type-change", "Incompatible type change", "Request drift",
             'quantity becomes an integer but the value is "3.5" - converting would lose data.',
             {"sku": "str", "quantity": "str"}, {"sku": "str", "quantity": "int"}, {"sku": "S1", "quantity": "3.5"},
             "stopped", "schema_drift"),
    Scenario("nested-move", "Fields move into a nested object", "Request drift", "name and email move under customer.",
             {"name": "str", "email": "str"}, {"customer.name": "str", "customer.email": "str"},
             {"name": "Asha", "email": "asha@example.com"}, "recovered", "schema_drift"),
    Scenario("removed-field", "Removed field the caller still sends", "Request drift",
             "The API drops the optional 'nickname' field and silently ignores it, answering 200. The caller "
             "asked to set it, so the task was not done: verification must catch this and stop.",
             {**U, "nickname": "str?"}, U, {**UA, "nickname": "Ash"}, "stopped", "verification_failed"),
    Scenario("nested-response-move", "Response output moved into a nested object", "Response drift",
             "The API moves its 'status' output under 'result' (result.status). The call itself still works.",
             U, U, UA, "recovered", "schema_drift",
             v2_response={"status_field": "result.status", "status_value": "updated", "echo_field": "received"}),
    Scenario("response-rename", "Renamed response field", "Response drift",
             "The API renames its 'status' output to 'state' (documented). The call itself still works.",
             U, U, UA, "recovered", "schema_drift",
             v2_response={"status_field": "state", "status_value": "updated", "echo_field": "received",
                          "status_renamed_from": "status"}),
    Scenario("header-configured", "New required header (credential configured)", "Headers & auth",
             "The API starts requiring x-api-key; a credential group for it is configured.",
             U, U, UA, "recovered", "schema_drift", v2_headers=["x-api-key"]),
    Scenario("header-unconfigured", "New required header (no credential)", "Headers & auth",
             "The API starts requiring x-tenant-id; nothing is configured for it, so ADAPTIX must not guess.",
             U, U, UA, "stopped", "schema_drift", v2_headers=["x-tenant-id"]),
    Scenario("new-required-field", "New required field with no source", "Request drift",
             "A required 'reason' field is added; the caller never supplied one.",
             U, {**U, "reason": "str"}, UA, "stopped", "schema_drift"),
    Scenario("ambiguous-rename", "Ambiguous rename", "Request drift",
             "sender_id and receiver_id become 'from' and 'to' - which is which cannot be proven.",
             {"sender_id": "str", "receiver_id": "str"}, {"from": "str", "to": "str"},
             {"sender_id": "u1", "receiver_id": "u2"}, "stopped", "schema_drift"),
    Scenario("unit-change", "Hidden unit change", "Request drift",
             "amount (rupees) becomes amount_cents; moving the value unchanged would be wrong.",
             {"payment_id": "str", "amount": "str"}, {"payment_id": "str", "amount_cents": "int"},
             {"payment_id": "p1", "amount": "20"}, "stopped", "schema_drift"),
    Scenario("invalid-input", "Invalid user input (no drift)", "Not schema drift",
             "The schema is unchanged; the caller sends a role the API does not allow.",
             {"user_id": "str", "role": "enum[admin|member]"}, {"user_id": "str", "role": "enum[admin|member]"},
             {"user_id": "7", "role": "guest"}, "stopped", "invalid_input"),
    Scenario("business-rule", "Business-rule rejection", "Not schema drift",
             "The request is valid but the API refuses it (insufficient balance).",
             {"payment_id": "str", "amount": "float"}, {"payment_id": "str", "amount": "float"},
             {"payment_id": "p1", "amount": 10.0}, "stopped", "business_rule",
             v2_faults={"business_rule": "insufficient_balance"}),
    Scenario("credential-failover", "Expired key with authorized failover", "Headers & auth",
             "The primary simulated key expires; a second key in the same group is configured for failover.",
             U, U, UA, "recovered", "credential_expired", auth="expired_primary"),
    Scenario("credential-invalid", "Invalid key", "Headers & auth",
             "Every configured simulated key is rejected as invalid; ADAPTIX must not alter keys or 'repair' auth.",
             U, U, UA, "stopped", "credential_invalid", auth="invalid"),
    Scenario("rate-limit", "Rate limit with Retry-After", "Transient",
             "The API answers 429 with Retry-After once, then succeeds.",
             U, U, UA, "recovered", "rate_limited", v2_faults={"fail_times": 1, "retry_after": 0.2}),
    Scenario("server-error", "Upstream outage (503)", "Transient",
             "The API fails with 503 on every call. Bounded retries happen, then ADAPTIX stops: an outage is not "
             "schema drift, so no repair is generated.",
             U, U, UA, "stopped", "server_error", v2_faults={"status": 503}),
    Scenario("verification-rollback", "Repair accepted but task not applied", "Verification",
             "After a rename, the API returns 200 but reports 'no_change'; the repair must be rolled back.",
             U, {"userId": "str", "email": "str"}, UA, "rolled_back", "schema_drift",
             v2_faults={"status_value": "no_change"}),
    Scenario("method-change", "HTTP method changed", "Not repairable",
             "The endpoint moves from POST to PUT; changing a method can alter side effects.",
             U, U, UA, "stopped", "method_changed", v2_method="PUT"),
    Scenario("window-rename-llm", "Prose-documented rename (needs the LLM tier)", "LLM-assisted",
             "start_date/end_date become window_start/window_end, documented only in prose. Rules cannot prove "
             "the mapping; an LLM can, if it quotes the schema text. Without an LLM it stops safely.",
             {"start_date": "str", "end_date": "str"},
             {"window_start": DOC("First day of the booking window (inclusive)"),
              "window_end": DOC("Last day of the booking window (inclusive)")},
             {"start_date": "2026-01-01", "end_date": "2026-01-31"}, "recovered", "schema_drift", needs_llm=True),
]
BY_ID = {s.id: s for s in SCENARIOS}


def build_app(s: Scenario, phase: str, keys: dict | None = None):
    """The scenario's API at phase v1 (registration) or v2 (after the change). `keys` = {"a", "b"} test keys."""
    if s.auth:
        a, b = (keys or {}).get("a"), (keys or {}).get("b")
        if phase == "v1":
            auth = {"header": "x-api-key", "valid": [a, b]}
        elif s.auth == "expired_primary":
            auth = {"header": "x-api-key", "valid": [b], "expired": [a]}
        else:
            auth = {"header": "x-api-key", "valid": []}
        return make_app(s.path, s.v1 if phase == "v1" else s.v2, title=s.title, auth=auth)
    if phase == "v1":
        return make_app(s.path, s.v1, s.v1_headers, title=s.title, method=s.method)
    return make_app(s.path, s.v2, s.v2_headers, title=s.title, method=s.v2_method or s.method,
                    content_type=s.v2_content_type, response=s.v2_response, faults=dict(s.v2_faults or {}))


class SimTarget:
    """In-process simulated APIs (sim://). Environment SIMULATION."""
    environment = "SIMULATION"

    def __init__(self, engine):
        self.engine = engine

    def base_url(self, name: str) -> str:
        return f"sim://{name}"

    def install(self, name: str, s: Scenario, phase: str, keys: dict | None = None):
        self.engine.upstreams.set_simulated(name, build_app(s, phase, keys))

    def stats(self, name: str) -> dict:
        app = self.engine.upstreams.sim_app(name)
        return {"calls": app.state.calls, "effects": app.state.effects}


class ServiceTarget:
    """The controlled test service: a real HTTP server (REAL_API_TEST). Versions are switched over HTTP."""
    environment = "REAL_API_TEST"

    def __init__(self, service):
        self.service = service

    def base_url(self, name: str) -> str:
        return f"{self.service.url}/m/{name}"

    def install(self, name: str, s: Scenario, phase: str, keys: dict | None = None):
        self.service.install(name, s.id, phase, keys)

    def stats(self, name: str) -> dict:
        d = self.service.stats(name)
        return {"calls": d["calls"], "effects": d["effects"]}


class SimulationError(RuntimeError):
    pass


def require_simulation(engine):
    if getattr(engine, "mode", None) != "simulation":
        raise SimulationError("simulations run only on the simulation engine, never against production")


def _target_for(engine, target):
    if target is None:
        require_simulation(engine)
        return SimTarget(engine)
    if isinstance(target, ServiceTarget) and engine.mode != "real_api_test":
        raise SimulationError("the controlled test service is used only by the REAL_API_TEST engine")
    if isinstance(target, SimTarget):
        require_simulation(engine)
    return target


def _prepare(engine, s: Scenario, run_id: str, mode: str, target, heal: bool = True) -> tuple[str, str | None]:
    """Install v1, register the tool against it, then switch the API to v2. Returns (tool name, key group)."""
    name = f"{s.id}-{mode}-{run_id}"
    prefix = "sim" if target.environment == "SIMULATION" else "test"
    group, keys = None, None
    if s.auth or "x-api-key" in s.v2_headers + s.v1_headers:
        group = f"{prefix}-{s.id}"
        made = engine.creds.add_simulated_group(group, "x-api-key", [f"{group}-a", f"{group}-b"])
        keys = {"a": made[f"{group}-a"], "b": made[f"{group}-b"]}
    target.install(name, s, "v1", keys if s.auth else None)
    engine.register(ToolSpec(name=name, base_url=target.base_url(name), path=s.path, method=s.method,
                             description=s.description, auth_header="x-api-key" if group else None,
                             credential_group=group, simulated=engine.mode == "simulation"))
    if s.auth:
        for c in engine.creds._creds:                     # fresh state for this run's test keys
            if c.group == group:
                c.state, c.cooldown_until = "unverified", 0.0
                c.failover_on = ["rate_limited", "credential_expired"] if heal else []   # baseline: no failover
    target.install(name, s, "v2", keys if s.auth else None)
    return name, group


def install(engine, s: Scenario, run_id: str, mode: str, target=None) -> str:
    """Prepare a scenario without calling it (tests use this to drive the tool themselves)."""
    return _prepare(engine, s, run_id, mode, _target_for(engine, target))[0]


def run(engine, scenario_id: str, seed: int = 7, heal: bool = True, target=None, evidence: bool = True,
        evidence_run: str | None = None) -> dict:
    """Run one scenario. Deterministic for a given seed (ids are derived from it)."""
    target = _target_for(engine, target)
    s = BY_ID[scenario_id]
    rnd = random.Random(f"{scenario_id}:{seed}:{heal}:{engine.storage.environment}")
    run_id = f"{seed}-{rnd.randrange(16 ** 6):06x}"
    if isinstance(target, ServiceTarget):            # real server state persists: unique per execution
        run_id += "-" + __import__("uuid").uuid4().hex[:4]
    mode = "adaptix" if heal else "baseline"
    name, group = _prepare(engine, s, run_id, mode, target, heal)
    res = engine.execute(name, dict(s.args), scenario=s.id, heal=heal)
    if s.auth:
        for c in engine.creds._creds:
            if c.group == group:
                c.failover_on = ["rate_limited", "credential_expired"]
    out = _summarize(s, mode, res, target.stats(name), expected_outcome(s, engine, heal), engine,
                     target.environment)
    out["tool"] = name
    if evidence:
        from . import evidence as evidence_mod
        inc = engine.storage.incident(res["incident"]) if res.get("incident") else None
        ev = evidence_mod.build(engine, inc.model_dump(mode="json") if inc else None,
                                run_id=evidence_run or f"{target.environment.lower()}-{run_id}", scenario=s.id,
                                tool=name, expected=out["expected"], expected_category=s.expect_category,
                                result=res, extra={"mode": mode})
        out["evidence_id"] = ev["evidence_id"]
    return out


def expected_outcome(s: Scenario, engine, heal: bool = True) -> str:
    """What a correct run should end with. Baseline (no self-healing) only succeeds when nothing changed."""
    if not heal:
        return "stopped"
    if s.needs_llm and not is_live(engine.llm):
        return "stopped"                    # without a real model the safe outcome is to stop
    return s.expect


def llm_label(llm_meta: dict | None) -> dict:
    """How the LLM was involved in a run, labelled so mock output is never mistaken for real inference."""
    if not llm_meta:
        return {"used": False, "label": "not used"}
    real = bool(llm_meta.get("real"))
    prov = str(llm_meta.get("provider"))
    if not real:
        kind = "MOCK (not a real model)" if "mock" in prov else "not a real request (test double)"
    elif prov == "ollama":
        kind = "LOCAL LLM request (Ollama, on this machine)"
    elif prov == "gemini":
        kind = "REAL Gemini request"
    else:
        kind = "real provider request"
    return {"used": True, "label": kind, "real": real, "provider": llm_meta.get("provider"),
            "model": llm_meta.get("model"), "ok": llm_meta.get("ok"), "error_category": llm_meta.get("error_category"),
            "prompt_tokens": llm_meta.get("prompt_tokens"), "output_tokens": llm_meta.get("output_tokens")}


def _summarize(s: Scenario, mode: str, res: dict, stats: dict | None, expected: str, engine=None,
               environment: str = "SIMULATION") -> dict:
    outcome = "recovered" if res["status"] in ("ok", "recovered") else res["status"]
    inc = engine.storage.incident(res["incident"]) if engine is not None and res.get("incident") else None
    stats = stats or {}
    return {"scenario": s.id, "title": s.title, "mode": mode, "simulated": environment == "SIMULATION",
            "environment": environment, "expected": expected, "expected_category": s.expect_category,
            "llm": llm_label(inc.llm if inc else None), "llm_planner": getattr(engine.llm, "name", "none") if engine else None,
            "outcome": outcome, "matches_expectation": outcome == expected, "category": res.get("category"),
            "incident": res.get("incident"), "reason": res.get("reason"), "duration_ms": res.get("duration_ms"),
            "tool_version": res.get("tool_version"), "upstream_calls": stats.get("calls"),
            "side_effects": stats.get("effects"), "repair_by": repair_by(inc)}


def repair_by(inc) -> str | None:
    """Who proposed the patch that was applied, from the stored incident (None when nothing was applied)."""
    if inc is None or not inc.plan or not inc.to_version or inc.rolled_back:
        return None
    src = inc.plan.get("source")
    if src == "llm":
        lab = llm_label(inc.llm)
        return f"{lab['label']} ({lab['model']})" if lab["used"] else "LLM (no successful request)"
    return "previously validated repair (reused)" if src == "reuse" else "rules planner"
