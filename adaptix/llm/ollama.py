"""Local LLM provider: a model served by Ollama on THIS machine (LOCAL DEMO only).

* The endpoint must be plain http on a loopback address (127.0.0.1, localhost, ::1). Anything else is refused,
  so a request can never leave the machine. Proxies from the environment are ignored.
* Ollama "cloud" models (tags ending in -cloud / :cloud) are refused: they run remotely.
* Every plan is a genuine inference request to /api/chat with the repair-plan JSON schema as the required output
  format, temperature 0, a bounded output length and a timeout. The output is validated (LLMRepairPlan) and then
  validated again, independently, by ADAPTIX's validator before anything is applied.
* There is no fallback: if Ollama is stopped, the model is missing or inference fails, the result says so and
  the repair stops. It never switches to Gemini, the mock, or canned output.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ..models import LLMRepairPlan, now_iso
from ..security import scrub
from . import SYSTEM_INSTRUCTION, LLMResult, build_prompt

LOOPBACK = ("127.0.0.1", "localhost", "::1")
SMALLER = {"qwen2.5:7b-instruct": "qwen2.5:3b-instruct", "qwen2.5:3b-instruct": "qwen2.5:1.5b-instruct"}


class LocalLLMError(ValueError):
    pass


def check_local_endpoint(url: str) -> str:
    p = urlsplit(url)
    if p.scheme != "http" or (p.hostname or "") not in LOOPBACK or p.username or p.password:
        raise LocalLLMError("the local LLM endpoint must be http on a loopback address (e.g. http://127.0.0.1:11434)")
    return url.rstrip("/")


def check_local_model(model: str) -> str:
    m = (model or "").strip()
    if not m or "://" in m or any(ch.isspace() for ch in m):
        raise LocalLLMError(f"{m or '(empty)'} is not a local Ollama model name")
    if m.endswith(("-cloud", ":cloud")):
        raise LocalLLMError("Ollama cloud models run remotely and are not allowed in Local LLM mode")
    return m


def plan_schema(quotes: list[str]) -> dict:
    """The repair-plan JSON schema Ollama enforces while decoding. When the live contract has quotable text, each
    mapping's evidence must be one of those exact strings, so a small model cannot leave it blank or
    paraphrase it. This only shapes the output format: ADAPTIX's validator still checks every proposal."""
    import copy
    schema = copy.deepcopy(LLMRepairPlan.model_json_schema())
    mapping = schema.get("$defs", {}).get("Mapping")
    if quotes and mapping:
        mapping["properties"]["evidence"] = {"type": "string", "enum": list(quotes),
                                             "description": "an exact quote from the schema text"}
        mapping["required"] = sorted(set(mapping.get("required", [])) | {"evidence", "source_arg", "target_field"})
    return schema


class OllamaProvider:
    name, real = "ollama", True
    label = "Local LLM"

    def __init__(self, base_url: str, model: str, timeout_s: float = 90.0, max_tokens: int = 600,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = check_local_endpoint(base_url)
        self.model = check_local_model(model)
        self.timeout_s, self.max_tokens = timeout_s, max_tokens
        # tests inject a transport (a test double); results are then never labelled as real inference
        self.test_double = transport is not None
        self.real = transport is None
        self._client = httpx.Client(base_url=self.base_url, trust_env=False, follow_redirects=False,
                                    timeout=httpx.Timeout(timeout_s, connect=3.0), transport=transport)
        self._lock = threading.Lock()
        self.last: dict = {}
        self.last_success_at: str | None = None

    def configured(self) -> bool:
        return True                      # readiness (server up, model present) is reported by status()

    # ---------------------------------------------------------------- readiness
    def readiness(self) -> dict:
        """Is the local server up and is the model installed? Sends no inference request."""
        out = {"provider": self.label, "model": self.model, "endpoint": self.base_url, "server": "unreachable",
               "model_installed": False, "version": None, "size_bytes": None, "installed_models": [], "action": None}
        try:
            v = self._client.get("/api/version", timeout=3.0)
            out["version"] = v.json().get("version") if v.status_code == 200 else None
            tags = self._client.get("/api/tags", timeout=5.0).json().get("models", [])
        except (httpx.HTTPError, ValueError):
            out["action"] = ("Start Ollama (open the Ollama app, or run: ollama serve). "
                             "Install it from https://ollama.com/download if it is missing.")
            return out
        out["server"] = "running"
        names = [t.get("name") or t.get("model") for t in tags]
        out["installed_models"] = [n for n in names if n]
        hit = next((t for t in tags if (t.get("name") or t.get("model")) in (self.model, f"{self.model}:latest")), None)
        if hit:
            out["model_installed"], out["size_bytes"] = True, hit.get("size")
        else:
            out["action"] = f"Pull the model once: ollama pull {self.model}" + (
                f" (a smaller option is {SMALLER[self.model]})" if self.model in SMALLER else "")
        return out

    def status(self) -> dict:
        r = self.readiness()
        conn = "not_running" if r["server"] != "running" else (
            "model_missing" if not r["model_installed"] else
            "verified" if self.last_success_at else ("failing" if self.last else "ready_unverified"))
        return {"provider": self.label, "model": self.model, "configured": True, "connectivity": conn,
                "last_success_at": self.last_success_at,
                "last_error_category": self.last.get("error_category") if self.last else None, "readiness": r}

    # ---------------------------------------------------------------- inference
    def _chat(self, prompt: str, json_output: bool, max_tokens: int, output_schema: dict | None = None) -> LLMResult:
        t0, attempts = time.perf_counter(), 0
        cat = err = None
        body = {"model": self.model, "stream": False, "keep_alive": "10m",
                "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}],
                "options": {"temperature": 0, "num_predict": max_tokens}}
        if json_output:
            body["format"] = output_schema or LLMRepairPlan.model_json_schema()
        while attempts < 2:                       # one bounded retry, only for malformed output
            attempts += 1
            try:
                r = self._client.post("/api/chat", json=body)
            except httpx.TimeoutException:
                return self._result(False, t0, attempts, "timeout", f"no answer within {self.timeout_s:.0f}s")
            except httpx.ConnectError:
                return self._result(False, t0, attempts, "ollama_not_running",
                                    "Ollama is not running on this machine (start the app or run: ollama serve)")
            except httpx.HTTPError as e:
                return self._result(False, t0, attempts, "network", scrub(str(e))[:200])
            try:
                data = r.json()
            except ValueError:
                data = {}
            if r.status_code == 404:
                return self._result(False, t0, attempts, "model_missing",
                                    f"model {self.model} is not installed (run: ollama pull {self.model})")
            if r.status_code != 200:
                return self._result(False, t0, attempts, f"http_{r.status_code}",
                                    scrub(str(data.get("error") or r.text))[:200])
            pt, ot = data.get("prompt_eval_count"), data.get("eval_count")
            text = (data.get("message") or {}).get("content") or ""
            if not text.strip():
                cat, err = "empty_output", "the model returned no text"
                continue
            if not json_output:
                return self._result(True, t0, attempts, None, None, pt, ot)
            try:
                plan = LLMRepairPlan.model_validate_json(text)
            except ValidationError as e:
                cat, err = "malformed_output", scrub(str(e))[:300]
                continue
            return self._result(True, t0, attempts, None, None, pt, ot, plan)
        return self._result(False, t0, attempts, cat or "unknown", err)

    def _result(self, ok, t0, attempts, cat, err, pt=None, ot=None, plan=None) -> LLMResult:
        r = LLMResult(provider=self.name, model=self.model, ok=ok, real=self.real and attempts > 0, plan=plan,
                      error_category=cat, error=err, duration_ms=round((time.perf_counter() - t0) * 1000, 1),
                      prompt_tokens=pt, output_tokens=ot, attempts=attempts, ts=now_iso())
        with self._lock:
            self.last = r.meta()
            if ok:
                self.last_success_at = r.ts
        return r

    def plan(self, context: dict) -> LLMResult:
        quotes = sorted({q for f in context.get("unresolved_fields", []) for q in f.get("quotable_evidence") or []})
        return self._chat(build_prompt(context), json_output=True, max_tokens=self.max_tokens,
                          output_schema=plan_schema(quotes))

    def check(self) -> LLMResult:
        """One tiny real inference request (readiness check)."""
        return self._chat("Reply with the single word OK.", json_output=False, max_tokens=8)
