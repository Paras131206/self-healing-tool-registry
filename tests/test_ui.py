"""Dashboard tests: navigation, page structure and the main workflows, against the in-process API."""
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["overview", "monitoring", "tools", "drift", "recovery", "evidence", "demo", "chaos", "health", "review",
         "multichange"]


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIX_DB_PATH", str(tmp_path / "ui.db"))
    monkeypatch.setenv("ADAPTIX_SIM_DB_PATH", str(tmp_path / "ui-sim.db"))
    monkeypatch.setenv("ADAPTIX_TEST_DB_PATH", str(tmp_path / "ui-test.db"))
    monkeypatch.setenv("ADAPTIX_MONITOR_ENABLED", "false")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ADAPTIX_LOCAL_DEMO", raising=False)
    monkeypatch.delenv("ADAPTIX_REVIEW_URL", raising=False)
    from adaptix.config import get_settings
    get_settings.cache_clear()
    import streamlit as st
    st.cache_resource.clear()
    yield
    get_settings.cache_clear()
    st.cache_resource.clear()


def _page(name, timeout=60):
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=timeout)
    at.session_state["_force_page"] = name
    return at.run()


def _text(at) -> str:
    parts = []
    for kind in ("markdown", "caption", "success", "warning", "info", "error", "subheader", "title"):
        parts += [getattr(e, "value", "") or "" for e in getattr(at, kind)]
    return "\n".join(str(p) for p in parts)


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_empty(page):
    at = _page(page)
    assert not at.exception, at.exception


def test_navigation_titles():
    src = (ROOT / "app.py").read_text()
    titles = re.findall(r'P\([^)]*?title="([^"]+)"', src)
    assert titles == ["Overview", "API Registry", "Tool Registry", "Schema Drift Explorer", "Recovery Center",
                      "Evidence & Scorecard", "Drift Demo", "Simulation Lab", "Multi-Change Lab", "System Health",
                      "Local Demo Lab"]
    assert 'if LOCAL:' in src and "link=review.review_link(" in src and "REPO_URL" not in src   # logo -> review page
    assert "Product Review" not in src and 'st.query_params.get("view") == review.VIEW' in src     # not a nav page
    assert "Product overview</a>" in src and 'f"?view={review.VIEW}"' in src                         # sidebar link
    every_view = "\n".join(p.read_text() for p in (ROOT / "views").glob("*.py")) + src
    for gone in ("Real API Monitoring", "Live Project Integration", "Production Audit", "Schema Changes",
                 "All changes", "Baseline (no repair)", "livedb", "run_limit", "max_runs",
                 "Automatic injection", "STOP NOW"):        # schema-check intervals in API Registry are monitoring, not injection
        assert gone not in every_view, gone


def test_drift_explorer_shows_only_the_selected_tool():
    at = _page("chaos")
    at.checkbox(key="sim_confirm").check().run()
    at.button[0].click().run()                                   # rename-param
    at.selectbox[1].set_value(next(o for o in at.selectbox[1].options if "nested object" in o.lower())).run()
    at.button[0].click().run()                                   # a second scenario -> a second tool
    at.session_state["_force_page"] = "drift"
    at.session_state["env_choice"] = "SIMULATION"
    at.run()
    assert not at.exception, at.exception
    assert not at.tabs                                           # no tabs any more (All changes removed)
    tool_box = next(s for s in at.selectbox if s.label == "Tool")
    assert len(tool_box.options) == 2
    for opt in tool_box.options:
        tool_box.set_value(opt).run()
        text = _text(at)
        other = next(o for o in tool_box.options if o != opt)
        assert opt in text and other not in text                 # nothing from the other tool is shown
        assert "No schema drift detected" in text                # repaired tool now matches the live schema


def test_simulation_lab_needs_confirmation_and_shows_no_baseline_card():
    at = _page("chaos")
    btn = next(b for b in at.button if b.label == "Introduce change")
    assert btn.disabled
    at.checkbox(key="sim_confirm").check().run()
    next(b for b in at.button if b.label == "Introduce change").click().run()
    assert not at.exception, at.exception
    runs = at.session_state["chaos_last"][1]
    assert len(runs) == 1 and runs[0]["mode"] == "adaptix"       # only the ADAPTIX result
    text = _text(at)
    assert "ADAPTIX recovery" in text and "SIMULATED" in text and "Baseline (no repair)" not in text
    result = [m.value for m in at.markdown if "ADAPTIX recovery" in m.value]
    assert len(result) == 1 and "Baseline" not in result[0]   # one result, no baseline card beside it


def test_drift_demo_introduce_change_recovers_automatically_and_syncs_every_page():
    at = _page("demo", timeout=120)
    assert next(b for b in at.button if b.label == "Introduce change").disabled       # needs confirmation
    at.checkbox(key="demo_confirm").check().run()
    next(b for b in at.button if b.label == "Introduce change").click().run()
    assert not at.exception, at.exception
    text = _text(at)
    assert "Recovered and verified" in text and "Change introduced" in text and "Baseline check" in text
    assert "REAL API TEST" in text and "Baseline (no repair)" not in text
    incident = at.session_state["demo_job"]
    # the same records drive every other page, without a reload of the app
    for page in ("evidence", "recovery", "drift", "overview"):
        at.session_state["_force_page"] = page
        at.session_state["env_choice"] = "REAL API TEST"
        at.run()
        assert not at.exception, (page, at.exception)
        t = _text(at)
        if page == "evidence":
            assert any("recovered" in str(s.options) for s in at.selectbox if s.label == "Incident")
            board = at.dataframe[0].value
            assert int(board.loc[board["stage"] == "Verified recoveries", "count"].iloc[0]) == 1
        if page == "drift":
            assert "No schema drift detected" in t                # registered schema now = live schema (v2)
    assert incident


def test_mutation_clears_derived_state():
    at = _page("chaos")
    at.session_state["rt_matrix"] = {"stale": True}          # derived copies that other pages would show
    at.session_state["demo_job"] = "demo_stale"
    at.session_state["chaos_last"] = ("some-other-scenario", [])
    at.checkbox(key="sim_reset_ok").check().run()
    next(b for b in at.button if b.label == "Reset simulation data").click().run()
    assert not at.exception, at.exception
    for k in ("rt_matrix", "demo_job", "chaos_last"):
        assert k not in at.session_state, k


def test_evidence_page_empty_state_and_scorecard_rows():
    at = _page("evidence")
    assert not at.exception, at.exception
    assert any("No incidents recorded" in i.value for i in at.info)
    stages = list(at.dataframe[0].value["stage"])
    assert stages[:6] == ["Incidents recorded", "Schema drift detected", "Repairs proposed", "Patches validated",
                          "Patches applied", "Verified recoveries"]


def test_displayed_times_are_ist():
    at = _page("demo", timeout=120)
    at.checkbox(key="demo_confirm").check().run()
    next(b for b in at.button if b.label == "Introduce change").click().run()
    assert not at.exception, at.exception
    text = _text(at)
    assert " IST" in text and "UTC" not in text and "Z ·" not in text
    at.session_state["_force_page"] = "evidence"
    at.session_state["env_choice"] = "REAL API TEST"
    at.run()
    inc_box = next(s for s in at.selectbox if s.label == "Incident")
    assert inc_box.options and all(" IST · " in o for o in inc_box.options), inc_box.options   # labels as shown


def test_tool_registry_shares_the_selected_tool_with_the_drift_explorer():
    at = _page("chaos")
    at.checkbox(key="sim_confirm").check().run()
    at.button[0].click().run()
    at.selectbox[1].set_value(next(o for o in at.selectbox[1].options if "nested object" in o.lower())).run()
    at.button[0].click().run()
    at.session_state["_force_page"] = "tools"
    at.session_state["env_choice"] = "SIMULATION"
    at.run()
    assert not at.exception, at.exception
    box = next(s for s in at.selectbox if s.label == "Tool")
    chosen = box.options[-1]
    box.set_value(chosen).run()
    text = _text(at)
    assert "Registered schema" in text and "not the live" in text            # snapshot clearly labelled
    at.session_state["_force_page"] = "drift"
    at.run()
    assert not at.exception, at.exception
    assert next(s for s in at.selectbox if s.label == "Tool").value == chosen  # same tool selected
    assert chosen in _text(at)


def test_unavailable_schema_is_never_shown_as_no_drift():
    at = _page("chaos")
    at.checkbox(key="sim_confirm").check().run()
    at.button[0].click().run()
    at.session_state["_force_page"] = "drift"
    at.session_state["env_choice"] = "SIMULATION"
    at.run()
    tool = next(s for s in at.selectbox if s.label == "Tool").value
    # make the simulated API's schema unreadable (the in-process app disappears), then look again
    from views.common import _client
    api_app = _client()[0].app.state.adaptix
    api_app.sim_engine.upstreams._sim_clients.pop(tool)
    at.run()
    text = _text(at)
    assert "Unable to retrieve the authoritative schema" in text and "Automatic repair is paused" in text
    assert "No schema drift detected" not in text and not at.success


def test_public_ui_has_no_local_controls_or_database_paths():
    """Without ADAPTIX_LOCAL_DEMO=1 (the deployed site), no local page, route or database detail is exposed."""
    at = _page("health")
    assert not at.exception, at.exception
    text = _text(at)
    for leak in ("data/adaptix", ".db", "database `", "API in-process", "API: in-process", "Production YES"):
        assert leak not in text, leak
    assert "Real API monitoring" in text
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception, at.exception
    from views.common import _client, env_options
    assert "LOCAL DEMO" not in env_options()
    assert _client()[0].get("/api/local-demo/status").status_code == 404
    at.session_state["_force_page"] = "localdemo"
    at.run()
    assert at.exception and "localdemo" in str(at.exception[0].value)   # the page is not even registered


def test_review_page_without_records_says_not_measured_and_does_not_submit_feedback():
    at = _page("review")
    assert not at.exception, at.exception
    text = _text(at)
    assert "When APIs Break, ADAPTIX Works to Restore Them." in text
    assert "Not measured yet" in text and "Illustrative example" in text
    assert "What ADAPTIX does not promise" in text and "LOCAL_DEMO.md" in text           # local demo not reachable here
    assert "Nothing is sent or stored" in text
    form_btn = next(b for b in at.button if b.label == "Prepare feedback")
    form_btn.click().run()
    assert any("at least 5 characters" in e.value for e in at.error)                    # validated, nothing claimed
    assert not any("submitted" in s.value.lower() and "not submitted" not in s.value.lower() for s in at.success)


def test_review_page_metrics_come_from_recorded_runs():
    at = _page("chaos")
    at.checkbox(key="sim_confirm").check().run()
    at.button[0].click().run()                                      # one recorded SIMULATION recovery
    at.session_state["_force_page"] = "review"
    at.run()
    assert not at.exception, at.exception
    text = _text(at)
    assert "SIMULATION" in text and "very small sample (1)" in str([d.value for d in at.dataframe])


def test_local_demo_lab_renders_when_enabled_and_blocks_changes_until_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIX_LOCAL_DEMO", "1")
    monkeypatch.setenv("ADAPTIX_LOCAL_DEMO_DB", str(tmp_path / "local-demo.db"))
    monkeypatch.setenv("ADAPTIX_LOCAL_DB_PATH", str(tmp_path / "adaptix-local.db"))
    monkeypatch.setenv("ADAPTIX_LOCAL_SERVICE_URL", "http://127.0.0.1:9")       # nothing listens: not ready
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    from adaptix.config import get_settings
    get_settings.cache_clear()
    import streamlit as st
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["_force_page"] = "localdemo"
    at.run()
    assert not at.exception, at.exception
    text = _text(at)
    assert "LOCAL DEMO · this machine only" in text and "python -m adaptix.localdemo db init" in text
    assert "ollama.com/download" in text
    btn = next(b for b in at.button if b.label == "Introduce Drift & Run Recovery")
    assert btn.disabled                                                           # not ready, not confirmed
    assert "Local LLM** `qwen2.5:3b-instruct`" in text                            # its real model name, not Gemini


@pytest.mark.parametrize("page", ["overview", "tools", "drift", "recovery", "evidence"])
def test_every_record_page_works_with_the_local_demo_environment(page, tmp_path, monkeypatch):
    monkeypatch.setenv("ADAPTIX_LOCAL_DEMO", "1")
    monkeypatch.setenv("ADAPTIX_LOCAL_DEMO_DB", str(tmp_path / "local-demo.db"))
    monkeypatch.setenv("ADAPTIX_LOCAL_DB_PATH", str(tmp_path / "adaptix-local.db"))
    monkeypatch.setenv("ADAPTIX_LOCAL_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")
    from adaptix.config import get_settings
    get_settings.cache_clear()
    import streamlit as st
    st.cache_resource.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["_force_page"] = page
    at.session_state["env_choice"] = "LOCAL DEMO"
    at.run()
    assert not at.exception, (page, at.exception)


def _patch_panel_app():
    import views.localdemo as ld

    def fake_api(method, path, **kw):
        if "/patch/" in path:
            return {"error_before": None, "request_after": None, "model": None, "model_proposal": None, "plan": None,
                    "validation": None, "applied_version": None, "versions": [], "rolled_back": False}
        return {"status": "ok", "tool_version": 2, "duration_ms": 3.0, "sent_as": {"client_id": 101},
                "business_checks": []}
    ld.api = fake_api
    ld.patch_story("inc_x", key="k")


def test_patch_story_panel_resends_the_original_request():
    """The Local Demo Lab's patch panel: the re-send button must not collide with its own session state."""
    import views.localdemo as ld
    real_api = ld.api
    try:
        at = AppTest.from_function(_patch_panel_app).run()
        assert not at.exception, at.exception
        next(b for b in at.button if "Send the original request again" in b.label).click().run()
        assert not at.exception, at.exception
        assert any("Succeeded with tool v2" in s.value for s in at.success)
    finally:
        ld.api = real_api


def test_light_dark_toggle_is_on_every_page_and_keeps_the_current_view():
    from views.common import TOKENS
    assert set(TOKENS) == {"light", "dark"} and TOKENS["light"].keys() == TOKENS["dark"].keys()
    for page in ("overview", "review", "health"):
        at = _page(page)
        assert not at.exception, at.exception
        nav = next(m.value for m in at.markdown if 'class="ax-theme"' in m.value)
        assert 'href="?embed_options=light_theme"' in nav and 'href="?embed_options=dark_theme"' in nav
        assert 'target="_self"' in nav and 'aria-current="true"' in nav           # one mode marked as current
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.query_params["view"] = "review"
    at.run()
    nav = next(m.value for m in at.markdown if 'class="ax-theme"' in m.value)
    assert "view=review&amp;embed_options=dark_theme" in nav                    # switching keeps the review page
    cfg = (ROOT / ".streamlit" / "config.toml").read_text()
    assert "[theme.light]" in cfg and "[theme.dark]" in cfg and "\nbase =" not in cfg


def test_multichange_lab_states_reflect_what_actually_ran():
    at = _page("multichange")
    assert not at.exception, at.exception
    assert "127.0.0.1 in this run is the ADAPTIX server's own runtime" in _text(at)
    assert [o for o in next(s for s in at.selectbox if s.label == "Repair engine").options] == \
        ["Rules only (deterministic)"]                                            # no Gemini key: not offered
    btn = lambda: next(b for b in at.button if b.label == "Apply Changes & Start Recovery")   # noqa: E731
    assert btn().disabled                                                          # nothing selected yet
    at.session_state["mc_changes_service"] = [{"kind": "remove", "field": "email"},
                                              {"kind": "rename", "field": "email", "new": "mail"}]
    at.run()
    assert any("contradictory" in e.value for e in at.error)
    at.checkbox(key="mc_confirm_service").check().run()
    assert btn().disabled                                                          # invalid set: still disabled
    at.session_state["mc_changes_service"] = [{"kind": "rename", "field": "customer_id", "new": "client_id"},
                                              {"kind": "rename", "field": "email", "new": "contact_email"}]
    at.run()
    assert not btn().disabled
    btn().click().run()
    assert not at.exception, at.exception
    text = _text(at)
    assert "VERIFIED RECOVERY" in text and "RULES ONLY" in text
    assert "REAL GEMINI CALL" not in text and "LOCAL LLM CALL" not in text        # labels follow the real engine
    assert "Repair proposal · rules planner" in text
    assert any("Reset" == b.label for b in at.button) or "Reset this experiment" in str(at.expander)
