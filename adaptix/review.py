"""Product review metrics, computed ONLY from recorded incidents and evidence, per environment.

Phase timings come from the incident timeline's timestamps (UTC, millisecond precision). The phases are
consecutive, non-overlapping segments of one incident:
  detect      incident start            -> drift report recorded (diff)
  propose     drift report recorded     -> last repair-proposal step (plan)        includes any LLM inference
  validate    last proposal step        -> validation result (validate)
  apply       validation result         -> new tool version written (apply)
  verify      tool version written      -> verified outcome (last verify step)     includes the retry
  total       the engine's measured duration of the incident (start -> outcome)
LLM inference latency is reported separately (the provider's own measured duration); it is part of "propose".
Environments are never combined: LIVE_API, REAL_API_TEST, SIMULATION and LOCAL_DEMO are reported separately.
"""
from __future__ import annotations

import statistics
from datetime import datetime

PHASES = ("detect", "propose", "validate", "apply", "verify")


def _t(ts: str | None) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp() * 1000 if ts else None
    except ValueError:
        return None


def _step(tl: list[dict], stage: str, last: bool = False, status: str | None = None) -> dict | None:
    hits = [s for s in tl if s["stage"] == stage and (status is None or s["status"] == status)]
    return (hits[-1] if last else hits[0]) if hits else None


def phase_timings(inc: dict) -> dict | None:
    """Per-phase durations (ms) of one VERIFIED recovery, or None if the incident did not recover."""
    if inc.get("status") != "recovered":
        return None
    tl = inc.get("timeline") or []
    marks = [_t(inc.get("started_at")),
             _t((_step(tl, "diff") or {}).get("ts")),
             _t((_step(tl, "plan", last=True) or {}).get("ts")),
             _t((_step(tl, "validate", last=True) or {}).get("ts")),
             _t((_step(tl, "apply", last=True) or {}).get("ts")),
             _t((_step(tl, "verify", last=True, status="ok") or {}).get("ts"))]
    out = {}
    for name, a, b in zip(PHASES, marks, marks[1:]):
        out[name] = round(b - a, 1) if a is not None and b is not None and b >= a else None
    out["total"] = inc.get("duration_ms")
    out["llm_inference"] = (inc.get("llm") or {}).get("duration_ms") if (inc.get("llm") or {}).get("real") else None
    return out


def summarize(values: list[float]) -> dict:
    """Median and p95 only with enough samples; otherwise the individual values and a small-sample warning."""
    v = sorted(x for x in values if x is not None)
    out = {"n": len(v), "median_ms": None, "p95_ms": None, "mean_ms": None, "values_ms": None, "warning": None}
    if not v:
        return out
    if len(v) >= 20:
        out["median_ms"] = round(statistics.median(v), 1)
        out["p95_ms"] = round(v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))], 1)
    elif len(v) >= 5:
        out["median_ms"] = round(statistics.median(v), 1)
        out["warning"] = f"small sample ({len(v)}): p95 needs at least 20 runs"
    else:
        out["values_ms"] = [round(x, 1) for x in v]
        out["mean_ms"] = round(statistics.mean(v), 1)
        out["warning"] = f"very small sample ({len(v)}): individual runs shown"
    return out


def environment_metrics(environment: str, incidents: list[dict], evidence: list[dict]) -> dict:
    """Recovery, refusal and failure rates from evidence records whose expected outcome was declared up front."""
    eligible = [e for e in evidence if (e.get("expected") or {}).get("outcome") == "recovered"]
    verified = [e for e in eligible if (e.get("final") or {}).get("verified_recovery")]
    should_stop = [e for e in evidence if (e.get("expected") or {}).get("outcome") in ("stopped", "rolled_back")]
    refused = [e for e in should_stop if (e.get("final") or {}).get("outcome") in ("stopped", "rolled_back")]
    failed = [e for e in eligible if not (e.get("final") or {}).get("verified_recovery")]
    reasons: dict[str, int] = {}
    for e in failed:
        r = ((e.get("final") or {}).get("reason") or (e.get("final") or {}).get("outcome") or "unknown")[:90]
        reasons[r] = reasons.get(r, 0) + 1
    timings = [t for t in (phase_timings(i) for i in incidents) if t]
    phases = {p: summarize([t[p] for t in timings]) for p in PHASES + ("total", "llm_inference")}
    return {"environment": environment, "incidents": len(incidents), "evidence_records": len(evidence),
            "eligible": len(eligible), "verified": len(verified),
            "verification_rate": round(100 * len(verified) / len(eligible), 1) if eligible else None,
            "unsafe_expected": len(should_stop), "safely_refused": len(refused),
            "safe_refusal_rate": round(100 * len(refused) / len(should_stop), 1) if should_stop else None,
            "failed": len(failed), "failure_rate": round(100 * len(failed) / len(eligible), 1) if eligible else None,
            "failure_reasons": reasons, "verified_recoveries_timed": len(timings), "phases": phases}


def stage_counts(incidents: list[dict]) -> dict:
    """How many recorded incidents actually reached each pipeline stage (a stage counts once per incident).
    Validation and verification count only when they passed; rollback counts when one was performed."""
    out: dict[str, int] = {}
    for inc in incidents:
        seen = set()
        for s in inc.get("timeline") or []:
            if s["stage"] in ("validate", "verify", "apply") and s["status"] != "ok":
                continue
            if s["stage"] == "rollback" and s["status"] == "fail":
                continue
            seen.add(s["stage"])
        for k in seen:
            out[k] = out.get(k, 0) + 1
    return out


def latest_evidence(evidence: list[dict], n: int = 10) -> list[dict]:
    """Already-redacted evidence records (redacted when stored), newest first, trimmed for display."""
    keep = ("evidence_id", "run_id", "recorded_at", "environment", "evidence_source", "scenario", "api", "first_failure",
            "schema_diff", "llm", "proposal", "validation", "applied_change", "retry", "verification", "timing",
            "rollback", "final", "expected")
    rows = sorted(evidence, key=lambda e: e.get("recorded_at") or "", reverse=True)[:n]
    return [{k: e.get(k) for k in keep} for e in rows]
