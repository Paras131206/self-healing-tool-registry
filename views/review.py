"""Product Review: what ADAPTIX solves, how it works, what it is built with, how fast it recovers (from recorded
experiments only), the evidence behind each claim, and a feedback form that exports instead of pretending to submit.
Every number on this page is computed from stored records at render time; illustrative content is labelled."""
import importlib.util
import json
import time
from xml.etree import ElementTree

import streamlit as st

from views.common import ROOT, api, fmt_ms, ist, local_demo_enabled, mutated, safe_call

REPO = "https://github.com/Paras131206/self-healing-tool-registry"
VIEW = "review"                  # opened only from the ADAPTIX logo: <app>/?view=review (no sidebar entry)
ENV_LABEL = {"LIVE_API": "REAL API", "REAL_API_TEST": "REAL API TEST (controlled service)",
             "SIMULATION": "SIMULATION", "LOCAL_DEMO": "LOCAL DEMO"}
SECTIONS = [("overview", "Overview"), ("how-it-works", "How It Works"), ("technology", "Technology"),
            ("performance", "Performance"), ("evidence", "Evidence"), ("feedback", "Feedback")]
STAGES = [("A", "request", "Execute the original operation", "Send the caller's request and capture the failure."),
          ("B", "discover", "Retrieve the live schema", "Read the authoritative OpenAPI/JSON Schema; fail closed if it "
                                                         "cannot be read."),
          ("C", "diff", "Compare contracts", "Field-level diff of the registered contract against the live one."),
          ("D", "classify", "Diagnose", "Only schema drift is repaired; auth, outages and rate limits are not."),
          ("E", "plan", "Propose a minimal repair", "Rules or the configured model propose field mappings only."),
          ("F", "validate", "Validate independently", "Evidence, types, required fields, one-to-one, no value loss."),
          ("G", "apply", "Apply as a new version", "Versioned patch with the previous version kept for rollback."),
          ("H", "retry", "Retry the original operation", "Rebuilt from the caller's own values; bounded retries."),
          ("I", "verify", "Verify the business result", "Task status and applied values, not just an HTTP 2xx."),
          ("J", "rollback", "Roll back or block", "Failed verification rolls back; unsafe changes stop safely.")]
CSS = """
<style>
.rv-hero { padding: 1.4rem 1.5rem; border:1px solid #DCE3EE; border-radius:14px; background:#F8FAFC; }
.rv-hero h1 { font-size:2rem; line-height:1.2; margin:.2rem 0 .5rem 0; color:#0F172A; }
.rv-hero p { color:#334155; font-size:1.02rem; max-width: 820px; }
.rv-nav a, .rv-cta a { display:inline-block; margin:.15rem .35rem .15rem 0; padding:.35rem .8rem; border-radius:999px;
  border:1px solid #CBD5E1; color:#0F172A; text-decoration:none; font-size:.88rem; background:#fff; }
.rv-cta a.primary { background:#1D4ED8; border-color:#1D4ED8; color:#fff; }
.rv-nav a:focus-visible, .rv-cta a:focus-visible { outline:3px solid #93C5FD; outline-offset:2px; }
.rv-flow { list-style:none; padding:0; margin:.4rem 0 1rem 0; display:grid; gap:.5rem;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.rv-step { border:1px solid #DCE3EE; border-radius:10px; padding:.5rem .6rem; background:#fff; font-size:.9rem;
  color:#0F172A; }
.rv-step b { color:#1D4ED8; margin-right:.25rem; }
.rv-tag { display:inline-block; padding:.05rem .5rem; border-radius:6px; font-size:.75rem; font-weight:650;
  background:#F1F5F9; color:#334155; border:1px solid #CBD5E1; }
</style>
"""


def review_link(settings_url: str | None, current_url: str | None) -> str | None:
    """Where the logo leads: ADAPTIX_REVIEW_URL when it is a valid http(s) URL, else this app's ?view=review."""
    from urllib.parse import urlsplit
    if settings_url:
        p = urlsplit(settings_url)
        if p.scheme in ("http", "https") and p.netloc:
            return settings_url
    if current_url:
        p = urlsplit(current_url)
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}/?view={VIEW}"
    return None


def _anchor(key: str, title: str):
    st.header(title, anchor=key)


def _tests_result() -> dict | None:
    """The last recorded test run, only if a JUnit report exists (pytest --junitxml=logs/pytest-junit.xml)."""
    f = ROOT / "logs" / "pytest-junit.xml"
    if not f.is_file():
        return None
    try:
        root = ElementTree.parse(f).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        a = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
        return a | {"recorded": time.strftime("%d %b %Y %H:%M", time.localtime(f.stat().st_mtime))}
    except (ElementTree.ParseError, AttributeError, ValueError):
        return None


def _hero(local: bool):
    st.markdown(CSS, unsafe_allow_html=True)
    nav = "".join(f'<a href="#{k}">{t}</a>' for k, t in SECTIONS)
    st.markdown(f'<nav class="rv-nav" aria-label="Review page sections">{nav}</nav>', unsafe_allow_html=True)
    cta = ('<a class="primary" href="#how-it-works">Explore How It Works</a><a href="#evidence">View Evidence</a>'
           + ('<a href="#try-it">Try Local Demo</a>' if local else ""))
    st.markdown(f"""<section class="rv-hero" aria-labelledby="overview-title">
<div class="ax-eyebrow">ADAPTIX</div>
<h1 id="overview-title">When APIs Break, ADAPTIX Works to Restore Them.</h1>
<p>An AI-powered, safety-gated self-healing layer that detects API and tool schema drift, proposes a repair,
validates it, and verifies whether the original operation works again.</p>
<p>Integrations break when an API renames, removes or retypes a field. ADAPTIX notices the mismatch, works out
a minimal fix, checks it, applies it as a new tool version and confirms the original request works again.
It does not promise to fix every failure.</p>
<div class="rv-cta">{cta}</div></section>""", unsafe_allow_html=True)


def _problem():
    _anchor("overview", "The problem ADAPTIX solves")
    st.markdown("An integration expects `customers.customer_id`. The service renames it to `customers.client_id`. "
                "The integration still sends the old field, so the request fails. Monitoring can report the failure, "
                "but it does not repair the mapping. A model's suggestion alone does not prove the application "
                "recovered, and an unchecked automatic change can introduce new failures.")
    st.caption("Illustrative example with fictional data. Recorded runs are in the Evidence section.")
    st.dataframe([
        {"stage": "BEFORE", "request": '{"customer_id": 101}', "service expects": "customer_id",
         "result": "200 · Asha Verma returned"},
        {"stage": "BREAKAGE", "request": '{"customer_id": 101}', "service expects": "client_id",
         "result": "422 · customer_id not permitted, client_id required"},
        {"stage": "RECOVERY", "request": '{"client_id": 101} (rebuilt from the caller\'s value)',
         "service expects": "client_id", "result": "200 · identity re-checked: customer 101, Asha Verma"}],
        hide_index=True, use_container_width=True)


def _how(stage_counts: dict):
    _anchor("how-it-works", "How ADAPTIX solves it")
    cells = "".join(f'<li class="rv-step"><b>{l}</b> {t}</li>' for l, _, t, _ in STAGES)
    st.markdown(f'<ol class="rv-flow" aria-label="Recovery pipeline, in order">{cells}</ol>', unsafe_allow_html=True)
    st.dataframe([{"stage": f"{l}. {t}", "what happens": d,
                   "observed in recorded incidents": stage_counts.get(k, 0) or "not observed yet"}
                  for l, k, t, d in STAGES], hide_index=True, use_container_width=True)
    st.caption("PROPOSED → VALIDATED → APPLIED → VERIFIED are separate states. A model response or an HTTP 200 on "
               "its own is never counted as a recovery. Counts are from the stored incident timelines.")


def _technology(m: dict, local: bool):
    _anchor("technology", "Technology used")
    have = {n: importlib.util.find_spec(n) is not None for n in ("streamlit", "fastapi", "httpx", "jsonschema",
                                                                  "pydantic", "rapidfuzz", "uvicorn", "google.genai")}
    llm = m.get("llm") or {}
    rows = [
        ("Streamlit", "Dashboard and this review page", "Fast, readable UI in Python", have["streamlit"], "active"),
        ("FastAPI + uvicorn", "The ADAPTIX API every page calls; local demo services", "Typed routes, validation",
         have["fastapi"] and have["uvicorn"], "active"),
        ("httpx + egress guard", "Tool calls and schema retrieval for real APIs", "Allowlist, public IPs only, DNS "
         "pinning, TLS, redirect checks, timeouts, size limits", have["httpx"], "active"),
        ("OpenAPI / JSON Schema parsing + field diff", "Registered vs live contract comparison", "Explainable "
         "field-level differences", have["jsonschema"], "active"),
        ("Rules planner (rapidfuzz)", "Deterministic name matching for simple renames", "Cheap and repeatable",
         have["rapidfuzz"], "active"),
        ("Google Gemini API", f"Repair proposals when selected · model `{llm.get('model') or '—'}`",
         "Structured JSON output", have["google.genai"],
         f"configured: {str(llm.get('connectivity') or 'not configured').replace('_', ' ')}"),
        ("Local LLM via Ollama", "Repair proposals on this machine (local demo only)", "No cloud dependency for the "
         "local demo", True, "enabled on this machine" if local else "optional · local setup only"),
        ("Independent validator", "Checks every proposal before it is applied", "Untrusted model output never "
         "applies itself", True, "active"),
        ("SQLite (WAL)", "Tool versions, incidents, evidence and audit records per environment", "Simple, local, "
         "transactional", True, "active"),
        ("pytest", "Automated tests", "Regression safety", True, "development")]
    st.dataframe([{"technology": t, "what it does": w, "why": y, "status": s} for t, w, y, ok, s in rows if ok],
                 hide_index=True, use_container_width=True)
    st.graphviz_chart("""digraph{rankdir=LR; node[shape=box,style=rounded,fontname=Helvetica,fontsize=10];
      apis[label="API / tool integrations\\n(real APIs, controlled test API,\\nlocal demo API)"];
      core[label="ADAPTIX\\nengine + registry",style="rounded,filled",fillcolor="#EFF6FF"];
      model[label="Repair model\\n(Gemini or local LLM)"]; val[label="Independent validator"];
      store[label="Audit & evidence storage\\n(versions, incidents, evidence)"];
      apis->core[dir=both,label="requests / schemas"]; core->model[dir=both,label="proposal"];
      core->val[dir=both,label="checks"]; core->store[label="records"]; {rank=same; model; val}}""",
                      use_container_width=True)


def _phase_table(e: dict) -> list[dict]:
    rows = []
    for p, label in (("detect", "Detect drift"), ("propose", "Repair proposal"), ("validate", "Independent validation"),
                     ("apply", "Apply repair"), ("verify", "Retry + business verification"),
                     ("total", "Total end-to-end recovery"), ("llm_inference", "LLM inference only (part of proposal)")):
        s = e["phases"][p]
        val = (f"median {fmt_ms(s['median_ms'])}" + (f" · p95 {fmt_ms(s['p95_ms'])}" if s["p95_ms"] else "")
               if s["median_ms"] is not None else
               ("runs: " + ", ".join(fmt_ms(v) for v in s["values_ms"]) + f" · mean {fmt_ms(s['mean_ms'])}"
                if s["values_ms"] else "Not measured yet"))
        rows.append({"phase": label, "result": val, "samples": s["n"], "note": s["warning"] or ""})
    return rows


def _performance(m: dict):
    _anchor("performance", "How fast does it solve the problem?")
    st.caption("Computed now from recorded incidents. Phases are consecutive segments of one incident (start → drift "
               "report → last proposal step → validation → new version → verified), so nothing is counted twice. "
               "Total is the engine's own measured duration. Environments are never combined.")
    envs = m["environments"]
    any_data = False
    for env, e in envs.items():
        with st.container(border=True):
            st.markdown(f"**{ENV_LABEL.get(env, env)}** · {e['incidents']} incidents · {e['evidence_records']} "
                        f"evidence records")
            if not e["evidence_records"] and not e["incidents"]:
                st.caption("Not measured yet.")
                continue
            any_data = True
            c1, c2, c3 = st.columns(3)
            c1.metric("Verified recovery rate", f"{e['verification_rate']}%" if e["verification_rate"] is not None
                      else "Not measured", help=f"{e['verified']} of {e['eligible']} runs that were expected to recover")
            c2.metric("Safe refusal rate", f"{e['safe_refusal_rate']}%" if e["safe_refusal_rate"] is not None
                      else "Not measured", help=f"{e['safely_refused']} of {e['unsafe_expected']} deliberately unsafe runs")
            c3.metric("Failed recoveries", f"{e['failed']} of {e['eligible']}" if e["eligible"] else "Not measured")
            st.dataframe(_phase_table(e), hide_index=True, use_container_width=True)
            if e["failure_reasons"]:
                st.caption("Failure reasons: " + "; ".join(f"{k} ({v})" for k, v in e["failure_reasons"].items()))
    if not any_data:
        st.info("Not measured yet: no experiments are recorded in this app instance.", icon=":material/info:")
    with st.expander("Run Benchmark (simulation, rules only)"):
        st.caption("Runs every Simulation Lab scenario against in-process simulated APIs with the deterministic "
                   "rules planner. It writes SIMULATION records only (no real API, no model call). Results appear "
                   "above under SIMULATION. Success criterion: each run's outcome matches its declared expectation.")
        runs = st.number_input("Runs per scenario", 1, 3, 1, key="rv_bench_runs")
        ok = st.checkbox("I confirm: add benchmark runs to the SIMULATION records", key="rv_bench_ok")
        if st.button("Run Benchmark", disabled=not ok, icon=":material/speed:"):
            cat = safe_call(api, "GET", "/api/simulations") or []
            rows, bar = [], st.progress(0.0)
            for i in range(int(runs)):
                for j, sc in enumerate(cat):
                    r = safe_call(api, "POST", "/api/simulations", json={"scenario": sc["id"], "mode": "adaptix",
                                                                           "llm": "none", "seed": 7 + i})
                    rows += (r or {}).get("runs", [])
                    bar.progress((i * len(cat) + j + 1) / (int(runs) * max(1, len(cat))))
            mutated()
            ok_n = sum(r["matches_expectation"] for r in rows)
            st.success(f"SIMULATION benchmark: {ok_n}/{len(rows)} runs matched their expected outcome.")
            st.dataframe([{"scenario": r["title"], "expected": r["expected"], "outcome": r["outcome"],
                           "as expected": r["matches_expectation"], "duration": fmt_ms(r["duration_ms"])}
                          for r in rows], hide_index=True, use_container_width=True)


def _try_it(m: dict, local: bool):
    st.header("Interactive schema-drift example", anchor="try-it")
    if not local:
        st.info("The live customer_id → client_id demonstration runs on a local machine (local database, local API "
                "and local model). It is not reachable from this website. Setup instructions: "
                f"[LOCAL_DEMO.md]({REPO}/blob/main/LOCAL_DEMO.md).", icon=":material/computer:")
        return
    st.page_link(st.session_state["_pages"]["localdemo"], label="Open the Local Demo Lab", icon=":material/science:")
    st.caption("The lab changes the local demo database only after you confirm; opening this page changes nothing.")
    rec = next((r for r in m["environments"].get("LOCAL_DEMO", {}).get("latest", [])), None)
    if rec:
        st.markdown(f"Latest recorded local experiment: **{(rec.get('final') or {}).get('outcome')}** · "
                    f"{ist(rec.get('recorded_at'))} · scenario `{rec.get('scenario')}`")


def _evidence(m: dict):
    _anchor("evidence", "Evidence and trust")
    envs = [e for e, v in m["environments"].items() if v["latest"]]
    if not envs:
        st.info("No evidence records yet. Run the Drift Demo, the Simulation Lab or the Local Demo Lab.",
                icon=":material/inbox:")
    else:
        env = st.radio("Environment", envs, format_func=lambda e: ENV_LABEL.get(e, e), horizontal=True, key="rv_env")
        recs = m["environments"][env]["latest"]
        pick = st.selectbox("Record", range(len(recs)), key=f"rv_rec_{env}",
                            format_func=lambda i: f"{ist(recs[i].get('recorded_at'))} · {recs[i].get('scenario') or ''}"
                                                  f" · {(recs[i].get('final') or {}).get('outcome')}")
        r = recs[pick]
        llm, diff = r.get("llm") or {}, r.get("schema_diff") or {}
        st.dataframe([
            {"item": "Experiment / run", "value": f"{r.get('run_id')} · {r.get('evidence_id')}"},
            {"item": "Recorded", "value": ist(r.get("recorded_at"))},
            {"item": "Environment and source", "value": f"{ENV_LABEL.get(env, env)} · {r.get('evidence_source')}"},
            {"item": "Model / provider", "value": f"{llm.get('provider')} · {llm.get('model')} · "
                                                  f"{'real request' if llm.get('real_request_performed') else 'no real model request'}"
             if llm.get("used") else "not used (rules or no repair)"},
            {"item": "Original failure", "value": f"HTTP {(r.get('first_failure') or {}).get('http_status')} · "
                                                  f"{(r.get('first_failure') or {}).get('classification')}"},
            {"item": "Schema difference", "value": diff.get("summary") or "none recorded"},
            {"item": "Validation", "value": "accepted" if (r.get("validation") or {}).get("accepted") else
             ("rejected" if r.get("validation") else "not reached")},
            {"item": "Applied change", "value": f"v{r['applied_change']['from_version']} → v{r['applied_change']['to_version']}"
             if r.get("applied_change") else "none"},
            {"item": "Retry and verification", "value": f"final HTTP {(r.get('retry') or {}).get('final_http_status')} · "
                                                        f"verified recovery: {(r.get('final') or {}).get('verified_recovery')}"},
            {"item": "Total recovery time", "value": fmt_ms((r.get("timing") or {}).get("elapsed_ms"))},
            {"item": "Rollback / refusal", "value": "rolled back" if (r.get("rollback") or {}).get("performed") else
             ((r.get("final") or {}).get("reason") or "—")}], hide_index=True, use_container_width=True)
        with st.expander("Technical details (redacted record)"):
            st.json(r, expanded=False)
        st.caption("Provenance: live record from this app instance's stored evidence, redacted when it was written.")
    st.subheader("What ADAPTIX does not promise")
    st.markdown("- It cannot guarantee recovery from every failure.\n"
                "- It cannot reconstruct deleted information without a valid source.\n"
                "- It does not automatically approve ambiguous or unsafe changes.\n"
                "- Schema retrieval or network restrictions can prevent a repair.\n"
                "- A valid HTTP response does not necessarily mean the business operation succeeded.")


def _differentiators_and_uses():
    st.header("Design goals", anchor="design-goals")
    st.dataframe([
        {"design goal": "Verified recovery, not just a suggested patch", "shown by": "verification checks on every recovery"},
        {"design goal": "Independent validation before applying", "shown by": "validator results in each incident"},
        {"design goal": "Explainable field-level differences", "shown by": "Schema Drift Explorer and drift reports"},
        {"design goal": "Versioned audit evidence and safe rollback", "shown by": "Tool Registry versions, audit log"},
        {"design goal": "Controlled, repeatable drift experiments", "shown by": "Drift Demo, Simulation Lab, Local Demo Lab"},
        {"design goal": "Clear separation of local, simulated and real execution", "shown by": "environment labels"}],
        hide_index=True, use_container_width=True)
    st.caption("Stated as design goals of this project; no claim is made about other products.")
    st.header("Use cases", anchor="use-cases")
    st.markdown("- An API response field is renamed\n- A request parameter or field type changes\n"
                "- A tool schema no longer matches its registered contract\n- A required field is introduced "
                "(ADAPTIX stops safely when the caller never supplied the value)\n- An incompatible field is removed "
                "(stopped safely: deleted data is not invented)\n- An integration breaks after an upstream API change")
    st.caption("Authentication failures, outages, rate limits, network restrictions and missing business data are "
               "different failure classes. ADAPTIX classifies them and does not treat them as schema mappings.")


def _scorecard(m: dict, stage_counts: dict):
    st.header("Reviewer scorecard", anchor="scorecard")
    envs = m["environments"]
    verified = sum(e["verified"] for e in envs.values())
    refused = sum(e["safely_refused"] for e in envs.values())
    rollbacks = stage_counts.get("rollback", 0)
    local = envs.get("LOCAL_DEMO")
    rows = [
        ("Schema drift detection", "registered vs live contract diff", f"{stage_counts.get('diff', 0)} recorded drift "
         "reports", "implemented" if stage_counts.get("diff") else "implemented · not measured here"),
        ("Verified recovery", "retry + business verification", f"{verified} verified recoveries",
         "implemented" if verified else "implemented · not measured here"),
        ("Safe refusal", "unsafe changes are not applied", f"{refused} safe refusals",
         "implemented" if refused else "implemented · not measured here"),
        ("Rollback", "failed verification reverts the version", f"{rollbacks} recorded rollback steps",
         "implemented" if rollbacks else "implemented · not measured here"),
        ("Gemini repair proposals", "cloud model proposes, validator decides",
         f"connectivity: {str((m.get('llm') or {}).get('connectivity')).replace('_', ' ')}", "implemented"),
        ("Local LLM repair (Ollama)", "local model proposes, validator decides",
         f"{local['evidence_records']} local records" if local else "local machine only",
         "implemented (local setup)" if local is None or not local["evidence_records"] else "implemented")]
    t = _tests_result()
    rows.append(("Automated tests", "regression checks (not proof of production readiness)",
                 f"{t['tests']} tests · {t['failures']} failed · {t['errors']} errors · {t['skipped']} skipped "
                 f"(report {t['recorded']})" if t else "no recorded test report in this app instance",
                 "implemented" if t else "not measured"))
    st.dataframe([{"capability": a, "what it demonstrates": b, "evidence available": c, "status": d}
                  for a, b, c, d in rows], hide_index=True, use_container_width=True)


def _feedback():
    _anchor("feedback", "Feedback")
    st.caption("This form is not connected to a server. Nothing is sent or stored. After you complete it, download "
               "or copy your feedback and share it with the team.")
    with st.form("rv_feedback", clear_on_submit=False):
        role = st.text_input("Reviewer role or affiliation (optional)", max_chars=80)
        overall = st.slider("Overall rating", 1, 5, 4)
        clarity = st.select_slider("Ease of understanding", ["very hard", "hard", "ok", "easy", "very easy"], "ok")
        safety = st.select_slider("Confidence in repair safety", ["very low", "low", "medium", "high", "very high"],
                                  "medium")
        valuable = st.selectbox("Most valuable feature", ["Verified recovery", "Independent validation",
                                                          "Field-level drift explanation", "Audit and rollback",
                                                          "Repeatable experiments", "Local LLM demo", "Other"])
        concern = st.text_area("Biggest concern or missing feature", max_chars=600)
        comments = st.text_area("Other comments (optional)", max_chars=1500)
        done = st.form_submit_button("Prepare feedback")
    if done:
        problems = validate_feedback(concern)
        for p in problems:
            st.error(p)
        if not problems:
            fb = {"role": role.strip() or None, "overall": overall, "ease_of_understanding": clarity,
                  "confidence_in_safety": safety, "most_valuable": valuable, "concern": concern.strip(),
                  "comments": comments.strip() or None, "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
            st.info("Feedback prepared, not submitted. Download or copy it below.", icon=":material/content_copy:")
            st.download_button("Download feedback (JSON)", json.dumps(fb, indent=2), "adaptix-feedback.json",
                               "application/json")
            st.code(json.dumps(fb, indent=2), language="json")


def validate_feedback(concern: str) -> list[str]:
    if not concern or len(concern.strip()) < 5:
        return ["Please describe your biggest concern or a missing feature (at least 5 characters)."]
    return []


def render():
    from adaptix.config import get_settings
    local = local_demo_enabled()
    _hero(local)
    m = safe_call(api, "GET", "/api/review/metrics")
    if m is None:
        return
    counts = m.get("stages") or {}
    _problem()
    _how(counts)
    _technology(m, local)
    _performance(m)
    _try_it(m, local)
    _evidence(m)
    _differentiators_and_uses()
    _scorecard(m, counts)
    _feedback()
    s = get_settings()
    st.divider()
    if st.button("Back to the ADAPTIX app", icon=":material/arrow_back:", key="rv_back"):
        st.query_params.clear()
        st.rerun()
    if not s.review_url:
        st.caption("ADAPTIX_REVIEW_URL is not set, so the logo opens this page in the current app.")


__all__ = ["render", "review_link", "validate_feedback"]
