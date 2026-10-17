"""Local demo command line.

  python -m adaptix.localdemo db init            create + seed the demo database (never touches an existing file)
  python -m adaptix.localdemo db reset --yes     back up, delete and re-seed the demo database
  python -m adaptix.localdemo db backup          copy the demo database to data/local-demo-backups/
  python -m adaptix.localdemo db check           integrity, foreign keys, row counts, current schema
  python -m adaptix.localdemo service            run the local customer API (loopback only)
  python -m adaptix.localdemo check [--inference] readiness of database, local API and local model
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

from ..config import get_settings
from .database import DemoDatabase, DemoDatabaseError


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_db(args, s) -> int:
    db = DemoDatabase(s.local_demo_db)
    try:
        if args.action == "init":
            _print(db.init())
        elif args.action == "reset":
            if not args.yes:
                print("Reset deletes the local demo database (a backup is taken first) and re-creates the seed data.\n"
                      "Re-run with --yes to confirm. ADAPTIX's LOCAL_DEMO records are cleared from the Local Demo Lab "
                      "(Reset Demo).")
                return 2
            _print(db.reset())
        elif args.action == "backup":
            b = db.backup()
            print(f"backup written: {b.name}" if b else "no database to back up")
        else:
            if not db.exists():
                print("not initialised: run  python -m adaptix.localdemo db init")
                return 1
            _print({"integrity": db.integrity(), "counts": db.counts(), "lookup_columns": db.lookup_columns(),
                    "columns": [{k: c[k] for k in ("name", "type", "not_null", "primary_key")} for c in db.columns()],
                    "migrations": db.migrations()})
    except DemoDatabaseError as e:
        print(f"error: {e}")
        return 1
    return 0


def cmd_service(args, s) -> int:
    from .service import create_service, port_free
    p = urlsplit(s.local_service_url)
    host, port = p.hostname or "127.0.0.1", args.port or p.port or 8766
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("error: the local demo service binds loopback only (ADAPTIX_LOCAL_SERVICE_URL must be 127.0.0.1)")
        return 1
    if not DemoDatabase(s.local_demo_db).is_demo():
        print("error: demo database missing; run  python -m adaptix.localdemo db init")
        return 1
    if not port_free(host, port):
        print(f"error: port {port} is already in use. Stop the other process (lsof -nP -iTCP:{port} -sTCP:LISTEN) or "
              f"choose another port with --port N and set ADAPTIX_LOCAL_SERVICE_URL=http://127.0.0.1:N for the UI.")
        return 1
    import uvicorn
    print(f"local customer API on http://{host}:{port}  (contract: /contract/openapi.json) - Ctrl+C to stop")
    uvicorn.run(create_service(s.local_demo_db), host=host, port=port, log_level="warning")
    return 0


def cmd_check(args, s) -> int:
    from ..llm.ollama import LocalLLMError, OllamaProvider
    from .lab import LabError, LocalLab
    try:
        llm = OllamaProvider(s.ollama_url, s.local_llm_model, s.local_llm_timeout_s, s.local_llm_max_tokens)
        lab = LocalLab(s, llm=llm)
    except (LocalLLMError, LabError) as e:
        print(f"error: {e}")
        return 1
    st = lab.status()
    ready = st["database"]["status"] == "ready" and st["service"]["status"] == "running" and \
        st["model"]["server"] == "running" and st["model"]["model_installed"]
    out = {"database": {k: st["database"].get(k) for k in ("status", "counts", "key_column", "action")},
           "local_api": st["service"], "local_model": st["model"]}
    if args.inference and st["model"]["model_installed"]:
        r = llm.check()
        out["inference"] = {"ok": r.ok, "error_category": r.error_category, "duration_ms": r.duration_ms,
                            "real": r.real, "model": r.model}
        ready = ready and r.ok
    _print(out)
    print("READY" if ready else "NOT READY - see the 'action' fields above")
    return 0 if ready else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m adaptix.localdemo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("db")
    d.add_argument("action", choices=["init", "reset", "backup", "check"])
    d.add_argument("--yes", action="store_true", help="confirm a destructive reset")
    sv = sub.add_parser("service")
    sv.add_argument("--port", type=int)
    c = sub.add_parser("check")
    c.add_argument("--inference", action="store_true", help="also send one tiny real request to the local model")
    args = ap.parse_args(argv)
    s = get_settings()
    return {"db": cmd_db, "service": cmd_service, "check": cmd_check}[args.cmd](args, s)


if __name__ == "__main__":
    sys.exit(main())
