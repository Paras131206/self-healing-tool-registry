# ADAPTIX: autonomous API resilience

**APIs change. ADAPTIX adapts.**

PS-01 · Genesis 2.0 · Team Runtime Terrors (Paras Patil, Nitish Shidne, Sanchit Tawde, Om Ekbote)

When an API that an AI agent depends on changes its schema, ADAPTIX:

1. detects the failure;
2. decides whether it really is schema drift;
3. reads the authoritative schema;
4. plans a repair and validates it independently;
5. applies the repair as a new tool version;
6. retries the original task and verifies it.

Supported repairs run **autonomously, with no human approval step**. A repair that is ambiguous, unsafe or unverifiable stops with a plain-language reason. A repair that fails verification is rolled back.

> Honest scope: all evaluation numbers below come from **simulated upstreams** (in-process FastAPI apps, `sim://…`) and from cases our team wrote. They are not measurements of production incidents.

## Pipeline

```
agent ──► /api/repair/execute  or  /v1/openai/tool_calls
            A  execute the call (timeouts, idempotency key, credential from the registry)
            B  classify the failure  ── auth / rate limit / 5xx / business rule / invalid input are NOT drift
            C  discover the authoritative schema (OpenAPI or JSON Schema; fail closed if unavailable)
            D  diff registered vs live (renames, moves, type/required/enum/format, headers, response fields)
            E  plan: deterministic rules first; Gemini only for what the rules cannot ground
            F  validate independently (evidence grounding, lossless transforms, schema, credentials, provenance)
            G  apply atomically as a new version (optimistic concurrency + per-tool lock)
            H  rebuild the request from the caller's original arguments
            I  retry, bounded (backoff + jitter, honours Retry-After, total time budget)
            J  verify the task actually succeeded (status + applied values); repair response drift if needed
            K  roll back if verification fails
```

What is never treated as schema drift: 401/403 (credential problems; failover only to another *authorised* credential), 404, 429, 5xx, business-rule errors, and invalid input against an *unchanged* schema. A 400 or 422 counts as drift only when the live schema actually changed in a way that touches the failing request.

## Repository layout

| Path | Purpose |
|---|---|
| `adaptix/engine.py` | the pipeline A–K, the incident timeline, reuse of known mappings, loop prevention |
| `adaptix/classify.py` | failure classification with a plain-language explanation per category |
| `adaptix/schema.py`, `adaptix/diff.py` | authoritative schema parsing (OpenAPI / JSON Schema) and the drift report |
| `adaptix/planner.py`, `adaptix/validator.py` | rule planner and the independent validator (the LLM never bypasses it) |
| `adaptix/llm/gemini.py` | Gemini via the official `google-genai` SDK, structured output validated with Pydantic |
| `adaptix/llm/mock.py` | deterministic **mock**, used only in tests and always labelled "mock (not a real model)" |
| `adaptix/credentials.py` | credential registry: aliases, groups, scopes, states, authorised failover |
| `adaptix/security.py` | redaction and secret scrubbing for logs, audit payloads, errors and prompts |
| `adaptix/storage.py` | SQLite (WAL) per world: tools, versions, schema snapshots, incidents, events, executions, mappings, locks, monitoring checks; refuses records from the other world |
| `adaptix/netguard.py` | SSRF guard for real egress: approved hosts, https, public IPs only, metadata blocked, DNS pinning with verified TLS, redirect re-validation, per-host pacing, Retry-After cooldown |
| `adaptix/upstreams.py` | production = guarded real providers only; simulation = in-process `sim://` only |
| `adaptix/monitor.py` | API Registry monitoring: scheduled and on-demand schema checks, GET probes through the full pipeline |
| `adaptix/testservice.py` | the controlled, versioned test API (real uvicorn HTTP server on 127.0.0.1) for REAL_API_TEST |
| `adaptix/demo.py` | the manual Drift Demo: introduce one confirmed change on the controlled service, then automatic recovery with live, timed progress |
| `adaptix/realtest.py` | the 12-step end-to-end recovery demonstration and the real-HTTP scenario matrix |
| `adaptix/evidence.py` | evidence records per run, scorecard with denominators, CSV export |
| `adaptix/changes.py` | the schema change log (inside Evidence & Scorecard): rows from stored records, server-side filters, sorting and pagination |
| `adaptix/integrations.py` | Integration Health statuses (VERIFIED only from recorded real requests) |
| `adaptix/review.py` | product review metrics: per-environment rates and non-overlapping phase timings from stored records |
| `adaptix/llm/ollama.py` | Local LLM provider (Ollama on loopback only, structured output, no cloud fallback) |
| `adaptix/localdemo/` | LOCAL DEMO: SQLite demo database + migrations, the local customer API, the Local Demo Lab controller, CLI |
| `adaptix/verify.py` | task verification (status/echo outputs, or the authoritative response schema for read operations) |
| `adaptix/api.py` | FastAPI backend: production `/api/*`, simulation `/api/sim/*`, monitoring, integrations, OpenAI adapter |
| `adaptix/simulation.py`, `adaptix/mock_api.py` | Simulation Lab: 21 drift/failure scenarios against isolated simulated APIs |
| `adaptix/evaluation.py`, `adaptix/cases.py`, `fixtures/` | baseline vs ADAPTIX: 21 scenarios, 125 planned cases, 30 adversarial fixtures |
| `adaptix/metrics.py` | dashboard metrics computed only from stored records |
| `app.py`, `views/` | Streamlit dashboard: Overview, API Registry, Schema Drift Explorer, Recovery Center, Evidence & Scorecard; Drift Demo and Simulation Lab (labelled, not production); System Health |
| `tests/` | pytest suite (see below) |

## Run it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                                   # fill in on the server only; never commit .env
.venv/bin/streamlit run app.py                         # dashboard (runs the API in-process)
.venv/bin/uvicorn adaptix.api:app --port 8000          # standalone API, OpenAPI docs at /docs
.venv/bin/python -m pytest -q                          # tests
.venv/bin/python -m adaptix.evaluation --suite planned # baseline vs ADAPTIX (scenarios | planned | adversarial)
```

To point the dashboard at a separately deployed API, set `ADAPTIX_API_URL` and `ADAPTIX_ADMIN_TOKEN`.

### API (all `/api/*` routes need `Authorization: Bearer $ADAPTIX_ADMIN_TOKEN`)

| Method and path | Purpose |
|---|---|
| `GET /api/health/live` | liveness check (no auth) |
| `GET /api/health` | DB, LLM status, credential aliases, limits |
| `POST /api/health/llm-check` | sends one minimal **real** request to the configured provider |
| `GET/POST /api/tools`, `GET/DELETE /api/tools/{name}` | registry (registration reads the authoritative schema) |
| `GET /api/tools/{name}/versions`, `/drift` | version history; registered vs live schema |
| `POST /api/tools/{name}/rollback` | restore the parent version |
| `POST /api/repair/execute` | run a tool call through the pipeline |
| `POST /api/repair/analyze` | diagnose and plan **without sending anything** |
| `GET /api/incidents[/{id}]`, `GET /api/audit`, `GET /api/metrics` | records, audit log, metrics |
| `GET /api/monitor`, `POST /api/monitor/{name}/check`, `PUT /api/monitor/{name}`, `GET /api/monitor/{name}/checks` | API Registry: overview, Check now, schedule, history |
| `GET /api/integrations` | Integration Health report |
| `GET /api/sim/tools…`, `/api/sim/incidents…`, `/api/sim/audit`, `/api/sim/metrics` | the same read routes for the **simulation** database |
| `GET/POST /api/simulations`, `POST /api/simulations/reset` | Simulation Lab runs (`llm`: `none` / `mock` / `gemini`) |
| `POST /api/evaluation`, `GET /api/evaluation/latest` | baseline vs ADAPTIX evaluation (simulation database) |
| `GET /v1/openai/tools`, `POST /v1/openai/tool_calls` | OpenAI-compatible tool calling (see `examples/openai_agent_loop.py`) |

## Using the dashboard

| Page | What it is for |
|---|---|
| **Overview** | counts per environment from stored records |
| **API Registry** | register real HTTPS APIs and monitor them: Check now, schedules, live schema status |
| **Tool Registry** | every registered tool (any environment): endpoint, method, schema source and URL, active version, status, the registered schema snapshot, versions and rollback |
| **Schema Drift Explorer** | pick ONE tool: registered vs live schema, each difference and what ADAPTIX does about it |
| **Recovery Center** | every incident as a timeline: failure → schema → drift → proposal → validation → patch → retry → verified or rolled back |
| **Evidence & Scorecard** | honest pipeline counts (proposed ≠ validated ≠ applied ≠ verified), one incident in detail, audit history, schema change log, exports |
| **Drift Demo** | choose a change, confirm, and watch ADAPTIX detect and repair it automatically on its own controlled test API (REAL API TEST) |
| **Simulation Lab** | the same idea with in-process simulated APIs, plus the baseline-vs-ADAPTIX evaluation |
| **System Health** | Gemini, credentials and external API status |
| **Product Review** | for reviewers: the problem, the pipeline, the technology, performance and evidence computed from recorded runs, and a feedback form that exports (nothing is submitted). It has no sidebar tab: the ADAPTIX logo opens it (`ADAPTIX_REVIEW_URL` if set, otherwise this app's `?view=review`) |
| **Local Demo Lab** | local machine only (`ADAPTIX_LOCAL_DEMO=1`): a real SQLite database, a real local customer API and a local LLM (Ollama). Never shown on the deployed site. See [LOCAL_DEMO.md](LOCAL_DEMO.md) |

Times are shown in Indian Standard Time (records are stored in UTC). All pages read the same records through the API, and a shared environment selector (REAL API / REAL API TEST / SIMULATION) keeps them aligned. There is no automatic fault injection: a demo change happens only when you choose it and confirm.

## API Registry

Register a genuine HTTPS API on the **API Registry** page (or `POST /api/tools`). Each registration needs:

- the provider and endpoint;
- the schema source: an OpenAPI or JSON Schema URL, or "none";
- optionally, a credential group and a safe **read-only probe** (GET tools only).

Each check, run on a schedule or with **Check now**:

1. **Fetches the authoritative schema** through the SSRF guard.
2. **Compares it with the schema saved for the active version.** With no change, it reports "No schema drift detected". Nothing is invented.
3. **Runs the probe through the full pipeline**, if the tool has one. A call broken by confirmed drift is diagnosed, repaired, validated, retried, verified, and rolled back if verification fails. A call that still works shows the drift does not break that task.
4. **Stores the result:** connectivity, schema status, the changes, verification, and any incident and repair.

Rules:

- **Only approved hosts.** Hosts must be listed in `ADAPTIX_ALLOWED_HOSTS`. Private, loopback, link-local and metadata addresses are always refused, as are redirects to them.
- **Polite to providers.** The minimum interval is 5 minutes, there is at most one request per host per second, and a 429's Retry-After is honoured.
- **Errors are not drift.** Authentication, authorization, quota, network, TLS and business-rule failures are reported with their category and never repaired as schema drift. A rejected key is never altered.
- **Providers without a schema** are registered with declared fields. These are *not* authoritative: drift cannot be verified, the tool is never repaired, and the page says so.

## Real API Test: a controlled, reproducible recovery

The **REAL_API_TEST** environment uses a small versioned API that ADAPTIX itself controls. It is served by a real HTTP server (uvicorn on a loopback TCP socket) with an authoritative OpenAPI document. No third-party API is modified. The guard exempts only that server's exact address; any other loopback port, private address or `sim://` URL is still refused. Records go to their own database (`ADAPTIX_TEST_DB_PATH`).

The app starts this service itself, on `127.0.0.1` with a port the OS picks at start-up. That address only exists inside the app's server: on Streamlit Cloud it is the cloud container, so it cannot be opened from your computer. To run the service on its own, locally, with a fixed port:

```bash
python -m adaptix.testservice --port 8765
curl http://127.0.0.1:8765/orders-api/health
```

**End-to-end demonstration** (Drift Demo page → *Orders API*, or `POST /api/test/demo`). The schema change: `customer_id` becomes `client_id`.

1. **Preflight.** The test service answers over HTTP, and Gemini answers a real minimal request. If either fails, the run stops with an actionable error and nothing falls back.
2. **Original task on schema v1.** The order is placed. The request, response, schema fingerprint and version are recorded, and the business outcome is confirmed by reading the stored order back (`GET /orders/{id}`).
3. **Controlled schema change.** The test service switches to v2 through its token-protected admin API.
4. **The original request now fails.** It is sent unchanged and fails with HTTP 422, classified as schema drift using the authoritative diff.
5. **Repair.** ADAPTIX runs with the **LLM required**: real Gemini proposes the mapping, quoting the schema. The rules plan is recorded only as a cross-check.
6. **Validation and apply.** The independent validator accepts or rejects the proposal. The registry moves v1 → v2 with a version check, and the call is retried with the same idempotency key.
7. **Verification.** The API's answer is verified, then the order is **read back independently** to confirm the intended customer, product and quantity.
8. **Failure demo (`fault: drop_writes`).** v2 answers "created" but stores nothing. The read-back fails, the registry is rolled back to v1, and the order is **not** re-sent.

**Scenario matrix** (`POST /api/test/matrix`). 15 change types run over real HTTP against the same service:

- rename (by name, documented, prose-only);
- safe and lossy type changes;
- new required field;
- removed field;
- nested request and response changes;
- ambiguous rename;
- invalid credential and expired-key failover;
- 429 with Retry-After, and a 503 outage;
- a repair that fails verification.

The LLM choice is rules only or real Gemini. The mock is refused here.

**Evidence.** Every run (REAL_API_TEST, SIMULATION, and production incidents) produces an evidence record with:

- the run and incident IDs, the environment and the evidence source;
- the sanitized endpoint and schema fingerprints before and after;
- the original request and the redacted response, the HTTP status and the classification;
- the schema diff;
- the Gemini model and whether a real request was made;
- the proposal, the validation decision and the applied version diff;
- retries, timestamps and elapsed time;
- the API-reported and the independent business verification;
- rollback, and audit references.

Inspect it on **Evidence & Scorecard**, or export `GET /api/test/evidence/export.json` and `/export.csv`. Secrets and sensitive fields are redacted.

**Results measured on 2026-10-16** (REAL_API_TEST, one run per scenario):

| Run | Outcomes as expected | Verified recoveries / eligible | Unsafe repairs accepted | False-positive drift | Real Gemini requests |
|---|---|---|---|---|---|
| Matrix, rules only | 15 / 15 | 7 / 7 | 0 | 0 / 15 | 0 |
| Matrix, real Gemini | 15 / 15 | 8 / 8 (adds the prose-documented rename) | 0 | 0 / 15 | 3 |
| Orders demo | recovered, business outcome verified | 1 / 1 | 0 | 0 | 2 (preflight + proposal) |
| Orders demo, failure | rolled back, correctly | — | 0 | 0 | 2 |

These are small demonstration samples against a test service we control, not a reliability guarantee. Gemini's response time on these runs was 3–37 s per request; the rules-only repairs took a few milliseconds.

## Simulation Lab and data separation

- **Separate in every layer.** Simulations use their own database (`ADAPTIX_SIM_DB_PATH`), their own credential registry with throwaway keys, and in-process `sim://` APIs only.
- **Enforced in the backend:**
  - each database records its mode, and every write is checked against it;
  - production upstreams refuse `sim://`, and simulation upstreams refuse real URLs;
  - production refuses simulated tools;
  - the production engine never accepts the mock LLM, and `LLM_PROVIDER=mock` means no LLM there;
  - simulations refuse to run on the production engine.
- **Choosing the LLM per run:** rules only (the default, deterministic), the **mock** (labelled "MOCK (not a real model)"), or **real Gemini** (labelled "REAL Gemini request"; only offered when a key is configured).
- **What each run shows:** the original and changed schema, detected drift, proposed repair, validation checks, retry, verification and rollback.
- **Honest outcomes:** stopping safely is the correct result in many scenarios, and a mismatch with the expected outcome is reported as one.

## Configuration

See [.env.example](.env.example) for every variable. The main ones:

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | unset | enables the Gemini tier. Without it, gaps the rules cannot ground stop safely |
| `GEMINI_API_KEY_SECONDARY` | unset | optional second authorised key, used for failover only |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | model name |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MIN_CONFIDENCE` | 20, 2, 0.8 | LLM request limits; plans below the confidence floor are rejected |
| `ADAPTIX_CREDENTIALS` | unset | JSON credential registry. Values are read from the env vars it names, never stored |
| `ADAPTIX_MAX_REPAIR_ATTEMPTS`, `ADAPTIX_MAX_RETRIES`, `ADAPTIX_TIME_BUDGET_SECONDS` | 2, 2, 30 | repair and retry bounds |
| `ADAPTIX_ADMIN_TOKEN` | random per process | bearer token for `/api/*` |
| `ADAPTIX_CORS_ORIGINS` | empty (CORS off) | comma-separated allowed origins |
| `ADAPTIX_MAX_BODY_BYTES` | 256000 | larger requests are rejected with 413 |
| `ADAPTIX_DB_PATH`, `ADAPTIX_SIM_DB_PATH`, `ADAPTIX_RETENTION_DAYS` | `data/adaptix.db`, `data/adaptix-sim.db`, 30 | production and simulation databases (must differ), retention |
| `ADAPTIX_ALLOWED_HOSTS` | empty (no real egress) | hosts you are authorized to call, e.g. `petstore3.swagger.io` or `*.example.com` |
| `ADAPTIX_MONITOR_INTERVAL_SECONDS`, `ADAPTIX_MONITOR_ENABLED` | 3600, true | default schema-check interval (min 300) and the scheduler switch |
| `ADAPTIX_HOST_MIN_INTERVAL_SECONDS`, `ADAPTIX_ALLOW_HTTP` | 1, false | per-host pacing; plain http only if explicitly enabled |

On Streamlit Cloud, put these under the app's **Secrets**. `config.env()` reads environment variables first, then `st.secrets`.

### Gemini

- Uses `google-genai==2.31.0`: `client.models.generate_content(model=…, contents=…, config=GenerateContentConfig(system_instruction=…, response_mime_type="application/json", response_json_schema=LLMRepairPlan schema, temperature=0))`.
- The SDK's own retries are disabled. ADAPTIX applies its own bounded retries, timeouts and failover.
- The model receives field names, types, documentation and the arguments' **names**, never their values. Schema text is marked as untrusted data.
- Every mapping must quote evidence that appears verbatim in the schema. The independent validator re-checks every plan; the LLM cannot approve its own repair.
- The connectivity status stays "configured, not yet verified" until a real request succeeds (`POST /api/health/llm-check`, or a plan call).
- `tests/test_gemini_live.py` makes a real call and runs only when `GEMINI_API_KEY` is set. All other Gemini tests use a **fake SDK client** and are not live-provider tests.

### Credential failover: what it does and does not do

- Failover moves to another configured credential **in the same group**, with a scope that permits the call, and only for the categories listed in that credential's `failover_on` setting (by default: `rate_limited`, `credential_expired`).
- An invalid or revoked credential is marked as such and is never modified, retried with variations, or "repaired". An authentication error is never treated as schema drift.
- It cannot create, refresh or rotate credentials; an operator must supply new ones. Credential state is held in process memory and resets on restart.

## Results (simulated upstreams, measured on 2026-10-16)

Without an LLM key:

"Baseline" makes the same call with repair disabled, so every drifted call fails.

| Suite | Cases | Recovered (repairable) | Safe stop (unrepairable) | Incorrect patches accepted | Median recovery |
|---|---|---|---|---|---|
| Simulation Lab scenarios | 21 | 9 / 9 (100%) | 11 / 11 (100%), plus 1 expected rollback | 0 | 7.1 ms |
| Planned schema changes | 125 | 79 / 85 (92.9%) | 40 / 40 (100%) | 0 | 6.7 ms |
| Adversarial fixtures | 30 | 10 / 13 (76.9%) | 17 / 17 (100%) | 0 | 7.0 ms |

With a **real Gemini key** (`gemini-3.5-flash-lite`, run on 2026-10-16):

- **Simulation Lab scenarios:** 9 / 9 recovered, including the LLM-only booking-window rename, and 10 / 10 stopped safely. 0 incorrect patches; 4 Gemini calls, all successful.
- **Adversarial fixtures:** unchanged at 10 / 13 recovered and 17 / 17 safe stops, with 0 incorrect patches. Gemini proposed mappings for the 3 misses, but the independent validator rejected them: the schema contains no text that grounds the rename, so ADAPTIX stops rather than trusting the model.

The repairable cases ADAPTIX did not recover are **safe misses**: it stopped instead of guessing, and no wrong data was sent. Examples: `uid`, `user_identifier`, `room_ref` and `phone → mobile` with no documentation linking the names, and the scenario that needs an LLM. Recovery times are against in-process simulations. They exclude network latency and LLM latency.

## Tests

Run everything that needs no network or key:

```bash
python -m pytest -q          # 211 passed, 5 skipped (2026-10-16); the 5 skipped are the live tests
```

| Category | Files | What it means |
|---|---|---|
| Unit tests | `test_units.py`, `test_netguard.py`, `test_changes.py` (row/query parts) | pure logic; DNS and HTTP faked where needed |
| Pipeline and simulation tests | `test_pipeline.py`, `test_isolation.py`, `test_gemini.py` | in-process simulated APIs; Gemini via a fake SDK client (a **test double**, labelled "not a real request") |
| Mocked integration tests | `test_api.py`, `test_monitoring.py` | the full API with a fake network layer (`tests/fakes.py`) standing in for an approved HTTPS provider |
| **Controlled real HTTP API tests** | `test_realtest.py` | a real uvicorn server on 127.0.0.1, real TCP requests through the guard; no internet or key needed. Model behaviour comes from the labelled test double |
| **External live tests** (opt-in) | `test_gemini_live.py` (needs `GEMINI_API_KEY`), `test_live_api.py` (needs `ADAPTIX_LIVE_API_TESTS=1`) | real Gemini requests, including the end-to-end demo, and read-only calls to the public Swagger Petstore. All 5 passed on 2026-10-16 |
| Dashboard smoke tests | `test_ui.py` | every page renders; Changes table filters, empty state and clear filters |

```bash
GEMINI_API_KEY=... python -m pytest -q tests/test_gemini_live.py
ADAPTIX_LIVE_API_TESTS=1 python -m pytest -q tests/test_live_api.py
```

## Reproduce the demonstration from a clean state

```bash
git clone https://github.com/Paras131206/self-healing-tool-registry && cd self-healing-tool-registry
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
export GEMINI_API_KEY=...            # your own key, server-side only
.venv/bin/streamlit run app.py
```

Then:

1. **Drift Demo.** Choose *Rename a field*, tick the confirmation and click **Introduce change**. Watch the steps:
   baseline check, change introduced, failing call, diagnosis, schema, drift, proposal, validation, patch, retry and
   verification. It should end "Recovered and verified".
2. Repeat with *Add a required field*. It should end "Stopped safely", because no value can be invented.
3. With `GEMINI_API_KEY` set, choose *Orders API* or *Rename documented only in prose*. Real Gemini proposes the repair.
4. **Schema Drift Explorer → REAL API TEST.** Pick a repaired tool. It shows "No schema drift detected", because the
   registered schema is now version 2.
5. **Evidence & Scorecard → REAL API TEST.** Check the counts, open an incident, and export JSON or CSV.
6. **Drift Demo → Reset** (with confirmation) to return to a known-good state.

## Limitations

- The evaluation uses simulated APIs and team-written cases. Independently sourced, real API changes are still needed.
- Real Gemini calls were verified on 2026-10-16. Google can return `503 high demand` for a model; ADAPTIX retries a bounded number of times and otherwise stops safely.
- Name similarity and documentation cannot prove two fields mean the same thing. When the evidence is not explicit, ADAPTIX stops.
- SQLite suits one host. Multiple instances need a shared database and a distributed lock. Credential state is in memory.
- The REAL_API_TEST service runs inside the ADAPTIX process on loopback, over http (TLS is used for every remote provider). It demonstrates a real HTTP recovery we control. It is not an independent third-party API.
- On Streamlit Cloud the SQLite files are ephemeral, so records (including monitoring history) reset when the app restarts, and the scheduler only runs while the app is awake.
- A monitoring probe verifies a read operation by checking the response against the provider's own schema. That proves the shape of the result, not its business meaning.
- Drift detection relies on the provider's published schema being accurate; an undocumented change is only caught when it breaks a real call.
- Not yet covered: auth-scheme migrations, `oneOf` discriminators, non-JSON payloads and pagination changes.
