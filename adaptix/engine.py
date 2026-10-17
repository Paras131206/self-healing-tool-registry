"""The ADAPTIX recovery pipeline.

A. execute the tool call and capture structured metadata
B. classify the failure (only schema drift is repaired)
C. discover the authoritative schema (fail closed without it)
D. diff it against the registered schema
E. plan a minimal repair: rules first, the LLM for what rules cannot resolve
F. validate the plan independently (untrusted input)
G. apply it atomically as a new tool version (lock + optimistic version check)
H. rebuild the request from the caller's own values
I. retry within strict budgets (backoff + jitter, Retry-After, idempotency key, no loops)
J. verify the task really succeeded (status + applied values), repairing response-schema drift if needed
K. roll back a repair that fails verification and report an actionable outcome

Supported, validated, reversible repairs run autonomously. Ambiguous, unsafe, unauthorized or unsupported
changes stop safely with a clear reason. Every step is recorded on the incident timeline and in the audit log.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .classify import EXPLAIN, Classification, classify_exception, classify_response
from .config import Settings
from .credentials import CredentialError, CredentialRegistry
from .diff import DriftReport, diff, request_conflicts, sent_locations
from .models import Incident, LLMRepairPlan, Mapping, RepairPlan, ToolSpec, now_iso
from .planner import leaf, plan_request, plan_response
from .schema import Operation, parse_json_schema, parse_openapi
from .security import redact, redact_headers, redact_reply, scrub
from .storage import ConflictError, Storage, new_holder
from .tooldef import ToolDef, py_type
from .netguard import EgressDenied
from .upstreams import Upstreams
from .validator import validate, validate_response
from .verify import verify as _verify

CREDENTIAL_CATEGORIES = {"credential_expired", "credential_revoked", "credential_invalid", "rate_limited",
                         "authorization_failed", "authentication_missing"}


class EngineError(RuntimeError):
    pass


@dataclass
class Discovery:
    """Result of authoritative schema discovery, with a sanitized error category when it fails."""
    op: Operation | str | None = None
    status: str = "failed"              # verified | failed | unsupported
    error_category: str | None = None   # egress_denied:<why> | dns_failure | tls_error | timeout | network |
    #                                     http_<code> | not_json | operation_not_in_schema | no_schema_source
    detail: str = ""
    url: str = ""


@dataclass
class Sent:
    status: int | None = None
    body: object = None
    headers: dict = field(default_factory=dict)
    error_kind: str | None = None          # timeout | network | credential:<category>
    error: str = ""
    retries: int = 0
    request: dict = field(default_factory=dict)
    first_failure: str | None = None       # category of the first failure seen while sending


def _arg_name(loc: str) -> str:
    where, name = loc.split(":", 1)
    return name.replace(".", "_") if where == "body" else name


class Engine:
    def __init__(self, storage: Storage, creds: CredentialRegistry, llm, settings: Settings,
                 upstreams: Upstreams | None = None, sleep=time.sleep):
        self.storage, self.creds, self.llm, self.s = storage, creds, llm, settings
        self.mode = storage.mode
        self.upstreams = upstreams or Upstreams(settings.request_timeout_s, mode=self.mode)
        if self.upstreams.mode != self.mode:
            raise EngineError(f"{self.mode} storage cannot be paired with {self.upstreams.mode} upstreams")
        if self.mode != "simulation" and getattr(llm, "name", "").startswith("mock"):
            raise EngineError(f"the {self.mode} pipeline never uses the mock LLM")
        self.sleep = sleep
        # llm_required: the LLM must propose every changed mapping (the rules plan is recorded only as a
        # cross-check). If the LLM is unavailable or its output is invalid, the repair stops - no fallback.
        self.llm_required = False
        # business_check(response) -> [check dicts]: optional business assertions (e.g. the LOCAL DEMO checks the
        # returned customer's identity). They are added to every verification; a 2xx alone never verifies a task.
        self.business_check = None

    def set_llm(self, llm):
        if self.mode != "simulation" and getattr(llm, "name", "").startswith("mock"):
            raise EngineError(f"the {self.mode} pipeline never uses the mock LLM")
        self.llm = llm

    # ================================================================ schema discovery (C)
    def discover_detailed(self, spec: ToolSpec) -> Discovery:
        if spec.schema_source.kind == "none":
            return Discovery(status="unsupported", error_category="no_schema_source",
                             detail="the provider publishes no machine-readable schema")
        url = self.upstreams.rebase(spec.schema_url())       # the address actually contacted in this runtime
        try:
            r = self.upstreams.fetch_json(url)
        except EgressDenied as e:
            return Discovery(error_category=f"egress_denied:{e.category}", detail=scrub(str(e))[:200], url=url)
        except httpx.TimeoutException:
            return Discovery(error_category="timeout", detail="the schema request timed out", url=url)
        except httpx.ConnectError as e:
            cat = "tls_error" if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper() else "network"
            return Discovery(error_category=cat, detail=scrub(str(e))[:200], url=url)
        except (httpx.HTTPError, KeyError) as e:
            return Discovery(error_category="network", detail=scrub(str(e))[:200], url=url)
        if r.status != 200:
            return Discovery(error_category=f"http_{r.status}", detail=f"the schema URL answered HTTP {r.status}", url=url)
        if not isinstance(r.json, dict):
            return Discovery(error_category="not_json", detail="the schema URL did not return a JSON object", url=url)
        try:
            if spec.schema_source.kind == "json_schema":
                op = parse_json_schema(r.json, spec.path, spec.method)
            else:
                op = parse_openapi(r.json, spec.path, spec.method)
        except Exception as e:                        # malformed document: never treated as "no drift"
            return Discovery(error_category="invalid_schema", url=url,
                             detail=f"the document is not a valid {spec.schema_source.kind} schema "
                                    f"({type(e).__name__})")
        if op is None:
            return Discovery(error_category="operation_not_in_schema", url=url,
                             detail=f"{spec.method} {spec.path} is not in the published schema")
        return Discovery(op=op, status="verified", url=url)

    def discover(self, spec: ToolSpec) -> Operation | str | None:
        return self.discover_detailed(spec).op

    def verify(self, spec: ToolSpec, td: ToolDef, body, request: dict):
        op = self.storage.get_schema(td.schema_signature) if not td.response_map else None
        v = _verify(spec, td, body, request, op)
        if self.business_check is not None and v.ok:
            try:
                extra = list(self.business_check(body))
            except Exception as e:                   # an assertion that cannot run is a failed assertion
                extra = [{"name": "business assertions ran", "ok": False, "detail": scrub(str(e))[:120]}]
            v = v.model_copy(update={"checks": v.checks + extra, "ok": all(c["ok"] for c in v.checks + extra)})
        return v

    # ================================================================ registration
    def _declared_operation(self, spec: ToolSpec) -> Operation:
        """Operator-declared fields for providers without a schema. NOT authoritative: never used as evidence."""
        from .schema import FieldSpec
        fields = {}
        for loc, t in spec.schema_source.declared_fields.items():
            if not loc.startswith(("body:", "query:", "path:")):
                raise EngineError(f"declared field {loc!r} must start with body:, query: or path:")
            fields[loc] = FieldSpec([t if t in ("str", "int", "float", "bool", "list", "object") else "str"], False)
        return Operation(spec.method, spec.path, fields, [], None,
                         ["application/json"] if any(k.startswith("body:") for k in fields) else [],
                         source="declared")

    def register(self, spec: ToolSpec) -> ToolDef:
        if spec.simulated != (self.mode == "simulation"):
            raise EngineError(f"a {'simulated' if spec.simulated else 'real'} tool cannot be registered in "
                              f"{self.mode} mode")
        if spec.schema_source.kind == "none":
            if not spec.schema_source.declared_fields:
                raise EngineError("a provider without a schema needs declared_fields (they are not authoritative)")
            op = self._declared_operation(spec)
        else:
            d = self.discover_detailed(spec)
            op = d.op
            if not isinstance(op, Operation):
                raise EngineError(f"cannot register {spec.name}: no authoritative schema for {spec.method} "
                                  f"{spec.path} at {spec.schema_url()} ({d.error_category}: {d.detail})")
        request_map = {loc: [_arg_name(loc), "none"] for loc in op.fields}
        header_map = {}
        for h in op.headers:
            if spec.auth_header and h == spec.auth_header.lower() and spec.credential_group:
                header_map[h] = spec.credential_group
            else:
                groups = self.creds.groups_for_header(h, f"tool:{spec.name}")
                if len(groups) == 1:
                    header_map[h] = groups[0]
        response_map = {}
        if f"response:{spec.status_output}" in op.response_fields:
            response_map["status"] = spec.status_output
        if spec.echo_output and f"response:{spec.echo_output}" in op.response_fields:
            response_map["echo"] = spec.echo_output
        td = ToolDef(spec.name, 1, spec.method, spec.path, request_map, header_map, response_map)
        self.storage.register(spec, td, op)
        return td

    def arguments_schema(self, name: str) -> dict:
        """The caller-facing argument schema of the registered tool (used for OpenAI tool definitions)."""
        td = self.storage.versions(name)[0]
        op = self.storage.get_schema(td.schema_signature)
        props, required = {}, []
        for loc, (arg, _) in td.request_map.items():
            f = op.fields.get(loc) if op else None
            js = {"type": {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array",
                           "date": "string", "enum": "string", "object": "object"}.get(f.type if f else "str", "string")}
            if f and f.enum is not None:
                js["enum"] = f.enum
            if f and f.description:
                js["description"] = f.description
            props[arg] = js
            if f and f.required:
                required.append(arg)
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    # ================================================================ sending (A, I) with credential failover
    def _send(self, spec: ToolSpec, td: ToolDef, args: dict, inc: Incident, idem: str, label: str,
              deadline: float, resilient: bool = True) -> Sent:
        """resilient=False is the baseline: one plain request, no retries, no credential failover."""
        max_retries = self.s.max_transport_retries if resilient else 0
        req = td.build(args)
        out = Sent(request=req)
        headers = {"Idempotency-Key": idem} if spec.supports_idempotency_key else {}
        scope = f"tool:{spec.name}"
        used: dict[str, object] = {}
        tried: dict[str, set] = {}
        try:
            for h, group in td.header_map.items():
                cred, key = self.creds.select(group, scope)
                headers[h], used[h] = key, cred
                tried.setdefault(h, set()).add(cred.alias)
        except CredentialError as e:
            out.error_kind, out.error = "credential:credential_unavailable", str(e)
            inc.add("credential", "No authorized credential available", "fail", {"reason": e.category})
            return out
        retry_safe = spec.is_idempotent() or spec.supports_idempotency_key
        while True:
            t0 = time.perf_counter()
            try:
                r = self.upstreams.request(spec.base_url, req["method"], req["url"], params=req["query"] or None,
                                           json=req["body"], headers=headers, timeout=self.s.request_timeout_s)
            except EgressDenied as e:                   # refused before sending anything
                kind = "rate_limited" if e.category == "rate_limited_local" else \
                    ("dns_failure" if e.category == "dns_failure" else "egress_denied")
                out.first_failure = out.first_failure or kind
                inc.add("request", f"{label}: not sent ({e.category})", "fail", {"error": scrub(str(e))[:200]})
                out.error_kind, out.error = kind, scrub(str(e))[:200]
                return out
            except (httpx.TimeoutException, httpx.TransportError, KeyError) as e:
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "network"
                if isinstance(e, httpx.ConnectError) and ("CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper()):
                    kind = "tls_error"
                out.first_failure = out.first_failure or kind
                inc.add("request", f"{label}: no answer ({kind})", "fail", {"error": scrub(str(e))[:200]})
                if spec.is_idempotent() and out.retries < max_retries and self._wait(out, None, deadline, inc):
                    continue
                out.error_kind, out.error = kind, scrub(str(e))[:200]
                return out
            try:
                body = r.json()
            except ValueError:
                body = {"detail": scrub(r.text[:300])}
            ms = round((time.perf_counter() - t0) * 1000, 1)
            inc.add("request", f"{label}: HTTP {r.status_code}", "ok" if r.status_code < 400 else "fail",
                    {"method": req["method"], "url": req["url"], "sent": redact(req["body"]),
                     "query": redact(req["query"]), "headers": redact_headers(headers), "status": r.status_code,
                     "reply": redact_reply(body), "ms": ms})
            out.status, out.body, out.headers = r.status_code, body, dict(r.headers)
            pre = classify_response(r.status_code, body, schema_changed=False)
            if r.status_code >= 400 and pre.category in CREDENTIAL_CATEGORIES | {"server_error"}:
                out.first_failure = out.first_failure or pre.category
            # authorized credential failover: the request was rejected before doing anything, so resending is safe
            if pre.category in CREDENTIAL_CATEGORIES and used and out.retries < max_retries:
                h = next(iter(used))
                cred = used[h]
                retry_after = self._retry_after(r)
                self.creds.report(cred.alias, pre.category, retry_after)
                alt = self.creds.failover(cred, pre.category, td.header_map[h], scope, tried[h])
                self.storage.event("credential_failure", spec.name, {"alias": cred.alias, "category": pre.category,
                                                                     "failover_to": alt[0].alias if alt else None},
                                   inc.correlation_id, inc.simulated)
                if alt is not None:
                    inc.add("credential", f"Credential '{cred.alias}' {pre.category.replace('_', ' ')}; "
                                          f"failing over to '{alt[0].alias}'", "warn",
                            {"from": cred.alias, "to": alt[0].alias, "reason": pre.category})
                    headers[h], used[h] = alt[1], alt[0]
                    tried[h].add(alt[0].alias)
                    out.retries += 1
                    continue
            if used and r.status_code < 400:
                for c in used.values():
                    self.creds.report(c.alias, "ok")
            # transient upstream failures: bounded retry with backoff, honouring Retry-After
            if r.status_code in (429, 502, 503, 504) and (r.status_code == 429 or retry_safe) \
                    and out.retries < max_retries:
                if self._wait(out, self._retry_after(r), deadline, inc):
                    continue
            return out

    @staticmethod
    def _retry_after(r) -> float | None:
        v = r.headers.get("retry-after")
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    def _wait(self, out: Sent, retry_after: float | None, deadline: float, inc: Incident) -> bool:
        backoff = min(self.s.backoff_max_s, self.s.backoff_base_s * 2 ** out.retries) * (0.5 + random.random() / 2)
        wait = retry_after if retry_after is not None else backoff
        if time.monotonic() + wait > deadline:
            inc.add("retry", f"Not retrying: waiting {wait:.1f}s would exceed the time budget", "warn")
            return False
        inc.add("retry", f"Retrying in {wait:.2f}s" + (" (Retry-After)" if retry_after is not None else " (backoff)"),
                "info", {"attempt": out.retries + 1})
        self.sleep(wait)
        out.retries += 1
        return True

    # ================================================================ public entry points
    def execute(self, tool: str, args: dict, correlation_id: str | None = None, scenario: str | None = None,
                heal: bool = True, observer=None) -> dict:
        """observer(step): optional callback receiving each timeline step as it happens (live progress)."""
        spec = self.storage.tool(tool)
        if spec is None:
            raise EngineError(f"unknown tool {tool}")
        inc = Incident(tool=tool, simulated=spec.simulated, scenario=scenario, healing=heal,
                       environment=self.storage.environment, correlation_id=correlation_id or uuid.uuid4().hex)
        inc._observer = observer
        t0, deadline = time.perf_counter(), time.monotonic() + self.s.total_time_budget_s
        td = self.storage.current(tool)
        inc.from_version = td.version
        idem = uuid.uuid4().hex                        # one key per logical call, reused on every retry/repair
        try:
            res = self._run(spec, td, args, inc, idem, deadline, heal)
        except Exception as e:                         # never leak internals or secrets; record and report
            inc.status, inc.category = "failed", "unknown"
            inc.reason = "internal error: " + scrub(str(e))[:200]
            inc.add("outcome", "Internal error", "fail", {"error": inc.reason})
            res = None
        inc.duration_ms = round((time.perf_counter() - t0) * 1000, 1)
        inc.finished_at = now_iso()
        if res is not None and inc.status == "open":     # plain success, no incident
            self.storage.record_execution(tool, True, "ok", inc.duration_ms, spec.simulated)
            return {"status": "ok", "result": res, "tool_version": td.version, "incident": None,
                    "correlation_id": inc.correlation_id, "duration_ms": inc.duration_ms}
        self.storage.save_incident(inc)
        self.storage.record_execution(tool, inc.status == "recovered", inc.category, inc.duration_ms, spec.simulated, inc.id)
        self.storage.event("incident_" + inc.status, tool, {"incident": inc.id, "category": inc.category,
                                                            "reason": inc.reason, "zero_touch": inc.zero_touch},
                           inc.correlation_id, inc.simulated)
        return {"status": inc.status, "result": inc.result, "tool_version": inc.to_version or inc.from_version,
                "incident": inc.id, "category": inc.category, "reason": inc.reason,
                "correlation_id": inc.correlation_id, "duration_ms": inc.duration_ms}

    # ================================================================ the pipeline
    def _stop(self, inc: Incident, category: str, reason: str, status: str = "stopped"):
        inc.category = inc.category if category is None else category
        inc.status, inc.reason = status, reason
        inc.add("outcome", reason, "fail")
        return None

    def _run(self, spec, td, args, inc, idem, deadline, heal):
        sent = self._send(spec, td, args, inc, idem, f"call with v{td.version}", deadline, resilient=heal)
        inc.retries += sent.retries
        if sent.error_kind:
            if sent.error_kind.startswith("credential:"):
                cat = sent.error_kind.split(":", 1)[1]
                return self._stop(inc, cat, EXPLAIN.get(cat, sent.error))
            c = classify_exception(sent.error_kind)
            inc.add("classify", c.explanation, "fail", {"category": c.category})
            return self._stop(inc, c.category, c.explanation)
        if sent.status < 400:
            v = self.verify(spec, td, sent.body, sent.request)
            if v.ok:
                if sent.first_failure:                 # recovered by a bounded retry or credential failover
                    failover = any(s.stage == "credential" for s in inc.timeline)
                    inc.category = sent.first_failure
                    inc.add("verify", "Original task verified", "ok", {"checks": v.checks})
                    inc.status, inc.zero_touch, inc.result, inc.to_version = "recovered", True, sent.body, td.version
                    inc.reason = "succeeded with an authorized failover credential" if failover \
                        else "succeeded after a bounded retry"
                    inc.add("outcome", inc.reason, "ok")
                return sent.body
            inc.add("verify", "The API answered 2xx but the task is not confirmed", "fail", {"checks": v.checks})
            inc.verification = v.model_dump()
            if v.contract_drift and heal:
                return self._repair_response(spec, td, args, inc, sent)
            return self._stop(inc, "verification_failed", "The API accepted the call but did not confirm the task: "
                              + "; ".join(c["detail"] for c in v.checks if not c["ok"]))
        # ---- B/C/D: classify with authoritative evidence
        new = self.discover(spec)
        old = self.storage.get_schema(td.schema_signature)
        if isinstance(new, str):
            inc.add("discover", f"Schema now expects {new} on {spec.path}", "fail")
            return self._stop(inc, "method_changed", EXPLAIN["method_changed"])
        report = diff(old, new) if (new is not None and old is not None) else None
        changed = None if report is None else report.changed
        conflicts = request_conflicts(report, sent_locations(sent.request), set(td.header_map)) if report else []
        cls = classify_response(sent.status, sent.body, changed, report.touched() if report else None, conflicts)
        inc.category = cls.category
        inc.add("classify", cls.explanation, "warn" if cls.category == "schema_drift" else "fail",
                {"category": cls.category, "evidence": cls.evidence})
        if new is not None:
            inc.add("discover", f"Authoritative schema read from {spec.schema_url()}", "ok",
                    {"signature": new.signature(), "source": new.source})
            inc.old_signature, inc.new_signature = td.schema_signature, new.signature()
        if report is not None:
            inc.drift = report.model_dump()
            inc.add("diff", f"Drift report: {report.summary}", "info", {"compatibility": report.compatibility})
        if cls.category != "schema_drift":
            return self._stop(inc, cls.category, cls.explanation)
        if not heal:
            return self._stop(inc, "schema_drift", "schema drift detected; self-healing disabled for this run (baseline)")
        if report.content_type_changed and "application/json" not in report.content_type_changed:
            return self._stop(inc, "unsupported_change", EXPLAIN["unsupported_change"])
        return self._repair_request(spec, td, args, inc, idem, deadline, old, new, report, sent)

    # ---------------------------------------------------------------- request repair (E-K)
    def _plan(self, spec, td, args, inc, old, new, report, sent) -> RepairPlan:
        # a run that requires the LLM never reuses a cached repair: the LLM must propose this one
        cached = None if self.llm_required else self.storage.get_mapping(spec.name, td.schema_signature, new.signature())
        if cached:
            plan = RepairPlan.model_validate({**cached, "source": "reuse"})
            inc.add("plan", "Reusing a previously validated repair for this exact schema change (revalidated below)",
                    "info", {"plan": plan.model_dump(include={"mappings", "header_sources"})})
            return plan
        plan = plan_request(td, spec, new, args, self.creds, self.s.auto_min_similarity)
        inc.add("plan", f"Rules planner: {len(plan.mappings)} mapping(s), {len(plan.unresolved)} unresolved"
                + (" (cross-check only: this run requires the LLM to propose the repair)" if self.llm_required else ""),
                "ok" if not plan.unresolved else "warn",
                {"evidence": plan.evidence, "risks": plan.compatibility_risks, "unresolved": plan.unresolved,
                 "mappings": [m.model_dump() for m in plan.mappings]})
        if self.llm_required:
            kept = [m for m in plan.mappings if m.evidence == "unchanged field"]
            targets = [loc for loc in new.fields if loc not in {m.target_field for m in kept}]
            headers_unresolved = [u for u in plan.unresolved if u.startswith("header:")]
            inc.add("plan", f"LLM required for {len(targets)} changed field(s): {', '.join(targets)[:200]}", "info")
            usable = getattr(self.llm, "real", False) or getattr(self.llm, "test_double", False)
            if not (usable and self.llm.configured()):
                inc.add("plan", "No real LLM is configured; not falling back to rules or a mock", "fail")
                return plan.model_copy(update={"source": "llm", "mappings": kept, "confidence": 0.0,
                                               "unresolved": [t for t in targets if new.fields[t].required]
                                               + headers_unresolved})
            plan = plan.model_copy(update={"source": "llm", "mappings": kept, "confidence": 0.0,
                                           "unresolved": [t for t in targets if new.fields[t].required]
                                           + headers_unresolved})
            field_unresolved = targets
        else:
            field_unresolved = [u for u in plan.unresolved if not u.startswith("header:")]
        if field_unresolved and self.llm.configured():
            used = {m.source_arg for m in plan.mappings}
            ctx = {"tool": spec.name, "drift_summary": report.summary,
                   "unresolved_fields": [{"field": loc, "type": new.fields[loc].label(),
                                          "description": new.fields[loc].description,
                                          # documented rename, quotable verbatim as "x-renamed-from: <name>"
                                          "x-renamed-from": new.fields[loc].renamed_from,
                                          "quotable_evidence": [q for q in (
                                              new.fields[loc].description,
                                              f"x-renamed-from: {new.fields[loc].renamed_from}"
                                              if new.fields[loc].renamed_from else None) if q]}
                                         for loc in field_unresolved if loc in new.fields],
                   "candidate_arguments": [{"name": a, "type": py_type(v)} for a, v in args.items() if a not in used],
                   "api_errors": [{"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")}
                                  for e in (sent.body.get("detail") if isinstance(sent.body, dict)
                                            and isinstance(sent.body.get("detail"), list) else [])]}
            res = self.llm.plan(ctx)
            inc.llm = res.meta()
            label = f"{res.provider} ({res.model})"
            if res.ok and res.plan:
                inc.add("plan", f"LLM planner {label} proposed {sum(1 for m in res.plan.mappings if m.source_arg)} "
                                f"mapping(s), confidence {res.plan.confidence:.2f}", "info",
                        {"diagnosis": res.plan.diagnosis, "risks": res.plan.compatibility_risks,
                         "mappings": [m.model_dump() for m in res.plan.mappings], "real_model": res.real})
                extra = [m for m in res.plan.mappings if m.source_arg and m.target_field in field_unresolved]
                plan = plan.model_copy(update={
                    "source": "llm", "mappings": plan.mappings + extra,
                    "unresolved": [u for u in plan.unresolved if u not in {m.target_field for m in extra}],
                    "confidence": res.plan.confidence, "diagnosis": res.plan.diagnosis,
                    "evidence": plan.evidence + res.plan.evidence,
                    "compatibility_risks": plan.compatibility_risks + res.plan.compatibility_risks})
            else:
                inc.add("plan", f"LLM planner {label} unavailable: {res.error_category}", "warn",
                        {"error_category": res.error_category})
        return plan

    def _repair_request(self, spec, td, args, inc, idem, deadline, old, new, report, sent):
        holder = new_holder()
        seen: set[str] = set()
        cache_entry = (None, None, None)
        for attempt in range(1, self.s.max_repair_attempts + 1):
            inc.repair_attempts = attempt
            if not self.storage.acquire_lock(spec.name, holder):
                return self._stop(inc, "schema_drift", "another worker is repairing this tool; not repairing twice")
            try:
                current = self.storage.current(spec.name)
                if current.version != td.version and current.schema_signature == new.signature():
                    inc.add("apply", f"Another worker already repaired this tool (v{current.version}); reusing it", "info")
                    candidate = current
                else:
                    plan = self._plan(spec, td, args, inc, old, new, report, sent)
                    inc.plan = plan.model_dump()
                    vres, candidate = validate(plan, td, spec, new, args, self.creds, self.s.auto_min_similarity,
                                               self.s.llm_min_confidence)
                    inc.validation = vres.model_dump()
                    inc.add("validate", "Repair accepted by independent validation" if vres.accepted
                            else "Repair rejected: " + "; ".join(vres.rejected_reasons)[:300],
                            "ok" if vres.accepted else "fail", {"checks": vres.checks})
                    if not vres.accepted:
                        reason = ("Stopped safely: the change is ambiguous or unsafe to repair automatically ("
                                  + "; ".join(vres.rejected_reasons)[:400] + ")")
                        self.storage.event("repair_rejected", spec.name, {"reasons": vres.rejected_reasons,
                                                                          "source": plan.source},
                                           inc.correlation_id, inc.simulated)
                        return self._stop(inc, "schema_drift", reason)
                    sig = hashlib.sha256(json.dumps(candidate.request_map, sort_keys=True).encode()).hexdigest()
                    if sig in seen:
                        return self._stop(inc, "schema_drift", "the same repair was already tried; stopping to avoid a loop")
                    seen.add(sig)
                    self.storage.save_schema(new)          # the new version's schema must be on record
                    try:
                        candidate = self.storage.apply_repair(candidate, td.version,
                                                              "reuse" if plan.source == "reuse" else "repair",
                                                              inc.id, inc.correlation_id, inc.simulated)
                    except ConflictError as e:
                        return self._stop(inc, "schema_drift", f"registry changed during repair ({e}); not applied")
                    inc.add("apply", f"Tool updated v{td.version} -> v{candidate.version} (previous version kept)",
                            "ok", {"patch": candidate.patch})
                    cache_entry = (plan, td.schema_signature, new.signature())
            finally:
                self.storage.release_lock(spec.name, holder)
            inc.to_version = candidate.version
            sent2 = self._send(spec, candidate, args, inc, idem, f"retry with v{candidate.version}", deadline)
            inc.retries += sent2.retries + 1
            if sent2.error_kind:
                self._rollback(spec, candidate, inc, "the repaired call did not complete")
                c = classify_exception(sent2.error_kind) if not sent2.error_kind.startswith("credential:") else None
                return self._stop(inc, inc.category, (c.explanation if c else sent2.error), "rolled_back")
            if sent2.status < 400:
                v = self.verify(spec, candidate, sent2.body, sent2.request)
                inc.verification = v.model_dump()
                if v.ok:
                    inc.add("verify", "Original task verified: success status and the API applied the sent values",
                            "ok", {"checks": v.checks})
                    plan, f, t = cache_entry
                    if plan is not None and plan.source != "reuse":
                        self.storage.put_mapping(spec.name, f, t, plan.model_dump(exclude={"source"}))
                    inc.status, inc.zero_touch, inc.result = "recovered", True, sent2.body
                    inc.reason = f"recovered autonomously with tool v{candidate.version}"
                    inc.add("outcome", inc.reason, "ok")
                    return sent2.body
                if v.contract_drift:
                    return self._repair_response(spec, candidate, args, inc, sent2)
                inc.add("verify", "Verification failed: " + "; ".join(c["detail"] for c in v.checks if not c["ok"]),
                        "fail", {"checks": v.checks})
                self._rollback(spec, candidate, inc, "verification failed")
                return self._stop(inc, "schema_drift", "The repaired call was accepted but the task was not confirmed; "
                                  "the repair was rolled back", "rolled_back")
            cls = classify_response(sent2.status, sent2.body, True, report.touched())
            inc.add("classify", f"Repaired call still failed: {cls.category}", "fail", {"status": sent2.status})
            self._rollback(spec, candidate, inc, f"repaired call failed with HTTP {sent2.status}")
            if cls.category != "schema_drift" or attempt == self.s.max_repair_attempts:
                return self._stop(inc, inc.category, "Repair did not work and was rolled back: " + cls.explanation,
                                  "rolled_back")
            td = self.storage.current(spec.name)
        return self._stop(inc, "schema_drift", f"stopped after {self.s.max_repair_attempts} repair attempts")

    def _rollback(self, spec, candidate: ToolDef, inc: Incident, reason: str):
        restored = self.storage.rollback(spec.name, candidate.version, reason, inc.correlation_id, inc.simulated)
        inc.rolled_back = restored is not None
        inc.add("rollback", f"Rolled back v{candidate.version} -> v{restored.version}: {reason}" if restored
                else f"Rollback of v{candidate.version} not possible", "warn" if restored else "fail",
                {"restored_version": restored.version if restored else None})

    # ---------------------------------------------------------------- response repair (J)
    def _repair_response(self, spec, td, args, inc, sent):
        new = self.discover(spec)
        if not isinstance(new, Operation):
            return self._stop(inc, "schema_unavailable", EXPLAIN["schema_unavailable"])
        inc.category = "schema_drift"
        old = self.storage.get_schema(td.schema_signature)
        report = diff(old, new) if old else None
        if report:
            inc.drift = report.model_dump()
            inc.add("diff", f"Response drift report: {report.summary}", "info")
        plan = plan_response(td, new)
        inc.plan = plan.model_dump()
        vres, cand = validate_response(plan, td, new, sent.body)
        inc.validation = vres.model_dump()
        inc.add("validate", "Response mapping accepted" if vres.accepted else
                "Response mapping rejected: " + "; ".join(vres.rejected_reasons)[:300],
                "ok" if vres.accepted else "fail", {"checks": vres.checks})
        if not vres.accepted:
            return self._stop(inc, "schema_drift", "Response schema changed and no evidence-backed mapping exists; "
                              "the call's outcome must be checked by a person (it was NOT resent)")
        self.storage.save_schema(new)
        try:
            cand = self.storage.apply_repair(cand, td.version, "repair", inc.id, inc.correlation_id, inc.simulated)
        except ConflictError as e:
            return self._stop(inc, "schema_drift", f"registry changed during repair ({e})")
        inc.to_version = cand.version
        inc.add("apply", f"Response mapping updated v{td.version} -> v{cand.version}", "ok", {"patch": cand.patch})
        v = self.verify(spec, cand, sent.body, sent.request)        # same response: nothing is sent twice
        inc.verification = v.model_dump()
        if v.ok:
            inc.status, inc.zero_touch, inc.result = "recovered", True, sent.body
            inc.reason = f"response schema change handled with tool v{cand.version} (no request resent)"
            inc.add("verify", "Original task verified with the new response mapping", "ok", {"checks": v.checks})
            inc.add("outcome", inc.reason, "ok")
            return sent.body
        self._rollback(spec, cand, inc, "verification failed after response remap")
        return self._stop(inc, "schema_drift", "Response remap did not verify the task; rolled back", "rolled_back")

    # ---------------------------------------------------------------- analysis without execution
    def analyze(self, tool: str, args: dict, observed_status: int | None = None, observed_body=None) -> dict:
        """Classify an observed failure and propose a validated repair. Sends nothing, changes nothing."""
        spec = self.storage.tool(tool)
        if spec is None:
            raise EngineError(f"unknown tool {tool}")
        td = self.storage.current(tool)
        old = self.storage.get_schema(td.schema_signature)
        new = self.discover(spec)
        if isinstance(new, str):
            return {"classification": Classification(category="method_changed", retryable=False,
                                                     explanation=EXPLAIN["method_changed"]).model_dump()}
        report = diff(old, new) if new is not None else None
        out: dict = {"tool": tool, "current_version": td.version, "schema_available": new is not None,
                     "drift": report.model_dump() if report else None}
        if observed_status is not None:
            out["classification"] = classify_response(observed_status, observed_body, report.changed if report else None,
                                                      report.touched() if report else None).model_dump()
        if report is None or not report.changed:
            out["proposed_repair"] = None
            out["note"] = "no authoritative schema change detected" if report else EXPLAIN["schema_unavailable"]
            return out
        inc = Incident(tool=tool, simulated=spec.simulated)
        plan = self._plan(spec, td, args, inc, old, new, report, type("S", (), {"body": observed_body})())
        vres, cand = validate(plan, td, spec, new, args, self.creds, self.s.auto_min_similarity, self.s.llm_min_confidence)
        out.update({"proposed_repair": plan.model_dump(), "validation": vres.model_dump(),
                    "would_apply": cand.describe() if cand else None, "llm": inc.llm,
                    "trace": [s.model_dump() for s in inc.timeline]})
        return redact(out)
