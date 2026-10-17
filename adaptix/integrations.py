"""Integration health: one honest status per integration.

Statuses:  VERIFIED         a real request succeeded (recorded evidence, with its time)
           CONFIGURED       configuration is present, but no successful real request is on record yet
           FAILED           the latest real attempt failed (sanitized category only)
           ACTION REQUIRED  something must be configured by the operator (the exact variable is named)
           NOT RUN          no attempt has been made
           UNSUPPORTED      the provider offers no machine-readable schema (drift cannot be verified)
           NOT REQUIRED     e.g. a public API that needs no credential
A secret being present is never reported as VERIFIED. No key value, header or payload appears here.
"""
from __future__ import annotations

from importlib import metadata
from urllib.parse import urlsplit

from .netguard import host_allowed
from .timefmt import ist


def _sdk() -> dict:
    try:
        import google.genai  # noqa: F401
        return {"status": "VERIFIED", "detail": f"google-genai {metadata.version('google-genai')} imports correctly"}
    except Exception as e:                    # pragma: no cover - depends on the environment
        return {"status": "FAILED", "detail": f"google-genai is not importable ({type(e).__name__}); "
                                              "run pip install -r requirements.txt"}


def gemini(state) -> dict:
    llm = state.llm
    st = llm.status()
    configured = bool(st.get("configured")) and st.get("provider") == "gemini"
    creds = {"status": "CONFIGURED" if configured else "ACTION REQUIRED",
             "detail": "GEMINI_API_KEY is set on the server (value never shown)" if configured else
             "Set GEMINI_API_KEY on the server (Streamlit Cloud: app Settings -> Secrets)"}
    checks = state.storage.events(kind="llm_check", limit=1)
    last_ok = st.get("last_success_at")
    if last_ok:
        real = {"status": "VERIFIED", "detail": f"a real Gemini request succeeded at {ist(last_ok)}", "at": last_ok}
    elif checks and checks[0]["data"].get("ok"):
        real = {"status": "VERIFIED", "detail": f"a real Gemini request succeeded at {ist(checks[0]['ts'])} "
                                                "(earlier in this deployment)", "at": checks[0]["ts"]}
    elif checks and checks[0]["data"].get("real"):
        real = {"status": "FAILED", "detail": f"last real request failed: {checks[0]['data'].get('error_category')}",
                "at": checks[0]["ts"]}
    elif st.get("last_error_category"):
        real = {"status": "FAILED", "detail": f"last real request failed: {st['last_error_category']}"}
    else:
        real = {"status": "NOT RUN", "detail": "no real Gemini request has been made by this server yet"
                if configured else "not possible until GEMINI_API_KEY is configured"}
    pipeline = state.engine.llm is llm and configured
    return {"name": "Google Gemini", "model": st.get("model"), "sdk": _sdk(), "credentials": creds,
            "real_request": real,
            "pipeline": {"status": "YES" if pipeline else "NO",
                         "detail": "the production repair pipeline calls Gemini for fields the rules cannot ground"
                         if pipeline else "without a configured key, unresolved repairs stop safely (no mock is used)"}}


def external_apis(state) -> dict:
    s = state.settings
    hosts = {"status": "CONFIGURED" if s.allowed_hosts else "ACTION REQUIRED",
             "detail": ", ".join(s.allowed_hosts) if s.allowed_hosts else
             "Set ADAPTIX_ALLOWED_HOSTS (comma-separated host names you are authorized to call)"}
    rows = []
    for t in state.storage.tools():
        spec = t["spec"]
        host = (urlsplit(spec.base_url).hostname or "").lower()
        latest = state.storage.checks(spec.name, limit=20)
        last = latest[0] if latest else None
        ok_checks = [c for c in latest if c["schema_status"] == "verified" or c["verification"] == "verified"]
        if not spec.auth_header:
            cred = {"status": "NOT REQUIRED", "detail": "the tool sends no credential"}
        elif spec.credential_group and any(c["group"] == spec.credential_group and c["configured"]
                                           and c["state"] != "missing" for c in state.creds.status()):
            cred = {"status": "CONFIGURED", "detail": f"credential group '{spec.credential_group}' has a key"}
        else:
            cred = {"status": "ACTION REQUIRED", "detail": f"configure credential group '{spec.credential_group}' "
                                                           "in ADAPTIX_CREDENTIALS and set its secret variable"}
        if last is None:
            conn = {"status": "NOT RUN", "detail": "no check yet - use Check now"}
            schema = {"status": "NOT RUN", "detail": "no check yet"}
        else:
            conn = {"status": {"reachable": "VERIFIED", "unreachable": "FAILED"}.get(last["connectivity"], "NOT RUN"),
                    "detail": last["connectivity"] + (f" ({last['error_category']})" if last.get("error_category")
                                                      and last["connectivity"] == "unreachable" else ""),
                    "at": last["ts"]}
            schema = {"status": {"verified": "VERIFIED", "unsupported": "UNSUPPORTED"}.get(last["schema_status"], "FAILED"),
                      "detail": last.get("drift_summary") or last.get("error_category") or last.get("detail", "")[:160],
                      "at": last["ts"]}
        rows.append({"tool": spec.name, "provider": spec.provider or host, "endpoint": f"{spec.method} "
                     f"{spec.base_url.rstrip('/')}{spec.path}", "host": host,
                     "host_allowed": {"status": "CONFIGURED" if host_allowed(host, s.allowed_hosts) else "ACTION REQUIRED",
                                      "detail": host},
                     "credentials": cred, "connectivity": conn, "schema_discovery": schema,
                     "schema_source": spec.schema_source.kind,
                     "last_successful_check": ok_checks[0]["ts"] if ok_checks else None,
                     "pipeline": {"status": "YES", "detail": "real calls go through the guarded production pipeline"}})
    return {"allowed_hosts": hosts, "apis": rows}


def report(state) -> dict:
    # public-facing summary: no file paths, connection strings or deployment internals
    llm = state.engine.llm.status()
    return {"mode": {"production": {"upstreams": "real providers only, every request checked by the egress guard",
                                    "llm": llm.get("provider"), "llm_connectivity": llm.get("connectivity"),
                                    "approved_hosts": len(state.settings.allowed_hosts),
                                    "real_tools": len(state.storage.tools())},
                     "simulation": {"upstreams": "in-process simulated APIs only (never a real provider)",
                                    "llm": "selectable per run: rules only, mock (labelled), or real Gemini"}},
            "gemini": gemini(state), "external": external_apis(state),
            "monitor": state.monitor.status() if state.monitor else {"scheduler_running": False}}
