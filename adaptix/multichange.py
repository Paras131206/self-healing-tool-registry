"""Multi-Change Simulation Lab: several schema changes in ONE experiment, then automatic recovery.

Two controlled targets, one runner (the existing pipeline does the work):

* "service" - ADAPTIX's controlled test service (REAL_API_TEST). A customer-update API is served over real HTTP
  inside the ADAPTIX runtime (127.0.0.1 there is the server's own machine, never a visitor's laptop). The change
  set is applied by replacing the API that this one experiment serves; nothing else is touched. Available on the
  deployed site and locally. The drift is CONTROLLED (we inject it); the HTTP calls, schema reads and repairs are
  real.
* "local" - the LOCAL DEMO SQLite database and its customer API (only with ADAPTIX_LOCAL_DEMO=1 on a developer
  machine). The changes are real migrations of the demo database.

Every experiment: validate the change set -> baseline call (verified) -> apply all changes together -> the SAME
call -> classify, read the live schema, field-level diff -> the selected repair engine proposes (rules, real
Gemini, or a genuinely local model; never a substitute) -> independent validation -> versioned patch -> retry ->
business verification -> rollback when verification fails. Each change then gets its own honest status.
Nothing a model writes is executed: it can only propose field mappings with allowlisted transforms.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass

from . import evidence as evidence_mod
from .demo import Jobs, _event, public
from .llm import NoProvider, is_live
from .models import ToolSpec, now_iso
from .security import redact, scrub

NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{1,30}(\.[a-z][a-z0-9_]{1,30})?$")
ENUM_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,20}$")
KINDS = ("rename", "retype", "add_optional", "add_required", "remove", "enum")
KIND_LABEL = {"rename": "Rename a field", "retype": "Change a field type", "add_optional": "Add an optional field",
              "add_required": "Add a required field (no default)", "remove": "Remove a field",
              "enum": "Change allowed enum values"}
TYPES = ("str", "int", "float")
TYPE_LABEL = {"str": "string", "int": "integer", "float": "number"}
MAX_CHANGES = 6

# ---------------------------------------------------------------- the controlled service's baseline operation
SERVICE_PATH = "/v1/customers/update"
SERVICE_FIELDS = {
    "customer_id": {"type": "int", "description": "Unique identifier of the customer."},
    "email": {"type": "str", "description": "Customer's contact email address."},
    "tier": {"type": "enum[standard|gold|platinum]", "description": "Loyalty tier."},
    "address.city": {"type": "str", "description": "City of the customer's billing address."},
    "notes": {"type": "str?", "description": "Optional free-text note (not sent by this caller)."},
}
SERVICE_ARGS = {"customer_id": 101, "email": "asha.verma@example.com", "tier": "gold", "address_city": "Pune"}
GUIDED = {
    "two_renames": {"title": "Two documented renames", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "rename", "field": "email", "new": "contact_email"}]},
    "rename_retype": {"title": "Rename + type change", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "retype", "field": "customer_id", "to": "str"}]},
    "rename_required": {"title": "Rename + new required field (one unsafe change)", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "add_required", "field": "region_code", "to": "str"}]},
    "remove_optional_rename": {"title": "Remove an unused field + rename", "changes": [
        {"kind": "remove", "field": "notes"},
        {"kind": "rename", "field": "customer_id", "new": "client_id"}]},
    "nested_enum": {"title": "Nested rename + enum change (one unsafe change)", "changes": [
        {"kind": "rename", "field": "address.city", "new": "town"},
        {"kind": "enum", "field": "tier", "values": "standard|GOLD|platinum"}]},
}

# ---------------------------------------------------------------- the local demo database's allowed changes
LOCAL_RENAMABLE = ("customer_id", "city", "created_on")
LOCAL_REMOVABLE = ("city", "email", "created_on")
LOCAL_GUIDED = {
    "key_and_city": {"title": "Rename the key and the city column", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "rename", "field": "city", "new": "billing_city"}]},
    "rename_retype_optional": {"title": "Rename + type widening + optional column", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "retype", "field": "loyalty_points", "to": "float"},
        {"kind": "add_optional", "field": "nickname", "to": "str"}]},
    "rename_required": {"title": "Rename + required lookup field (one unsafe change)", "changes": [
        {"kind": "rename", "field": "customer_id", "new": "client_id"},
        {"kind": "add_required", "field": "region_code", "to": "str"}]},
    "remove_email_rename": {"title": "Remove email + rename (one unsafe change)", "changes": [
        {"kind": "remove", "field": "email"},
        {"kind": "rename", "field": "customer_id", "new": "client_id"}]},
}


class ChangeError(ValueError):
    pass


@dataclass(frozen=True)
class Change:
    kind: str
    field: str
    new: str | None = None
    to: str | None = None
    values: str | None = None
    documented: bool = True

    @classmethod
    def parse(cls, d: dict) -> "Change":
        if not isinstance(d, dict) or set(d) - {"kind", "field", "new", "to", "values", "documented"}:
            raise ChangeError("each change is {kind, field, new?, to?, values?, documented?}")
        kind, fld = d.get("kind"), d.get("field")
        if kind not in KINDS:
            raise ChangeError(f"unknown change kind {kind!r}")
        if not isinstance(fld, str) or not FIELD.match(fld):
            raise ChangeError(f"{fld!r} is not a valid field name")
        new, to, values = d.get("new"), d.get("to"), d.get("values")
        if kind == "rename" and (not isinstance(new, str) or not NAME.match(new)):
            raise ChangeError(f"rename of {fld}: {new!r} is not a valid new name (lowercase, digits, _)")
        if kind in ("retype", "add_optional", "add_required") and to not in TYPES:
            raise ChangeError(f"{fld}: type must be one of {', '.join(TYPES)}")
        if kind == "enum":
            vals = (values or "").split("|")
            if len(vals) < 2 or len(vals) > 8 or not all(ENUM_VALUE.match(v) for v in vals) or len(set(vals)) != len(vals):
                raise ChangeError(f"{fld}: enum values must be 2-8 distinct simple words separated by |")
        return cls(kind, fld, new if kind == "rename" else None, to if kind in ("retype", "add_optional",
                   "add_required") else None, values if kind == "enum" else None, bool(d.get("documented", True)))

    def label(self) -> str:
        if self.kind == "rename":
            return f"rename {self.field} -> {self.new}" + ("" if self.documented else " (undocumented)")
        if self.kind == "retype":
            return f"{self.field}: type -> {TYPE_LABEL[self.to]}"
        if self.kind in ("add_optional", "add_required"):
            return f"add {'optional' if self.kind == 'add_optional' else 'required'} {self.field} ({TYPE_LABEL[self.to]})"
        if self.kind == "remove":
            return f"remove {self.field}"
        return f"{self.field}: allowed values -> {self.values.replace('|', ', ')}"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "field": self.field, "new": self.new, "to": self.to, "values": self.values,
                "documented": self.documented, "label": self.label()}


def parse_changes(raw: list) -> list[Change]:
    if not isinstance(raw, list) or not raw:
        raise ChangeError("select at least one change")
    if len(raw) > MAX_CHANGES:
        raise ChangeError(f"at most {MAX_CHANGES} changes per experiment")
    out = [Change.parse(d) for d in raw]
    if len(set(out)) != len(out):
        raise ChangeError("the same change is selected twice")
    return out


def _type_of(spec: dict) -> str:
    return spec["type"].rstrip("?")


def combine(base: dict, changes: list[Change], *, removable=None, renamable=None, retypable=None,
            enum_ok: bool = True) -> tuple[dict, dict]:
    """Apply a validated change set to a field map (keys are field paths). Returns (new fields, old->new key).
    Raises ChangeError for anything invalid, contradictory or unsupported by the target."""
    by_field: dict[str, list[Change]] = {}
    for c in changes:
        by_field.setdefault(c.field, []).append(c)
    for fld, cs in by_field.items():
        kinds = [c.kind for c in cs]
        if any(k in ("add_optional", "add_required") for k in kinds):
            if fld in base:
                raise ChangeError(f"{fld} already exists; it cannot be added")
            if len(cs) > 1:
                raise ChangeError(f"{fld} is added more than once")
            continue
        if fld not in base:
            raise ChangeError(f"{fld} is not a field of the current schema")
        if "remove" in kinds and len(cs) > 1:
            raise ChangeError(f"{fld} is removed and also changed - contradictory")
        for k in ("rename", "retype", "enum"):
            if kinds.count(k) > 1:
                raise ChangeError(f"{fld}: more than one '{k}' change")
        if "retype" in kinds and "enum" in kinds:
            raise ChangeError(f"{fld}: a type change and an enum change together are not supported")
    out, key_map = {}, {}
    for fld, spec in base.items():
        cs = {c.kind: c for c in by_field.get(fld, [])}
        if "remove" in cs:
            if removable is not None and fld not in removable:
                raise ChangeError(f"{fld} cannot be removed in this environment")
            key_map[fld] = None
            continue
        spec = dict(spec)
        key = fld
        if "rename" in cs:
            c = cs["rename"]
            if renamable is not None and fld not in renamable:
                raise ChangeError(f"{fld} cannot be renamed in this environment")
            parent = fld.rpartition(".")[0]
            key = f"{parent}.{c.new}" if parent else c.new
            spec["description"] = spec.get("description", "") + (f" (formerly {fld.split('.')[-1]})" if c.documented else "")
            if c.documented:
                spec["renamed_from"] = fld.split(".")[-1]
            else:
                spec.pop("renamed_from", None)
        if "retype" in cs:
            c = cs["retype"]
            if _type_of(spec).startswith("enum["):
                raise ChangeError(f"{fld} is an enum; change its allowed values instead")
            if retypable is not None and fld not in retypable:
                raise ChangeError(f"{fld} cannot change type in this environment")
            if _type_of(spec) == c.to:
                raise ChangeError(f"{fld} is already {TYPE_LABEL[c.to]}")
            spec["type"] = c.to + ("?" if spec["type"].endswith("?") else "")
        if "enum" in cs:
            if not enum_ok or not _type_of(spec).startswith("enum["):
                raise ChangeError(f"{fld} is not an enum field" if enum_ok else "enum changes are not supported here")
            spec["type"] = f"enum[{cs['enum'].values}]" + ("?" if spec["type"].endswith("?") else "")
        if key in out:
            raise ChangeError(f"two fields would be called {key}")
        out[key] = spec
        key_map[fld] = key
    for c in changes:
        if c.kind in ("add_optional", "add_required"):
            if c.field in out:
                raise ChangeError(f"{c.field} would collide with another field")
            out[c.field] = {"type": c.to + ("?" if c.kind == "add_optional" else ""),
                            "description": "New field" + (" (optional)." if c.kind == "add_optional"
                                                          else "; now required, no default.")}
    return out, key_map


def schema_rows(fields: dict) -> list[dict]:
    return [{"field": k, "type": spec["type"].rstrip("?"), "required": not spec["type"].endswith("?"),
             "documentation": spec.get("description", "") + (f" · x-renamed-from: {spec['renamed_from']}"
                                                            if spec.get("renamed_from") else "")}
            for k, spec in fields.items()]


def diff_rows(before: dict, after: dict, key_map: dict) -> list[dict]:
    rows = []
    for old, new in key_map.items():
        b = before[old]
        if new is None:
            rows.append({"change": "removed", "field": old, "before": b["type"], "after": "—"})
        elif new != old:
            rows.append({"change": "renamed", "field": f"{old} -> {new}", "before": b["type"],
                         "after": after[new]["type"]})
        elif after[new]["type"] != b["type"]:
            rows.append({"change": "type changed", "field": old, "before": b["type"], "after": after[new]["type"]})
    for k in after:
        if k not in key_map.values():
            rows.append({"change": "added", "field": k, "before": "—", "after": after[k]["type"]})
    return rows


# ---------------------------------------------------------------- spec -> served API (re-validated server-side)
SPEC_TYPE = re.compile(r"^(str|int|float|enum\[[A-Za-z][A-Za-z0-9_]{0,20}(\|[A-Za-z][A-Za-z0-9_]{0,20}){1,7}\])\??$")


def build_spec_app(spec: dict):
    """Build the FastAPI app the test service serves for one experiment. Only the fixed path and plain field specs
    are accepted: no code, no URLs, no SQL."""
    from .mock_api import make_app
    if not isinstance(spec, dict) or spec.get("path") != SERVICE_PATH:
        raise ChangeError("unsupported spec")
    fields = spec.get("fields")
    if not isinstance(fields, dict) or not 1 <= len(fields) <= 12:
        raise ChangeError("unsupported field set")
    clean = {}
    for k, v in fields.items():
        if not isinstance(k, str) or not FIELD.match(k) or not isinstance(v, dict) or set(v) - {"type", "description",
                                                                                             "renamed_from"}:
            raise ChangeError(f"unsupported field {k!r}")
        if not isinstance(v.get("type"), str) or not SPEC_TYPE.match(v["type"]):
            raise ChangeError(f"unsupported type for {k}")
        if v.get("renamed_from") is not None and not NAME.match(str(v["renamed_from"])):
            raise ChangeError(f"unsupported renamed_from for {k}")
        clean[k] = {"type": v["type"], "description": str(v.get("description", ""))[:200],
                    **({"renamed_from": v["renamed_from"]} if v.get("renamed_from") else {})}
    return make_app(SERVICE_PATH, clean, title="ADAPTIX Multi-Change Lab customer API")


# ---------------------------------------------------------------- per-change outcome (from the stored incident)
def _arg(fld: str) -> str:
    return fld.replace(".", "_")


def change_status(c: Change, key_map: dict, res: dict, inc: dict | None, verified: bool,
                  request_fields: set[str], caller_sent: set[str], checked_response: set[str]) -> dict:
    """What happened to ONE change, derived from the incident record (never assumed)."""
    plan = (inc or {}).get("plan") or {}
    maps = {m["target_field"]: m for m in plan.get("mappings", []) if m.get("source_arg")}
    applied = bool(inc and inc.get("to_version") and inc.get("to_version") != inc.get("from_version")
                   and not inc.get("rolled_back") and inc.get("status") == "recovered")
    in_request = c.field in request_fields
    if c.kind == "add_optional":
        return {"status": "compatible", "detail": "optional: the caller need not send it"}
    if c.kind == "add_required":
        return {"status": "refused", "detail": "required with no documented default: only the user can supply this "
                                                "value, and ADAPTIX never invents business data"}
    if c.kind == "remove":
        if in_request and c.field in caller_sent:
            return {"status": "refused", "detail": "the caller's value for this field would be silently dropped"}
        if c.field in checked_response:
            return {"status": "refused", "detail": "data the caller relies on is no longer returned; a removed field "
                                                   "cannot be reconstructed"}
        return {"status": "compatible" if verified else "unverified", "detail": "the caller does not use this field"}
    if c.kind == "enum":
        return {"status": "refused", "detail": "allowed values changed; there is no allowlisted, evidence-backed "
                                               "value mapping, so the old value is not rewritten"}
    new_key = key_map.get(c.field) or c.field
    loc = f"body:{new_key}"
    if not in_request:
        return {"status": "compatible" if verified else "unverified",
                "detail": "response-only change; the caller's verified fields are unaffected"}
    m = maps.get(loc)
    rolled = bool(inc and inc.get("rolled_back"))
    if c.kind == "rename" and m and m["source_arg"] == _arg(c.field):
        how = f"{m['source_arg']} -> {loc}" + (f" [{m['transform']}]" if m.get("transform") not in (None, "none") else "")
        return {"status": "repaired" if applied and verified else "applied, then rolled back" if rolled
                else "proposed, not applied",
                "detail": how + (f" · evidence: {m.get('evidence', '')[:80]}" if m.get("evidence") else "")}
    if c.kind == "retype" and m and m.get("transform") not in (None, "none"):
        return {"status": "repaired" if applied and verified else "applied, then rolled back" if rolled
                else "proposed, not applied",
                "detail": f"lossless conversion {m['transform']} for {loc}"}
    if c.kind == "retype" and res.get("status") == "ok":
        return {"status": "compatible", "detail": "the API accepts the caller's value"}
    return {"status": "refused", "detail": "no evidence-backed mapping was proposed and accepted for this change"}


PARTIAL_TRIGGER = ("PARTIAL - SAFELY REFUSED", "FAILED (rolled back)")
REVERTED = {"status": "reverted", "detail": "unsafe for automatic repair: reverted on the controlled target and NOT "
                                            "repaired (possible only because ADAPTIX controls this target; a real "
                                            "provider's change cannot be undone)"}


def split_safe(changes: list[Change], statuses: list[dict]) -> tuple[list[Change], list[Change]]:
    """Changes ADAPTIX could handle safely vs. changes it refused, from the first attempt's per-change outcome."""
    unsafe = [c for c, s in zip(changes, statuses) if s["status"] == "refused"]
    return [c for c in changes if c not in unsafe], unsafe


def final_state(res: dict, inc: dict | None, verified: bool, statuses: list[dict]) -> str:
    status = res.get("status")
    if any(s["status"] == "reverted" for s in statuses):
        return "PARTIAL RECOVERY" if verified else ("FAILED (rolled back)" if (inc or {}).get("rolled_back")
                                                    else "SAFELY REFUSED")
    if status == "recovered" and verified:
        return "VERIFIED RECOVERY"
    if status == "ok" and verified:
        return "VERIFIED (no repair needed)"
    if status == "rolled_back" or (inc or {}).get("rolled_back"):
        return "FAILED (rolled back)"
    if status == "stopped":
        partly = any(s["status"] in ("proposed, not applied", "compatible") for s in statuses)
        return "PARTIAL - SAFELY REFUSED" if partly else "SAFELY REFUSED"
    if status in ("ok", "recovered"):
        return "UNVERIFIED"
    return "FAILED"


# ---------------------------------------------------------------- the runner
class MultiChangeLab:
    """Owns the Multi-Change jobs (one at a time per process) and the per-experiment reset."""

    def __init__(self, state):
        self.state = state
        self.jobs = Jobs()

    # ---- catalog / preview
    def catalog(self) -> dict:
        targets = [{"key": "service", "title": "Controlled test API (ADAPTIX's own service)",
                    "note": "real HTTP inside the ADAPTIX server runtime; the drift is injected by ADAPTIX"}]
        engines = [{"key": "none", "title": "Rules only (deterministic)"}]
        if is_live(self.state.llm):
            engines.append({"key": "gemini", "title": f"Gemini · {self.state.llm.status().get('model')} (real)"})
        out = {"targets": targets, "engines": {"service": engines}, "kinds": KIND_LABEL, "types": TYPE_LABEL,
               "service": {"fields": schema_rows(SERVICE_FIELDS), "field_names": list(SERVICE_FIELDS),
                           "args": SERVICE_ARGS, "guided": GUIDED, "path": SERVICE_PATH}}
        lab = self.state.local_lab
        if lab is not None:
            targets.append({"key": "local", "title": "Local demo database (this machine)",
                            "note": "real SQLite migrations + the local customer API"})
            out["engines"]["local"] = [{"key": "none", "title": "Rules only (deterministic)"},
                                       {"key": "local", "title": f"Local LLM · {lab.llm.model} (Ollama, this machine)"}]
            try:
                base = local_fields(lab)
                changed = [m for m in lab.db.migrations() if m["kind"] != "seed"]
                out["local"] = {"fields": schema_rows(base), "field_names": list(base), "guided": LOCAL_GUIDED,
                                "args": {"customer_id": 101}, "at_baseline": not changed,
                                "changes_applied": [m["kind"] for m in changed]}
            except Exception as e:
                out["local"] = {"error": scrub(str(e))[:160]}
        return out

    def preview(self, target: str, raw: list) -> dict:
        try:
            changes = parse_changes(raw)
            if target == "service":
                before = SERVICE_FIELDS
                after, key_map = combine(before, changes)
            elif target == "local" and self.state.local_lab is not None:
                before = local_fields(self.state.local_lab)
                after, key_map = combine_local(before, changes)
            else:
                raise ChangeError("unknown target")
        except ChangeError as e:
            return {"valid": False, "problems": [str(e)]}
        return {"valid": True, "problems": [], "changes": [c.as_dict() for c in changes],
                "before": schema_rows(before), "after": schema_rows(after), "diff": diff_rows(before, after, key_map)}

    # ---- run
    def start(self, target: str, raw: list, engine: str, confirm: bool, session: str, partial: bool = True) -> dict:
        if not confirm:
            raise ChangeError("confirmation required: Apply Changes & Start Recovery")
        if not re.match(r"^[a-z0-9]{6,32}$", session or ""):
            raise ChangeError("invalid session id")
        pv = self.preview(target, raw)
        if not pv["valid"]:
            raise ChangeError("; ".join(pv["problems"]))
        if target == "service" and engine not in ("none", "gemini"):
            raise ChangeError("the controlled test API uses rules or real Gemini")
        if target == "local" and engine not in ("none", "local"):
            raise ChangeError("the local demo uses rules or the local model, never a cloud model")
        if engine == "gemini" and not is_live(self.state.llm):
            raise ChangeError("Gemini is not configured on this server (GEMINI_API_KEY); nothing was started")
        with self.jobs.lock:
            if self.jobs.running():
                raise ChangeError("another experiment is running; try again in a few seconds")
            job = {"id": "mcx_" + uuid.uuid4().hex[:10], "target": target, "engine": engine, "session": session,
                   "partial": bool(partial),
                   "changes": pv["changes"], "preview": pv, "status": "running", "events": [], "result": None,
                   "error": None, "started_at": now_iso(), "t0": time.perf_counter(), "finished": None,
                   "_lock": threading.Lock(), "evidence": {}}
            self.jobs.jobs[job["id"]] = job
        threading.Thread(target=self._run, args=(job,), name=f"adaptix-{job['id']}", daemon=True).start()
        return public(self.jobs.get(job["id"]))

    def job(self, job_id: str, session: str) -> dict | None:
        j = self.jobs.get(job_id)
        return public(j) if j and j["session"] == session else None

    def _run(self, job: dict):
        try:
            changes = [Change.parse({k: v for k, v in c.items() if k != "label"}) for c in job["changes"]]
            if job["target"] == "service":
                self._run_service(job, changes)
            else:
                self._run_local(job, changes)
        except Exception as e:                                   # recorded, never swallowed
            job["error"] = f"{type(e).__name__}: {scrub(str(e))[:200]}"
            _event(job, "Outcome", "fail", "The experiment failed: " + job["error"])
            job["result"] = job["result"] or {"final": "FAILED", "reason": job["error"]}
        finally:
            job["finished"] = time.perf_counter()
            job["status"] = "done"

    def _engine_llm(self, job, engine_obj, local_llm=None):
        if job["engine"] == "gemini":
            engine_obj.set_llm(self.state.llm)
            engine_obj.llm_required = True
            return f"REAL Gemini · {self.state.llm.status().get('model')}"
        if job["engine"] == "local":
            engine_obj.set_llm(local_llm)
            engine_obj.llm_required = True
            return f"LOCAL LLM · {local_llm.model} (Ollama)"
        engine_obj.set_llm(NoProvider())
        engine_obj.llm_required = False
        return "RULES ONLY (no model call)"

    def _run_service(self, job: dict, changes: list[Change]):
        st = self.state
        after, key_map = combine(SERVICE_FIELDS, changes)
        eng = st.test_engine
        with st.test_lock:
            svc = st.test_service_started()
            name = "mc-" + job["id"][4:]
            job["tool"] = name
            _event(job, "Selected changes", "info", "; ".join(c.label() for c in changes))
            svc.install_spec(name, {"path": SERVICE_PATH, "fields": SERVICE_FIELDS})
            eng.register(ToolSpec(name=name, base_url=f"{svc.url}/m/{name}", path=SERVICE_PATH, method="POST",
                                  description="Update one customer (Multi-Change Lab)",
                                  provider="ADAPTIX controlled test service (Multi-Change Lab)"))
            id_key = {"v": "customer_id"}

            def business(body):
                rec = body.get("received") if isinstance(body, dict) else None
                if not isinstance(rec, dict):
                    return [{"name": "applied record returned", "ok": False, "detail": "no 'received' object"}]
                node = rec
                for part in id_key["v"].split("."):
                    node = node.get(part) if isinstance(node, dict) else None
                return [{"name": "customer identity", "ok": node is not None and str(node) == "101",
                         "detail": f"{id_key['v']} = {node!r} (expected 101)"}]
            eng.business_check = business
            try:
                base = eng.execute(name, dict(SERVICE_ARGS), scenario=f"multichange:{job['id']}:baseline")
                job["evidence"]["baseline"] = {"status": base["status"], "result": redact(base.get("result")),
                                               "ms": base["duration_ms"]}
                if base["status"] != "ok":
                    _event(job, "Baseline", "fail", f"Baseline did not verify ({base['status']}); no change applied")
                    job["result"] = {"final": "NOT RUN", "reason": "baseline failed", "mutated": False}
                    return
                _event(job, "Baseline", "ok", f"Original update for customer 101 verified ({base['duration_ms']} ms)")
                svc.install_spec(name, {"path": SERVICE_PATH, "fields": after})
                id_key["v"] = key_map.get("customer_id") or "customer_id"
                _event(job, "Changes applied", "warn", f"CONTROLLED DRIFT · {len(changes)} change(s) now live on "
                                                       f"the test API served at /m/{name}")
                label = self._engine_llm(job, eng)
                _event(job, "Repair engine", "info", label)
                res = eng.execute(name, dict(SERVICE_ARGS), scenario=f"multichange:{job['id']}",
                                  observer=lambda s: _event(job, PHASE.get(s.stage, s.stage), s.status, s.title))
            finally:
                eng.business_check = None
                eng.set_llm(NoProvider())
                eng.llm_required = False
            kw = dict(caller_sent={"customer_id", "email", "tier", "address.city"}, checked={"customer_id"})
            self._finish(job, eng, name, res, changes, key_map, SERVICE_FIELDS, after, "service",
                         request_fields=set(SERVICE_FIELDS) | {c.field for c in changes if c.kind.startswith("add")}, **kw)
            first = job["result"]
            safe, unsafe = split_safe(changes, first["changes"])
            if not (job["partial"] and first["final"] in PARTIAL_TRIGGER and safe and unsafe):
                return
            after2, key_map2 = combine(SERVICE_FIELDS, safe)
            svc.install_spec(name, {"path": SERVICE_PATH, "fields": after2})
            id_key["v"] = key_map2.get("customer_id") or "customer_id"
            _event(job, "Unsafe changes reverted", "warn",
                   "CONTROLLED TARGET · reverted: " + "; ".join(c.label() for c in unsafe)
                   + " · kept: " + "; ".join(c.label() for c in safe))
            eng.business_check = business
            try:
                self._engine_llm(job, eng)
                res2 = eng.execute(name, dict(SERVICE_ARGS), scenario=f"multichange:{job['id']}:partial",
                                   observer=lambda s: _event(job, PHASE.get(s.stage, s.stage), s.status, s.title))
            finally:
                eng.business_check = None
                eng.set_llm(NoProvider())
                eng.llm_required = False
            self._finish(job, eng, name, res2, safe, key_map2, SERVICE_FIELDS, after2, "service",
                         request_fields=set(SERVICE_FIELDS) | {c.field for c in safe if c.kind.startswith("add")},
                         reverted=unsafe, all_changes=changes, first=first, **kw)

    def _run_local(self, job: dict, changes: list[Change]):
        lab = self.state.local_lab
        if lab is None:
            raise ChangeError("the local demo is not enabled")
        with lab.lock:
            stt = lab.status()
            m = stt["model"]
            problems = []
            if stt["database"]["status"] != "ready":
                problems.append("demo database not ready")
            if stt["service"]["status"] != "running":
                problems.append("local customer API not reachable")
            if job["engine"] == "local" and (m["server"] != "running" or not m["model_installed"]):
                problems.append(f"local model not ready: {m.get('action') or m['model']}")
            if stt["database"].get("migrations"):
                problems.append("the demo database already has a schema change - reset it first")
            if problems:
                _event(job, "Readiness", "fail", "; ".join(problems))
                job["result"] = {"final": "NOT RUN", "reason": "; ".join(problems), "mutated": False}
                return
            before = local_fields(lab)
            after, key_map = combine_local(before, changes)
            _event(job, "Selected changes", "info", "; ".join(c.label() for c in changes))
            lab.ensure_registered()
            from .localdemo.lab import ARGS, TOOL
            eng = lab.engine
            eng.business_check = lab.business_checks
            try:
                base = eng.execute(TOOL, dict(ARGS), scenario=f"multichange:{job['id']}:baseline")
                job["evidence"]["baseline"] = {"status": base["status"], "result": redact(base.get("result"))}
                if base["status"] != "ok":
                    _event(job, "Baseline", "fail", "Baseline lookup did not verify; no change applied")
                    job["result"] = {"final": "NOT RUN", "reason": "baseline failed", "mutated": False}
                    return
                _event(job, "Baseline", "ok", "LOCAL REAL EXECUTION · customer 101 returned and verified")
                backup = lab.db.backup()
                try:
                    applied = apply_local(lab.db, changes)
                except Exception as e:
                    restore_backup(lab.db, backup)
                    _event(job, "Changes applied", "fail", f"Migration failed and the backup was restored: "
                                                           f"{scrub(str(e))[:160]}")
                    job["result"] = {"final": "FAILED", "reason": "migration failed; database restored",
                                     "mutated": False}
                    return
                job["evidence"]["migrations"] = applied
                _event(job, "Changes applied", "warn", f"LOCAL REAL EXECUTION · {len(applied)} migration(s) on the "
                                                       f"demo database (backup {backup.name})")
                label = self._engine_llm(job, eng, lab.llm)
                _event(job, "Repair engine", "info", label)
                res = eng.execute(TOOL, dict(ARGS), scenario=f"multichange:{job['id']}",
                                  observer=lambda s: _event(job, PHASE.get(s.stage, s.stage), s.status, s.title))
            finally:
                eng.business_check = None
                eng.set_llm(lab.llm)
                eng.llm_required = False
            job["tool"] = TOOL
            kw = dict(caller_sent={"customer_id"}, checked={"customer_id", "full_name", "email", "loyalty_points"})
            self._finish(job, eng, TOOL, res, changes, key_map, before, after, "local",
                         request_fields={"customer_id"} | {c.field for c in changes if c.kind == "add_required"}, **kw)
            first = job["result"]
            safe, unsafe = split_safe(changes, first["changes"])
            if not (job["partial"] and first["final"] in PARTIAL_TRIGGER and safe and unsafe):
                return
            restore_backup(lab.db, backup)                     # back to the baseline, then only the safe changes
            after2, key_map2 = combine_local(before, safe)
            job["evidence"]["migrations_kept"] = apply_local(lab.db, safe)
            _event(job, "Unsafe changes reverted", "warn",
                   "LOCAL REAL EXECUTION · database restored from the backup and only the safe changes re-applied · "
                   "reverted: " + "; ".join(c.label() for c in unsafe))
            eng.business_check = lab.business_checks
            try:
                self._engine_llm(job, eng, lab.llm)
                res2 = eng.execute(TOOL, dict(ARGS), scenario=f"multichange:{job['id']}:partial",
                                   observer=lambda s: _event(job, PHASE.get(s.stage, s.stage), s.status, s.title))
            finally:
                eng.business_check = None
                eng.set_llm(lab.llm)
                eng.llm_required = False
            self._finish(job, eng, TOOL, res2, safe, key_map2, before, after2, "local",
                         request_fields={"customer_id"} | {c.field for c in safe if c.kind == "add_required"},
                         reverted=unsafe, all_changes=changes, first=first, **kw)

    def _finish(self, job, eng, tool, res, changes, key_map, before, after, target, request_fields, caller_sent,
                checked, reverted=None, all_changes=None, first=None):
        inc = eng.storage.incident(res["incident"]) if res.get("incident") else None
        incd = inc.model_dump(mode="json") if inc else None
        verification = (incd or {}).get("verification") or {}
        checks = verification.get("checks") or []
        verified = res["status"] in ("ok", "recovered") and (res["status"] == "ok" or bool(verification.get("ok")))
        by_change = {c: change_status(c, key_map, res, incd, verified, request_fields, caller_sent, checked)
                     for c in changes}
        by_change.update({c: REVERTED for c in reverted or []})
        statuses = [{**c.as_dict(), **by_change[c]} for c in (all_changes or changes)]
        final = final_state(res, incd, verified, statuses)
        tl = (incd or {}).get("timeline") or []
        llm_step = next((s for s in reversed(tl) if s["stage"] == "plan" and "LLM planner" in s["title"]), None)
        reqs = [s for s in tl if s["stage"] == "request" and isinstance(s.get("detail"), dict)]
        pick = lambda s: None if not s else {k: s["detail"].get(k) for k in    # noqa: E731
                                             ("method", "url", "sent", "status", "reply", "ms")}
        versions = [{"version": v.version, "status": v.status, "request_map": v.request_map, "patch": v.patch,
                     "incident_id": v.incident_id} for v in eng.storage.versions(tool)]
        ev = evidence_mod.build(eng, incd, run_id=job["id"], scenario=f"multichange:{target}", tool=tool,
                                expected=None, result=res, business={"ok": verified, "checks": checks},
                                extra={"multichange": {"changes": statuses, "final": final,
                                                       "engine": job["engine"]}})
        job["result"] = {
            "final": final, "status": res["status"], "reason": res.get("reason"), "incident": res.get("incident"),
            "evidence_id": ev["evidence_id"], "changes": statuses, "mutated": True, "verified": verified,
            "business_checks": checks, "recovery_ms": res.get("duration_ms"),
            "before": schema_rows(before), "after": schema_rows(after),
            "diff": (incd or {}).get("drift"), "llm": (incd or {}).get("llm"),
            "llm_step": (llm_step or {}).get("detail"), "llm_title": (llm_step or {}).get("title"),
            "plan": (incd or {}).get("plan"), "validation": (incd or {}).get("validation"),
            "failure": pick(next((s for s in reqs if s["status"] == "fail"), None)),
            "retry": pick(next((s for s in reqs if s["status"] == "ok" and s["title"].startswith("retry")), None)),
            "versions": versions, "rolled_back": (incd or {}).get("rolled_back", False), "tool": tool,
            "reverted": [c.label() for c in reverted or []],
            "first_attempt": None if first is None else {k: first.get(k) for k in (
                "final", "reason", "incident", "evidence_id", "changes", "failure", "llm", "llm_step", "plan",
                "validation", "after")}}
        _event(job, "Outcome", "ok" if final.startswith(("VERIFIED", "PARTIAL RECOVERY")) else "fail",
               f"{final}: {res.get('reason') or 'original operation verified'}")

    # ---- reset (only this experiment / this session)
    def reset(self, target: str, job_id: str, session: str, confirm: bool) -> dict:
        if not confirm:
            raise ChangeError("confirmation required")
        j = self.jobs.jobs.get(job_id)
        if j is None or j["session"] != session:
            raise ChangeError("unknown experiment for this session")
        if j["status"] == "running":
            raise ChangeError("the experiment is still running")
        if target == "local":
            out = self.state.local_lab.reset(True)
            return {"reset": True, "scope": "local demo database and LOCAL DEMO records", **out}
        name = j.get("tool")
        with self.state.test_lock:
            if name:
                self.state.test_storage.delete_tool(name)
                if self.state.test_service is not None:
                    self.state.test_service.remove(name)
        return {"reset": True, "scope": f"experiment {job_id} only (tool {name} and its test API)"}


PHASE = {"request": "Request", "classify": "Diagnosis", "discover": "Schema retrieval", "diff": "Drift detection",
         "plan": "Repair proposal", "validate": "Independent validation", "apply": "Patch applied",
         "retry": "Retry", "verify": "Business verification", "rollback": "Rollback", "outcome": "Pipeline outcome",
         "credential": "Credentials"}


# ---------------------------------------------------------------- local demo database target
def local_fields(lab) -> dict:
    cols = lab.db.columns()
    out = {}
    for c in cols:
        t = {"INTEGER": "int", "REAL": "float"}.get(c["type"], "str")
        out[c["name"]] = {"type": t + ("" if c["not_null"] else "?"), "description": c["description"]}
    return out


def combine_local(before: dict, changes: list[Change]) -> tuple[dict, dict]:
    for c in changes:
        if c.kind == "enum":
            raise ChangeError("enum changes are not available on the local demo database")
        if c.kind == "retype" and not (c.field == "loyalty_points" and c.to == "float"):
            raise ChangeError("the local demo supports one type change: loyalty_points integer -> number")
        if c.kind == "add_required" and sum(1 for x in changes if x.kind == "add_required") > 1:
            raise ChangeError("at most one new required field on the local demo")
        if "." in c.field:
            raise ChangeError("the local demo table has no nested fields")
    return combine(before, changes, removable=LOCAL_REMOVABLE, renamable=LOCAL_RENAMABLE,
                   retypable=("loyalty_points",), enum_ok=False)


def apply_local(db, changes: list[Change]) -> list[dict]:
    """Run the migrations in a fixed, safe order (adds, type, renames, removals). Fixed code only."""
    order = {"add_optional": 0, "retype": 1, "rename": 2, "add_required": 3, "remove": 4}
    done = []
    for c in sorted(changes, key=lambda c: order[c.kind]):
        if c.kind == "add_optional":
            done.append(db.add_optional_column(c.field))
        elif c.kind == "retype":
            done.append(db.change_type(c.field, "REAL"))
        elif c.kind == "rename":
            done.append(db.rename_column(c.field, c.new))
        elif c.kind == "add_required":
            done.append(db.add_required_lookup_field(c.field))
        else:
            done.append(db.remove_column(c.field))
    return done


def restore_backup(db, backup):
    import sqlite3
    if backup is None:
        return
    src, dst = sqlite3.connect(backup), sqlite3.connect(db.path)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
