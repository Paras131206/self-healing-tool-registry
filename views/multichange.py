"""Multi-Change Lab: several schema changes in one confirmed experiment, then automatic recovery.
Deployed site: ADAPTIX's controlled test API. Local demo: also the local SQLite demo database."""
import json
import time
import uuid

import streamlit as st

from views.common import api, badge, fmt_ms, header, ist, mutated, safe_call
from views.demo import ICON

FINAL_STYLE = {"VERIFIED RECOVERY": "VERIFIED", "VERIFIED (no repair needed)": "VERIFIED", "PARTIAL RECOVERY": "VERIFIED",
               "PARTIAL - SAFELY REFUSED": "ACTION REQUIRED", "SAFELY REFUSED": "ACTION REQUIRED",
               "FAILED (rolled back)": "FAILED", "FAILED": "FAILED", "UNVERIFIED": "FAILED", "NOT RUN": "NOT RUN"}
CHANGE_STYLE = {"repaired": "VERIFIED", "compatible": "CONFIGURED", "refused": "ACTION REQUIRED",
                "proposed, not applied": "NOT RUN", "applied, then rolled back": "FAILED", "unverified": "FAILED",
                "reverted": "ACTION REQUIRED"}


def _chip(text: str, fg: str, bg: str) -> str:
    return f'<span class="ax-badge" style="color:{fg};background:{bg};border-color:{fg}33">{text}</span>'


def _session() -> str:
    if "mc_session" not in st.session_state:
        st.session_state["mc_session"] = uuid.uuid4().hex[:12]
    return st.session_state["mc_session"]


def _builder(cat: dict, target: str) -> list[dict]:
    t = cat[target]
    key = f"mc_changes_{target}"
    changes = st.session_state.setdefault(key, [])
    guided = t["guided"]
    c1, c2 = st.columns([2, 1])
    pick = c1.selectbox("Guided scenario", ["(build my own)"] + list(guided),
                        format_func=lambda k: k if k == "(build my own)" else guided[k]["title"], key=f"mc_g_{target}")
    if c2.button("Use this scenario", disabled=pick == "(build my own)", key=f"mc_use_{target}"):
        st.session_state[key] = [dict(c) for c in guided[pick]["changes"]]
        st.rerun()
    with st.container(border=True):
        st.markdown("**Add a change**")
        a, b, c, d = st.columns([1.4, 1.2, 1.1, 0.7])
        kind = a.selectbox("Change", list(cat["kinds"]), format_func=cat["kinds"].get, key=f"mc_kind_{target}")
        adding = kind in ("add_optional", "add_required")
        fld = (b.text_input("New field name", "region_code", key=f"mc_newf_{target}") if adding else
               b.selectbox("Field", t["field_names"], key=f"mc_f_{target}"))
        item = {"kind": kind, "field": fld}
        if kind == "rename":
            item["new"] = c.text_input("New name", "client_id", key=f"mc_new_{target}")
        elif kind in ("retype", "add_optional", "add_required"):
            item["to"] = c.selectbox("Type", list(cat["types"]), format_func=cat["types"].get, key=f"mc_to_{target}")
        elif kind == "enum":
            item["values"] = c.text_input("Allowed values (a|b|c)", "standard|GOLD|platinum", key=f"mc_vals_{target}")
        if kind == "rename":
            item["documented"] = st.checkbox("Document the rename in the schema (x-renamed-from)", True,
                                             key=f"mc_doc_{target}",
                                             help="Without documentation, a rename is repaired only on strong "
                                                  "evidence; otherwise ADAPTIX refuses it")
        d.markdown("&nbsp;")
        if d.button("Add", key=f"mc_add_{target}", disabled=len(changes) >= 6):
            changes.append(item)
            st.rerun()
    if changes:
        st.markdown("**Selected changes**")
        for i, ch in enumerate(changes):
            x, y = st.columns([9, 1])
            desc = {"rename": f"rename **{ch['field']}** → **{ch.get('new')}**" + ("" if ch.get("documented", True)
                                                                                   else " (undocumented)"),
                    "retype": f"**{ch['field']}** type → {cat['types'].get(ch.get('to'), ch.get('to'))}",
                    "add_optional": f"add optional **{ch['field']}**", "add_required": f"add required **{ch['field']}**",
                    "remove": f"remove **{ch['field']}**",
                    "enum": f"**{ch['field']}** allowed values → {ch.get('values')}"}[ch["kind"]]
            x.markdown(f"{i + 1}. {desc}")
            if y.button("✕", key=f"mc_rm_{target}_{i}", help="Remove this change"):
                changes.pop(i)
                st.rerun()
    else:
        st.caption("No changes selected yet. Pick a guided scenario or add changes one by one (up to 6).")
    return changes


def _events(job: dict, box):
    with box.container():
        for e in job["events"]:
            st.markdown(f"{ICON.get(e['status'], '•')} **{e['phase']}** · {e['title']} "
                        f"<span style='color:#64748B;font-size:.8rem'>+{fmt_ms(e['elapsed_ms'])}</span>",
                        unsafe_allow_html=True)


def _req(title: str, r: dict | None):
    st.markdown(f"**{title}**")
    if not r:
        st.caption("not recorded")
        return
    st.caption(f"{r.get('method')} {r.get('url')} · HTTP {r.get('status')} · {fmt_ms(r.get('ms'))}")
    st.code(json.dumps({"sent": r.get("sent"), "reply": r.get("reply")}, indent=1, default=str)[:1800], language="json")


def _result(job: dict):
    r = job.get("result") or {}
    if not r:
        return
    final = r["final"]
    eng = job["engine"]
    labels = [_chip("CONTROLLED DRIFT · test API" if job["target"] == "service" else "LOCAL REAL EXECUTION",
                    "#155E75", "#ECFEFF")]
    llm = r.get("llm") or {}
    if eng == "gemini":
        labels.append(_chip("REAL GEMINI CALL" if llm.get("real") else "GEMINI NOT CALLED", "#1D4ED8", "#EFF6FF"))
    elif eng == "local":
        labels.append(_chip("LOCAL LLM CALL" if llm.get("real") else "LOCAL LLM NOT CALLED", "#1D4ED8", "#EFF6FF"))
    else:
        labels.append(_chip("RULES ONLY", "#475569", "#F1F5F9"))
    if any(v["incident_id"] == r.get("incident") and v["status"] == "active" for v in r.get("versions", [])):
        labels.append(_chip("PATCH APPLIED", "#B45309", "#FFFBEB"))
    if r.get("reverted"):
        labels.append(_chip("UNSAFE CHANGES REVERTED", "#6D28D9", "#F5F3FF"))
    if final.startswith("VERIFIED") or final == "PARTIAL RECOVERY":
        labels.append(_chip("VERIFIED" if final.startswith("VERIFIED") else "VERIFIED (safe changes only)",
                            "#047857", "#ECFDF5"))
    with st.container(border=True):
        st.markdown(" ".join(labels), unsafe_allow_html=True)
        st.markdown(f"### {final} &nbsp; {badge(FINAL_STYLE.get(final, 'NOT RUN'))}", unsafe_allow_html=True)
        st.caption(f"Experiment `{job['id']}` · started {ist(job['started_at'])} · total {fmt_ms(job['elapsed_ms'])}"
                   + (f" · recovery {fmt_ms(r['recovery_ms'])}" if r.get("recovery_ms") else "")
                   + (f" · evidence `{r['evidence_id']}`" if r.get("evidence_id") else ""))
        if r.get("reason"):
            st.write(r["reason"])
        fa = r.get("first_attempt")
        if fa:
            st.info(f"First attempt with all changes: **{fa['final']}** ({(fa.get('reason') or '')[:160]}). "
                    f"ADAPTIX then reverted the unsafe change(s) on the controlled target, kept the safe ones and "
                    f"repaired them: {', '.join(r['reverted'])} reverted. This revert is possible only because ADAPTIX "
                    f"controls this target; a real provider's change cannot be undone.", icon=":material/call_split:")
            with st.expander("First attempt details (all changes applied)"):
                st.dataframe([{"change": c["label"], "outcome": c["status"], "why": c["detail"]}
                              for c in fa.get("changes") or []], hide_index=True, use_container_width=True)
                if fa.get("validation"):
                    st.markdown("Validation: " + ("accepted" if fa["validation"].get("accepted") else "rejected — "
                                + "; ".join(fa["validation"].get("rejected_reasons", []))[:300]))
                if fa.get("failure"):
                    _req("Original call with all changes", fa["failure"])
        if r.get("changes"):
            st.markdown("**Per-change outcome**")
            st.dataframe([{"change": c["label"], "outcome": c["status"], "why": c["detail"]} for c in r["changes"]],
                         hide_index=True, use_container_width=True)
        if r.get("before"):
            a, b = st.columns(2)
            a.markdown("**Schema before**")
            a.dataframe(r["before"], hide_index=True, use_container_width=True)
            b.markdown("**Schema after the changes** (served now)")
            b.dataframe(r["after"], hide_index=True, use_container_width=True)
        if r.get("diff"):
            d = r["diff"]
            st.markdown(f"**Drift detected from the live schema:** {d.get('summary')}")
            st.dataframe([{"change": c["kind"].replace("_", " "), "field": c["location"], "was": c.get("old") or "—",
                           "now": c.get("new") or "—"} for c in d["request_changes"] + d["response_changes"]],
                         hide_index=True, use_container_width=True)
        _model_work(r, eng)
        if r.get("failure") or r.get("retry"):
            a, b = st.columns(2)
            with a:
                _req("Original call after the changes", r.get("failure"))
            with b:
                _req("Same call after the patch", r.get("retry"))
        if r.get("versions"):
            st.markdown("**Tool versions** (patches are versioned; failed ones are rolled back)")
            st.dataframe([{"version": v["version"], "status": v["status"],
                           "request mapping": ", ".join(f"{k} <- {a}" + (f" [{t}]" if t != "none" else "")
                                                        for k, (a, t) in v["request_map"].items())}
                          for v in r["versions"]], hide_index=True, use_container_width=True)
        if r.get("business_checks"):
            st.markdown("**Business verification**")
            for c in r["business_checks"]:
                st.markdown(f"{'✅' if c['ok'] else '❌'} {c['name']} · {c['detail']}")


def _model_work(r: dict, eng: str):
    llm, step = r.get("llm") or {}, r.get("llm_step") or {}
    plan, val = r.get("plan") or {}, r.get("validation") or {}
    with st.container(border=True):
        if eng == "none":
            st.markdown("**Repair proposal · rules planner** (no model call)")
        else:
            st.markdown(f"**Model work · `{llm.get('provider') or '—'}` · `{llm.get('model') or '—'}`**")
            if not llm:
                st.caption("No model request was made: the run stopped before a proposal was needed.")
            else:
                st.caption(("genuine request" if llm.get("real") else "NOT a real request (test double)")
                           + f" · {'answered' if llm.get('ok') else 'failed: ' + str(llm.get('error_category'))}"
                           + f" · {fmt_ms(llm.get('duration_ms'))} · {llm.get('prompt_tokens') or '—'} prompt / "
                             f"{llm.get('output_tokens') or '—'} output tokens · {llm.get('attempts')} attempt(s)")
                if step.get("error"):
                    st.error(f"Provider error ({llm.get('error_category')}): {step['error']}")
            if step.get("context_sent"):
                with st.expander("Sanitized context sent to the model (names, types, docs; no values or keys)"):
                    st.json(step["context_sent"], expanded=True)
            if step.get("diagnosis") or step.get("mappings"):
                st.markdown(f"Model diagnosis: _{step.get('diagnosis') or '—'}_ · confidence "
                            f"{step.get('confidence', '—')}")
                with st.expander("Model's structured repair plan (as returned, parsed)"):
                    st.json({k: step.get(k) for k in ("diagnosis", "mappings", "evidence", "risks", "confidence")},
                            expanded=True)
        if plan.get("mappings"):
            st.dataframe([{"new field": m["target_field"], "from argument": m["source_arg"] or "—",
                           "transform": m["transform"], "evidence": m.get("evidence", "")[:90]} for m in plan["mappings"]],
                         hide_index=True, use_container_width=True)
        for u in plan.get("unresolved") or []:
            st.warning(f"Unresolved: {u}")
        if val:
            st.markdown("**Independent validation:** " + ("accepted" if val.get("accepted") else "rejected — " +
                                                           "; ".join(val.get("rejected_reasons", []))[:300]))
            with st.expander("Validation checks"):
                st.dataframe([{"check": c["name"], "passed": c["ok"], "detail": c["detail"]} for c in val["checks"]],
                             hide_index=True, use_container_width=True)


def render():
    header("Multi-Change Lab", "Several API changes at once, one automatic recovery",
           "Choose several schema changes, preview the combined schema, then confirm. ADAPTIX applies them together "
           "to a controlled target, makes the original call, and recovers automatically: drift detection, repair "
           "proposal, independent validation, patch, retry and business verification.")
    cat = safe_call(api, "GET", "/api/multichange/catalog")
    if cat is None:
        return
    session = _session()
    targets = {t["key"]: t for t in cat["targets"]}
    c1, c2 = st.columns([1.4, 1])
    target = c1.radio("Target", list(targets), format_func=lambda k: targets[k]["title"], key="mc_target",
                      horizontal=True)
    engines = {e["key"]: e["title"] for e in cat["engines"][target]}
    engine = c2.selectbox("Repair engine", list(engines), format_func=engines.get, key=f"mc_engine_{target}",
                          help="The selected engine is used or the run stops; ADAPTIX never switches providers")
    st.caption(targets[target]["note"] + (". 127.0.0.1 in this run is the ADAPTIX server's own runtime, not your "
                                          "computer." if target == "service" else ""))
    if target == "local" and cat.get("local", {}).get("error"):
        st.error(cat["local"]["error"])
        return
    if target == "local" and not cat["local"].get("at_baseline", True):
        st.warning("The local demo database already has schema changes (" + ", ".join(cat["local"]["changes_applied"])
                   + "). Every experiment starts from the same baseline, so reset it first.", icon=":material/restart_alt:")
        sure = st.checkbox("I understand: restore the local demo database baseline (a backup is taken first) and "
                           "clear LOCAL DEMO records", key="mc_local_reset_ok")
        if st.button("Reset the local demo to its baseline", disabled=not sure, key="mc_local_reset"):
            if safe_call(api, "POST", "/api/local-demo/reset", json={"confirm": True}):
                mutated()
                st.session_state.pop("mc_job", None)
                st.session_state["mc_changes_local"] = []
                st.rerun()
        return
    changes = _builder(cat, target)
    pv = safe_call(api, "POST", "/api/multichange/preview", json={"target": target, "changes": changes}) \
        if changes else None
    if pv:
        if not pv["valid"]:
            for p in pv["problems"]:
                st.error(p, icon=":material/block:")
        else:
            a, b = st.columns(2)
            a.markdown("**Current schema**")
            a.dataframe(pv["before"], hide_index=True, use_container_width=True)
            b.markdown("**Combined schema after the changes** (preview, nothing changed yet)")
            b.dataframe(pv["after"], hide_index=True, use_container_width=True)
            st.dataframe(pv["diff"], hide_index=True, use_container_width=True)
    ok = st.checkbox("I confirm: apply these changes to the controlled target " +
                     ("(ADAPTIX's test API, never a real provider)" if target == "service"
                      else "(the LOCAL demo database; a backup is taken first)"), key=f"mc_confirm_{target}")
    partial = st.checkbox("If only some changes can be fixed safely: apply those fixes and revert the unsafe changes "
                          "on this controlled target, then verify again", True, key=f"mc_partial_{target}",
                          help="Off: an unsafe change stops the whole experiment and nothing is applied")
    running = st.session_state.get("mc_job") and st.session_state.get("mc_job_status") == "running"
    if st.button("Apply Changes & Start Recovery", type="primary", icon=":material/bolt:",
                 disabled=not (ok and pv and pv["valid"]) or bool(running)):
        job = safe_call(api, "POST", "/api/multichange/run", json={"target": target, "changes": changes,
                                                                  "engine": engine, "confirm": True,
                                                                  "session": session, "partial": partial})
        if job:
            mutated()
            st.session_state["mc_job"], st.session_state["mc_job_target"] = job["id"], target
    job_id = st.session_state.get("mc_job")
    if job_id:
        st.subheader("Recovery timeline")
        box = st.empty()
        job = safe_call(api, "GET", f"/api/multichange/jobs/{job_id}", params={"session": session})
        with st.status("Running… (a model call can take several seconds)" if job and job["status"] == "running"
                       else "Finished", expanded=True) as stat:
            while job and job["status"] == "running":
                _events(job, box)
                time.sleep(0.5)
                job = safe_call(api, "GET", f"/api/multichange/jobs/{job_id}", params={"session": session})
            if job:
                _events(job, box)
                good = ((job.get("result") or {}).get("final") or "").startswith(("VERIFIED", "PARTIAL RECOVERY"))
                stat.update(label=f"Finished in {fmt_ms(job['elapsed_ms'])}", state="complete" if good else "error")
        if job:
            st.session_state["mc_job_status"] = job["status"]
            _result(job)
            with st.expander("Reset this experiment"):
                scope = ("Removes only this experiment's tool and its test API; other visitors' experiments and "
                         "records are not touched." if job["target"] == "service" else
                         "Restores the local demo database baseline (after a backup) and clears LOCAL DEMO records.")
                st.caption(scope)
                sure = st.checkbox("I understand", key=f"mc_reset_ok_{job_id}")
                if st.button("Reset", disabled=not sure, key=f"mc_reset_{job_id}"):
                    out = safe_call(api, "POST", "/api/multichange/reset",
                                    json={"target": job["target"], "job_id": job_id, "session": session,
                                          "confirm": True})
                    if out:
                        mutated()
                        st.session_state.pop("mc_job", None)
                        st.success(f"Reset done: {out['scope']}")
