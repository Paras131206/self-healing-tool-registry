"""Schema change log (shown inside Evidence & Scorecard): one row per detected change, from stored records only."""
import json
from datetime import date

import pandas as pd
import streamlit as st

from views.common import api, go, header, ist, safe_call

ENV_TEXT = {"LIVE_API": "REAL API", "REAL_API_TEST": "REAL API TEST", "SIMULATION": "SIMULATION", "LOCAL_DEMO": "LOCAL DEMO"}
ENV_PREFIX = {"LIVE_API": "/api", "REAL_API_TEST": "/api/test", "SIMULATION": "/api/sim", "LOCAL_DEMO": "/api/local"}
ENV_CHOICE = {"LIVE_API": "REAL API", "REAL_API_TEST": "REAL API TEST", "SIMULATION": "SIMULATION", "LOCAL_DEMO": "LOCAL DEMO"}
REPAIR_ICON = {"Detected": "○", "Analyzed": "◔", "Proposed": "◑", "Validated": "◕", "Applied": "●",
               "Verified": "✔", "Failed": "✖", "Rolled back": "↺", "Requires review": "⚠"}
FILTER_KEYS = ["chg_q", "chg_env", "chg_type", "chg_status", "chg_ver", "chg_dates", "chg_sort", "chg_order", "chg_page"]


def _fmt(v, limit: int = 80) -> str:
    if v is None:
        return "—"
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _clear():
    for k in FILTER_KEYS:
        st.session_state.pop(k, None)


def _details(r: dict):
    d = r.get("details") or {}
    st.markdown(f"**{r['change_type']}** · `{r['field_path']}` · {r['tool']} · {ENV_TEXT[r['environment']]} · "
                f"{REPAIR_ICON.get(r['repair_status'], '')} {r['repair_status']} · verification: {r['verification']}")
    c1, c2 = st.columns(2)
    c1.markdown("**Before**")
    c1.code(_fmt(r.get("previous"), 2000), language=None)
    c2.markdown("**After**")
    c2.code(_fmt(r.get("new"), 2000), language=None)
    t1, t2, t3, t4 = st.tabs(["Schema evidence", "Repair & validation", "Retry & verification", "Timeline"])
    with t1:
        st.markdown(f"Drift summary: **{d.get('drift_summary') or '—'}** · compatibility {d.get('compatibility') or '—'}")
        st.markdown(f"Schema fingerprint before `{d.get('schema_before') or 'not stored'}` → after "
                    f"`{d.get('schema_after') or 'not stored'}`")
        st.markdown(f"Authoritative schema source: `{d.get('schema_evidence_source') or 'not stored'}`")
        if r.get("change_detail"):
            st.caption(f"Detector note: {r['change_detail']}")
    with t2:
        m = d.get("mapping")
        if m:
            st.markdown(f"Proposed mapping (planner `{d.get('planner') or '—'}`):")
            st.json(m)
        else:
            st.caption("No mapping was proposed for this field.")
        if d.get("llm"):
            l = d["llm"]
            st.caption(f"LLM: {l.get('provider')} {l.get('model')} · "
                       f"{'REAL request' if l.get('real') else 'not a real request'} · {l.get('error_category') or 'ok'}")
        acc = d.get("validation_accepted")
        st.markdown("Independent validation: " + ("**accepted**" if acc else "**rejected**" if acc is False else "not run"))
        if d.get("validation_checks"):
            st.dataframe(pd.DataFrame(d["validation_checks"]), hide_index=True, use_container_width=True)
    with t3:
        st.markdown(f"Retries: {d.get('retries') if d.get('retries') is not None else '—'} · "
                    f"versions: v{(d.get('versions') or {}).get('from') or '?'} → v{(d.get('versions') or {}).get('to') or '—'}"
                    f" · rolled back: {d.get('rolled_back') if d.get('rolled_back') is not None else '—'}")
        if d.get("verification_checks"):
            st.dataframe(pd.DataFrame(d["verification_checks"]), hide_index=True, use_container_width=True)
        if d.get("monitor_check"):
            st.json(d["monitor_check"])
        for label, key in (("First request (redacted)", "request"), ("Final response (redacted)", "final_response")):
            if d.get(key):
                with st.expander(label):
                    st.json(d[key])
        if not any(d.get(k) for k in ("verification_checks", "request", "monitor_check")):
            st.caption("No retry or verification evidence is stored for this change.")
    with t4:
        if d.get("timeline"):
            st.dataframe(pd.DataFrame(d["timeline"]), hide_index=True, use_container_width=True)
        else:
            st.caption("No incident timeline (detected by a monitoring check without a failing call).")
    a1, a2, a3 = st.columns(3)
    if r.get("incident_id") and a1.button("View incident", key=f"vi-{r['change_id']}", icon=":material/timeline:"):
        go("recovery", incident_id=r["incident_id"], env_choice=ENV_CHOICE[r["environment"]])
    a2.download_button("Export evidence (JSON)", json.dumps(r, indent=2, default=str),
                       f"change-{r['change_id'].replace('#', '-')}.json", "application/json",
                       key=f"dl-{r['change_id']}", icon=":material/download:")
    if a3.button("Compare schemas", key=f"cs-{r['change_id']}", icon=":material/difference:"):
        go("drift", tool=r["tool"], env_choice=ENV_CHOICE[r["environment"]])


def render_table(default_env: list[str] | None = None, key: str = "chg"):
    ss = st.session_state
    f1, f2, f3 = st.columns([2, 1.3, 1.3])
    q = f1.text_input("Search API, field path, change or incident ID", key="chg_q", placeholder="e.g. client_id")
    env = f2.multiselect("Environment", list(ENV_TEXT), default=default_env, format_func=ENV_TEXT.get, key="chg_env")
    sort = f3.selectbox("Sort by", ["timestamp", "tool", "change_type", "status"], key="chg_sort",
                        format_func={"timestamp": "Newest / oldest", "tool": "API / tool", "change_type": "Change type",
                                     "status": "Repair status"}.get)
    base = safe_call(api, "GET", "/api/changes", params={"page_size": 1})
    if base is None:
        return
    facets = base["facets"]
    g1, g2, g3, g4, g5 = st.columns([1.4, 1.3, 1.1, 1.4, 0.8])
    ctype = g1.multiselect("Change type", facets["change_type"], key="chg_type")
    status = g2.multiselect("Repair status", facets["repair_status"], key="chg_status")
    ver = g3.multiselect("Verification", facets["verification"], key="chg_ver")
    dates = g4.date_input("Date range", value=(), key="chg_dates", format="YYYY-MM-DD")
    order = g5.selectbox("Order", ["desc", "asc"], key="chg_order")
    params = {"q": q, "environment": env, "change_type": ctype, "repair_status": status, "verification": ver,
              "sort": sort, "order": order, "page_size": 25, "page": ss.get("chg_page", 1)}
    if isinstance(dates, (list, tuple)) and len(dates) == 2:
        params["since"], params["until"] = str(dates[0]), str(dates[1])
    elif isinstance(dates, date):
        params["since"] = params["until"] = str(dates)
    with st.spinner("Loading changes…"):
        res = safe_call(api, "GET", "/api/changes", params=params)
    if res is None:
        return
    top = st.columns([3, 1])
    top[0].caption(f"Showing {len(res['rows'])} of {res['total']} matching change(s) · {res['unfiltered_total']} in total · "
                   f"page {res['page']} of {res['pages']}")
    if top[1].button("Clear filters", icon=":material/filter_alt_off:", on_click=_clear):
        st.rerun()
    if res["unfiltered_total"] == 0:
        st.info("No schema changes recorded yet. Changes appear when a monitoring check, a REAL API TEST run or a "
                "simulation detects drift.", icon=":material/info:")
        return
    if not res["rows"]:
        st.warning("No change matches these filters.", icon=":material/search_off:")
        return
    df = pd.DataFrame([{
        "Change ID": r["change_id"], "API / Tool": r["tool"], "Environment": ENV_TEXT[r["environment"]],
        "Field path": r["field_path"], "Change type": r["change_type"],
        "Repair status": f"{REPAIR_ICON.get(r['repair_status'], '')} {r['repair_status']}",
        "Verification": r["verification"], "Timestamp (IST)": ist(r["timestamp"])}
        for r in res["rows"]])
    sel = st.dataframe(df, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                       key=f"{key}-table", column_config={
                           "Change ID": st.column_config.TextColumn(width="medium"),
                           "Field path": st.column_config.TextColumn(width="medium"),
                           "Timestamp (IST)": st.column_config.TextColumn(width="medium")})
    p1, p2, p3 = st.columns([1, 2, 1])
    if p1.button("◀ Previous", disabled=res["page"] <= 1, key=f"{key}-prev"):
        ss["chg_page"] = res["page"] - 1
        st.rerun()
    p2.markdown(f"<div style='text-align:center'>page {res['page']} / {res['pages']}</div>", unsafe_allow_html=True)
    if p3.button("Next ▶", disabled=res["page"] >= res["pages"], key=f"{key}-next"):
        ss["chg_page"] = res["page"] + 1
        st.rerun()
    rows_sel = (sel.selection.rows if sel and hasattr(sel, "selection") else []) or []
    if rows_sel:
        with st.container(border=True):
            _details(res["rows"][rows_sel[0]])
    else:
        st.caption("Select a row to see full before/after values, schema evidence, the repair, validation, retry, "
                   "verification and the incident timeline.")

