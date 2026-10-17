"""LIVE local-model test: a genuine inference request to Ollama on this machine. Skipped unless Ollama is running
and the configured model (LOCAL_LLM_MODEL, default qwen2.5:3b-instruct) is installed. Never uses a cloud model."""
import os
import time

import httpx
import pytest

from adaptix.config import Settings
from adaptix.llm.ollama import OllamaProvider
from adaptix.localdemo.database import DemoDatabase
from adaptix.localdemo.lab import LocalLab
from adaptix.localdemo.service import ServiceThread

MODEL = os.environ.get("LOCAL_LLM_MODEL", "qwen2.5:3b-instruct")
URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def _installed() -> bool:
    try:
        tags = httpx.get(URL + "/api/tags", timeout=2, trust_env=False).json().get("models", [])
        return any(t.get("name") == MODEL for t in tags)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _installed(), reason=f"live test: Ollama with {MODEL} not available")


def test_live_local_model_repairs_the_customer_id_rename(tmp_path):
    db = DemoDatabase(tmp_path / "local-demo.db")
    db.init()
    svc = ServiceThread(str(db.path)).start()
    try:
        s = Settings(local_demo=True, local_demo_db=str(db.path), local_db_path=str(tmp_path / "l.db"),
                     db_path=str(tmp_path / "p.db"), sim_db_path=str(tmp_path / "s.db"),
                     test_db_path=str(tmp_path / "t.db"), local_service_url=svc.url, local_llm_model=MODEL)
        lab = LocalLab(s, llm=OllamaProvider(URL, MODEL, timeout_s=180))
        j = lab.start("rename", {}, True)
        while lab.job(j["id"])["status"] == "running":
            time.sleep(0.2)
        r = lab.job(j["id"])["result"]
        print("LIVE local result:", r["final"], r.get("reason"), r.get("llm"))
        assert r["llm"]["real"] is True and r["llm"]["model"] == MODEL      # genuine local inference
        assert r["final"] in ("VERIFIED", "SAFELY BLOCKED")                  # a refusal is safe; never a false success
        if r["final"] == "VERIFIED":
            assert all(c["ok"] for c in r["business_checks"])
    finally:
        svc.stop()
