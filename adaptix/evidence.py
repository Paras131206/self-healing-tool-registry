"""Judge-verifiable evidence records and an honest scorecard.

An evidence record is assembled ONLY from what was stored while the run happened: the incident timeline,
the tool's version history and, for REAL_API_TEST runs, the independent business read-back. Everything is
redacted before it is stored or exported (credentials, authorization headers, tokens, sensitive fields).
"""
from __future__ import annotations

import csv
import io
import statistics
import uuid
from urllib.parse import urlsplit, urlunsplit

from .models import now_iso
from .security import redact, scrub

SOURCE = {"LIVE_API": "real external provider over HTTPS (SSRF-guarded)",
          "REAL_API_TEST": "controlled test service: real HTTP server on 127.0.0.1 run by ADAPTIX",
          "LOCAL_DEMO": "local demo: real SQLite database and real customer API on this machine, local LLM via Ollama",
          "SIMULATION": "in-process simulated API (no network)"}


def sanitize_url(url: str) -> str:
    if url.startswith("sim://"):
        return url
    p = urlsplit(url)
    host = p.hostname or ""
    return urlunsplit((p.scheme, host + (f":{p.port}" if p.port else ""), p.path, "", ""))


def _first(timeline: list, stage: str, contains: str = "") -> dict | None:
    return next((s for s in timeline if s["stage"] == stage and contains in s["title"]), None)


def _last(timeline: list, stage: str) -> dict | None:
    return next((s for s in reversed(timeline) if s["stage"] == stage), None)


def build(engine, incident: dict | None, *, run_id: str, scenario: str | None, tool: str, expected: str | None = None,
          expected_category: str | None = None, business: dict | None = None, result: dict | None = None,
          extra: dict | None = None) -> dict:
    """incident: the stored incident (dict) or None when the call succeeded without one."""
    st = engine.storage
    env = st.environment
    spec = st.tool(tool)
    versions = st.versions(tool)
    inc = incident or {}
    tl = inc.get("timeline", [])
    first_req = _first(tl, "request")
    last_req = _last(tl, "request")
    applied = next((v for v in versions if v.incident_id == inc.get("id") and v.version > 1), None)
    llm = inc.get("llm") or {}
    outcome = inc.get("status") or ((result or {}).get("status")) or "unknown"
    if outcome == "ok":
        outcome = "succeeded_without_incident"
    verification = inc.get("verification")
    biz_ok = None if business is None else bool(business.get("ok"))
    verified = (outcome == "recovered" and (verification or {}).get("ok") is not False and biz_ok is not False)
    ev = {
        "evidence_id": "ev_" + uuid.uuid4().hex[:12], "run_id": run_id, "incident_id": inc.get("id"),
        "environment": env, "evidence_source": SOURCE[env], "recorded_at": now_iso(), "scenario": scenario,
        "api": {"tool": tool, "provider": spec.provider if spec else "", "method": spec.method if spec else None,
                "endpoint": sanitize_url((spec.base_url.rstrip("/") + spec.path) if spec else ""),
                "schema_url": sanitize_url(spec.schema_url()) if spec and spec.schema_source.kind != "none" else None},
        "schema": {"before": {"schema_signature": inc.get("old_signature"), "tool_version": inc.get("from_version")},
                   "after": {"schema_signature": inc.get("new_signature"), "tool_version": inc.get("to_version")}},
        "original_request": (first_req or {}).get("detail"),
        "first_failure": {"http_status": ((first_req or {}).get("detail") or {}).get("status"),
                          "classification": inc.get("category"),
                          "explanation": (_first(tl, "classify") or {}).get("title")},
        "schema_diff": inc.get("drift"),
        "llm": {"used": bool(llm), "model": llm.get("model"), "provider": llm.get("provider"),
                "real_request_performed": bool(llm.get("real")) and (llm.get("attempts") or 0) > 0,
                "ok": llm.get("ok"), "error_category": llm.get("error_category"),
                "prompt_tokens": llm.get("prompt_tokens"), "output_tokens": llm.get("output_tokens"),
                "duration_ms": llm.get("duration_ms")},
        "proposal": inc.get("plan"),
        "validation": inc.get("validation"),
        "applied_change": {"from_version": applied.parent_version, "to_version": applied.version,
                           "status_now": applied.status, "patch": applied.patch} if applied else None,
        "retry": {"retries": inc.get("retries", 0), "repair_attempts": inc.get("repair_attempts", 0),
                  "final_http_status": ((last_req or {}).get("detail") or {}).get("status")},
        "timing": {"started_at": inc.get("started_at"), "finished_at": inc.get("finished_at"),
                   "elapsed_ms": inc.get("duration_ms") or (result or {}).get("duration_ms")},
        "verification": {"api_reported": verification, "business_outcome": business},
        "rollback": {"performed": bool(inc.get("rolled_back")),
                     "steps": [s["title"] for s in tl if s["stage"] == "rollback"]},
        "final": {"outcome": outcome, "verified_recovery": verified, "reason": inc.get("reason")
                  or (result or {}).get("reason")},
        "expected": {"outcome": expected, "category": expected_category,
                     "matches": None if expected is None else (
                         outcome == expected or (outcome == "succeeded_without_incident" and expected in
                                                 ("recovered", "succeeded")))},
        "audit": {"correlation_id": inc.get("correlation_id") or (result or {}).get("correlation_id"),
                  "events": len(st.events(correlation_id=inc.get("correlation_id"), limit=500))
                  if inc.get("correlation_id") else 0},
        "timeline": [{"stage": s["stage"], "status": s["status"], "title": s["title"], "ts": s["ts"]} for s in tl],
    }
    if extra:
        ev.update(extra)
    ev = redact(ev)
    st.save_evidence(ev)
    return ev


# ---------------------------------------------------------------- scorecard
def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def _latency(values: list[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    out = {"n": len(v), "median_ms": round(statistics.median(v), 1) if v else None, "p90_ms": None, "p95_ms": None,
           "note": ""}
    if len(v) >= 10:
        out["p90_ms"] = round(v[min(len(v) - 1, int(0.9 * len(v)))], 1)
    if len(v) >= 20:
        out["p95_ms"] = round(v[min(len(v) - 1, int(0.95 * len(v)))], 1)
    if len(v) < 10:
        out["note"] = f"only {len(v)} sample(s): percentiles are not reported"
    return out


def scorecard(records: list[dict]) -> dict:
    """Honest metrics from evidence records. Denominators are always reported with the rates."""
    eligible = [r for r in records if (r.get("expected") or {}).get("outcome") == "recovered"]
    should_stop = [r for r in records if (r.get("expected") or {}).get("outcome") in ("stopped", "rolled_back")]
    verified = [r for r in eligible if r["final"]["verified_recovery"]]
    applied = [r for r in records if r.get("applied_change")]
    failed_repairs = [r for r in applied if r["rollback"]["performed"] or r["final"]["outcome"] in ("rolled_back", "failed")]
    unsafe = [r for r in records if r["final"]["outcome"] == "recovered"
              and (r.get("expected") or {}).get("outcome") not in (None, "recovered")]
    known_cat = [r for r in records if (r.get("expected") or {}).get("category")]
    false_drift = [r for r in known_cat if r["first_failure"]["classification"] == "schema_drift"
                   and r["expected"]["category"] != "schema_drift"]
    rb_needed = [r for r in records if r["rollback"]["steps"]]
    rb_ok = [r for r in rb_needed if r["rollback"]["performed"]]
    rejected = [r for r in records if (r.get("validation") or {}).get("accepted") is False]
    n = len(records)
    out = {
        "records": n,
        "eligible_incidents": len(eligible),
        "verified_recoveries": len(verified),
        "verified_recovery_rate": _pct(len(verified), len(eligible)),
        "should_stop": len(should_stop),
        "stopped_correctly": sum(r["final"]["outcome"] in ("stopped", "rolled_back") for r in should_stop),
        "repairs_applied": len(applied), "successful_repairs": len(applied) - len(failed_repairs),
        "failed_repairs": len(failed_repairs), "rejected_repairs": len(rejected),
        "unsafe_repairs_accepted": len(unsafe),
        "false_positive_drift": len(false_drift), "false_positive_denominator": len(known_cat),
        "rollbacks_attempted": len(rb_needed), "rollback_success_rate": _pct(len(rb_ok), len(rb_needed)),
        "real_llm_requests": sum(bool(r["llm"]["real_request_performed"]) for r in records),
        "latency": _latency([r["timing"]["elapsed_ms"] for r in verified]),
        "by_environment": {},
        "by_scenario": {},
        "disclosure": (f"{n} recorded run(s). These are demonstration runs against test services, not a measure of "
                       "general reliability; small samples do not support a reliability guarantee.") if n else
                      "No runs recorded yet.",
    }
    for r in records:
        out["by_environment"][r["environment"]] = out["by_environment"].get(r["environment"], 0) + 1
    for sc in sorted({r.get("scenario") or "-" for r in records}):
        rs = [r for r in records if (r.get("scenario") or "-") == sc]
        out["by_scenario"][sc] = {
            "runs": len(rs), "eligible": sum((r.get("expected") or {}).get("outcome") == "recovered" for r in rs),
            "verified_recoveries": sum(r["final"]["verified_recovery"] for r in rs),
            "rejected_repairs": sum((r.get("validation") or {}).get("accepted") is False for r in rs),
            "failed_verifications": sum((r["verification"]["api_reported"] or {}).get("ok") is False
                                        or (r["verification"]["business_outcome"] or {}).get("ok") is False for r in rs),
            "rollbacks": sum(r["rollback"]["performed"] for r in rs),
            "as_expected": sum(bool((r.get("expected") or {}).get("matches")) for r in rs),
            "median_ms": _latency([r["timing"]["elapsed_ms"] for r in rs])["median_ms"],
            "environments": sorted({r["environment"] for r in rs}),
        }
    return out


def incident_scorecard(incidents: list[dict]) -> dict:
    """Pipeline counts from stored incidents (one source of truth for every page). Each stage is counted only when
    the record shows it actually happened: a proposal is not a validated patch, a validated patch is not an applied
    one, and only an applied patch whose business outcome was verified is a verified recovery."""
    def stages(i):
        return {s["stage"] for s in i.get("timeline", [])}
    real = [i for i in incidents if i.get("healing", True)]
    proposed = [i for i in real if (i.get("plan") or {}).get("mappings") or (i.get("plan") or {}).get("response_mappings")]
    validated = [i for i in real if (i.get("validation") or {}).get("accepted")]
    applied = [i for i in real if "apply" in stages(i) and i.get("to_version")]
    verified = [i for i in real if i.get("status") == "recovered" and (i.get("verification") or {}).get("ok", True)]
    times = [i["duration_ms"] for i in verified if i.get("duration_ms") is not None]
    return {"incidents": len(real),
            "detected_drift": sum(i.get("category") in ("schema_drift", "mapping_mismatch") for i in real),
            "proposed_repairs": len(proposed), "validated_patches": len(validated), "applied_patches": len(applied),
            "verified_recoveries": len(verified),
            "refused_or_stopped": sum(i.get("status") == "stopped" for i in real),
            "failures": sum(i.get("status") == "failed" for i in real),
            "rollbacks": sum(bool(i.get("rolled_back")) for i in real),
            "baseline_runs_excluded": len(incidents) - len(real),
            "latency": _latency(times)}


CSV_COLUMNS = ["evidence_id", "run_id", "incident_id", "environment", "scenario", "tool", "endpoint",
               "first_http_status", "classification", "llm_model", "real_llm_request", "validation_accepted",
               "from_version", "to_version", "retries", "final_http_status", "outcome", "verified_recovery",
               "business_verified", "rolled_back", "elapsed_ms", "expected_outcome", "as_expected", "recorded_at"]


def to_csv(records: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for r in records:
        w.writerow({
            "evidence_id": r["evidence_id"], "run_id": r.get("run_id"), "incident_id": r.get("incident_id"),
            "environment": r["environment"], "scenario": r.get("scenario"), "tool": r["api"]["tool"],
            "endpoint": r["api"]["endpoint"], "first_http_status": r["first_failure"]["http_status"],
            "classification": r["first_failure"]["classification"], "llm_model": r["llm"]["model"],
            "real_llm_request": r["llm"]["real_request_performed"],
            "validation_accepted": (r.get("validation") or {}).get("accepted"),
            "from_version": r["schema"]["before"]["tool_version"], "to_version": r["schema"]["after"]["tool_version"],
            "retries": r["retry"]["retries"], "final_http_status": r["retry"]["final_http_status"],
            "outcome": r["final"]["outcome"], "verified_recovery": r["final"]["verified_recovery"],
            "business_verified": (r["verification"]["business_outcome"] or {}).get("ok"),
            "rolled_back": r["rollback"]["performed"], "elapsed_ms": r["timing"]["elapsed_ms"],
            "expected_outcome": r["expected"]["outcome"], "as_expected": r["expected"]["matches"],
            "recorded_at": r["recorded_at"]})
    return scrub(buf.getvalue())
