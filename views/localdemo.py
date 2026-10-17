"""Local Demo Lab (LOCAL DEMO only, never on the deployed site): change the local demo database's schema after
confirmation and watch ADAPTIX recover with a local model. Every step is labelled with what really ran."""
import json
import time

import streamlit as st

from views.common import api, badge, fmt_ms, header, ist, mutated, safe_call
from views.demo import ICON

LOCAL_BADGE = ('<span class="ax-badge" style="background:#ECFEFF;color:#155E75;border-color:#67E8F9">'
               'LOCAL DEMO · this machine only</span>')
CHIP = {  # one status vocabulary, used only when the record shows it happened
    "LOCAL REAL EXECUTION": ("#155E75", "#ECFEFF"), "PROPOSED": ("#1D4ED8", "#EFF6FF"),
    "VALIDATED": ("#6D28D9", "#F5F3FF"), "APPLIED": ("#B45309", "#FFFBEB"), "VERIFIED": ("#047857", "#ECFDF5"),
    "SAFELY BLOCKED": ("#B45309", "#FFFBEB"), "ROLLED BACK": ("#6D28D9", "#F5F3FF"), "FAILED": ("#B91C1C", "#FEF2F2"),
    "NOT RUN": ("#475569", "#F1F5F9"), "SIMULATED": ("#92400E", "#FEF3C7"), "SCHEMA DIFFERS": ("#B45309", "#FFFBEB")}
SCENARIO_ORDER = ["rename", "type", "required", "remove"]


def chip(label: str) -> str:
    fg, bg = CHIP.get(label.split(" (")[0], ("#334155", "#F1F5F9"))
    return f'<span class="ax-badge" style="color:{fg};background:{bg};border-color:{fg}33">{label}</span>'


def _readiness(stt: dict):
    db, svc, m = stt["database"], stt["service"], stt["model"]
    c1, c2, c3 = st.columns(3)
    with c1.container(border=True):
        st.markdown("**Demo database** " + badge("VERIFIED" if db["status"] == "ready" else "ACTION REQUIRED"),
                    unsafe_allow_html=True)
        if db["status"] == "ready":
            n = db["counts"]
            st.caption(f"SQLite · {n['customers']} customers · {n['products']} products · {n['orders']} orders · "
                       f"{n['order_items']} order items · integrity ok · key column `{db['key_column']}`")
        else:
            st.caption(db.get("action") or db.get("detail") or db["status"])
    with c2.container(border=True):
        st.markdown("**Local customer API** " + badge("VERIFIED" if svc["status"] == "running" else "ACTION REQUIRED"),
                    unsafe_allow_html=True)
        st.caption("real HTTP on loopback · contract generated from the live database" if svc["status"] == "running"
                   else svc.get("action") or svc.get("detail", ""))
    with c3.container(border=True):
        ready = m["server"] == "running" and m["model_installed"]
        st.markdown(f"**Local LLM** `{m['model']}` " + badge("CONFIGURED" if ready else "ACTION REQUIRED"),
                    unsafe_allow_html=True)
        if ready:
            size = f" · {m['size_bytes'] / 1e9:.1f} GB on disk" if m.get("size_bytes") else ""
            st.caption(f"Ollama {m.get('version') or ''} on this machine{size}. Not Gemini; no cloud fallback.")
        else:
            st.caption(m.get("action") or "")
        if ready and st.button("Send one test request to the local model", key="lm_check"):
            with st.spinner("Asking the local model…"):
                r = safe_call(api, "POST", "/api/local-demo/model-check")
            if r:
                (st.success if r["ok"] else st.error)(
                    f"{'Answered' if r['ok'] else 'Failed'} in {fmt_ms(r['duration_ms'])}"
                    + ("" if r["ok"] else f" · {r['error_category']}: {r.get('error') or ''}"))


def _contracts():
    c = safe_call(api, "GET", "/api/local-demo/contracts")
    if not c:
        return
    if c.get("error"):
        st.warning(f"Live contract unavailable: {c['error']}", icon=":material/cloud_off:")
    d = c.get("diff")
    if d:
        if d["changed"]:
            st.markdown(chip("SCHEMA DIFFERS") + f" &nbsp; registered vs live: **{d['summary']}**", unsafe_allow_html=True)
            st.dataframe(d["changes"], hide_index=True, use_container_width=True)
        else:
            st.markdown(chip("VERIFIED") + " &nbsp; the registered contract matches the live contract",
                        unsafe_allow_html=True)
    a, b = st.columns(2)
    a.markdown(f"**Registered contract** (ADAPTIX, tool v{c.get('version') or '—'})")
    a.dataframe(c.get("registered") or [], hide_index=True, use_container_width=True)
    b.markdown("**Live contract** (read from the local API now)")
    b.dataframe(c.get("live") or [], hide_index=True, use_container_width=True)


def _events(job: dict, box):
    with box.container():
        for e in job["events"]:
            st.markdown(f"{ICON.get(e['status'], '•')} **{e['phase']}** · {e['title']} "
                        f"<span style='color:#64748B;font-size:.8rem'>{ist(e['ts'])} · +{fmt_ms(e['elapsed_ms'])}</span>",
                        unsafe_allow_html=True)


def _chips(r: dict) -> list[str]:
    out = []
    if r.get("mutated"):
        out.append("LOCAL REAL EXECUTION")
    plan, val = r.get("plan") or {}, r.get("validation") or {}
    if plan.get("source") == "llm" and (r.get("llm") or {}).get("ok"):
        out.append("PROPOSED")
    if val.get("accepted"):
        out.append("VALIDATED")
    if (r.get("tool_version") or 1) > 1 and not r.get("rolled_back"):
        out.append("APPLIED")
    out.append(r["final"])
    return out


def _result(job: dict):
    r = job.get("result") or {}
    if not r:
        return
    ev = job.get("evidence") or {}
    with st.container(border=True):
        st.markdown(" ".join(chip(c) for c in _chips(r)), unsafe_allow_html=True)
        st.markdown(f"### {r['final']}")
        st.caption(f"Experiment `{job['id']}` · started {ist(job['started_at'])} · total {fmt_ms(job['elapsed_ms'])}"
                   + (f" · recovery {fmt_ms(r['recovery_ms'])}" if r.get("recovery_ms") else "")
                   + f" · expected **{r.get('expected', '—')}** ({'as expected' if r.get('as_expected') else 'NOT as expected'})")
        if r.get("reason"):
            st.write(r["reason"])
        if r.get("failed_stage"):
            st.warning(f"Stopped at: **{r['failed_stage']}**. Evidence is kept below.", icon=":material/report:")
        llm = r.get("llm")
        if llm:
            st.caption(f"Model: **{llm['model']}** via Ollama on this machine · "
                       f"{'genuine local inference' if llm.get('real') else 'NOT a real request (test double)'} · "
                       f"{'answered' if llm.get('ok') else 'failed: ' + str(llm.get('error_category'))} in "
                       f"{fmt_ms(llm.get('duration_ms'))}")
        if r.get("business_checks"):
            st.markdown("**Business verification**")
            for c in r["business_checks"]:
                st.markdown(f"{'✅' if c['ok'] else '❌'} {c['name']} · {c['detail']}")
        if r.get("incident"):
            patch_story(r["incident"], key=job["id"])
        with st.expander("Technical evidence (redacted)"):
            if ev.get("baseline"):
                st.markdown("**Baseline request and response**")
                st.json(ev["baseline"], expanded=False)
            if ev.get("mutation"):
                st.markdown("**Database change**")
                st.json(ev["mutation"], expanded=False)
            if ev.get("contracts"):
                st.markdown("**Contracts after the change** (registered vs live)")
                st.json(ev["contracts"], expanded=False)
            if r.get("plan"):
                st.markdown("**Proposed mapping**")
                st.json(r["plan"], expanded=False)
            if r.get("validation"):
                st.markdown("**Independent validation**")
                st.json(r["validation"], expanded=False)
            st.download_button("Download this experiment (JSON)", json.dumps(job, indent=2, default=str),
                               f"adaptix-{job['id']}.json", "application/json")


def _request_box(title: str, req: dict | None, ok: bool):
    with st.container(border=True):
        st.markdown(f"**{title}** " + badge("VERIFIED" if ok else "FAILED"), unsafe_allow_html=True)
        if not req:
            st.caption("not recorded")
            return
        st.caption(f"{req.get('method')} {req.get('url')} · HTTP {req.get('status')} · {fmt_ms(req.get('ms'))}")
        st.markdown("Sent")
        st.code(json.dumps(req.get("sent"), indent=1), language="json")
        st.markdown("Response")
        st.code(json.dumps(req.get("reply"), indent=1, default=str)[:1500], language="json")


def _patch_diff(versions: list[dict], applied: dict | None) -> str:
    if not applied:
        return ""
    parent = next((v for v in versions if v["version"] == applied["parent_version"]), None) or {}
    lines = [f"# tool customer_lookup: v{applied['parent_version']} -> v{applied['version']}  (request mapping)"]
    old, new = parent.get("request_map", {}), applied["request_map"]
    lines += [f"  {loc} <- {new[loc][0]}" for loc in sorted(new) if old.get(loc) == new[loc]]
    lines += [f"- {loc} <- {old[loc][0]}" for loc in sorted(old) if old[loc] != new.get(loc)]
    lines += [f"+ {loc} <- {new[loc][0]}" + (f"  [{new[loc][1]}]" if new[loc][1] != "none" else "")
              for loc in sorted(new) if new[loc] != old.get(loc)]
    return "\n".join(lines)


def patch_story(incident_id: str, key: str):
    """The real error, the model's proposal, validation, the patch written and the request that now succeeds."""
    p = safe_call(api, "GET", f"/api/local-demo/patch/{incident_id}")
    if not p:
        return
    st.markdown("#### How the error was removed")
    a, b = st.columns(2)
    with a:
        _request_box("1 · The error (original request after the schema change)", p["error_before"], False)
    with b:
        _request_box("5 · The same request after the patch", p["request_after"], bool(p["request_after"]))
    m = p.get("model") or {}
    with st.container(border=True):
        real = m.get("real")
        st.markdown(f"**2 · Proposal by `{m.get('model') or '—'}`** "
                    + chip("PROPOSED" if (m.get("ok") and p.get("plan")) else "NOT RUN"), unsafe_allow_html=True)
        if m:
            st.caption(f"{'Genuine local inference via Ollama on this machine' if real else 'NOT a real model request'}"
                       f" · {fmt_ms(m.get('duration_ms'))} · {m.get('prompt_tokens') or '—'} prompt / "
                       f"{m.get('output_tokens') or '—'} output tokens"
                       + (f" · error: {m.get('error_category')}" if m.get("error_category") else ""))
        prop = (p.get("model_proposal") or {})
        if prop.get("mappings"):
            st.dataframe([{"new field": x["target_field"], "filled from caller argument": x["source_arg"],
                           "transform": x["transform"], "evidence quoted from the live contract": x["evidence"]}
                          for x in prop["mappings"]], hide_index=True, use_container_width=True)
            if prop.get("diagnosis"):
                st.caption(f"Model diagnosis: {prop['diagnosis']}")
            with st.expander("Raw model output (structured JSON)"):
                st.json(prop, expanded=True)
        elif not m:
            st.caption("No model request was needed for this run.")
    v = p.get("validation") or {}
    with st.container(border=True):
        st.markdown("**3 · Independent validation** " + chip("VALIDATED" if v.get("accepted") else "SAFELY BLOCKED"
                                                            if v else "NOT RUN"), unsafe_allow_html=True)
        if v:
            st.dataframe([{"check": c["name"], "passed": c["ok"], "detail": c["detail"]} for c in v.get("checks", [])],
                         hide_index=True, use_container_width=True)
    with st.container(border=True):
        ap = p.get("applied_version")
        st.markdown("**4 · Patch written to the tool registry** " + chip(
            "ROLLED BACK" if p.get("rolled_back") else "APPLIED" if ap else "NOT RUN"), unsafe_allow_html=True)
        if ap:
            st.code(_patch_diff(p["versions"], ap), language="diff")
            st.caption("This is the actual stored tool version. The caller still sends customer_id=101; ADAPTIX now "
                       "puts that value in the field the live API expects. No SQL or code from the model was run.")
        st.dataframe([{"version": x["version"], "status": x["status"], "origin": x["origin"],
                       "from version": x["parent_version"], "incident": x["incident_id"],
                       "request mapping": ", ".join(f"{k} <- {a}" for k, (a, _) in x["request_map"].items())}
                      for x in p["versions"]], hide_index=True, use_container_width=True)
    if st.button("Send the original request again (proves the patch is in effect)", key=f"again-{key}",
                 icon=":material/replay:"):
        st.session_state[f"again-res-{key}"] = safe_call(api, "POST", "/api/local-demo/call")
    res = st.session_state.get(f"again-res-{key}")
    if res:
        ok = res["status"] == "ok"
        (st.success if ok else st.error)(
            f"{'Succeeded' if ok else res['status']} with tool v{res['tool_version']} in {fmt_ms(res['duration_ms'])}: "
            f"the caller sent {{'customer_id': 101}}, ADAPTIX sent {json.dumps(res['sent_as'])}"
            + ("" if ok else f" · {res.get('reason')}"))
        for c in res.get("business_checks") or []:
            st.markdown(f"{'✅' if c['ok'] else '❌'} {c['name']} · {c['detail']}")


def database_viewer():
    """Read-only view of the local demo database: data, structure and migration history."""
    tables = safe_call(api, "GET", "/api/local-demo/db/tables")
    if not tables:
        return
    info = {t["table"]: t for t in tables}
    c1, c2, c3 = st.columns([1.2, 1, 1])
    table = c1.selectbox("Table", list(info), format_func=lambda t: f"{t} ({info[t]['rows']} rows)", key="ldb_table")
    only = c2.number_input("Only customer (optional)", 0, 99999, 0, key="ldb_cust",
                           help="0 = all rows. For customers, orders and order_items.")
    size = c3.selectbox("Rows per page", [25, 50, 100], key="ldb_size")
    pages = max(1, -(-info[table]["rows"] // size))
    page = st.number_input("Page", 1, pages, 1, key=f"ldb_page_{table}") if pages > 1 else 1
    params = {"limit": size, "offset": (page - 1) * size}
    if only and table in ("customers", "orders", "order_items"):
        params["customer"] = int(only)
    data = safe_call(api, "GET", f"/api/local-demo/db/tables/{table}", params=params)
    t_data, t_struct, t_hist = st.tabs(["Data", "Structure", "Migration history"])
    with t_data:
        if data:
            st.caption(f"{data['total']} matching rows · showing {len(data['rows'])} · read-only")
            st.dataframe(data["rows"], hide_index=True, use_container_width=True)
    with t_struct:
        i = info[table]
        st.dataframe(i["columns"], hide_index=True, use_container_width=True)
        if i["foreign_keys"]:
            st.markdown("Foreign keys")
            st.dataframe(i["foreign_keys"], hide_index=True, use_container_width=True)
        if i["indexes"]:
            st.markdown("Indexes")
            st.dataframe(i["indexes"], hide_index=True, use_container_width=True)
    with t_hist:
        h = safe_call(api, "GET", "/api/local-demo/db/tables/schema_migrations", params={"limit": 200})
        if h:
            st.dataframe([{"#": r["id"], "applied (IST)": ist(r["applied_at"]), "change": r["kind"],
                           "detail": r["detail"]} for r in h["rows"]], hide_index=True, use_container_width=True)


def render():
    header("Local Demo Lab", "Manual schema drift on a real local database",
           "Change the schema of the local demo database, and ADAPTIX recovers using a model running on this "
           "machine. The database, the customer API and the model are real local components. Nothing here reaches "
           "a cloud service or production data.")
    st.markdown(LOCAL_BADGE, unsafe_allow_html=True)
    stt = safe_call(api, "GET", "/api/local-demo/status")
    if stt is None:
        return
    st.subheader("Readiness")
    _readiness(stt)
    st.subheader("Current contract")
    _contracts()

    with st.expander("Local database (read-only viewer)", icon=":material/table_view:"):
        database_viewer()

    st.subheader("Introduce a schema change")
    scen = stt["scenarios"]
    c1, c2 = st.columns([1.4, 1])
    key = c1.selectbox("Change", SCENARIO_ORDER, format_func=lambda k: scen[k]["title"], key="ld_scenario")
    params = {}
    if key == "rename":
        old = (stt["database"].get("key_column") or "customer_id")
        c2.text_input("Old field", old, disabled=True, key="ld_old")
        params = {"old": old, "new": st.text_input("New field name", "client_id", key="ld_new",
                                                   help="lowercase letters, digits and underscores")}
    elif key == "required":
        params = {"column": c2.text_input("New required field", "region_code", key="ld_req")}
    elif key == "type":
        c2.text_input("Field", "loyalty_points (INTEGER → REAL)", disabled=True, key="ld_type")
    else:
        c2.text_input("Field to remove", "email", disabled=True, key="ld_rm")
    pv = safe_call(api, "POST", "/api/local-demo/preview", json={"scenario": key, "params": params})
    if pv:
        with st.container(border=True):
            st.markdown(f"**Preview** · {pv['title']}")
            st.code(pv["database_change"], language="sql")
            st.caption(f"{pv['api_effect']} Expected: **{pv['expected']}**. {pv['why']}")
            st.caption(pv["note"])
            for p in pv["problems"]:
                st.error(p)
    ready = (stt["database"]["status"] == "ready" and stt["service"]["status"] == "running"
             and stt["model"]["server"] == "running" and stt["model"]["model_installed"])
    if not ready:
        st.info("Finish the readiness steps above first (see LOCAL_DEMO.md).", icon=":material/info:")
    confirm = st.checkbox("I confirm: change the schema of the LOCAL demo database (a backup is taken first)",
                          key="ld_confirm")
    if st.button("Introduce Drift & Run Recovery", type="primary", icon=":material/bolt:",
                 disabled=not (confirm and ready and pv and pv["valid"]) or bool(stt.get("running_job"))):
        job = safe_call(api, "POST", "/api/local-demo/run", json={"scenario": key, "params": params, "confirm": True})
        if job:
            mutated()
            st.session_state["ld_job"] = job["id"]
    job_id = st.session_state.get("ld_job")
    if job_id:
        st.subheader("Timeline")
        st.caption("baseline request → schema mutation → detected drift → model proposal → independent validation → "
                   "patch application → retry → business verification")
        box = st.empty()
        job = safe_call(api, "GET", f"/api/local-demo/jobs/{job_id}")
        with st.status("Running…" if job and job["status"] == "running" else "Finished", expanded=True) as stat:
            while job and job["status"] == "running":
                _events(job, box)
                time.sleep(0.5)
                job = safe_call(api, "GET", f"/api/local-demo/jobs/{job_id}")
            if job:
                _events(job, box)
                ok = ((job.get("result") or {}).get("final") or "").startswith("VERIFIED")
                stat.update(label=f"Finished in {fmt_ms(job['elapsed_ms'])}", state="complete" if ok else "error")
        if job:
            _result(job)

    incs = safe_call(api, "GET", "/api/local/incidents", params={"limit": 50}) or []
    repaired = [i for i in incs if i.get("llm")]
    if repaired and not st.session_state.get("ld_job"):
        with st.expander("Earlier repairs by the local model"):
            pick = st.selectbox("Run", [i["id"] for i in repaired], key="ld_hist",
                                format_func=lambda x: next(f"{ist(i['started_at'])} · {i['status']} · {i['reason'][:60]}"
                                                           for i in repaired if i["id"] == x))
            patch_story(pick, key=f"hist-{pick}")

    with st.expander("Reset Demo"):
        st.caption("Reset deletes the local demo database (after a backup in data/local-demo-backups) and every "
                   "LOCAL DEMO record in ADAPTIX: tool versions, incidents, evidence and audit events. It then "
                   "re-creates the same seed data and registers the customer tool against the baseline contract. "
                   "Production, REAL API TEST and Simulation records are not touched.")
        ok = st.checkbox("I understand: delete the local demo database and LOCAL DEMO records", key="ld_reset_ok")
        if st.button("Reset Demo", disabled=not ok, icon=":material/restart_alt:"):
            r = safe_call(api, "POST", "/api/local-demo/reset", json={"confirm": True})
            if r:
                mutated()
                st.session_state.pop("ld_job", None)
                st.success(f"Reset done. Backup: {r['backup'] or 'none'} · tool registered: {r['registered']}")
