import streamlit as st

from views.common import api, badge, BASELINE_BADGE, empty, fmt_ms, header, ist, ist_time, safe_call, SIM_BADGE, STAGE_ICON

DOT = {"ok": "#047857", "warn": "#B45309", "fail": "#B91C1C", "info": "#2563EB"}


def timeline(inc: dict):
    for s in inc["timeline"]:
        icon = STAGE_ICON.get(s["stage"], "radio_button_checked")
        st.markdown(f'<div class="ax-step" style="border-left-color:{DOT[s["status"]]}">'
                    f'<span class="ax-eyebrow" style="color:{DOT[s["status"]]}">{s["stage"]}</span> &nbsp; '
                    f'<b>{s["title"]}</b> <span style="color:#94A3B8;font-size:.8rem">{ist_time(s["ts"], millis=True)}</span></div>',
                    unsafe_allow_html=True)
        if s.get("detail"):
            with st.expander(f":material/{icon}: details", expanded=False):
                if isinstance(s["detail"], (dict, list)):
                    st.json(s["detail"], expanded=True)
                else:
                    st.write(s["detail"])


PHASES = [("Failure detected", "request"), ("Schema fetched", "discover"), ("Drift identified", "diff"),
          ("Repair proposed", "plan"), ("Repair validated", "validate"), ("Change applied", "apply"),
          ("Retry executed", "retry"), ("Task verified / rolled back", "final")]
from views.common import ENV_PREFIX  # noqa: E402  (one shared environment map)


def phases(inc: dict) -> list[dict]:
    """The recovery as eight phases, each with its actual state and timestamp from the stored timeline."""
    tl = inc["timeline"]
    out = []
    for label, key in PHASES:
        if key == "request":
            s = next((x for x in tl if x["stage"] == "request" and x["status"] == "fail"), None)
            state = "done" if s else "not reached"
        elif key == "retry":
            s = next((x for x in tl if x["stage"] == "request" and "retry" in x["title"]), None)
            state = "not reached" if s is None else ("done" if s["status"] == "ok" else "failed")
        elif key == "final":
            s = next((x for x in reversed(tl) if x["stage"] in ("verify", "rollback")), None)
            if s is None:
                state = "not reached"
            elif s["stage"] == "rollback":
                state = "rolled back"
            else:
                state = "verified" if s["status"] == "ok" else "failed"
        elif key == "plan":
            s = next((x for x in reversed(tl) if x["stage"] == "plan"), None)
            state = "not reached" if s is None else ("proposed" if inc.get("plan") and
                                                     any(m.get("source_arg") for m in inc["plan"].get("mappings", []))
                                                     else "no proposal")
        else:
            s = next((x for x in reversed(tl) if x["stage"] == key), None)
            state = "not reached" if s is None else {"ok": "done", "fail": "failed", "warn": "done", "info": "done"}[s["status"]]
            if key == "validate" and s is not None:
                state = "validated" if s["status"] == "ok" else "rejected"
            if key == "apply" and s is not None:
                state = "applied"
        out.append({"phase": label, "state": state, "ts": s["ts"] if s else None, "evidence": s["title"] if s else ""})
    return out


PHASE_COLOR = {"done": "#047857", "proposed": "#1D4ED8", "validated": "#047857", "applied": "#047857",
               "verified": "#047857", "failed": "#B91C1C", "rejected": "#B91C1C", "rolled back": "#6D28D9",
               "no proposal": "#B45309", "not reached": "#94A3B8"}


def phase_strip(inc: dict):
    ph = phases(inc)
    cols = st.columns(len(ph))
    for c, p in zip(cols, ph):
        color = PHASE_COLOR.get(p["state"], "#475569")
        c.markdown(f"<div style='border-top:4px solid {color};padding-top:.35rem'><div style='font-size:.78rem;"
                   f"color:#475569;font-weight:650'>{p['phase']}</div><div style='color:{color};font-weight:700'>"
                   f"{p['state'].upper()}</div><div style='font-size:.72rem;color:#94A3B8'>"
                   f"{ist_time(p['ts'], millis=True) if p['ts'] else ''}</div></div>", unsafe_allow_html=True)
    with st.expander("Phase evidence"):
        st.dataframe([{"phase": p["phase"], "state": p["state"], "time": ist_time(p["ts"], millis=True) if p["ts"] else "—", "evidence": p["evidence"]}
                      for p in ph], hide_index=True, use_container_width=True)


def render(prefix: str | None = None):
    switchable = prefix is None
    if switchable:
        from views.common import env_picker
        env = env_picker("rc_env")
        prefix = ENV_PREFIX[env]
    sim = prefix == "/api/sim"
    title = {"/api": "Recovery Center", "/api/test": "Real API Test Records", "/api/sim": "Simulation Records",
             "/api/local": "Local Demo Records"}[prefix]
    header(title, "Every incident, step by step",
           "Failure detected → schema fetched → drift identified → repair proposed → validated → applied → retry → "
           "task verified or rolled back. Each phase shows its actual state, time and evidence.")
    if sim:
        st.markdown(f"{SIM_BADGE} &nbsp; Simulation database only.", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    status = c1.selectbox("Outcome", ["all", "recovered", "stopped", "failed", "rolled_back"])
    params = {"limit": 500}
    if status != "all":
        params["status"] = status
    incs = safe_call(api, "GET", f"{prefix}/incidents", params=params)
    if incs is None:
        return
    tools = sorted({i["tool"] for i in incs})
    tool = c2.selectbox("API / tool", ["all"] + tools)
    cats = sorted({i["category"] for i in incs})
    cat = c3.selectbox("Failure category", ["all"] + cats)
    days = c4.selectbox("Time range", ["all time", "last 24 hours", "last 7 days"])
    from datetime import datetime, timedelta, timezone
    cutoff = {"last 24 hours": 1, "last 7 days": 7}.get(days)
    since = (datetime.now(timezone.utc) - timedelta(days=cutoff)).isoformat() if cutoff else ""
    incs = [i for i in incs if (tool == "all" or i["tool"] == tool) and (cat == "all" or i["category"] == cat)
            and i["started_at"] >= since]
    st.caption(f"{len(incs)} incident(s) match.")
    if not incs:
        empty("No incidents match. Incidents are created when a tool call fails; a successful call creates none.")
        return
    ids = [i["id"] for i in incs]
    want = st.session_state.get("incident_id")
    labels = {i["id"]: f"{ist(i['started_at'])} · {i['tool']} · {i['status']} · {i['category']}"
              + ("" if i.get("healing", True) else " · baseline") for i in incs}
    sel = st.selectbox("Incident", ids, index=ids.index(want) if want in ids else 0, format_func=labels.get)
    inc = safe_call(api, "GET", f"{prefix}/incidents/{sel}")
    if not inc:
        return
    st.markdown(f"### {badge(inc['status'])} &nbsp; `{inc['tool']}` " + (SIM_BADGE if inc["simulated"] else "")
                + ("" if inc.get("healing", True) else " " + BASELINE_BADGE), unsafe_allow_html=True)
    st.markdown(f"**Reason:** {inc['reason'] or '—'}")
    st.caption(f"Environment: {inc.get('environment') or 'not recorded'}")
    phase_strip(inc)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Category", inc["category"].replace("_", " "))
    m2.metric("Duration", fmt_ms(inc["duration_ms"]))
    m3.metric("Retries", inc["retries"])
    m4.metric("Repair attempts", inc["repair_attempts"])
    m5.metric("Version", f"v{inc['from_version']} → v{inc['to_version']}" if inc["to_version"] else
              (f"v{inc['from_version']}" if inc["from_version"] else "—"))
    st.caption(f"incident {inc['id']} · correlation {inc['correlation_id']}"
               + (f" · scenario {inc['scenario']}" if inc.get("scenario") else "")
               + (" · zero-touch" if inc["zero_touch"] else "") + (" · rolled back" if inc["rolled_back"] else ""))
    left, right = st.columns([1.4, 1], gap="large")
    with left:
        st.subheader("Timeline")
        timeline(inc)
    with right:
        if inc.get("verification"):
            v = inc["verification"]
            st.subheader("Task verification")
            (st.success if v.get("ok") else st.error)("Task verified" if v.get("ok") else "Task not verified")
            for c in v.get("checks", []):
                st.markdown(f"{'✅' if c.get('ok') else '❌'} {c.get('name')}"
                            + (f" · {c['detail']}" if c.get("detail") else ""))
            if v.get("contract_drift"):
                st.warning("Expected response fields are missing: possible response-schema drift.")
        if inc.get("llm"):
            l = inc["llm"]
            st.subheader("LLM call")
            st.caption("real provider request" if l.get("real") else "not a real provider request (mock or disabled)")
            st.json({k: l.get(k) for k in ("provider", "model", "ok", "error_category", "duration_ms",
                                           "prompt_tokens", "output_tokens", "attempts", "credential_alias")})
        if inc.get("result") is not None:
            st.subheader("Mapped result")
            st.json(inc["result"], expanded=False)
        events = safe_call(api, "GET", f"{prefix}/audit", params={"correlation_id": inc["correlation_id"], "limit": 50}) or []
        if events:
            st.subheader("Audit events")
            st.dataframe([{"time": ist_time(e["ts"]), "event": e["kind"]} for e in events], hide_index=True,
                         use_container_width=True)
