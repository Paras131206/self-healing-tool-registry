"""ADAPTIX dashboard. Every page talks to the ADAPTIX API (in-process by default, or ADAPTIX_API_URL).

Three separate environments, each with its own database: REAL API (/api, real providers), REAL API TEST
(/api/test, the controlled demo service over real HTTP) and SIMULATION (/api/sim). Pages that show records
offer one shared environment selector."""
import streamlit as st

from adaptix.config import get_settings
from views import chaos, demo, drift, evidence, health, localdemo, monitoring, overview, recovery, review, tools
from views.common import ROOT, api, inject_css, local_demo_enabled, safe_call, theme_toggle

st.set_page_config(page_title="ADAPTIX · Autonomous API Resilience", page_icon=":material/autorenew:",
                   layout="wide", initial_sidebar_state="expanded")
inject_css()
# the logo opens the product review page: ADAPTIX_REVIEW_URL if configured, else this app's own ?view=review
st.logo(str(ROOT / "assets" / "logo.svg"), size="large",
        link=review.review_link(get_settings().review_url, st.context.url))
LOCAL = local_demo_enabled()


def recovery_center():
    recovery.render()


RENDER = {"overview": overview.render, "monitoring": monitoring.render, "tools": tools.render, "drift": drift.render,
          "recovery": recovery_center, "evidence": evidence.render, "demo": demo.render, "chaos": chaos.render,
          "health": health.render, "review": review.render}
if LOCAL:
    RENDER["localdemo"] = localdemo.render
P = st.Page
pages = {
    "overview": P(overview.render, title="Overview", icon=":material/dashboard:", default=True),
    "monitoring": P(monitoring.render, title="API Registry", icon=":material/hub:", url_path="api-registry"),
    "tools": P(tools.render, title="Tool Registry", icon=":material/inventory_2:", url_path="tool-registry"),
    "drift": P(drift.render, title="Schema Drift Explorer", icon=":material/difference:", url_path="drift"),
    "recovery": P(recovery_center, title="Recovery Center", icon=":material/healing:", url_path="recovery"),
    "evidence": P(evidence.render, title="Evidence & Scorecard", icon=":material/gavel:", url_path="evidence"),
    "demo": P(demo.render, title="Drift Demo", icon=":material/bolt:", url_path="drift-demo"),
    "chaos": P(chaos.render, title="Simulation Lab", icon=":material/science:", url_path="simulation-lab"),
    "health": P(health.render, title="System Health", icon=":material/verified:", url_path="integrations"),
}
if LOCAL:   # local-only page: present only when ADAPTIX_LOCAL_DEMO=1 is set on this machine
    pages["localdemo"] = P(localdemo.render, title="Local Demo Lab", icon=":material/science:", url_path="local-demo")
theme_toggle([p.url_path for p in pages.values()])
st.session_state["_pages"] = pages
nav = st.navigation({
    "Monitor and repair": [pages[k] for k in ("overview", "monitoring", "tools", "drift", "recovery", "evidence")],
    "Demonstrate (not production)": [pages["demo"], pages["chaos"]],
    **({"Local only (this machine)": [pages["localdemo"]]} if LOCAL else {}),
    "System": [pages["health"]]})

with st.sidebar:
    # the product overview (review) page: same destination as the logo, opened in this tab
    from html import escape
    on_review = st.query_params.get("view") == review.VIEW
    href = get_settings().review_url or f"?view={review.VIEW}"
    st.markdown(f'<a class="ax-overview-link{" current" if on_review else ""}" href="{escape(href)}" target="_self"'
                f'{" aria-current=\"page\"" if on_review else ""}>'
                f'<span aria-hidden="true">★</span> Product overview</a>', unsafe_allow_html=True)
    h = safe_call(api, "GET", "/api/health")
    if h:
        llm = h["llm"]
        st.caption(f"ADAPTIX v{h['version']}" + (" · LOCAL DEMO enabled" if LOCAL else ""))
        st.caption(f"LLM: {llm['provider']} · {llm['connectivity'].replace('_', ' ')}")
        st.caption(f"Real APIs: {h['real_tools']} · approved hosts: {len(h['allowed_hosts'])}")
    st.caption("Team Runtime Terrors · PS-01")

# tests can render a page directly; visitors always go through the navigation. The product review page is not a
# navigation page: the ADAPTIX logo and the sidebar's "Product overview" link open it (?view=review)
if "_force_page" in st.session_state:
    RENDER[st.session_state["_force_page"]]()
elif st.query_params.get("view") == review.VIEW:
    review.render()
else:
    nav.run()
