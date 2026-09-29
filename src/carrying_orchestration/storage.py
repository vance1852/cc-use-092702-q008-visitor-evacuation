"""承载编排服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS carrying_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','ranger','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    source_revision TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(resource_kind, resource_id, scope_key, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_capacity_versions_resource
ON capacity_versions(resource_kind, resource_id, scope_key, version_id);

CREATE TABLE IF NOT EXISTS closure_windows (
    window_id TEXT PRIMARY KEY,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity_percent INTEGER NOT NULL CHECK(capacity_percent BETWEEN 0 AND 100),
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_closure_windows_resource_time
ON closure_windows(resource_kind, resource_id, scope_key, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS weather_alerts (
    alert_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK(level IN ('blue','yellow','orange','red')),
    title TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','lifted')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_alert_refs (
    alert_id TEXT NOT NULL REFERENCES weather_alerts(alert_id),
    resource_ref TEXT NOT NULL,
    PRIMARY KEY(alert_id, resource_ref)
);

CREATE TABLE IF NOT EXISTS carrying_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision INTEGER NOT NULL UNIQUE,
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    visitor_name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    party_size INTEGER NOT NULL CHECK(party_size > 0),
    enters_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK(state IN ('reserved','confirmed','checked_in','evacuating','evacuated','released','rescheduled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_state_time
ON reservations(state, enters_at);

CREATE TABLE IF NOT EXISTS reservation_requirements (
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    PRIMARY KEY(reservation_id, resource_kind, resource_id, scope_key)
);

CREATE TABLE IF NOT EXISTS assistance_needs (
    need_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    assistance_kind TEXT NOT NULL,
    headcount INTEGER NOT NULL CHECK(headcount > 0),
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'requested' CHECK(state IN ('requested','arranged','completed')),
    UNIQUE(reservation_id, assistance_kind)
);

CREATE TABLE IF NOT EXISTS capacity_holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    snapshot_revision INTEGER NOT NULL REFERENCES carrying_snapshots(revision),
    version_id INTEGER NOT NULL REFERENCES capacity_versions(version_id),
    occupied_at TEXT NOT NULL,
    released_at TEXT,
    release_receipt_id INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_capacity_holds_active
ON capacity_holds(reservation_id, resource_kind, resource_id, scope_key)
WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_capacity_holds_resource
ON capacity_holds(resource_kind, resource_id, scope_key, released_at);

CREATE TABLE IF NOT EXISTS adjustment_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_revision INTEGER NOT NULL REFERENCES carrying_snapshots(revision),
    manifest_sha256 TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','confirmed','superseded')),
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(manifest_sha256, plan_sha256)
);

CREATE TABLE IF NOT EXISTS plan_decisions (
    plan_id INTEGER NOT NULL REFERENCES adjustment_plans(plan_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    action TEXT NOT NULL CHECK(action IN ('retain','retain_in_park','reschedule','release_quota','evacuate')),
    proposed_enters_at TEXT,
    decision_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, reservation_id)
);

CREATE TABLE IF NOT EXISTS evacuation_batches (
    batch_id TEXT PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES adjustment_plans(plan_id),
    sequence_no INTEGER NOT NULL,
    sector_key TEXT NOT NULL,
    assembly_point TEXT NOT NULL,
    alert_ids_json TEXT NOT NULL,
    headcount INTEGER NOT NULL,
    assistance_headcount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_progress','completed')),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS evacuation_batch_members (
    batch_id TEXT NOT NULL REFERENCES evacuation_batches(batch_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    PRIMARY KEY(batch_id, reservation_id)
);

CREATE TABLE IF NOT EXISTS evacuation_checklist (
    ack_key TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES evacuation_batches(batch_id),
    step_code TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','acked')),
    acked_by TEXT REFERENCES carrying_users(user_id),
    acked_at TEXT,
    receipt_id TEXT,
    UNIQUE(batch_id, step_code)
);

CREATE TABLE IF NOT EXISTS evacuation_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES evacuation_batches(batch_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    completed_steps_json TEXT NOT NULL,
    released_reservation_ids_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES carrying_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carrying_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS carrying_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_carrying_audit_entity
ON carrying_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
