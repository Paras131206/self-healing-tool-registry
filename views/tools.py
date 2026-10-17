"""Version history, rollback and deletion for one registered tool (used inside API Registry)."""
import streamlit as st

from views.common import api, go, mutated, safe_call


def versions_panel(prefix: str, name: str, current_version: int):
    vers = safe_call(api, "GET", f"{prefix}/tools/{name}/versions")
    if vers is None:
        return
    for v in reversed(vers):
        line = f"**v{v['version']}** · {v['status']} · {v['origin']}" + (f" · from v{v['parent_version']}"
                                                                        if v["parent_version"] else "")
        if v["patch"]:
            line += " · " + "; ".join(f"{p.get('argument') or p.get('header') or p.get('output')}: "
                                      f"{p.get('from') or '—'} -> {p.get('to') or p.get('credential_group') or '—'}"
                                      for p in v["patch"])
        st.markdown(line)
        if v["incident_id"] and st.button("Open incident", key=f"vinc-{name}-{v['version']}"):
            env = {"/api": "REAL API", "/api/test": "REAL API TEST", "/api/sim": "SIMULATION",
                   "/api/local": "LOCAL DEMO"}[prefix]
            go("recovery", incident_id=v["incident_id"], env_choice=env)
    c1, c2 = st.columns(2)
    if c1.button("Roll back current version", key=f"rb-{name}", icon=":material/undo:", disabled=current_version <= 1,
                 help="Restores the parent of the current version"):
        res = safe_call(api, "POST", f"{prefix}/tools/{name}/rollback")
        if res:
            mutated()
            st.success(f"Rolled back to v{res['version']}")
            st.rerun()
    if c2.checkbox("Confirm delete", key=f"delok-{name}") and c2.button("Delete tool", key=f"del-{name}",
                                                                        icon=":material/delete:"):
        if safe_call(api, "DELETE", f"{prefix}/tools/{name}"):
            mutated()
            st.rerun()


def render():
    """Tool Registry: the configuration and version history of every registered tool (any environment).
    API Registry is where real APIs are registered and monitored; this page is the inventory of what is registered."""
    import pandas as pd

    from views.common import ENV_PREFIX, badge, empty, env_picker, header, ist
    header("Tool Registry", "Every registered tool: configuration, schema source and versions",
           "Inspect how each tool is configured and which version is active. To register a real API or schedule its "
           "checks, use API Registry. To compare a tool with its live schema, use Schema Drift Explorer.")
    env = env_picker("tr_env")
    prefix = ENV_PREFIX[env]
    tools = safe_call(api, "GET", f"{prefix}/tools")
    if tools is None:
        return
    if not tools:
        empty("No tools are registered in this environment yet.", "Open API Registry", "monitoring")
        return
    st.dataframe(pd.DataFrame([{
        "tool": t["name"], "method": t["method"], "endpoint": f"{t['base_url'].rstrip('/')}{t['path']}",
        "schema source": t.get("schema_source", "openapi"), "version": t["current_version"], "status": t["health"],
        "last check": ist((t.get("last_check") or {}).get("ts")) if t.get("last_check") else "—"} for t in tools]),
        hide_index=True, use_container_width=True,
        column_config={"endpoint": st.column_config.TextColumn(width="large")})
    names = [t["name"] for t in tools]
    want = st.session_state.get("tool")
    sel = st.selectbox("Tool", names, index=names.index(want) if want in names else 0, key=f"tr_tool_{env}")
    st.session_state["tool"] = sel                       # shared with Schema Drift Explorer and the other pages
    t = safe_call(api, "GET", f"{prefix}/tools/{sel}")
    if not t:
        return
    spec = t["spec"]
    st.markdown(f"### `{sel}` &nbsp; {badge(t['health'])}", unsafe_allow_html=True)
    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.markdown("**Configuration**")
        st.dataframe(pd.DataFrame([
            ("Provider", spec.get("provider") or "—"), ("HTTP method", spec["method"]),
            ("API endpoint", f"{spec['base_url'].rstrip('/')}{spec['path']}"),
            ("Schema source", spec["schema_source"]["kind"]),
            ("Schema URL", "none (no published schema)" if spec["schema_source"]["kind"] == "none"
             else (spec["schema_source"].get("url") or f"{spec['base_url'].rstrip('/')}/openapi.json")),
            ("Active version", f"v{t['current_version']}"), ("Status", t["health"]),
            ("Credential", f"header {spec['auth_header']} from group '{spec['credential_group']}'"
             if spec.get("auth_header") else "none (values never shown)"),
            ("Environment", env)], columns=["setting", "value"]), hide_index=True, use_container_width=True)
        a1, a2 = st.columns(2)
        if a1.button("Compare with live schema", icon=":material/difference:", key=f"cmp-{sel}"):
            go("drift", tool=sel, env_choice=env)
        if env == "REAL API" and a2.button("Check now", icon=":material/refresh:", key=f"chk-tr-{sel}",
                                           help="Reads the live schema and runs the probe, as in API Registry"):
            if safe_call(api, "POST", f"/api/monitor/{sel}/check"):
                mutated()
                st.rerun()
    with c2:
        reg = safe_call(api, "GET", f"{prefix}/tools/{sel}/schema")
        st.markdown(f"**Registered schema** (v{reg['version'] if reg else '?'}, fingerprint "
                    f"`{reg['signature'] if reg else '—'}`)")
        st.caption("What the active tool version was built against. This is a stored snapshot, not the live "
                   "authoritative schema: use Compare with live schema for that.")
        if reg:
            st.dataframe(pd.DataFrame(reg["fields"]), hide_index=True, use_container_width=True)
    with st.expander("Caller arguments (as exposed to agents)"):
        st.json(t["arguments"], expanded=False)
    with st.expander("Versions, rollback and delete", expanded=True):
        versions_panel(prefix, sel, t["current_version"])
