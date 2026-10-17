# ADAPTIX Local Demo

A repeatable demonstration of self-healing schema drift that runs entirely on your own machine:

| Component | What it is | Label in the UI |
|---|---|---|
| Demo database | SQLite file `data/local-demo.db` with fictional customers, products, orders and order items | LOCAL REAL EXECUTION |
| Customer API | FastAPI service on `127.0.0.1:8766`; its OpenAPI contract is generated from the live database schema | LOCAL REAL EXECUTION |
| Local LLM | a model served by [Ollama](https://ollama.com) on `127.0.0.1:11434` | Local LLM · `<model name>` |
| ADAPTIX | the same engine, validator, versioned patches, audit and rollback used everywhere else | PROPOSED / VALIDATED / APPLIED / VERIFIED |

The local demo is separate from the other modes:

* **REAL API monitoring** is the production pipeline for real HTTPS providers. It is unchanged, and Gemini remains its repair model.
* **REAL API TEST** and **SIMULATION** are the existing demo modes.
* **LOCAL DEMO** has its own records (`data/adaptix-local.db`) and only runs when `ADAPTIX_LOCAL_DEMO=1` is set in your terminal. It never appears on the deployed website, and it never calls Gemini or any cloud model. If the local model is unavailable, the run stops with a clear message instead of switching providers.

## Prerequisites

* macOS 13+ (Apple Silicon recommended), Linux or Windows (WSL), Python 3.11+.
* About 3 GB free disk for the default model, and 8 GB RAM or more.
* Ollama, installed from the official site: <https://ollama.com/download>. On macOS, download it, open it and move it to Applications. The setup script never installs applications for you.

## Model choice

| Model (Ollama tag) | Download | When to use |
|---|---|---|
| `qwen2.5:3b-instruct` (**default**) | 1.9 GB | 8–16 GB RAM, e.g. an M-series MacBook. Good structured JSON at low latency |
| `qwen2.5:1.5b-instruct` | 986 MB | older or 8 GB machines. Faster, but more proposals are rejected by validation |
| `qwen2.5:7b-instruct` | 4.7 GB | 16 GB+ RAM. Better mapping quality, slower |

The sizes come from the official tag list, <https://ollama.com/library/qwen2.5/tags>, checked on 17 Oct 2026. Gemini 3.5 Flash-Lite is the comparison point, but no local model is claimed to match it. Compare them with your own recorded runs on the Product Review page. Choose a model with `LOCAL_LLM_MODEL=<tag>`. Cloud tags (`*-cloud`) are refused.

## Setup (once)

```bash
cd selfheal
scripts/setup_local.sh
```

This installs Python dependencies into `.venv` and creates and seeds `data/local-demo.db`. It never overwrites an existing database.

```bash
scripts/setup_ollama.sh
```

This script:

* checks the OS, memory, the Ollama install and the Ollama server;
* if the model is missing, shows its size and your free disk space, and asks before running `ollama pull`;
* never downloads twice;
* sends one tiny real inference request to confirm the model works.

## Run

```bash
scripts/run_local_demo.sh
```

This script:

* checks ports 8766 (customer API) and 8501 (UI) and refuses to start if either is busy;
* starts the customer API, then starts the dashboard with the Local Demo Lab enabled at <http://localhost:8501/local-demo>;
* stops both on Ctrl+C.

To run the pieces separately, use two terminals:

```bash
.venv/bin/python -m adaptix.localdemo service
```

```bash
ADAPTIX_LOCAL_DEMO=1 .venv/bin/streamlit run app.py
```

Readiness check for the database, local API and model (add `--inference` to also send one real request):

```bash
.venv/bin/python -m adaptix.localdemo check --inference
```

Other ports: `LOCAL_SERVICE_PORT=8770 STREAMLIT_PORT=8502 scripts/run_local_demo.sh`. When starting the pieces separately, set `ADAPTIX_LOCAL_SERVICE_URL=http://127.0.0.1:8770` and run `service --port 8770`.

## Two-minute demo script

1. Open **Local Demo Lab**. Readiness shows three green boxes: the database (40 customers, 120 orders), the local customer API, and the local LLM with its real model name.
2. **Current contract**: the registered and live contracts match, and both use `customer_id`.
3. Choose **Rename a column** with new name `client_id`. The preview shows `ALTER TABLE customers RENAME COLUMN customer_id TO client_id` and the expected result, VERIFIED.
4. Tick the confirmation, then click **Introduce Drift & Run Recovery**. The timeline shows these steps live:
   * Baseline request: customer 101, Asha Verma, verified.
   * Backup.
   * Schema mutation, which changes the real database.
   * The request fails with HTTP 422.
   * Drift detection: `customer_id` was renamed to `client_id`.
   * Model proposal from the local model.
   * Independent validation.
   * Patch application: tool v1 becomes v2.
   * Retry sends `{"client_id": 101}`.
   * Business verification: identity, name and email are checked.
5. The result shows **VERIFIED**, with the status chips, the model name, its inference time, and the business checks.
6. Scroll to **How the error was removed**. It shows, in order:
   * the real HTTP 422 and its error body;
   * Qwen's structured proposal: the mapping, the quoted evidence, tokens and inference time;
   * the validation checks;
   * the stored patch as a diff (`- body:customer_id <- customer_id`, `+ body:client_id <- customer_id`) and the version table (v1 superseded, v2 active);
   * the same request now returning HTTP 200 with Asha Verma's record.
7. Click **Send the original request again**. The caller still sends `customer_id=101`, and ADAPTIX sends `{"client_id": 101}` through v2 with no new incident and no new model request: the patch stays in effect.
8. Open **Local database (read-only viewer)**. The `customers` table now has a `client_id` column. **Structure** shows the foreign key from `orders` now pointing at `customers.client_id`, and **Migration history** lists the rename with its timestamp. The viewer cannot write, and only lists the demo tables.
9. Open **Reset Demo**, confirm, and the baseline is back. Then show **Add a required field** or **Remove a field**: both end **SAFELY BLOCKED**, because ADAPTIX does not invent values or deleted data.

## Scenarios

| Scenario | Real database change | Expected |
|---|---|---|
| Rename a column | `RENAME COLUMN customer_id TO <new>` (documented through `x-renamed-from`) | VERIFIED |
| Compatible type change | `loyalty_points` INTEGER → REAL (table rebuild in one transaction) | VERIFIED (no repair needed) |
| Add a required field | new NOT NULL column added to the lookup key | SAFELY BLOCKED |
| Remove a field | `DROP COLUMN email` | SAFELY BLOCKED (deleted data is not recovered) |

Each run starts from the baseline. A second change without a reset is refused.

## Safety

* The model only proposes field mappings, as strict JSON checked against a schema. It never produces SQL or code that gets run.
* Every proposal goes through the independent validator, which checks:
  * the evidence is quoted from the live contract;
  * the target fields exist;
  * the source is a value the caller actually sent;
  * the mapping is one-to-one;
  * types match and the conversion is lossless;
  * the model's confidence is above the minimum.
* Migrations are fixed code with allowlisted, PRAGMA-validated identifiers and bound parameters. Each one runs in a transaction with a foreign-key check, and is preceded by a backup.
* The adapter refuses any database file that lacks the local-demo marker, and refuses to run if the demo database is the same file as any ADAPTIX records database.
* The customer API and Ollama must be on loopback (127.0.0.1), and only the configured API address is exempt from the egress guard. Proxies are ignored.
* Logs and evidence are redacted. No credentials are used.

## Reset and backups

**Reset Demo** in the lab, or `python -m adaptix.localdemo db reset --yes` for the database alone, deletes:

* `data/local-demo.db` (after copying it to `data/local-demo-backups/`, which keeps the 10 newest copies);
* in the lab, all LOCAL DEMO records in `data/adaptix-local.db`: tool versions, incidents, evidence and audit events.

It then re-creates the same seed data and re-registers the tool. Production, REAL API TEST and Simulation records are never touched. To make a manual backup:

```bash
.venv/bin/python -m adaptix.localdemo db backup
```

All of these files are in `.gitignore`, together with model files (`*.gguf`). Ollama stores its models in `~/.ollama`, outside the repository.

## Troubleshooting

| Message | Fix |
|---|---|
| Ollama is not running | open the Ollama app, or run `ollama serve` |
| model … is not installed | `ollama pull qwen2.5:3b-instruct`, or run `scripts/setup_ollama.sh` |
| timeout | the first request loads the model, so try again. On slower machines use `LOCAL_LLM_MODEL=qwen2.5:1.5b-instruct` or raise `LOCAL_LLM_TIMEOUT_SECONDS` |
| port … is already in use | stop the other process (`lsof -nP -iTCP:<port> -sTCP:LISTEN`) or choose other ports (see above) |
| Local customer API not reachable | start `python -m adaptix.localdemo service` |
| demo database not initialised / damaged | `python -m adaptix.localdemo db init`, or `db reset --yes` |
| SAFELY BLOCKED on a rename | small models sometimes produce mappings that validation rejects. That is a safe outcome. Try again or use a larger model |

The deployed Streamlit site cannot reach services on your laptop. The Product Review page links here instead of pretending otherwise.
