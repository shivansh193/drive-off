import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
  id TEXT PRIMARY KEY, name TEXT, address TEXT, spaces INTEGER, timezone TEXT, plate_format TEXT,
  rate_sign TEXT, rule_json TEXT, rule_confirmed_at TEXT, demo_offset_min INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS drivers (
  id TEXT PRIMARY KEY, plate TEXT UNIQUE, display_name TEXT, lot_ids TEXT, vault_id TEXT,
  paypal_customer_id TEXT, payer_email TEXT, enrol_order_id TEXT, status TEXT, consent_at TEXT,
  consent_version TEXT, created_at TEXT, access_token TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, lot_id TEXT, driver_id TEXT, plate TEXT, entry_at TEXT, exit_at TEXT,
  entry_event_id TEXT, exit_event_id TEXT, state TEXT, amount_cents INTEGER, quote_json TEXT, rule_json TEXT,
  order_id TEXT, capture_id TEXT, capture_status TEXT, refund_id TEXT, refund_cents INTEGER,
  dispute_json TEXT, flags TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, lot_id TEXT, lane TEXT, at TEXT, source TEXT, plate TEXT, plate_hash TEXT,
  confidence REAL, agree INTEGER, frames INTEGER, decision TEXT, reason TEXT, read_json TEXT,
  evidence TEXT, session_id TEXT, vehicle_color TEXT, review_state TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS anon_presence (
  plate_hash TEXT, lot_id TEXT, entry_at TEXT, exit_at TEXT
);
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY, event_type TEXT, resource_id TEXT, verified INTEGER, verification TEXT,
  received_at TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, ref TEXT, detail TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        return _conn


def reset(path=None):
    """Used by tests and the demo reset button."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
        if path is not None:
            config.DB_PATH = path
        if config.DB_PATH.exists():
            config.DB_PATH.unlink()
        for suffix in ("-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
        conn()


def q(sql: str, *args) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, *args) -> dict | None:
    rows = q(sql, *args)
    return rows[0] if rows else None


def exec_(sql: str, *args) -> None:
    with _lock:
        conn().execute(sql, args)


def insert(table: str, row: dict) -> dict:
    row = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
    cols = ",".join(row)
    with _lock:
        conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    return row


def update(table: str, id_: str, **fields) -> None:
    fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    sets = ",".join(f"{k}=?" for k in fields)
    with _lock:
        conn().execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), id_))


def audit(kind: str, ref: str, detail: dict | str) -> None:
    exec_("INSERT INTO audit (at, kind, ref, detail) VALUES (?,?,?,?)", iso(now()), kind, ref,
          detail if isinstance(detail, str) else json.dumps(detail))


def j(s, default=None):
    if s is None or s == "":
        return default
    return json.loads(s) if isinstance(s, str) else s
