"""Local Demo Lab: a manual, confirmed schema change on the LOCAL demo database -> ADAPTIX recovers.

One run (a background job, polled for live progress):
  1 readiness      database marker + integrity, local customer API health, local model installed
  2 baseline       the ORIGINAL business request (lookup customer 101) through the real ADAPTIX engine; the
                   customer's identity is asserted, not just the HTTP status                [LOCAL REAL EXECUTION]
  3 backup         SQLite online backup of the demo database
  4 mutation       the selected migration runs on the demo database (e.g. RENAME COLUMN customer_id TO client_id);
                   the API contract changes because it is generated from the live schema    [LOCAL REAL EXECUTION]
  5 recovery       the SAME request again; ADAPTIX's pipeline runs automatically with the local model required to
                   propose the repair: detect -> diagnose -> retrieve schema -> diff -> PROPOSED -> VALIDATED
                   (independent validator) -> APPLIED (new tool version) -> retry -> VERIFIED (business assertions)
                   or safely blocked / rolled back
Nothing here executes SQL or code produced by the model: the model only proposes field mappings, which the existing
validator checks against the retrieved schema before the existing versioned patch mechanism applies them.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import evidence as evidence_mod
from ..credentials import CredentialRegistry
from ..demo import Jobs, _event, public
from ..engine import Engine
from ..llm.ollama import LOOPBACK, OllamaProvider
from ..models import SchemaSource, ToolSpec, now_iso
from ..netguard import GuardedHTTP
from ..security import redact, scrub
from ..storage import Storage
from ..upstreams import Upstreams
from .database import FIXTURE, DemoDatabase, DemoDatabaseError, new_name_ok
from .service import CONTRACT, PATH

TOOL = "customer_lookup"
ARGS = {"customer_id": FIXTURE["customer_id"]}          # the caller's original business request (never changes)
PHASE = {"request": "Request", "classify": "Diagnosis", "discover": "Schema retrieval", "diff": "Drift detection",
         "plan": "Model proposal", "validate": "Independent validation", "apply": "Patch application",
         "retry": "Retry", "verify": "Business verification", "rollback": "Rollback", "outcome": "Outcome",
         "credential": "Credentials"}
SCENARIOS = {
    "rename": {"title": "Rename a column", "default": {"old": "customer_id", "new": "client_id"},
               "expected": "VERIFIED",
               "why": "A documented rename: the caller's value still exists under a new name, so a mapping can fix it."},
    "type": {"title": "Change a compatible field type", "default": {"column": "loyalty_points"},
             "expected": "VERIFIED (no repair needed)",
             "why": "INTEGER -> REAL is a lossless widening; the original request keeps working."},
    "required": {"title": "Add a required field", "default": {"column": "region_code"}, "expected": "SAFELY BLOCKED",
                 "why": "The caller never supplied a region, and ADAPTIX never invents business values."},
    "remove": {"title": "Remove a field", "default": {"column": "email"}, "expected": "SAFELY BLOCKED",
               "why": "Deleted data cannot be recovered by a mapping; the business check requires the email."},
}

# the same expectations in the pipeline's own outcome vocabulary (used by the shared scorecard)
ENGINE_EXPECTED = {"VERIFIED": "recovered", "VERIFIED (no repair needed)": "succeeded", "SAFELY BLOCKED": "stopped"}


class LabError(RuntimeError):
    pass


def _loopback_address(url: str) -> tuple[str, int]:
    p = urlsplit(url)
    if p.scheme != "http" or (p.hostname or "") not in LOOPBACK or not p.port:
        raise LabError("ADAPTIX_LOCAL_SERVICE_URL must be http://127.0.0.1:<port> (loopback only)")
    return p.hostname, p.port


class LocalLab:
    def __init__(self, settings, llm=None, storage: Storage | None = None):
        self.s = settings
        host, port = _loopback_address(settings.local_service_url)
        self.service_url = settings.local_service_url.rstrip("/")
        records = {Path(p).resolve() for p in (settings.db_path, settings.sim_db_path, settings.test_db_path,
                                                settings.local_db_path)}
        if Path(settings.local_demo_db).resolve() in records:
            raise LabError("the local demo database must be a separate file from every ADAPTIX records database")
        self.db = DemoDatabase(settings.local_demo_db)
        self.storage = storage or Storage(settings.local_db_path, mode="local_demo")
        self.llm = llm or OllamaProvider(settings.ollama_url, settings.local_llm_model, settings.local_llm_timeout_s,
                                         settings.local_llm_max_tokens)
        guard = GuardedHTTP((), timeout_s=settings.request_timeout_s, min_interval_s=0, exempt=frozenset({(host, port)}))
        self.engine = Engine(self.storage, CredentialRegistry([]), self.llm, settings,
                             Upstreams(settings.request_timeout_s, "local_demo", guard, settings.max_schema_bytes))
        self.jobs = Jobs()
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- readiness and state
    def spec(self) -> ToolSpec:
        return ToolSpec(name=TOOL, base_url=self.service_url, path=PATH, method="POST",
                        description="Look up one customer by its identifier (local demo customer API)",
                        schema_source=SchemaSource(kind="openapi", url=self.service_url + CONTRACT),
                        idempotent=True, supports_idempotency_key=False, status_output="status",
                        success_values=["found"], echo_output="query",
                        provider="ADAPTIX local demo service (this machine)", monitor_enabled=False)

    def service_health(self) -> dict:
        try:
            r = self.engine.upstreams.request(self.service_url, "GET", "/health", timeout=3.0)
            return {"status": "running" if r.status_code == 200 and r.json().get("status") == "ok" else "degraded",
                    "detail": r.json().get("status")}
        except Exception as e:
            return {"status": "unreachable", "detail": scrub(str(e))[:160],
                    "action": "Start it: python -m adaptix.localdemo service (or scripts/run_local_demo.sh)"}

    def database_state(self) -> dict:
        if not self.db.exists():
            return {"status": "missing", "action": "Initialise it: python -m adaptix.localdemo db init"}
        try:
            integ = self.db.integrity()
            return {"status": "ready" if integ["integrity_ok"] and integ["marker"] and not integ["foreign_key_violations"]
                    else "damaged", **integ, "counts": self.db.counts(), "key_column": self.db.key_column(),
                    "lookup_columns": self.db.lookup_columns(),
                    "migrations": [m for m in self.db.migrations() if m["kind"] != "seed"]}
        except Exception as e:
            return {"status": "damaged", "detail": scrub(str(e))[:160],
                    "action": "Reset the demo database: python -m adaptix.localdemo db reset --yes"}

    def status(self) -> dict:
        db, svc = self.database_state(), self.service_health()
        if (self.storage.tool(TOOL) is None and db["status"] == "ready" and not db["migrations"]
                and svc["status"] == "running"):
            with self.lock:                  # register against the baseline contract (ADAPTIX records only)
                self.ensure_registered()
        return {"database": db, "service": svc, "model": self.llm.readiness(),
                "registered": self.storage.tool(TOOL) is not None, "environment": "LOCAL_DEMO",
                "running_job": (self.jobs.running() or {}).get("id")}

    def contracts(self) -> dict:
        """Registered contract (ADAPTIX's stored schema) vs the live contract read from the local API right now."""
        from ..diff import diff
        out = {"registered": None, "live": None, "diff": None, "error": None, "version": None}
        if self.storage.tool(TOOL) is not None:
            td = self.storage.current(TOOL)
            op = self.storage.get_schema(td.schema_signature)
            out["version"] = td.version
            out["registered"] = _fields(op)
            out["request_map"] = td.request_map
        d = self.engine.discover_detailed(self.spec() if self.storage.tool(TOOL) is None else self.storage.tool(TOOL))
        if d.op is None or isinstance(d.op, str):
            out["error"] = f"{d.error_category}: {d.detail}"
            return out
        out["live"] = _fields(d.op)
        if self.storage.tool(TOOL) is not None:
            rep = diff(op, d.op)
            out["diff"] = {"changed": rep.changed, "summary": rep.summary,
                           "changes": [{"kind": c.kind, "field": c.location, "was": c.old, "now": c.new}
                                       for c in rep.request_changes + rep.response_changes]}
        return out

    def ensure_registered(self):
        if self.storage.tool(TOOL) is None:
            if self.db.migrations()[-1]["kind"] != "seed":
                raise LabError("the demo database is not at its baseline schema; reset the demo first")
            self.engine.register(self.spec())

    # ---------------------------------------------------------------- business assertions
    def business_checks(self, body) -> list[dict]:
        """Identity and required fields of the returned customer, against the documented fixture."""
        cust = body.get("customer") if isinstance(body, dict) else None
        if not isinstance(cust, dict):
            return [{"name": "customer record returned", "ok": False, "detail": "no customer object in the response"}]
        key = self.db.key_column()
        checks = [{"name": "customer identity", "ok": cust.get(key) == FIXTURE["customer_id"],
                   "detail": f"{key} = {cust.get(key)!r} (expected {FIXTURE['customer_id']})"},
                  {"name": "customer name", "ok": cust.get("full_name") == FIXTURE["full_name"],
                   "detail": f"full_name = {cust.get('full_name')!r}"},
                  {"name": "required field: email", "ok": cust.get("email") == FIXTURE["email"],
                   "detail": "email present and correct" if cust.get("email") == FIXTURE["email"]
                   else "email missing or different"}]
        if "loyalty_points" in cust:
            checks.append({"name": "loyalty points value", "ok": cust["loyalty_points"] == FIXTURE["loyalty_points"],
                           "detail": f"loyalty_points = {cust['loyalty_points']!r}"})
        return checks

    # ---------------------------------------------------------------- scenarios
    def preview(self, scenario: str, params: dict | None = None) -> dict:
        if scenario not in SCENARIOS:
            raise LabError(f"unknown scenario {scenario}")
        sc = SCENARIOS[scenario]
        p = {**sc["default"], **{k: v for k, v in (params or {}).items() if v not in (None, "")}}
        cols = [c["name"] for c in self.db.columns()] if self.db.exists() else []
        problems = []
        if scenario == "rename":
            if p["old"] != (self.db.key_column() if self.db.exists() else "customer_id"):
                problems.append("this demo renames the customer key column only")
            if not new_name_ok(p["new"]) or p["new"] in cols:
                problems.append(f"{p['new']!r} must be a new lowercase name (letters, digits, underscore)")
            change = f"ALTER TABLE customers RENAME COLUMN {p['old']} TO {p['new']}"
            effect = f"The API's request and response field {p['old']} becomes {p['new']}."
        elif scenario == "type":
            change = f"Rebuild customers with {p['column']} REAL instead of INTEGER (one transaction)"
            effect = f"The contract's {p['column']} type changes from integer to number."
        elif scenario == "required":
            if not new_name_ok(p["column"]) or p["column"] in cols:
                problems.append(f"{p['column']!r} must be a new lowercase column name")
            change = (f"ALTER TABLE customers ADD COLUMN {p['column']} TEXT NOT NULL; "
                      f"lookup index becomes (customer key, {p['column']})")
            effect = f"Every lookup request must now include {p['column']}."
        else:
            if p["column"] not in cols:
                problems.append(f"{p['column']!r} is not a current column")
            change = f"ALTER TABLE customers DROP COLUMN {p['column']}"
            effect = f"The response no longer contains {p['column']}; the data is gone from the demo table."
        return {"scenario": scenario, "title": sc["title"], "params": p, "database_change": change, "api_effect": effect,
                "expected": sc["expected"], "why": sc["why"], "valid": not problems, "problems": problems,
                "note": "The change text is a description for review; the migration itself is fixed code in the "
                        "database adapter with validated identifiers."}

    def _migrate(self, scenario: str, p: dict) -> dict:
        if scenario == "rename":
            return self.db.rename_column(p["old"], p["new"])
        if scenario == "type":
            return self.db.change_type(p["column"], "REAL")
        if scenario == "required":
            return self.db.add_required_lookup_field(p["column"])
        return self.db.remove_column(p["column"])

    def start(self, scenario: str, params: dict | None, confirm: bool) -> dict:
        if not confirm:
            raise LabError("confirmation required: this changes the local demo database schema")
        pv = self.preview(scenario, params)
        if not pv["valid"]:
            raise LabError("; ".join(pv["problems"]))
        with self.jobs.lock:
            if self.jobs.running():
                raise LabError("another local demo run is in progress")
            job = {"id": "exp_" + uuid.uuid4().hex[:10], "scenario": scenario, "title": pv["title"], "params": pv["params"],
                   "llm": f"Local LLM · {self.llm.model}", "status": "running", "events": [], "result": None,
                   "error": None, "started_at": now_iso(), "t0": time.perf_counter(), "finished": None,
                   "environment": "LOCAL_DEMO", "_lock": threading.Lock(), "evidence": {}}
            self.jobs.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job, pv), name=f"adaptix-{job['id']}", daemon=True).start()
        return public(self.jobs.get(job["id"]))

    def job(self, job_id: str) -> dict | None:
        j = self.jobs.get(job_id)
        return public(j) if j else None

    def _run(self, job: dict, pv: dict):
        try:
            with self.lock:
                self._run_locked(job, pv)
        except Exception as e:                                   # recorded, never swallowed
            job["error"] = f"{type(e).__name__}: {scrub(str(e))[:200]}"
            _event(job, "Outcome", "fail", "The run failed: " + job["error"])
            job["result"] = job["result"] or {"final": "FAILED", "failed_stage": "internal", "reason": job["error"]}
        finally:
            self.engine.llm_required, self.engine.business_check = False, None
            job["finished"] = time.perf_counter()
            job["status"] = "done"

    def _blocked(self, job, stage: str, reason: str):
        _event(job, stage, "fail", reason)
        job["result"] = {"final": "NOT RUN", "failed_stage": stage, "reason": reason, "mutated": False}

    def _run_locked(self, job: dict, pv: dict):
        st = self.status()
        if st["database"]["status"] != "ready":
            return self._blocked(job, "Readiness", f"Demo database not ready: {st['database'].get('action') or ''}")
        if st["service"]["status"] != "running":
            return self._blocked(job, "Readiness", f"Local customer API not reachable. {st['service'].get('action', '')}")
        m = st["model"]
        if m["server"] != "running" or not m["model_installed"]:
            return self._blocked(job, "Readiness", f"Local model not ready ({m['model']}). {m['action'] or ''}")
        if st["database"]["migrations"]:
            return self._blocked(job, "Readiness", "The demo database already has a schema change. Reset the demo "
                                                   "first so every run starts from the same baseline.")
        _event(job, "Readiness", "ok", f"Database, local customer API and local model {m['model']} are ready")
        self.ensure_registered()
        before = self.contracts()
        if before["diff"] and before["diff"]["changed"]:
            return self._blocked(job, "Readiness", "The registered contract does not match the live contract; "
                                                   "reset the demo first.")
        self.engine.business_check = self.business_checks
        # 2 baseline: the original business request, verified
        base = self.engine.execute(TOOL, dict(ARGS), scenario=f"local:{job['scenario']}:baseline")
        if base["status"] != "ok":
            return self._blocked(job, "Baseline request", f"Baseline request did not verify ({base['status']}): "
                                                          f"{base.get('reason')}; no change was introduced")
        job["evidence"]["baseline"] = {"request": {"method": "POST", "path": PATH, "body": redact(dict(ARGS))},
                                       "response": redact(base["result"]), "tool_version": base["tool_version"],
                                       "checks": self.business_checks(base["result"]),
                                       "registered_contract": before["registered"], "ms": base["duration_ms"]}
        _event(job, "Baseline request", "ok", f"LOCAL REAL EXECUTION · customer {ARGS['customer_id']} returned and "
                                             f"verified with tool v{base['tool_version']} ({base['duration_ms']} ms)")
        # 3 backup + 4 mutation (the only schema change, after explicit confirmation)
        backup = self.db.backup()
        _event(job, "Backup", "ok", f"Demo database backed up ({backup.name if backup else 'none'})")
        t = time.perf_counter()
        change = self._migrate(job["scenario"], pv["params"])
        job["evidence"]["mutation"] = {"change": change, "database_change": pv["database_change"],
                                       "ms": round((time.perf_counter() - t) * 1000, 1), "backup": backup.name}
        after = self.contracts()
        job["evidence"]["contracts"] = {"registered": after["registered"], "live": after["live"], "diff": after["diff"]}
        _event(job, "Schema mutation", "warn", f"LOCAL REAL EXECUTION · {pv['database_change']} · live contract: "
                                               f"{(after['diff'] or {}).get('summary') or after.get('error')}")
        # 5 the same request; recovery runs automatically (local model must propose the repair)
        self.engine.llm_required = True
        _event(job, "Request", "info", "Sending the original request again; ADAPTIX takes over if it fails")
        res = self.engine.execute(TOOL, dict(ARGS), scenario=f"local:{job['scenario']}",
                                  observer=lambda step: _event(job, PHASE.get(step.stage, step.stage), step.status,
                                                               step.title))
        inc = self.storage.incident(res["incident"]) if res.get("incident") else None
        incd = inc.model_dump(mode="json") if inc else None
        verification = (incd or {}).get("verification") or {}
        checks = verification.get("checks") or (self.business_checks(res.get("result")) if res["status"] == "ok" else [])
        biz_ok = bool(checks) and all(c["ok"] for c in checks) and res["status"] in ("ok", "recovered")
        if res["status"] == "recovered" and biz_ok:
            final = "VERIFIED"
        elif res["status"] == "ok" and biz_ok:
            final = "VERIFIED (no repair needed)"
        elif res["status"] == "stopped":
            final = "SAFELY BLOCKED"
        elif res["status"] == "rolled_back":
            final = "ROLLED BACK"
        else:
            final = "FAILED"
        stage = None if final.startswith("VERIFIED") else next(
            (PHASE.get(s["stage"], s["stage"]) for s in reversed((incd or {}).get("timeline", []))
             if s["status"] == "fail" and s["stage"] != "outcome"),
            "Business verification")
        ev = evidence_mod.build(self.engine, incd, run_id=job["id"], scenario=f"local:{job['scenario']}", tool=TOOL,
                                expected=ENGINE_EXPECTED[SCENARIOS[job["scenario"]]["expected"]], result=res,
                                business={"ok": biz_ok, "checks": checks},
                                extra={"experiment_events": job["events"], "mutation": job["evidence"]["mutation"],
                                       "contracts": job["evidence"]["contracts"], "local_final": final})
        llm = (incd or {}).get("llm") or {}
        job["result"] = {"final": final, "status": res["status"], "reason": res.get("reason"), "failed_stage": stage,
                         "incident": res.get("incident"), "evidence_id": ev["evidence_id"],
                         "tool_version": res.get("tool_version"), "recovery_ms": res.get("duration_ms"),
                         "business_checks": checks, "expected": SCENARIOS[job["scenario"]]["expected"],
                         "as_expected": final == SCENARIOS[job["scenario"]]["expected"], "mutated": True,
                         "plan": (incd or {}).get("plan"), "validation": (incd or {}).get("validation"),
                         "rolled_back": (incd or {}).get("rolled_back", False),
                         "llm": {"provider": llm.get("provider"), "model": llm.get("model"), "real": llm.get("real"),
                                 "ok": llm.get("ok"), "error_category": llm.get("error_category"),
                                 "duration_ms": llm.get("duration_ms")} if llm else None}
        _event(job, "Outcome", "ok" if final.startswith("VERIFIED") else "fail",
               f"{final}: {res.get('reason') or 'original request verified'}")

    # ---------------------------------------------------------------- after a run: the patch and its effect
    def patch_story(self, incident_id: str) -> dict:
        """From the stored incident and tool versions: the real error, the model's proposal, the validation, the
        exact patch written, and the retried request. Nothing here is reconstructed or invented."""
        inc = self.storage.incident(incident_id)
        if inc is None:
            raise LabError(f"unknown incident {incident_id}")
        tl = [s.model_dump(mode="json") for s in inc.timeline]
        reqs = [s for s in tl if s["stage"] == "request" and isinstance(s.get("detail"), dict)]
        before = next((s for s in reqs if s["status"] == "fail"), None)
        after = next((s for s in reqs if s["status"] == "ok" and s["title"].startswith("retry")), None)
        llm_step = next((s for s in tl if s["stage"] == "plan" and "LLM planner" in s["title"]), None)
        versions = [{"version": v.version, "status": v.status, "parent_version": v.parent_version,
                     "origin": v.origin, "incident_id": v.incident_id, "request_map": v.request_map,
                     "patch": v.patch} for v in self.storage.versions(TOOL)]
        applied = next((v for v in versions if v["incident_id"] == incident_id and v["version"] > 1), None)
        pick = lambda s: None if s is None else {k: s["detail"].get(k) for k in    # noqa: E731
                                                 ("method", "url", "sent", "status", "reply", "ms")}
        return {"incident": incident_id, "status": inc.status, "reason": inc.reason,
                "error_before": pick(before), "request_after": pick(after),
                "model": inc.llm, "model_proposal": (llm_step or {}).get("detail"), "plan": inc.plan,
                "validation": inc.validation, "applied_version": applied, "versions": versions,
                "rolled_back": inc.rolled_back, "verification": inc.verification}

    def call_original(self) -> dict:
        """Send the ORIGINAL business request again through the current tool version (no schema change)."""
        with self.lock:
            if self.storage.tool(TOOL) is None:
                raise LabError("the tool is not registered yet")
            self.engine.business_check = self.business_checks
            try:
                r = self.engine.execute(TOOL, dict(ARGS), scenario="local:call-again")
            finally:
                self.engine.business_check = None
        inc = self.storage.incident(r["incident"]) if r.get("incident") else None
        sent = next((s.detail for s in reversed(inc.timeline) if s.stage == "request"), None) if inc else None
        td = self.storage.current(TOOL)
        return {"status": r["status"], "tool_version": r["tool_version"], "incident": r.get("incident"),
                "duration_ms": r["duration_ms"], "result": redact(r.get("result")), "reason": r.get("reason"),
                "sent_as": td.build(dict(ARGS))["body"], "request_map": td.request_map,
                "business_checks": self.business_checks(r.get("result")) if r["status"] == "ok" else
                (inc.verification or {}).get("checks") if inc else [], "last_request": sent}

    # ---------------------------------------------------------------- reset
    def reset(self, confirm: bool) -> dict:
        """Deletes: the demo database (after a backup) and ADAPTIX's LOCAL_DEMO records (tool versions, incidents,
        evidence, audit). Then re-seeds and re-registers the tool against the baseline contract."""
        if not confirm:
            raise LabError("confirmation required: reset deletes the demo database and LOCAL_DEMO records")
        with self.lock:
            if self.jobs.running():
                raise LabError("a run is in progress")
            db = self.db.reset()
            self.storage.reset()
            registered = False
            if self.service_health()["status"] == "running":
                self.engine.register(self.spec())
                registered = True
        return {"reset": True, "backup": db["backup"], "counts": db["counts"], "registered": registered}


def _fields(op) -> list[dict]:
    if op is None:
        return []
    return [{"field": loc, "type": f.type, "required": f.required, "documentation": f.description or "",
             "renamed_from": f.renamed_from or ""} for loc, f in {**op.fields, **op.response_fields}.items()]


__all__ = ["LocalLab", "LabError", "SCENARIOS", "TOOL", "DemoDatabaseError"]
