"""游客承载编排服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orch_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','agent','risk','rescue','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_resources (
    domain TEXT NOT NULL CHECK(domain IN ('entry_slot','trail','shuttle','parking')),
    resource_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    direction TEXT,
    name TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(domain, resource_id, slot),
    CHECK(direction IS NULL OR direction IN ('up','down'))
);

CREATE TABLE IF NOT EXISTS closure_windows (
    window_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL CHECK(domain IN ('entry_slot','trail','shuttle','parking')),
    resource_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    capacity_percent INTEGER NOT NULL CHECK(capacity_percent BETWEEN 0 AND 100),
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','cancelled')),
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL,
    CHECK(ends_at > starts_at)
);

CREATE INDEX IF NOT EXISTS idx_closure_resource_time
ON closure_windows(domain, resource_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS weather_alerts (
    alert_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK(level IN ('blue','yellow','orange','red')),
    title TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','cleared')),
    affected_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_sha256 TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacity_snapshot_items (
    snapshot_id INTEGER NOT NULL REFERENCES capacity_snapshots(snapshot_id),
    domain TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    direction TEXT,
    revision INTEGER NOT NULL,
    nominal_capacity INTEGER NOT NULL,
    effective_capacity INTEGER NOT NULL,
    sources_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, domain, resource_id, slot)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    party_size INTEGER NOT NULL CHECK(party_size > 0),
    entry_resource_id TEXT NOT NULL,
    entry_slot TEXT NOT NULL,
    trail_resource_id TEXT,
    trail_direction TEXT,
    shuttle_resource_id TEXT,
    parking_resource_id TEXT,
    assistance_json TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK(state IN (
        'draft','confirmed','checked_in','rescheduled','evacuating','evacuated','cancelled','denied'
    )),
    latest_snapshot_id INTEGER REFERENCES capacity_snapshots(snapshot_id),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations(state, entry_slot);

CREATE TABLE IF NOT EXISTS reservation_resources (
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    domain TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    slot TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units > 0),
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    snapshot_id INTEGER NOT NULL REFERENCES capacity_snapshots(snapshot_id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(reservation_id, domain, resource_id, slot)
);

CREATE INDEX IF NOT EXISTS idx_held_occupancy
ON reservation_resources(domain, resource_id, slot, state);

CREATE TABLE IF NOT EXISTS orchestration_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('booking','weather')),
    snapshot_id INTEGER NOT NULL REFERENCES capacity_snapshots(snapshot_id),
    alert_id TEXT REFERENCES weather_alerts(alert_id),
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','failed','superseded')),
    failure_reason TEXT,
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(kind, snapshot_id, input_sha256)
);

CREATE TABLE IF NOT EXISTS plan_decisions (
    plan_id INTEGER NOT NULL REFERENCES orchestration_plans(plan_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    action TEXT NOT NULL CHECK(action IN ('retain','reschedule','deny','evacuate')),
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    blocked_by TEXT,
    capacity_sources_json TEXT NOT NULL,
    target_capacity_sources_json TEXT NOT NULL DEFAULT '[]',
    target_json TEXT NOT NULL,
    from_revision INTEGER,
    PRIMARY KEY(plan_id, reservation_id)
);

CREATE TABLE IF NOT EXISTS safety_action_ledger (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES orchestration_plans(plan_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','done')),
    opened_at TEXT NOT NULL,
    done_at TEXT,
    done_by TEXT REFERENCES orch_users(user_id),
    UNIQUE(plan_id, reservation_id, code)
);

CREATE INDEX IF NOT EXISTS idx_safety_open ON safety_action_ledger(reservation_id, state);

CREATE TABLE IF NOT EXISTS evacuations (
    evacuation_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL REFERENCES weather_alerts(alert_id),
    snapshot_id INTEGER NOT NULL REFERENCES capacity_snapshots(snapshot_id),
    plan_id INTEGER NOT NULL REFERENCES orchestration_plans(plan_id),
    state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','in_progress','completed','superseded')),
    created_by TEXT NOT NULL REFERENCES orch_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evacuation_batches (
    batch_id TEXT PRIMARY KEY,
    evacuation_id TEXT NOT NULL REFERENCES evacuations(evacuation_id),
    sequence_no INTEGER NOT NULL,
    assembly_point TEXT NOT NULL,
    shuttle_resource_id TEXT,
    shuttle_slot TEXT,
    seats INTEGER NOT NULL CHECK(seats > 0),
    priority_kind TEXT,
    state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','dispatched','arrived','handed_over')),
    dispatched_at TEXT,
    arrived_at TEXT,
    crew_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    UNIQUE(evacuation_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS evacuation_batch_members (
    batch_id TEXT NOT NULL REFERENCES evacuation_batches(batch_id),
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    pax INTEGER NOT NULL CHECK(pax > 0),
    assistance_json TEXT NOT NULL,
    handover_state TEXT NOT NULL DEFAULT 'pending' CHECK(handover_state IN ('pending','handed_over')),
    handed_over_at TEXT,
    receiver TEXT,
    checklist_json TEXT NOT NULL,
    PRIMARY KEY(batch_id, reservation_id)
);

CREATE INDEX IF NOT EXISTS idx_batch_members_reservation
ON evacuation_batch_members(reservation_id, handover_state);

CREATE TABLE IF NOT EXISTS orch_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS orch_audit_events (
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

CREATE INDEX IF NOT EXISTS idx_orch_audit_entity
ON orch_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用同一连接；WAL、busy_timeout 与
    # BEGIN IMMEDIATE 已保证写入串行化，因此允许跨线程复用。
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
