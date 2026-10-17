"""Product review page: link configuration, metric calculations, no-data states, redaction, form validation."""
from adaptix import review
from views.review import review_link, validate_feedback


def inc(status="recovered", start="2026-10-17T10:00:00.000+00:00", llm_ms=None):
    t = lambda ms: f"2026-10-17T10:00:00.{ms:03d}+00:00"           # noqa: E731
    return {"status": status, "started_at": start, "duration_ms": 900.0,
            "llm": {"real": llm_ms is not None, "duration_ms": llm_ms},
            "timeline": [{"stage": "request", "status": "fail", "ts": t(10)}, {"stage": "diff", "status": "info", "ts": t(100)},
                         {"stage": "plan", "status": "ok", "ts": t(150)}, {"stage": "plan", "status": "info", "ts": t(600)},
                         {"stage": "validate", "status": "ok", "ts": t(620)}, {"stage": "apply", "status": "ok", "ts": t(650)},
                         {"stage": "verify", "status": "ok", "ts": t(890)}]}


def test_review_link_prefers_configured_url_and_falls_back_to_this_app():
    assert review_link("https://adaptix.example.org/review", "http://localhost:8501/drift") == \
        "https://adaptix.example.org/review"
    assert review_link("javascript:alert(1)", "http://localhost:8501/drift") == "http://localhost:8501/?view=review"
    assert review_link(None, "https://x.streamlit.app/") == "https://x.streamlit.app/?view=review"
    assert review_link(None, None) is None


def test_phase_timings_are_consecutive_and_not_double_counted():
    t = review.phase_timings(inc(llm_ms=420.0))
    assert t == {"detect": 100.0, "propose": 500.0, "validate": 20.0, "apply": 30.0, "verify": 240.0,
                 "total": 900.0, "llm_inference": 420.0}
    assert sum(t[p] for p in review.PHASES) <= t["total"]
    assert review.phase_timings(inc(status="stopped")) is None                # only verified recoveries are timed
    assert review.phase_timings(inc())["llm_inference"] is None               # no real model request, no LLM time


def test_summaries_respect_sample_size():
    assert review.summarize([])["n"] == 0 and review.summarize([])["median_ms"] is None
    small = review.summarize([10, 30])
    assert small["values_ms"] == [10, 30] and small["median_ms"] is None and "very small" in small["warning"]
    mid = review.summarize(list(range(1, 11)))
    assert mid["median_ms"] == 5.5 and mid["p95_ms"] is None and "p95 needs" in mid["warning"]
    big = review.summarize(list(range(1, 101)))
    assert big["median_ms"] == 50.5 and big["p95_ms"] == 95 and big["warning"] is None


def test_environment_rates_use_declared_expectations():
    ev = [{"expected": {"outcome": "recovered"}, "final": {"verified_recovery": True, "outcome": "recovered"}},
          {"expected": {"outcome": "recovered"}, "final": {"verified_recovery": False, "outcome": "stopped",
                                                           "reason": "schema unavailable"}},
          {"expected": {"outcome": "stopped"}, "final": {"verified_recovery": False, "outcome": "stopped"}},
          {"expected": {"outcome": None}, "final": {"verified_recovery": False, "outcome": "ok"}}]
    m = review.environment_metrics("SIMULATION", [inc()], ev)
    assert (m["eligible"], m["verified"], m["verification_rate"]) == (2, 1, 50.0)
    assert (m["unsafe_expected"], m["safely_refused"], m["safe_refusal_rate"]) == (1, 1, 100.0)
    assert m["failure_reasons"] == {"schema unavailable": 1} and m["phases"]["total"]["n"] == 1
    empty = review.environment_metrics("LIVE_API", [], [])
    assert empty["verification_rate"] is None and empty["phases"]["total"]["n"] == 0


def test_latest_evidence_keeps_only_display_fields():
    rec = {"evidence_id": "ev_1", "recorded_at": "2026-10-17T00:00:00+00:00", "final": {"outcome": "recovered"},
           "demo_events": ["internal"], "audit": {"correlation_id": "abc"}}
    out = review.latest_evidence([rec])
    assert "demo_events" not in out[0] and "audit" not in out[0] and out[0]["evidence_id"] == "ev_1"


def test_feedback_validation():
    assert validate_feedback("") and validate_feedback("  ok ")
    assert validate_feedback("The rollback flow needs a diagram") == []
