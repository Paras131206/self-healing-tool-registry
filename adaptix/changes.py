"""The Changes table: one row per detected schema change, built only from stored records.

Sources: incidents that carry a drift report (all environments) and real-API monitoring checks that detected
drift without a failing call (LIVE_API only). Nothing is invented: a value the backend did not store is None.
Filtering, sorting and pagination happen here so the counts always match the rows returned.
"""
from __future__ import annotations

from .security import redact

CHANGE_LABEL = {"renamed": "Field renamed", "moved": "Nested schema changed", "type_changed": "Type changed",
                "removed": "Field removed", "required_changed": "Required status changed",
                "enum_changed": "Allowed values changed", "format_changed": "Format changed",
                "header_added": "Required header added", "header_removed": "Header removed"}
REPAIR_STATES = ["Detected", "Analyzed", "Proposed", "Validated", "Applied", "Verified", "Failed", "Rolled back",
                 "Requires review"]
VERIFICATION_STATES = ["Verified", "Failed", "Not run"]
ENV_LABEL = {"LIVE_API": "REAL API", "REAL_API_TEST": "REAL API TEST", "SIMULATION": "SIMULATION", "LOCAL_DEMO": "LOCAL DEMO"}


def change_label(c: dict, ambiguous: bool) -> str:
    if ambiguous:
        return "Ambiguous change"
    if c["kind"] == "added":
        return "Required field added" if c.get("detail") == "required" else "Optional field added"
    return CHANGE_LABEL.get(c["kind"], c["kind"].replace("_", " ").capitalize())


def _states(inc: dict) -> tuple[str, str]:
    """(repair status, verification result) of an incident, never claiming more than the record shows."""
    v = inc.get("validation")
    ver = inc.get("verification")
    stages = {s["stage"] for s in inc.get("timeline", [])}
    verification = "Not run" if ver is None else ("Verified" if ver.get("ok") else "Failed")
    if inc.get("rolled_back") or inc.get("status") == "rolled_back":
        return "Rolled back", "Failed" if verification != "Verified" else verification
    if inc.get("status") == "recovered" and verification == "Verified":
        return "Verified", verification
    if inc.get("status") == "failed":
        return "Failed", verification
    if v is not None and not v.get("accepted"):
        return "Requires review", verification
    if "apply" in stages:
        return "Applied", verification
    if v is not None and v.get("accepted"):
        return "Validated", verification
    if inc.get("plan"):
        return "Proposed", verification
    if "diff" in stages:
        return "Analyzed", verification
    return "Detected", verification


def _ambiguous(inc: dict, c: dict) -> bool:
    risks = (inc.get("plan") or {}).get("compatibility_risks") or []
    leaf = c["location"].split(":", 1)[-1].split(".")[-1]
    return any("almost equally" in r and leaf in r for r in risks)


def _mapping_for(inc: dict, c: dict) -> dict | None:
    plan = inc.get("plan") or {}
    for m in plan.get("mappings", []):
        if m.get("target_field") == c["location"]:
            return m
    for m in plan.get("response_mappings", []):
        if c["location"].endswith(":" + m.get("response_path", "")):
            return m
    return None


def _validation_for(inc: dict, c: dict) -> list[dict]:
    v = inc.get("validation") or {}
    leaf = c["location"].split(":", 1)[-1]
    return [x for x in v.get("checks", []) if leaf in (x.get("detail") or "") or c["location"] in (x.get("detail") or "")]


def rows_from_incident(inc: dict, environment: str) -> list[dict]:
    drift = inc.get("drift")
    if not drift:
        return []
    repair, verification = _states(inc)
    tl = inc.get("timeline", [])
    first_req = next((s for s in tl if s["stage"] == "request"), None)
    last_req = next((s for s in reversed(tl) if s["stage"] == "request"), None)
    out = []
    for i, c in enumerate(drift.get("request_changes", []) + drift.get("response_changes", [])):
        amb = _ambiguous(inc, c)
        out.append({
            "change_id": f"{inc['id']}#{i + 1}", "incident_id": inc["id"], "check_id": None,
            "tool": inc["tool"], "environment": environment, "environment_label": ENV_LABEL[environment],
            "field_path": c["location"], "change_kind": c["kind"], "change_type": change_label(c, amb),
            "previous": c.get("old"),
            "new": (f"{c['location']} ({c['new']})" if c["kind"] in ("renamed", "moved") and c.get("new") else c.get("new")),
            "change_detail": c.get("detail") or None,
            "repair_status": repair, "verification": verification, "timestamp": inc.get("started_at"),
            "incident_status": inc.get("status"), "scenario": inc.get("scenario"),
            "details": {
                "drift_summary": drift.get("summary"), "compatibility": drift.get("compatibility"),
                "schema_before": drift.get("old_signature"), "schema_after": drift.get("new_signature"),
                "schema_evidence_source": next((s.get("detail", {}).get("source") for s in tl if s["stage"] == "discover"
                                                and isinstance(s.get("detail"), dict)), None),
                "versions": {"from": inc.get("from_version"), "to": inc.get("to_version")},
                "mapping": _mapping_for(inc, c), "planner": (inc.get("plan") or {}).get("source"),
                "validation_accepted": (inc.get("validation") or {}).get("accepted"),
                "validation_checks": _validation_for(inc, c),
                "llm": {k: (inc.get("llm") or {}).get(k) for k in ("provider", "model", "real", "ok", "error_category")}
                if inc.get("llm") else None,
                "retries": inc.get("retries"), "verification_checks": (inc.get("verification") or {}).get("checks"),
                "rolled_back": inc.get("rolled_back"), "reason": inc.get("reason"),
                "request": (first_req or {}).get("detail"), "final_response": (last_req or {}).get("detail"),
                "timeline": [{"stage": s["stage"], "status": s["status"], "title": s["title"], "ts": s["ts"]}
                             for s in tl]}})
    return out


def rows_from_check(check: dict, check_id: str) -> list[dict]:
    rep = check.get("report") or {}
    if not check.get("drift") or not rep:
        return []
    out = []
    for i, c in enumerate(rep.get("request_changes", []) + rep.get("response_changes", [])):
        out.append({
            "change_id": f"{check_id}#{i + 1}", "incident_id": check.get("incident"), "check_id": check_id,
            "tool": check["tool"], "environment": "LIVE_API", "environment_label": ENV_LABEL["LIVE_API"],
            "field_path": c["location"], "change_kind": c["kind"], "change_type": change_label(c, False),
            "previous": c.get("old"), "new": c.get("new"), "change_detail": c.get("detail") or None,
            "repair_status": "Detected" if not check.get("repair") else "Verified",
            "verification": {"verified": "Verified", "failed": "Failed"}.get(check.get("verification"), "Not run"),
            "timestamp": check["ts"], "incident_status": None, "scenario": None,
            "details": {"drift_summary": rep.get("summary"), "compatibility": rep.get("compatibility"),
                        "schema_before": rep.get("old_signature"), "schema_after": rep.get("new_signature"),
                        "schema_evidence_source": check.get("schema_url"), "versions": {"from": check.get("tool_version")},
                        "monitor_check": {k: check.get(k) for k in ("trigger", "connectivity", "verification",
                                                                    "verification_detail")},
                        "timeline": None}})
    return out


def collect(worlds: dict) -> list[dict]:
    """worlds: {environment: storage}. Reads incidents (and LIVE_API monitoring checks)."""
    rows = []
    for env, storage in worlds.items():
        for inc in storage.incidents(limit=5000):
            rows += rows_from_incident(inc.model_dump(mode="json"), env)
        if env == "LIVE_API":
            for n, chk in enumerate(storage.checks(limit=1000)):
                if chk.get("drift") and not chk.get("incident"):
                    rows += rows_from_check(chk, f"chk_{chk['tool']}_{chk['ts'].replace(':', '').replace('-', '')[:15]}")
    return rows


SORT_KEYS = {"timestamp": lambda r: r["timestamp"] or "", "tool": lambda r: r["tool"],
             "change_type": lambda r: r["change_type"], "status": lambda r: REPAIR_STATES.index(r["repair_status"])}


def query(rows: list[dict], q: str = "", environment: list[str] | None = None, change_type: list[str] | None = None,
          repair_status: list[str] | None = None, verification: list[str] | None = None, since: str | None = None,
          until: str | None = None, sort: str = "timestamp", order: str = "desc", page: int = 1,
          page_size: int = 25) -> dict:
    ql = (q or "").strip().lower()
    out = []
    for r in rows:
        if ql and not any(ql in (str(r.get(k) or "")).lower() for k in ("tool", "field_path", "change_id", "incident_id")):
            continue
        if environment and r["environment"] not in environment:
            continue
        if change_type and r["change_type"] not in change_type:
            continue
        if repair_status and r["repair_status"] not in repair_status:
            continue
        if verification and r["verification"] not in verification:
            continue
        ts = r["timestamp"] or ""
        if since and ts[:10] < since[:10]:
            continue
        if until and ts[:10] > until[:10]:
            continue
        out.append(r)
    out.sort(key=SORT_KEYS.get(sort, SORT_KEYS["timestamp"]), reverse=(order != "asc"))
    page_size = max(1, min(page_size, 200))
    pages = max(1, -(-len(out) // page_size))
    page = max(1, min(page, pages))
    facets = {"environment": sorted({r["environment"] for r in rows}),
              "change_type": sorted({r["change_type"] for r in rows}),
              "repair_status": [s for s in REPAIR_STATES if any(r["repair_status"] == s for r in rows)],
              "verification": [s for s in VERIFICATION_STATES if any(r["verification"] == s for r in rows)]}
    return {"total": len(out), "unfiltered_total": len(rows), "page": page, "pages": pages, "page_size": page_size,
            "rows": redact(out[(page - 1) * page_size: page * page_size]), "facets": facets}
