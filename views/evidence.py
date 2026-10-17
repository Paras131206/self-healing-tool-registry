"""Evidence & Scorecard: honest pipeline counts, one selected incident in detail, audit history and exports."""
import json

import pandas as pd
import streamlit as st

from views.common import api, badge, env_picker, ENV_PREFIX, fmt_ms, header, ist, safe_call


def _scorecard(sc: dict):
    p = sc["pipeline"]
    rows = [("Incidents recorded", p["incidents"], "failed calls that needed attention"),
            ("Schema drift detected", p["detected_drift"], "confirmed against the authoritative schema"),
            ("Repairs proposed", p["proposed_repairs"], "a proposal is not a repair"),
            ("Patches validated", p["validated_patches"], "accepted by independent validation"),
            ("Patches applied", p["applied_patches"], "new tool version written"),
            ("Verified recoveries", p["verified_recoveries"], "original task verified after the patch"),
            ("Stopped safely", p["refused_or_stopped"], "unsafe or not repairable: nothing applied"),
            ("Failures", p["failures"], "pipeline errors"),
            ("Rollbacks", p["rollbacks"], "patch reverted after failed verification")]
    st.dataframe(pd.DataFrame(rows, columns=["stage", "count", "meaning"]), hide_index=True, use_container_width=True)
    lat = p["latency"]
    st.caption((f"Median verified recovery {fmt_ms(lat['median_ms'])} over {lat['n']} recoveries"
                + (f" · p90 {fmt_ms(lat['p90_ms'])}" if lat["p90_ms"] else "") + (f" · {lat['note']}" if lat["note"] else ""))
               if lat["n"] else "No verified recoveries yet, so no recovery time is reported.")
    st.caption(sc["disclosure"])


def _patch(prefix: str, inc: dict):
    vers = safe_call(api, "GET", f"{prefix}/tools/{inc['tool']}/versions") or []
    v = next((x for x in vers if x.get("incident_id") == inc["id"]), None)
    if not v or not v.get("patch"):
        st.caption("No patch was applied for this incident.")
        return
    lines = []
    for p in v["patch"]:
        what = p.get("argument") or p.get("header") or p.get("output")
        lines += [f"- {what}: {p.get('from') or '(none)'}",
                  f"+ {what}: {p.get('to') or p.get('credential_group') or '(removed)'}"
                  + (f"  [{p['transform']}]" if p.get("transform") not in (None, "none") else "")]
    st.code("\n".join(lines), language="diff")
    st.caption(f"Tool v{v.get('parent_version')} → v{v['version']} · current status of that version: **{v['status']}**")


def _selected(prefix: str, inc: dict):
    from views.recovery import phase_strip
    with st.container(border=True):
        st.markdown(f"### {badge(inc['status'])} &nbsp; `{inc['tool']}`", unsafe_allow_html=True)
        st.caption(f"{inc.get('environment') or ''} · {inc['category'].replace('_', ' ')} · started "
                   f"{ist(inc['started_at'])} · duration {fmt_ms(inc['duration_ms'])} · "
                   f"retries {inc['retries']} · schema `{inc.get('old_signature') or '—'}` → `{inc.get('new_signature') or '—'}`"
                   + (f" · engine {inc['llm'].get('provider')} ({'real request' if inc['llm'].get('real') else 'not a real request'})"
                      if inc.get("llm") else " · engine: rules"))
        st.write(inc["reason"] or "")
        phase_strip(inc)
        c1, c2 = st.columns(2, gap="large")
        with c1:
            st.markdown("**Field-level changes**")
            if inc.get("drift"):
                d = inc["drift"]
                st.dataframe([{"change": c["kind"].replace("_", " "), "field": c["location"], "before": c.get("old") or "—",
                               "after": c.get("new") or "—"} for c in d["request_changes"] + d["response_changes"]],
                             hide_index=True, use_container_width=True)
            else:
                st.caption("No schema drift: this incident was not a schema problem.")
            st.markdown("**Patch applied**")
            _patch(prefix, inc)
        with c2:
            v = inc.get("validation")
            st.markdown("**Validation**: " + ("not reached" if not v else "accepted" if v["accepted"] else "rejected"))
            if v:
                with st.expander("Validation checks"):
                    st.dataframe(pd.DataFrame(v["checks"]), hide_index=True, use_container_width=True)
            ver = inc.get("verification")
            st.markdown("**Verification**: " + ("not reached" if not ver else "verified" if ver.get("ok") else "failed"))
            for c in (ver or {}).get("checks", []):
                st.markdown(f"{'✅' if c.get('ok') else '❌'} {c.get('name')} {('· ' + c['detail']) if c.get('detail') else ''}")
            st.markdown("**Rollback**: " + ("performed" if inc.get("rolled_back") else
                                            "attempted, not possible" if any(s["stage"] == "rollback" for s in inc["timeline"])
                                            else "not needed"))
        with st.expander("Raw incident record (redacted)"):
            st.json(inc, expanded=False)


def render():
    header("Evidence & Scorecard", "What actually happened, from stored records",
           "Counts and details come from the same incident records every page reads. Pick an incident to see its "
           "timeline, field-level changes, the patch, validation, verification and rollback.")
    env = env_picker("ev_env")
    prefix = ENV_PREFIX[env]
    sc = safe_call(api, "GET", f"{prefix}/scorecard")
    incs = safe_call(api, "GET", f"{prefix}/incidents", params={"limit": 300})
    if sc is None or incs is None:
        return
    st.subheader("Scorecard")
    _scorecard(sc)
    st.subheader("Incidents")
    if not incs:
        st.info(f"No incidents recorded in {env} yet. Run the Drift Demo, or call a registered API.", icon=":material/inbox:")
    else:
        ids = [i["id"] for i in incs]
        want = st.session_state.get("ev_pick")
        labels = {i["id"]: f"{ist(i['started_at'])} · {i['tool']} · {i['status']} · "
                           f"{i['category'].replace('_', ' ')}" + ("" if i.get("healing", True) else " · comparison run")
                  for i in incs}
        pick = st.selectbox("Incident", ids, index=ids.index(want) if want in ids else 0, format_func=labels.get,
                            key=f"ev_inc_{env}")
        _selected(prefix, next(i for i in incs if i["id"] == pick))
    with st.expander("Audit history"):
        ev = safe_call(api, "GET", f"{prefix}/audit", params={"limit": 300}) or []
        if ev:
            st.dataframe(pd.DataFrame([{"time": ist(e["ts"]), "event": e["kind"], "tool": e["tool"],
                                        "correlation": (e["correlation_id"] or "")[:12],
                                        "details": json.dumps(e["data"], default=str)[:140]} for e in ev]),
                         hide_index=True, use_container_width=True,
                         column_config={"details": st.column_config.TextColumn(width="large")})
            st.caption("Payloads are redacted before they are stored: no credentials, authorization headers or tokens.")
        else:
            st.caption("No audit events yet.")
    with st.expander("Schema change log"):
        from views.changes import render_table
        render_table([{"REAL API": "LIVE_API", "REAL API TEST": "REAL_API_TEST", "SIMULATION": "SIMULATION",
                       "LOCAL DEMO": "LOCAL_DEMO"}[env]],
                     key=f"ev-chg-{env}")
    bundle = safe_call(api, "GET", f"{prefix}/evidence/export.json")
    csv_text = safe_call(api, "GET", f"{prefix}/evidence/export.csv")
    d1, d2 = st.columns(2)
    has = bool(bundle and bundle.get("records"))
    d1.download_button("Export evidence (JSON)", json.dumps(bundle or {}, indent=2, default=str),
                       f"adaptix-evidence-{env.lower().replace(' ', '-')}.json", "application/json",
                       icon=":material/download:", disabled=not has)
    d2.download_button("Export report (CSV)", csv_text if isinstance(csv_text, str) else "",
                       f"adaptix-evidence-{env.lower().replace(' ', '-')}.csv", "text/csv", icon=":material/table:",
                       disabled=not has)
