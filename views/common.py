"""Shared dashboard plumbing: the API client, theme CSS and small, consistent UI components.

The dashboard never reads the database or calls the engine directly: every view goes through the ADAPTIX
API. By default the API runs in this same process (works on Streamlit Cloud); set ADAPTIX_API_URL and
ADAPTIX_ADMIN_TOKEN to point the dashboard at a separately deployed API instead.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent

STATUS = {   # one consistent vocabulary and colour per outcome
    "recovered": ("Recovered", "#047857", "#ECFDF5"), "ok": ("OK", "#1D4ED8", "#EFF6FF"),
    "stopped": ("Stopped safely", "#B45309", "#FFFBEB"), "rolled_back": ("Rolled back", "#6D28D9", "#F5F3FF"),
    "failed": ("Failed", "#B91C1C", "#FEF2F2"), "open": ("Open", "#475569", "#F1F5F9"),
    "healthy": ("Healthy", "#047857", "#ECFDF5"), "degraded": ("Degraded", "#B45309", "#FFFBEB"),
    # integration / monitoring statuses
    "VERIFIED": ("VERIFIED", "#047857", "#ECFDF5"), "CONFIGURED": ("CONFIGURED", "#1D4ED8", "#EFF6FF"),
    "FAILED": ("FAILED", "#B91C1C", "#FEF2F2"), "ACTION REQUIRED": ("ACTION REQUIRED", "#B45309", "#FFFBEB"),
    "NOT RUN": ("NOT RUN", "#475569", "#F1F5F9"), "UNSUPPORTED": ("UNSUPPORTED", "#6D28D9", "#F5F3FF"),
    "NOT REQUIRED": ("NOT REQUIRED", "#475569", "#F1F5F9"), "YES": ("YES", "#047857", "#ECFDF5"),
    "NO": ("NO", "#B45309", "#FFFBEB"),
    "verified": ("Verified", "#047857", "#ECFDF5"), "unsupported": ("Schema not published", "#6D28D9", "#F5F3FF"),
    "reachable": ("Reachable", "#047857", "#ECFDF5"), "unreachable": ("Unreachable", "#B91C1C", "#FEF2F2"),
    "not_checked": ("Not checked", "#475569", "#F1F5F9"), "not_run": ("Not run", "#475569", "#F1F5F9"),
    "drift": ("Schema drift", "#B45309", "#FFFBEB"), "no_drift": ("No drift", "#047857", "#ECFDF5"),
}
STAGE_ICON = {"request": "send", "classify": "category", "discover": "travel_explore", "diff": "difference",
              "plan": "edit_note", "validate": "verified", "apply": "inventory", "retry": "replay",
              "verify": "fact_check", "rollback": "undo", "outcome": "flag", "credential": "key"}

CSS = """
<style>
.block-container { padding-top: 1.6rem; max-width: 1280px; }
.ax-eyebrow { color:var(--ax-accent); font-weight:700; font-size:.78rem; letter-spacing:.08em; text-transform:uppercase; }
.ax-title { font-size:1.75rem; font-weight:750; color:var(--ax-text); margin:.1rem 0 .2rem 0; }
.ax-sub { color:var(--ax-muted); font-size:.98rem; margin-bottom:1rem; max-width: 920px; }
.ax-badge { display:inline-block; padding:.12rem .55rem; border-radius:999px; font-size:.78rem; font-weight:650;
            border:1px solid transparent; }
.ax-sim { background:#FEF3C7; color:#92400E; border-color:#FCD34D; }
.ax-card { background:var(--ax-card); border:1px solid var(--ax-border); border-radius:12px; padding:1rem 1.1rem; height:100%; }
.ax-kpi .l { color:var(--ax-muted); font-size:.8rem; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.ax-kpi .v { color:var(--ax-text); font-size:1.9rem; font-weight:750; line-height:1.2; margin:.2rem 0; }
.ax-kpi .d { color:var(--ax-subtle); font-size:.85rem; }
.ax-grid { display:grid; gap:.8rem; grid-template-columns: repeat(var(--cols), minmax(0,1fr)); margin:.2rem 0 1rem 0; }
@media (max-width: 900px) { .ax-grid { grid-template-columns: repeat(2, minmax(0,1fr)); } }
.ax-step { border-left:3px solid var(--ax-border); padding:.15rem 0 .55rem .9rem; margin-left:.4rem; }
.ax-diff-add { background:#ECFDF5; } .ax-diff-del { background:#FEF2F2; } .ax-diff-chg { background:#FFFBEB; }
[data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small { color:#93A4C3 !important; }
</style>
"""


class ApiError(RuntimeError):
    def __init__(self, status: int, detail):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


def _code_version() -> str:
    """Fingerprint of the backend source. When a deployment updates the code, the cached in-process API is
    rebuilt instead of serving routes from the previous version (seen on Streamlit Cloud hot updates)."""
    import hashlib
    h = hashlib.sha1()
    for p in sorted((ROOT / "adaptix").rglob("*.py")):
        stat = p.stat()
        h.update(f"{p.name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return h.hexdigest()[:12]


def _client():
    return _client_for(_code_version())


@st.cache_resource(show_spinner=False, max_entries=1)
def _client_for(code_version: str):
    import os
    import sys
    previous = os.environ.get("ADAPTIX_LOADED_CODE_VERSION")      # survives Streamlit's module reloads
    if previous and previous != code_version:                    # the backend code changed since the last build
        for m in [m for m in sys.modules if m == "adaptix" or m.startswith("adaptix.")]:
            del sys.modules[m]                   # load the current backend code, not a stale copy
    os.environ["ADAPTIX_LOADED_CODE_VERSION"] = code_version
    from adaptix.config import get_settings
    s = get_settings()
    headers = {"Authorization": f"Bearer {s.admin_token}"}
    if s.api_url:
        return httpx.Client(base_url=s.api_url, headers=headers, timeout=120), s.api_url
    from fastapi.testclient import TestClient
    from adaptix.api import create_app
    return TestClient(create_app(), headers=headers), "in-process"


def api(method: str, path: str, **kw):
    client, _ = _client()
    r = client.request(method, path, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.json())
        except Exception:
            detail = r.text[:200]
        raise ApiError(r.status_code, detail)
    if "application/json" not in r.headers.get("content-type", ""):
        return r.text                                  # e.g. the CSV evidence export
    return r.json()


def api_mode() -> str:
    return _client()[1]


def safe_call(fn, *a, **kw):
    """Run an API call, show a readable error state instead of a traceback."""
    try:
        return fn(*a, **kw)
    except ApiError as e:
        d = e.detail.get("detail") if isinstance(e.detail, dict) else e.detail
        st.error(f"The ADAPTIX API returned {e.status}: {d}", icon=":material/error:")
    except httpx.HTTPError as e:
        st.error(f"Could not reach the ADAPTIX API: {type(e).__name__}", icon=":material/cloud_off:")
    return None


# colour tokens per mode (Streamlit's own widgets follow [theme.light] / [theme.dark] in .streamlit/config.toml)
TOKENS = {
    "light": {"text": "#0F172A", "muted": "#475569", "subtle": "#64748B", "card": "#FFFFFF", "border": "#DCE3EE",
              "hero": "#F8FAFC", "accent": "#1D4ED8", "pill": "#FFFFFF", "pill-border": "#CBD5E1", "body": "#334155",
              "sidebar": "#0B1B36"},
    "dark": {"text": "#E2E8F0", "muted": "#A3B1C6", "subtle": "#94A3B8", "card": "#131C2E", "border": "#25324A",
             "hero": "#111A2C", "accent": "#93C5FD", "pill": "#131C2E", "pill-border": "#334155", "body": "#CBD5E1",
             "sidebar": "#070D19"},
}
THEME_CSS = """
<style>
.ax-theme { position: fixed; bottom: 16px; left: 20px; z-index: 1000002; display: flex; gap: 2px; padding: 2px;
  border-radius: 999px; border: 1px solid rgba(226,232,240,.18); background: rgba(15,26,46,.92); }
.ax-theme a { width: 26px; height: 22px; display: inline-flex; align-items: center; justify-content: center;
  border-radius: 999px; text-decoration: none !important; color: #CBD5E1 !important; font-size: .95rem; line-height: 1; }
.ax-theme a:hover { background: rgba(226,232,240,.12); }
.ax-theme a[aria-current="true"] { background: #2563EB; color: #FFFFFF !important; }
.ax-theme a:focus-visible { outline: 3px solid #93C5FD; outline-offset: 2px; }
.st-key-ax_theme_store { display: none; }
.ax-overview-link { display: flex; align-items: center; gap: .55rem; margin: .1rem 0 .9rem 0; padding: .45rem .75rem;
  border-radius: .5rem; border: 1px solid rgba(147,197,253,.35); background: rgba(37,99,235,.14);
  color: #E2E8F0 !important; text-decoration: none !important; font-size: .9rem; font-weight: 600; }
.ax-overview-link span { color: #93C5FD; }
.ax-overview-link:hover { background: rgba(37,99,235,.28); }
.ax-overview-link.current { background: #2563EB; border-color: #2563EB; }
.ax-overview-link.current span { color: #FFFFFF; }
.ax-overview-link:focus-visible { outline: 3px solid #93C5FD; outline-offset: 2px; }
[data-testid="stSidebarUserContent"] { padding-bottom: 3.5rem; }   /* keep the last sidebar line clear of the toggle */
/* sidebar text fades out under the toggle instead of being cut by it */
[data-testid="stSidebar"]::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0; height: 58px;
  background: linear-gradient(to bottom, transparent, var(--ax-sidebar) 55%%); pointer-events: none; z-index: 2; }
[data-testid="stGraphVizChart"] svg { %s }
</style>
"""


def theme_mode() -> str:
    """The theme this browser is showing now ("light" or "dark"), as reported by Streamlit's frontend."""
    try:
        return "dark" if (st.context.theme.type or "light") == "dark" else "light"
    except Exception:
        return "light"


REMEMBER_THEME_JS = """<script>
// Remember the choice the way Streamlit's own Settings menu does: localStorage "stActiveTheme-<page path>-v2"
// = "Light" | "Dark", for every page of this app, in this browser only. Nothing is sent anywhere.
try {
  const w = window.parent, q = new URLSearchParams(w.location.search).get("embed_options") || "";
  const want = q.includes("dark_theme") ? "Dark" : q.includes("light_theme") ? "Light" : null;
  if (want) {
    const pages = %s;
    let path = w.location.pathname.replace(/\\/$/, "");
    for (const u of pages) { if (u && path.endsWith("/" + u)) { path = path.slice(0, -(u.length + 1)); break; } }
    for (const u of [""].concat(pages)) {
      w.localStorage.setItem("stActiveTheme-" + path + "/" + u + "-v2", JSON.stringify(want));
    }
  }
} catch (e) {}
</script>"""


def theme_toggle(page_paths: list[str]):
    """Light / Dark switch pinned to the bottom-left corner. A click reloads the page with Streamlit's own
    embed_options=light_theme|dark_theme option (this browser tab only), and the choice is remembered in this
    browser for every page, so it survives reloads."""
    import json as _json
    from html import escape
    from urllib.parse import urlencode
    mode = theme_mode()
    params = {k: v for k, v in st.query_params.to_dict().items() if k != "embed_options"}
    links = []
    for key, label, icon in (("light", "Light", "☀"), ("dark", "Dark", "☾")):
        href = "?" + urlencode({**params, "embed_options": f"{key}_theme"})
        links.append(f'<a href="{escape(href)}" target="_self" aria-current="{str(mode == key).lower()}" '
                     f'title="{label} mode" aria-label="Switch to {label.lower()} mode">{icon}</a>')
    st.markdown(f'<nav class="ax-theme" aria-label="Colour theme">{"".join(links)}</nav>', unsafe_allow_html=True)
    # Streamlit hides embed_options from st.query_params, so the (hidden) script checks the address itself
    with st.container(key="ax_theme_store"):
        # our own constant script (no user input): st.iframe runs it same-origin, like Streamlit's settings menu
        st.iframe(REMEMBER_THEME_JS % _json.dumps([p for p in page_paths if p]), height=1)


def inject_css():
    mode = theme_mode()
    tokens = "".join(f"--ax-{k}:{v};" for k, v in TOKENS[mode].items())
    st.markdown(f"<style>:root{{{tokens}}}</style>" + CSS, unsafe_allow_html=True)
    st.markdown(THEME_CSS % ("filter: invert(.88) hue-rotate(180deg);" if mode == "dark" else ""),
                unsafe_allow_html=True)


def header(eyebrow: str, title: str, sub: str = ""):
    st.markdown(f'<div class="ax-eyebrow">{eyebrow}</div><div class="ax-title">{title}</div>'
                + (f'<div class="ax-sub">{sub}</div>' if sub else ""), unsafe_allow_html=True)


def badge(status: str) -> str:
    label, fg, bg = STATUS.get(status, (status.replace("_", " ").title(), "#334155", "#F1F5F9"))
    return f'<span class="ax-badge" style="color:{fg};background:{bg};border-color:{fg}33">{label}</span>'


SIM_BADGE = '<span class="ax-badge ax-sim">SIMULATED</span>'
REAL_BADGE = '<span class="ax-badge" style="background:#ECFDF5;color:#065F46;border-color:#6EE7B7">REAL API</span>'
BASELINE_BADGE = '<span class="ax-badge" style="background:#E2E8F0;color:#334155">BASELINE · repair off</span>'


def kpis(items: list[tuple], cols: int = 4):
    html = "".join(f'<div class="ax-card ax-kpi"><div class="l">{l}</div><div class="v">{v}</div>'
                   f'<div class="d">{d}</div></div>' for l, v, d in items)
    st.markdown(f'<div class="ax-grid" style="--cols:{cols}">{html}</div>', unsafe_allow_html=True)


def fmt_pct(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def fmt_ms(v) -> str:
    return "—" if v is None else (f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.2f} s")


def go(page_key: str, **state):
    for k, v in state.items():
        st.session_state[k] = v
    st.switch_page(st.session_state["_pages"][page_key])


def empty(msg: str, action_label: str | None = None, page_key: str | None = None):
    st.info(msg, icon=":material/info:")
    if action_label and page_key and st.button(action_label, key=f"empty-{page_key}-{action_label}"):
        go(page_key)


# keys holding results derived from server data; cleared after any confirmed mutation so no page shows stale copies
DERIVED_KEYS = ("chaos_last", "chaos_all", "rt_matrix", "rt_demo", "rt_preflight", "demo_job", "eval", "mon_prefill",
                "llm_check", "ev_pick")


def mutated(*keep: str):
    """Call after a confirmed change (repair, rollback, reset, registration, demo run). Every page re-reads the API on
    its next render, so dropping derived copies is enough to keep all views consistent."""
    for k in DERIVED_KEYS:
        if k not in keep:
            st.session_state.pop(k, None)
    st.cache_data.clear()


# fixed mapping (never mutated at import time); the LOCAL DEMO choice is only OFFERED when it is enabled
ENV_PREFIX = {"REAL API": "/api", "REAL API TEST": "/api/test", "SIMULATION": "/api/sim", "LOCAL DEMO": "/api/local"}
ENV_NOTE = {"REAL API": "real external providers (production records)",
            "REAL API TEST": "controlled test service run by ADAPTIX (real HTTP on 127.0.0.1)",
            "SIMULATION": "in-process simulated APIs (no network)",
            "LOCAL DEMO": "local demo on this machine: real SQLite database, real local customer API, local LLM"}


def local_demo_enabled() -> bool:
    """True only when ADAPTIX_LOCAL_DEMO=1 is set in this machine's OS environment (never on a deployment)."""
    from adaptix.config import get_settings
    return get_settings().local_demo



def env_options() -> list[str]:
    return [e for e in ENV_PREFIX if e != "LOCAL DEMO" or local_demo_enabled()]


def env_picker(key: str, label: str = "Environment") -> str:
    """One shared environment selector; the choice is remembered across pages (same session key)."""
    options = env_options()
    cur = st.session_state.get("env_choice", "REAL API")
    env = st.radio(label, options, index=options.index(cur) if cur in options else 0, horizontal=True, key=key,
                   help="REAL API: your registered providers · REAL API TEST: the controlled demo service · "
                        "SIMULATION: in-process simulated APIs")
    st.session_state["env_choice"] = env
    if env != "REAL API":
        st.caption(f"{env}: {ENV_NOTE[env]}. Not production data.")
    return env


# Every displayed time is Indian Standard Time; records stay in UTC (see adaptix/timefmt.py)
from adaptix.timefmt import IST, ist, ist_time  # noqa: E402,F401


# ---------------------------------------------------------------- schema retrieval problems, in plain language
SCHEMA_PROBLEMS = {
    "egress_denied:host_not_allowed": ("The schema host is not on the permitted-host list.",
                                       "If you are authorized to call this API, add its host to ADAPTIX_ALLOWED_HOSTS."),
    "egress_denied:scheme_not_allowed": ("The schema URL is not HTTPS.",
                                         "Register an https:// schema URL. Plain HTTP is allowed only for ADAPTIX's own "
                                         "controlled test service."),
    "egress_denied:private_address": ("The schema URL points to a private, internal or metadata address, which is blocked.",
                                      "Use the provider's public HTTPS schema URL."),
    "egress_denied:dns_failure": ("The schema host name could not be resolved.", "Check the host name in the schema URL."),
    "dns_failure": ("The schema host name could not be resolved.", "Check the host name in the schema URL."),
    "egress_denied:redirect_denied": ("The schema URL redirected to a destination that is not permitted.",
                                      "Register the final schema URL directly."),
    "egress_denied:response_too_large": ("The schema document is larger than the allowed size.",
                                         "Raise ADAPTIX_MAX_SCHEMA_BYTES only if the document is genuinely that large."),
    "egress_denied:rate_limited_local": ("The provider asked ADAPTIX to wait before calling again (Retry-After).",
                                         "Retry after the wait period."),
    "egress_denied:invalid_url": ("The registered schema URL is not a valid URL.", "Correct the schema URL."),
    "tls_error": ("The provider's TLS certificate could not be verified.", "Check the provider's HTTPS certificate."),
    "timeout": ("The schema request timed out.", "Retry. If it keeps timing out, check that the provider is up."),
    "network": ("The schema host could not be reached.", "Retry, and check the provider's status."),
    "http_401": ("The schema URL requires authentication.", "Use a public schema URL, or one this server may read."),
    "http_403": ("Access to the schema URL is forbidden.", "Use a schema URL this server is allowed to read."),
    "http_404": ("No schema was found at the registered schema URL.",
                 "Check the schema URL. For a Drift Demo tool, the demo API may no longer exist: run the demo again or reset it."),
    "not_json": ("The schema URL did not return a JSON document.", "Point the tool at the provider's OpenAPI JSON."),
    "invalid_schema": ("The schema document is malformed.", "Check that the URL serves a valid OpenAPI document."),
    "operation_not_in_schema": ("The tool's endpoint and method are not in the published schema.",
                                "The endpoint may have moved or been removed. Check the provider's documentation."),
    "method_changed": ("The published schema no longer accepts this HTTP method.",
                       "A method change can alter side effects, so ADAPTIX does not repair it automatically."),
}


def schema_problem(category: str | None) -> tuple[str, str]:
    cat = category or "unknown"
    if cat in SCHEMA_PROBLEMS:
        return SCHEMA_PROBLEMS[cat]
    if cat.startswith("http_5"):
        return ("The provider returned a server error for the schema URL.", "Retry later.")
    if cat.startswith("http_"):
        return (f"The schema URL answered HTTP {cat[5:]}.", "Check the schema URL.")
    if cat.startswith("egress_denied:"):
        return ("The request was refused by ADAPTIX's outbound safety policy.", "Check the schema URL and permitted hosts.")
    return ("The authoritative schema could not be retrieved.", "Check the registered schema URL and permitted hosts.")


SCHEMA_STATE = {"no_drift": ("SCHEMA LOADED · NO DRIFT", "VERIFIED"), "differs": ("SCHEMA DIFFERS", "ACTION REQUIRED"),
                "unavailable": ("SCHEMA UNAVAILABLE", "FAILED"), "invalid": ("INVALID SCHEMA", "FAILED"),
                "unsupported": ("NO SCHEMA PUBLISHED", "UNSUPPORTED")}


def schema_state_chip(state: str) -> str:
    label, tone = SCHEMA_STATE.get(state, ("UNKNOWN", "NOT RUN"))
    return badge(tone).replace(tone, label)


def schema_unavailable(d: dict, tool: str, retry_key: str) -> bool:
    """Compact, actionable message for a failed live-schema read. Returns True if the user asked to retry."""
    what, step = schema_problem(d.get("error_category"))
    st.markdown(f"{schema_state_chip(d.get('state', 'unavailable'))} &nbsp; **{tool}**", unsafe_allow_html=True)
    st.markdown(f"Unable to retrieve the authoritative schema. {what} **Automatic repair is paused** until the "
                f"schema can be verified.<br><span style='color:#475569'>Next step: {step}</span>", unsafe_allow_html=True)
    retry = st.button("Retry", key=retry_key, icon=":material/refresh:")
    with st.expander("Technical details"):
        st.markdown(f"- Error code: `{d.get('error_category') or 'unknown'}`\n"
                    f"- Schema URL: `{d.get('schema_url') or 'not available'}`\n"
                    f"- Detail: {d.get('detail') or '—'}")
        st.caption("Troubleshooting: real APIs must use HTTPS and a host listed in ADAPTIX_ALLOWED_HOSTS. Private, "
                   "internal and cloud-metadata addresses are always blocked. The controlled test service (REAL API TEST) "
                   "is reached only at its own current address in this server process. No credentials are shown here.")
    return retry
