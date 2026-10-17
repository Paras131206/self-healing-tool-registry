"""Integration Health: Gemini and every external API, with honest statuses."""
import streamlit as st

from views.common import api, badge, header, ist, ist_time, safe_call


def _row(label: str, item: dict):
    c1, c2 = st.columns([1.2, 3])
    c1.markdown(f"**{label}**")
    c2.markdown(f"{badge(item['status'])} &nbsp; {item.get('detail', '')}", unsafe_allow_html=True)


def render():
    header("Integration Health", "Gemini and external APIs",
           "Each status comes from recorded evidence. VERIFIED means a real request succeeded; a configured secret "
           "alone is only CONFIGURED. Key values, authorization headers and payloads are never shown.")
    rep = safe_call(api, "GET", "/api/integrations")
    h = safe_call(api, "GET", "/api/health")
    if rep is None or h is None:
        return
    st.subheader("Modes")
    m = rep["mode"]
    p = m["production"]
    llm_state = {"verified": "VERIFIED", "configured_unverified": "CONFIGURED", "failing": "FAILED",
                 "not_configured": "NOT RUN"}.get(p.get("llm_connectivity"), "NOT RUN")
    c1, c2 = st.columns(2)
    with c1.container(border=True):
        st.markdown("**Real API monitoring**", unsafe_allow_html=True)
        st.caption(f"{p['upstreams']} · approved hosts: {p['approved_hosts']} · registered real APIs: {p['real_tools']}")
        st.markdown(f"Repair model: `{p['llm']}` {badge(llm_state)}", unsafe_allow_html=True)
    with c2.container(border=True):
        st.markdown("**Simulation Lab** (separate records)")
        st.caption(f"{m['simulation']['upstreams']} · {m['simulation']['llm']}")

    g = rep["gemini"]
    st.subheader(f"Google Gemini · `{g['model']}`")
    _row("SDK installed", g["sdk"])
    _row("Credentials", g["credentials"])
    _row("Real request", g["real_request"])
    _row("Used by the repair pipeline", g["pipeline"])
    if g["credentials"]["status"] == "CONFIGURED":
        if st.button("Send a real test request to Gemini", icon=":material/network_check:",
                     help="One minimal request (a few tokens) using the configured key"):
            with st.spinner("Calling Gemini…"):
                r = safe_call(api, "POST", "/api/health/llm-check")
            if r:
                st.session_state["llm_check"] = r["result"]
                st.rerun()
        res = st.session_state.get("llm_check")
        if res:
            (st.success if res["ok"] else st.error)(
                f"Last check {ist_time(res['ts'])}: {'succeeded' if res['ok'] else 'failed'} · "
                f"{res.get('error_category') or 'ok'} · {res.get('duration_ms')} ms · "
                f"credential {res.get('credential_alias') or '—'}")

    ext = rep["external"]
    st.subheader("External APIs")
    _row("Approved hosts", ext["allowed_hosts"])
    if not ext["apis"]:
        st.info("No real API is registered. Use API Registry to add one.", icon=":material/info:")
    for a in ext["apis"]:
        with st.container(border=True):
            st.markdown(f"**{a['provider']}** · `{a['tool']}` · `{a['endpoint']}`")
            _row("Host approved", a["host_allowed"])
            _row("Credentials", a["credentials"])
            _row("Connectivity", a["connectivity"])
            _row("Schema discovery", a["schema_discovery"])
            _row("Used by the repair pipeline", a["pipeline"])
            st.caption("Last successful check: " + (ist(a["last_successful_check"])
                                                     if a["last_successful_check"] else "none yet"))

    st.subheader("Credential registry (production)")
    st.dataframe([{"alias": c["alias"], "group": c["group"], "provider": c["provider"], "header": c["header"],
                   "configured": c["configured"] and c["state"] != "missing", "state": c["state"],
                   "last failure": c["last_failure_category"], "fails over on": ", ".join(c["failover_on"])}
                  for c in h["credentials"]], hide_index=True, use_container_width=True)
    st.caption("Failover moves only to another authorized credential in the same group, for the listed categories. "
               "A rejected key is never modified, and an authentication error is never treated as schema drift.")
    st.subheader("Limits")
    st.json(h["limits"] | {"scheduler": h["monitor"]})
