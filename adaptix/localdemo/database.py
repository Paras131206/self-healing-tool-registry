"""LOCAL DEMO database adapter: a disposable SQLite file of fictional business data.

Tables: customers, products, orders, order_items (primary keys, foreign keys, CHECK constraints), plus
column_docs (published as API documentation), schema_migrations (every schema change) and demo_meta (a marker
that identifies the file as the local demo database; mutations refuse any file without it).

Safety:
* identifiers are never taken from user input or model output as SQL: every table/column name is checked against
  the live schema (PRAGMA) and a strict name pattern, then quoted; values are always bound parameters
* every migration runs in one transaction, is recorded in schema_migrations, and is preceded by a backup
* the seed is deterministic: the same rows every time (fixture: customer_id 101 = Asha Verma, order 5001)
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

MARKER = ("purpose", "adaptix-local-demo")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TABLE = "customers"
LOOKUP_INDEX = "customers_lookup"
FIXTURE = {"customer_id": 101, "full_name": "Asha Verma", "email": "asha.verma@example.com", "city": "Pune",
           "tier": "gold", "loyalty_points": 1250, "created_on": "2025-02-14"}
FIXTURE_ORDER = 5001
SEED = 20261017
N_CUSTOMERS, N_PRODUCTS, N_ORDERS = 40, 12, 120

SCHEMA = """
CREATE TABLE demo_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL CHECK (length(full_name) BETWEEN 2 AND 80),
    email TEXT NOT NULL,
    city TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum')),
    loyalty_points INTEGER NOT NULL DEFAULT 0 CHECK (loyalty_points >= 0),
    created_on TEXT NOT NULL
);
CREATE UNIQUE INDEX customers_lookup ON customers (customer_id);
CREATE UNIQUE INDEX customers_email ON customers (email);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0)
);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers (customer_id) ON DELETE RESTRICT,
    order_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('placed', 'shipped', 'delivered', 'cancelled'))
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products (product_id) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0)
);
CREATE TABLE column_docs (
    table_name TEXT NOT NULL, column_name TEXT NOT NULL, description TEXT NOT NULL, renamed_from TEXT,
    PRIMARY KEY (table_name, column_name)
);
CREATE TABLE schema_migrations (
    id INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
);
"""
DOCS = {"customer_id": "Unique identifier of the customer.", "full_name": "Customer's full name.",
        "email": "Customer's contact email address.", "city": "City of the customer's billing address.",
        "tier": "Loyalty tier: standard, gold or platinum.", "loyalty_points": "Loyalty points balance.",
        "created_on": "Date the customer account was created (YYYY-MM-DD)."}
FIRST = ["Rohan", "Meera", "Kabir", "Isha", "Arjun", "Diya", "Vikram", "Ananya", "Nikhil", "Sara", "Aditya", "Leela",
         "Farhan", "Tara", "Dev", "Nisha", "Omar", "Priya", "Yash", "Zoya"]
LAST = ["Kulkarni", "Iyer", "Mehta", "Rao", "Shah", "Das", "Nair", "Joshi", "Bose", "Khan", "Pillai", "Ghosh"]
CITIES = ["Pune", "Mumbai", "Nagpur", "Nashik", "Bengaluru", "Chennai", "Kolkata", "Delhi"]
PRODUCTS = ["Desk Lamp", "Notebook Set", "Water Bottle", "Backpack", "Headphones", "Keyboard", "Mouse Pad",
            "Coffee Mug", "Phone Stand", "Cable Organizer", "Planner 2027", "Travel Pouch"]


class DemoDatabaseError(RuntimeError):
    pass


def _ident(name: str, allowed) -> str:
    """A safe, quoted identifier: must match the pattern AND exist in the live schema (allowed)."""
    if not isinstance(name, str) or not NAME.match(name) or name not in allowed:
        raise DemoDatabaseError(f"{name!r} is not an allowed column")
    return f'"{name}"'


def new_name_ok(name: str) -> bool:
    return isinstance(name, str) and bool(NAME.match(name))


class DemoDatabase:
    def __init__(self, path: str | Path, backup_dir: str | Path | None = None, keep_backups: int = 10):
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / "local-demo-backups"
        self.keep_backups = keep_backups

    # ---------------------------------------------------------------- connections
    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    @contextmanager
    def _read(self):
        if not self.exists():
            raise DemoDatabaseError("the local demo database is not initialised (run: python -m "
                                    "adaptix.localdemo db init)")
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _tx(self, foreign_keys: bool = True):
        self.require_marker()
        c = self._connect()
        if not foreign_keys:
            c.execute("PRAGMA foreign_keys = OFF")            # table rebuild; checked before commit
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            bad = c.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise DemoDatabaseError(f"foreign key check failed ({len(bad)} rows); nothing was changed")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def exists(self) -> bool:
        return self.path.is_file()

    def is_demo(self) -> bool:
        if not self.exists():
            return False
        try:
            with self._read() as c:
                r = c.execute("SELECT value FROM demo_meta WHERE key=?", (MARKER[0],)).fetchone()
            return bool(r) and r["value"] == MARKER[1]
        except sqlite3.Error:
            return False

    def require_marker(self):
        if not self.is_demo():
            raise DemoDatabaseError(f"{self.path.name} is not the ADAPTIX local demo database; refusing to change it")

    # ---------------------------------------------------------------- create / seed / reset / backup
    def init(self) -> dict:
        """Create and seed the database if it does not exist. Never touches an existing file."""
        if self.exists():
            self.require_marker()
            return {"created": False, "detail": "already initialised; nothing changed"}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = self._connect()
        try:
            c.executescript("BEGIN;" + SCHEMA + "COMMIT;")
            c.execute("BEGIN")
            self._seed(c)
            c.execute("COMMIT")
        except Exception:
            c.close()
            self.path.unlink(missing_ok=True)
            raise
        c.close()
        return {"created": True, "detail": "created and seeded", "counts": self.counts()}

    def _seed(self, c: sqlite3.Connection):
        rnd = random.Random(SEED)
        c.execute("INSERT INTO demo_meta VALUES (?, ?)", MARKER)
        c.execute("INSERT INTO demo_meta VALUES ('seed', ?)", (str(SEED),))
        c.executemany("INSERT INTO column_docs (table_name, column_name, description) VALUES ('customers', ?, ?)",
                      list(DOCS.items()))
        rows = [tuple(FIXTURE.values())]
        used = {FIXTURE["email"]}
        for cid in range(102, 101 + N_CUSTOMERS):
            first, last = rnd.choice(FIRST), rnd.choice(LAST)
            email = f"{first}.{last}{cid}@example.com".lower()
            used.add(email)
            created = date(2024, 1, 1) + timedelta(days=rnd.randrange(0, 600))
            rows.append((cid, f"{first} {last}", email, rnd.choice(CITIES), rnd.choice(["standard"] * 3 + ["gold", "platinum"]),
                         rnd.randrange(0, 5000, 10), created.isoformat()))
        c.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?)", rows)
        prods = [(i + 1, f"SKU-{1000 + i}", n, rnd.randrange(199, 9999)) for i, n in enumerate(PRODUCTS[:N_PRODUCTS])]
        c.executemany("INSERT INTO products VALUES (?,?,?,?)", prods)
        orders, items, item_id = [], [], 1
        for k in range(N_ORDERS):
            oid = FIXTURE_ORDER + k
            cid = 101 if k in (0, 1, 2) else rnd.randrange(101, 101 + N_CUSTOMERS)
            d = date(2025, 3, 1) + timedelta(days=rnd.randrange(0, 400))
            orders.append((oid, cid, d.isoformat(), "delivered" if k == 0 else rnd.choice(
                ["placed", "shipped", "delivered", "delivered", "cancelled"])))
            for _ in range(rnd.randint(1, 4)):
                p = rnd.choice(prods)
                items.append((item_id, oid, p[0], rnd.randint(1, 3), p[3]))
                item_id += 1
        c.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
        c.executemany("INSERT INTO order_items VALUES (?,?,?,?,?)", items)
        c.execute("INSERT INTO schema_migrations (applied_at, kind, detail) VALUES (?, 'seed', ?)",
                  (_now(), json.dumps({"seed": SEED})))

    def backup(self) -> Path | None:
        """Copy the current database (SQLite online backup). Keeps the newest `keep_backups` copies."""
        if not self.exists():
            return None
        self.require_marker()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"local-demo-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.db"
        src, dst = self._connect(), sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        for old in sorted(self.backup_dir.glob("local-demo-*.db"))[:-self.keep_backups]:
            old.unlink(missing_ok=True)
        return dest

    def reset(self) -> dict:
        """Back up, then delete and re-create the demo database with the deterministic seed."""
        backup = None
        if self.exists():
            self.require_marker()
            backup = self.backup()
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
        res = self.init()
        return {"reset": True, "backup": backup.name if backup else None, "counts": res.get("counts")}

    # ---------------------------------------------------------------- introspection
    def columns(self) -> list[dict]:
        with self._read() as c:
            cols = [dict(r) for r in c.execute(f"PRAGMA table_info({TABLE})")]
            docs = {r["column_name"]: dict(r) for r in c.execute(
                "SELECT column_name, description, renamed_from FROM column_docs WHERE table_name=?", (TABLE,))}
        return [{"name": r["name"], "type": (r["type"] or "TEXT").upper(), "not_null": bool(r["notnull"]),
                 "primary_key": bool(r["pk"]), "description": (docs.get(r["name"]) or {}).get("description", ""),
                 "renamed_from": (docs.get(r["name"]) or {}).get("renamed_from")} for r in cols]

    def lookup_columns(self) -> list[str]:
        with self._read() as c:
            return [r["name"] for r in c.execute(f"PRAGMA index_info({LOOKUP_INDEX})")]

    def key_column(self) -> str:
        """The current name of the primary key column (customer_id at baseline)."""
        return next(c["name"] for c in self.columns() if c["primary_key"])

    def migrations(self) -> list[dict]:
        with self._read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM schema_migrations ORDER BY id")]

    def counts(self) -> dict:
        with self._read() as c:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("customers", "products", "orders", "order_items")}

    def integrity(self) -> dict:
        with self._read() as c:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            fk = c.execute("PRAGMA foreign_key_check").fetchall()
        return {"integrity_ok": ok, "foreign_key_violations": len(fk), "marker": self.is_demo()}

    # ---------------------------------------------------------------- read-only browsing (Local Demo Lab viewer)
    BROWSABLE = ("customers", "orders", "order_items", "products", "column_docs", "schema_migrations")

    def _browsable(self, table: str) -> str:
        if table not in self.BROWSABLE:
            raise DemoDatabaseError(f"{table!r} is not a browsable table")
        return table

    def table_info(self, table: str) -> dict:
        """Columns, keys, foreign keys, indexes and row count of one allowlisted table."""
        t = self._browsable(table)
        with self._read() as c:
            cols = [{"column": r["name"], "type": r["type"], "not null": bool(r["notnull"]),
                     "primary key": bool(r["pk"]), "default": r["dflt_value"]}
                    for r in c.execute(f'PRAGMA table_info("{t}")')]
            fks = [{"column": r["from"], "references": f'{r["table"]}.{r["to"]}', "on delete": r["on_delete"]}
                   for r in c.execute(f'PRAGMA foreign_key_list("{t}")')]
            idx = [{"index": r["name"], "unique": bool(r["unique"]),
                    "columns": ", ".join(x["name"] for x in c.execute(f'PRAGMA index_info("{r["name"]}")'))}
                   for r in c.execute(f'PRAGMA index_list("{t}")')]
            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        return {"table": t, "columns": cols, "foreign_keys": fks, "indexes": idx, "rows": n}

    def browse(self, table: str, limit: int = 25, offset: int = 0, customer: int | None = None) -> dict:
        """Read rows of one allowlisted table (newest schema), optionally only those of one customer. Read-only:
        a query_only connection, bound parameters, identifiers checked against the live schema."""
        t = self._browsable(table)
        limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
        with self._read() as c:
            c.execute("PRAGMA query_only = ON")
            cols = [r["name"] for r in c.execute(f'PRAGMA table_info("{t}")')]
            where, args = "", []
            if customer is not None:
                if t == "customers":           # the key column's CURRENT name (customer_id, or client_id after drift)
                    key = next(r["name"] for r in c.execute(f'PRAGMA table_info("{t}")') if r["pk"])
                    where, args = f' WHERE "{key}" = ?', [customer]
                elif t == "orders":
                    where, args = ' WHERE "customer_id" = ?', [customer]
                elif t == "order_items":
                    where, args = ' WHERE "order_id" IN (SELECT order_id FROM orders WHERE customer_id = ?)', [customer]
            order = f'"{cols[0]}"' if cols else "rowid"
            rows = [dict(r) for r in c.execute(f'SELECT * FROM "{t}"{where} ORDER BY {order} LIMIT ? OFFSET ?',
                                               args + [limit, offset])]
            total = c.execute(f'SELECT COUNT(*) FROM "{t}"{where}', args).fetchone()[0]
        return {"table": t, "columns": cols, "rows": rows, "total": total, "limit": limit, "offset": offset}

    # ---------------------------------------------------------------- the business query
    def lookup(self, criteria: dict) -> dict | None:
        """SELECT one customer by the live lookup columns. Names are validated against PRAGMA; values are bound."""
        allowed = set(self.lookup_columns())
        if set(criteria) != allowed:
            raise DemoDatabaseError("lookup criteria must match the lookup columns exactly")
        where = " AND ".join(f"{_ident(k, allowed)} = ?" for k in sorted(criteria))
        with self._read() as c:
            r = c.execute(f'SELECT * FROM "{TABLE}" WHERE {where}', [criteria[k] for k in sorted(criteria)]).fetchone()
        return dict(r) if r else None

    def orders_for(self, key_value) -> int:
        with self._read() as c:
            return c.execute("SELECT COUNT(*) FROM orders WHERE customer_id = ?", (key_value,)).fetchone()[0]

    # ---------------------------------------------------------------- migrations (the manual drift scenarios)
    def _record(self, c, kind: str, detail: dict):
        c.execute("INSERT INTO schema_migrations (applied_at, kind, detail) VALUES (?,?,?)",
                  (_now(), kind, json.dumps(detail)))

    def rename_column(self, old: str, new: str) -> dict:
        self.require_marker()
        cols = [c["name"] for c in self.columns()]
        old_q = _ident(old, cols)
        if not new_name_ok(new) or new in cols:
            raise DemoDatabaseError(f"{new!r} is not a valid new column name (lowercase letters, digits, _; unused)")
        with self._tx() as c:
            c.execute(f'ALTER TABLE "{TABLE}" RENAME COLUMN {old_q} TO "{new}"')
            c.execute("UPDATE column_docs SET column_name=?, renamed_from=? WHERE table_name=? AND column_name=?",
                      (new, old, TABLE, old))
            self._record(c, "rename_column", {"table": TABLE, "from": old, "to": new})
        return {"table": TABLE, "from": old, "to": new}

    def change_type(self, column: str, new_type: str) -> dict:
        """Compatible numeric widening only (INTEGER -> REAL), by a table rebuild inside one transaction."""
        self.require_marker()
        cols = {c["name"]: c for c in self.columns()}
        _ident(column, cols)
        if cols[column]["primary_key"] or cols[column]["type"] != "INTEGER" or new_type != "REAL":
            raise DemoDatabaseError("only a non-key INTEGER column can be widened to REAL in this demo")
        with self._read() as c:
            ddl = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
            indexes = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
                                               "AND sql IS NOT NULL", (TABLE,))]
        new_ddl, n = re.subn(rf'(\b|"){re.escape(column)}("?)\s+INTEGER\b', rf'\1{column}\2 REAL', ddl, count=1)
        if n != 1:
            raise DemoDatabaseError("could not locate the column definition; nothing was changed")
        new_ddl = new_ddl.replace(f"CREATE TABLE {TABLE}", f"CREATE TABLE {TABLE}_rebuild", 1).replace(
            f'CREATE TABLE "{TABLE}"', f'CREATE TABLE "{TABLE}_rebuild"', 1)
        names = ", ".join(f'"{k}"' for k in cols)
        with self._tx(foreign_keys=False) as c:
            c.execute(new_ddl)
            c.execute(f'INSERT INTO "{TABLE}_rebuild" ({names}) SELECT {names} FROM "{TABLE}"')
            c.execute(f'DROP TABLE "{TABLE}"')
            c.execute(f'ALTER TABLE "{TABLE}_rebuild" RENAME TO "{TABLE}"')
            for sql in indexes:
                c.execute(sql)
            self._record(c, "change_type", {"table": TABLE, "column": column, "from": "INTEGER", "to": "REAL"})
        return {"table": TABLE, "column": column, "from": "INTEGER", "to": "REAL"}

    def add_optional_column(self, column: str) -> dict:
        """Add a nullable TEXT column (not part of the lookup key): a compatible, additive change."""
        self.require_marker()
        cols = [c["name"] for c in self.columns()]
        if not new_name_ok(column) or column in cols:
            raise DemoDatabaseError(f"{column!r} is not a valid new column name")
        with self._tx() as c:
            c.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{column}" TEXT')
            c.execute("INSERT INTO column_docs (table_name, column_name, description) VALUES (?,?,?)",
                      (TABLE, column, "Optional field added by a schema change."))
            self._record(c, "add_optional_field", {"table": TABLE, "column": column})
        return {"table": TABLE, "column": column, "optional": True}

    def add_required_lookup_field(self, column: str = "region_code") -> dict:
        """Add a NOT NULL column and make it part of the lookup key: the API then requires it on every request."""
        self.require_marker()
        cols = [c["name"] for c in self.columns()]
        if not new_name_ok(column) or column in cols:
            raise DemoDatabaseError(f"{column!r} is not a valid new column name")
        key = self.lookup_columns()
        keys = ", ".join(f'"{k}"' for k in key + [column])
        with self._tx() as c:
            c.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{column}" TEXT NOT NULL DEFAULT \'IN-MH\'')
            c.execute(f'DROP INDEX "{LOOKUP_INDEX}"')
            c.execute(f'CREATE UNIQUE INDEX "{LOOKUP_INDEX}" ON "{TABLE}" ({keys})')
            c.execute("INSERT INTO column_docs (table_name, column_name, description) VALUES (?,?,?)",
                      (TABLE, column, "Sales region code of the customer; now required to look a customer up."))
            self._record(c, "add_required_field", {"table": TABLE, "column": column, "lookup_key": key + [column]})
        return {"table": TABLE, "column": column, "lookup_key": key + [column]}

    def remove_column(self, column: str) -> dict:
        self.require_marker()
        cols = {c["name"]: c for c in self.columns()}
        q = _ident(column, cols)
        if cols[column]["primary_key"] or column in self.lookup_columns():
            raise DemoDatabaseError("the key column cannot be removed in this demo")
        with self._tx() as c:
            for (idx,) in c.execute("SELECT name FROM pragma_index_list(?) WHERE origin='c'", (TABLE,)).fetchall():
                if column in [r["name"] for r in c.execute("SELECT name FROM pragma_index_info(?)", (idx,))]:
                    c.execute(f'DROP INDEX "{idx}"')
            c.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN {q}')
            c.execute("DELETE FROM column_docs WHERE table_name=? AND column_name=?", (TABLE, column))
            self._record(c, "remove_column", {"table": TABLE, "column": column})
        return {"table": TABLE, "column": column}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
