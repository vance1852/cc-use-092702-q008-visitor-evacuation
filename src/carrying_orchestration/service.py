"""统一承载编排事务用例：容量版本快照、带原因调整方案、原子占用与疏散交接。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    AssistanceNeed,
    ClosureWindowInput,
    CapacityVersionInput,
    ReservationInput,
    WeatherAlertInput,
)
from .planning import (
    build_manifest,
    canonical_json,
    digest,
    effective_capacity,
    generate_plan,
    renumber_batches,
    resource_ref,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"capacity.write", "plan.generate", "report.read"},
    "dispatcher": {"reservation.write", "assistance.write", "plan.generate", "plan.confirm", "report.read"},
    "risk": {"closure.write", "alert.write", "plan.generate", "report.read"},
    "ranger": {"checkin.write", "evacuation.ack", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

ACTIVE_RESERVATION_STATES = ("reserved", "confirmed", "checked_in", "evacuating", "rescheduled")
EVACUATION_STEPS = (
    "notify_visitors",
    "headcount_assembly",
    "assistance_ready",
    "transport_dispatch",
    "handover_received",
)


class CarryingOrchestrationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM carrying_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM carrying_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO carrying_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotency_lookup(self, scope: str, key: str, request_sha: str) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM carrying_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_sha:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _idempotency_store(self, scope: str, key: str, request_sha: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO carrying_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_sha, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO carrying_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------ 容量来源

    def register_capacity_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "capacity.write")
        version = CapacityVersionInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO capacity_versions(resource_kind,resource_id,scope_key,capacity,source_revision,"
                    "effective_from,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        version.resource_kind,
                        version.resource_id,
                        version.scope_key,
                        version.capacity,
                        version.source_revision,
                        version.effective_from,
                        version.note,
                        actor_id,
                        self._now(),
                    ),
                )
                version_id = int(cursor.lastrowid)
                self._audit(
                    "capacity_version",
                    str(version_id),
                    "capacity_version.registered",
                    actor_id,
                    {
                        "resource_kind": version.resource_kind,
                        "resource_id": version.resource_id,
                        "scope_key": version.scope_key,
                        "source_revision": version.source_revision,
                        "capacity": version.capacity,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一容量来源修订已经登记") from exc
        return {
            "version_id": version_id,
            "resource_kind": version.resource_kind,
            "resource_id": version.resource_id,
            "scope_key": version.scope_key,
            "capacity": version.capacity,
            "source_revision": version.source_revision,
        }

    def register_closure_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "closure.write")
        window = ClosureWindowInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO closure_windows(window_id,resource_kind,resource_id,scope_key,starts_at,ends_at,"
                    "capacity_percent,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        window.window_id,
                        window.resource_kind,
                        window.resource_id,
                        window.scope_key,
                        window.starts_at,
                        window.ends_at,
                        window.capacity_percent,
                        window.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("closure_window", window.window_id, "closure_window.registered", actor_id, {
                    "capacity_percent": window.capacity_percent,
                    "reason": window.reason,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("临时关闭窗口编号已经存在") from exc
        return {"window_id": window.window_id, "state": "announced"}

    def trigger_weather_alert(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "alert.write")
        alert = WeatherAlertInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO weather_alerts(alert_id,level,title,starts_at,ends_at,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        alert.alert_id,
                        alert.level,
                        alert.title,
                        alert.starts_at,
                        alert.ends_at,
                        alert.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO weather_alert_refs(alert_id,resource_ref) VALUES(?,?)",
                    [(alert.alert_id, ref) for ref in alert.closed_resources],
                )
                self._audit("weather_alert", alert.alert_id, "weather_alert.triggered", actor_id, {
                    "level": alert.level,
                    "closed_resources": list(alert.closed_resources),
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("气象预警编号已经存在") from exc
        return {"alert_id": alert.alert_id, "state": "active", "level": alert.level}

    def lift_weather_alert(self, actor_id: str, alert_id: str) -> dict[str, Any]:
        self._require(actor_id, "alert.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE weather_alerts SET state='lifted',revision=revision+1 WHERE alert_id=? AND state='active'",
                (alert_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("气象预警不存在或已解除")
            self._audit("weather_alert", alert_id, "weather_alert.lifted", actor_id, {})
        return {"alert_id": alert_id, "state": "lifted"}

    # -------------------------------------------------------------- 预约

    def submit_reservation(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        reservation = ReservationInput.from_dict(raw)
        request_sha = digest(raw)
        stored = self._idempotency_lookup("reservation", reservation.idempotency_key, request_sha)
        if stored is not None:
            return stored
        now = self._now()
        response = {
            "reservation_id": reservation.reservation_id,
            "state": "reserved",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,visitor_name,contact,party_size,enters_at,state,"
                    "idempotency_key,submitted_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        reservation.reservation_id,
                        reservation.visitor_name,
                        reservation.contact,
                        reservation.party_size,
                        reservation.enters_at,
                        "reserved",
                        reservation.idempotency_key,
                        actor_id,
                        now,
                        now,
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO reservation_requirements(reservation_id,resource_kind,resource_id,scope_key,quantity) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (
                            reservation.reservation_id,
                            item.resource_kind,
                            item.resource_id,
                            item.scope_key,
                            item.quantity,
                        )
                        for item in reservation.requirements
                    ],
                )
                self.connection.executemany(
                    "INSERT INTO assistance_needs(reservation_id,assistance_kind,headcount,note) VALUES(?,?,?,?)",
                    [
                        (
                            reservation.reservation_id,
                            item.assistance_kind,
                            item.headcount,
                            item.note,
                        )
                        for item in reservation.assistance_needs
                    ],
                ) if reservation.assistance_needs else None
                self._idempotency_store("reservation", reservation.idempotency_key, request_sha, response)
                self._audit("reservation", reservation.reservation_id, "reservation.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("预约编号或幂等键冲突") from exc
        return response

    def check_in(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "checkin.write")
        now = self._now()
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise NotFound("预约不存在")
            if row["state"] not in {"confirmed", "rescheduled"}:
                raise InvalidState("只有已确认（含已改签）的预约可以检票入园")
            self.connection.execute(
                "UPDATE reservations SET state='checked_in',revision=revision+1,updated_at=? WHERE reservation_id=?",
                (now, reservation_id),
            )
            self._audit("reservation", reservation_id, "reservation.checked_in", actor_id, {})
        return {"reservation_id": reservation_id, "state": "checked_in"}

    def arrange_assistance(self, actor_id: str, reservation_id: str, assistance_kind: str) -> dict[str, Any]:
        self._require(actor_id, "assistance.write")
        need = AssistanceNeed.from_dict({"assistance_kind": assistance_kind, "headcount": 1})
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE assistance_needs SET state='arranged' WHERE reservation_id=? AND assistance_kind=? AND state='requested'",
                (reservation_id, need.assistance_kind),
            )
            if cursor.rowcount != 1:
                raise InvalidState("协助需求不存在或已安排")
            self.connection.execute(
                "UPDATE reservations SET updated_at=? WHERE reservation_id=?",
                (self._now(), reservation_id),
            )
            self._audit("reservation", reservation_id, "assistance.arranged", actor_id,
                        {"assistance_kind": need.assistance_kind})
        return {"reservation_id": reservation_id, "assistance_kind": need.assistance_kind, "state": "arranged"}

    # -------------------------------------------------------------- 快照

    def _collect_catalog(self, now: str) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
        versions = self.connection.execute(
            "SELECT v.* FROM capacity_versions v JOIN ("
            "SELECT resource_kind,resource_id,scope_key,MAX(version_id) version_id FROM capacity_versions "
            "WHERE effective_from<=? GROUP BY resource_kind,resource_id,scope_key) m "
            "ON m.version_id=v.version_id ORDER BY v.resource_kind,v.resource_id,v.scope_key",
            (now,),
        ).fetchall()
        windows = self.connection.execute(
            "SELECT * FROM closure_windows WHERE state='announced' ORDER BY window_id"
        ).fetchall()
        alerts = self.connection.execute(
            "SELECT * FROM weather_alerts WHERE state='active' ORDER BY alert_id"
        ).fetchall()
        return versions, windows, alerts

    def _take_snapshot(self, actor_id: str, now: str) -> dict[str, Any]:
        """在当前事务内把容量来源固化为带版本号的快照；相同清单复用同一版本。"""
        version_rows, window_rows, alert_rows = self._collect_catalog(now)
        alert_view = []
        for alert in alert_rows:
            refs = [
                row["resource_ref"]
                for row in self.connection.execute(
                    "SELECT resource_ref FROM weather_alert_refs WHERE alert_id=? ORDER BY resource_ref",
                    (alert["alert_id"],),
                ).fetchall()
            ]
            alert_view.append({
                "alert_id": alert["alert_id"],
                "level": alert["level"],
                "starts_at": alert["starts_at"],
                "ends_at": alert["ends_at"],
                "state": alert["state"],
                "refs": refs,
            })
        manifest = build_manifest(
            [dict(row) for row in version_rows],
            [dict(row) for row in window_rows],
            alert_view,
        )
        manifest_sha = digest(manifest)
        existing = self.connection.execute(
            "SELECT revision FROM carrying_snapshots WHERE manifest_sha256=?", (manifest_sha,)
        ).fetchone()
        if existing is not None:
            return {"revision": int(existing["revision"]), "manifest": manifest, "manifest_sha256": manifest_sha}
        revision_row = self.connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 revision FROM carrying_snapshots"
        ).fetchone()
        revision = int(revision_row["revision"])
        self.connection.execute(
            "INSERT INTO carrying_snapshots(revision,manifest_json,manifest_sha256,created_by,created_at) "
            "VALUES(?,?,?,?,?)",
            (revision, canonical_json(manifest), manifest_sha, actor_id, now),
        )
        self._audit("capacity_snapshot", str(revision), "snapshot.taken", actor_id,
                    {"manifest_sha256": manifest_sha, "resources": len(manifest["versions"])})
        return {"revision": revision, "manifest": manifest, "manifest_sha256": manifest_sha}

    def _reservation_views(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM reservations WHERE state IN (?,?,?,?,?) ORDER BY created_at,reservation_id",
            ACTIVE_RESERVATION_STATES,
        ).fetchall()
        views: list[dict[str, Any]] = []
        for row in rows:
            requirements = [
                {"kind": item["resource_kind"], "rid": item["resource_id"], "scope": item["scope_key"],
                 "quantity": int(item["quantity"])}
                for item in self.connection.execute(
                    "SELECT * FROM reservation_requirements WHERE reservation_id=?",
                    (row["reservation_id"],),
                ).fetchall()
            ]
            assistance = [
                {"assistance_kind": item["assistance_kind"], "headcount": int(item["headcount"]),
                 "arranged": item["state"] != "requested", "state": item["state"]}
                for item in self.connection.execute(
                    "SELECT * FROM assistance_needs WHERE reservation_id=?",
                    (row["reservation_id"],),
                ).fetchall()
            ]
            views.append({
                "reservation_id": row["reservation_id"],
                "visitor_name": row["visitor_name"],
                "party_size": int(row["party_size"]),
                "enters_at": row["enters_at"],
                "state": row["state"],
                "revision": int(row["revision"]),
                "created_at": row["created_at"],
                "requirements": requirements,
                "assistance": assistance,
            })
        return views

    @staticmethod
    def _plan_fingerprint(manifest_sha: str, views: Sequence[Mapping[str, Any]]) -> str:
        normalized = [
            {
                "reservation_id": view["reservation_id"],
                "state": view["state"],
                "revision": view["revision"],
                "enters_at": view["enters_at"],
                "requirements": view["requirements"],
                "assistance": [
                    {"assistance_kind": item["assistance_kind"], "headcount": item["headcount"],
                     "arranged": item["arranged"]}
                    for item in view["assistance"]
                ],
            }
            for view in views
        ]
        return digest({"manifest_sha256": manifest_sha, "reservations": normalized})

    # -------------------------------------------------------------- 方案

    def generate_adjustment_plan(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.generate")
        now = self._now()
        with transaction(self.connection, immediate=True):
            snapshot = self._take_snapshot(actor_id, now)
            views = self._reservation_views()
            plan_sha = self._plan_fingerprint(snapshot["manifest_sha256"], views)
            existing = self.connection.execute(
                "SELECT plan_id,result_json FROM adjustment_plans WHERE manifest_sha256=? AND plan_sha256=?",
                (snapshot["manifest_sha256"], plan_sha),
            ).fetchone()
            if existing is not None:
                result = json.loads(existing["result_json"])
                return {"plan_id": int(existing["plan_id"]), **result, "replayed": True}
            snapshot_view = {
                "snapshot_id": snapshot["revision"],
                "revision": snapshot["revision"],
                "resources": snapshot["manifest"]["versions"],
                "windows": snapshot["manifest"]["windows"],
                "alerts": snapshot["manifest"]["alerts"],
            }
            result = generate_plan(snapshot_view, views, now)
            cursor = self.connection.execute(
                "INSERT INTO adjustment_plans(snapshot_revision,manifest_sha256,plan_sha256,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (snapshot["revision"], snapshot["manifest_sha256"], plan_sha, canonical_json(result), actor_id, now),
            )
            plan_id = int(cursor.lastrowid)
            renumber_batches(result, plan_id)
            self.connection.execute(
                "UPDATE adjustment_plans SET result_json=? WHERE plan_id=?",
                (canonical_json(result), plan_id),
            )
            for decision in result["decisions"]:
                self.connection.execute(
                    "INSERT INTO plan_decisions(plan_id,reservation_id,action,proposed_enters_at,decision_json) "
                    "VALUES(?,?,?,?,?)",
                    (plan_id, decision["reservation_id"], decision["action"],
                     decision["proposed_enters_at"], canonical_json(decision)),
                )
            for batch in result["evacuation_batches"]:
                self.connection.execute(
                    "INSERT INTO evacuation_batches(batch_id,plan_id,sequence_no,sector_key,assembly_point,"
                    "alert_ids_json,headcount,assistance_headcount,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch["batch_id"], plan_id, batch["sequence_no"], batch["sector_key"],
                        batch["assembly_point"], canonical_json(batch["alert_ids"]),
                        batch["headcount"], batch["assistance_headcount"], now,
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO evacuation_batch_members(batch_id,reservation_id) VALUES(?,?)",
                    [(batch["batch_id"], member["reservation_id"]) for member in batch["members"]],
                )
                self.connection.executemany(
                    "INSERT INTO evacuation_checklist(ack_key,batch_id,step_code,content) VALUES(?,?,?,?)",
                    [(step["ack_key"], batch["batch_id"], step["step_code"], step["content"])
                     for step in batch["checklist"]],
                )
            self._audit("adjustment_plan", str(plan_id), "plan.generated", actor_id,
                        {"snapshot_revision": snapshot["revision"], "manifest_sha256": snapshot["manifest_sha256"]})
        return {"plan_id": plan_id, **result, "replayed": False}

    def _load_plan(self, plan_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM adjustment_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调整方案不存在")
        return row

    def confirm_plan(self, actor_id: str, plan_id: int, idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "plan.confirm")
        request_sha = digest({"plan_id": plan_id, "idempotency_key": idempotency_key})
        stored = self._idempotency_lookup("plan_confirm", idempotency_key, request_sha)
        if stored is not None:
            return stored
        now = self._now()
        with transaction(self.connection, immediate=True):
            plan = self._load_plan(plan_id)
            if plan["state"] != "proposed":
                raise InvalidState("方案不是待确认状态，可能已被确认或被更新的容量版本取代")
            result = json.loads(plan["result_json"])

            # 任一容量版本（含关闭窗口、预警状态）发生变化 → 整单失败
            current_snapshot = self._take_snapshot(actor_id, now)
            if current_snapshot["manifest_sha256"] != plan["manifest_sha256"]:
                raise Conflict(
                    f"容量版本已变化（当前快照 v{current_snapshot['revision']} 与方案快照 v{plan['snapshot_revision']} 不一致），整单确认失败"
                )

            decisions = {
                row["reservation_id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM plan_decisions WHERE plan_id=?", (plan_id,)
                ).fetchall()
            }
            reservations = {
                row["reservation_id"]: row
                for row in self.connection.execute(
                    "SELECT * FROM reservations WHERE reservation_id IN (%s)"
                    % ",".join("?" for _ in decisions),
                    tuple(decisions),
                ).fetchall()
            } if decisions else {}

            # 以方案外的在途占用为基线，叠加本方案各决定形成的新占用，重算每个容量来源的总量
            affected = set(decisions)
            baseline: dict[str, int] = {}
            if affected:
                placeholders = ",".join("?" for _ in affected)
                rows = self.connection.execute(
                    f"SELECT resource_kind,resource_id,scope_key,SUM(quantity) total FROM capacity_holds "
                    f"WHERE released_at IS NULL AND reservation_id NOT IN ({placeholders}) "
                    "GROUP BY resource_kind,resource_id,scope_key",
                    tuple(affected),
                ).fetchall()
                baseline = {
                    resource_ref(row["resource_kind"], row["resource_id"], row["scope_key"]): int(row["total"])
                    for row in rows
                }

            version_rows = {
                resource_ref(row["resource_kind"], row["resource_id"], row["scope_key"]): row
                for row in current_snapshot["manifest"]["versions"]
            }

            def capacity_at(kind: str, rid: str, scope: str, at: str) -> int:
                version = version_rows[resource_ref(kind, rid, scope)]
                return int(effective_capacity(
                    int(version["capacity"]),
                    at,
                    windows=current_snapshot["manifest"]["windows"],
                    alerts=current_snapshot["manifest"]["alerts"],
                    resource_kind=kind,
                    resource_id=rid,
                    scope_key=scope,
                )["capacity"])

            version_index = {ref: int(row["version_id"]) for ref, row in version_rows.items()}

            # 受影响预约当前在途的占用（在园游客的占用在疏散完成前必须继续计入容量）
            existing_holds: dict[str, list[dict[str, Any]]] = {rid: [] for rid in affected}
            if affected:
                placeholders = ",".join("?" for _ in affected)
                for row in self.connection.execute(
                    f"SELECT reservation_id,resource_kind,resource_id,scope_key,quantity FROM capacity_holds "
                    f"WHERE released_at IS NULL AND reservation_id IN ({placeholders})",
                    tuple(affected),
                ).fetchall():
                    existing_holds[row["reservation_id"]].append({
                        "resource_kind": row["resource_kind"],
                        "resource_id": row["resource_id"],
                        "scope_key": row["scope_key"],
                        "needed": int(row["quantity"]),
                    })

            needed: dict[str, int] = {}
            limits: dict[str, int] = {}
            new_holds: dict[str, list[Mapping[str, Any]]] = {}
            for decision in result["decisions"]:
                rid = decision["reservation_id"]
                action = decision["action"]
                state = reservations[rid]["state"]
                if action == "retain":
                    if state not in {"reserved", "confirmed"}:
                        raise InvalidState(f"预约 {rid} 状态已变化（{state}），不能按保留确认")
                    holds_for_rid: list[Mapping[str, Any]] = list(decision["capacity_sources"])
                elif action == "retain_in_park":
                    if state not in {"checked_in", "evacuating"}:
                        raise InvalidState(f"预约 {rid} 已不在园内，不能按在园保留确认")
                    holds_for_rid = []
                elif action == "reschedule":
                    if state not in {"reserved", "confirmed"}:
                        raise InvalidState(f"预约 {rid} 已入园或状态已变化（{state}），禁止自动改签")
                    target = decision["proposed_enters_at"]
                    holds_for_rid = [
                        dict(source, scope_key=target) if source["resource_kind"] == "entry-slot" else dict(source)
                        for source in decision["capacity_sources"]
                    ]
                elif action == "release_quota":
                    if state not in {"reserved", "confirmed"}:
                        raise InvalidState(f"预约 {rid} 已入园，不能按释放名额处理")
                    holds_for_rid = []
                elif action == "evacuate":
                    if state not in {"checked_in", "evacuating"}:
                        raise InvalidState(f"预约 {rid} 已不在园内，不能编入疏散")
                    holds_for_rid = []
                else:  # pragma: no cover - 决策动作受纯函数约束
                    raise InvalidState(f"未知方案动作 {action}")
                new_holds[rid] = holds_for_rid
                reservation_row = reservations[rid]
                blocked_refs = {
                    resource_ref(source["resource_kind"], source["resource_id"], source["scope_key"])
                    for source in decision["capacity_sources"]
                    if source["forced_zero"] or int(source["effective_capacity"]) == 0
                }
                counted_sources: Iterable[Mapping[str, Any]]
                if action in {"retain_in_park", "evacuate"}:
                    # 疏散交接完成前在园占用继续计入；但触发疏散的已关闭资源不再按容量上限校验
                    counted_sources = [
                        source for source in existing_holds.get(rid, [])
                        if resource_ref(source["resource_kind"], source["resource_id"], source["scope_key"]) not in blocked_refs
                    ]
                else:
                    counted_sources = holds_for_rid
                for source in counted_sources:
                    ref = resource_ref(source["resource_kind"], source["resource_id"], source["scope_key"])
                    needed[ref] = needed.get(ref, 0) + int(source["needed"])
                    if action == "reschedule":
                        at = target  # 改签后所有关联资源都按目标时段重新校验容量
                    elif action in {"retain_in_park", "evacuate"}:
                        at = now
                    elif source["resource_kind"] == "entry-slot":
                        at = source["scope_key"]
                    else:
                        at = reservation_row["enters_at"]
                    limits[ref] = capacity_at(source["resource_kind"], source["resource_id"],
                                             source["scope_key"], at)

            overflow = [
                {"resource": ref, "effective_capacity": limits[ref],
                 "required": baseline.get(ref, 0) + quantity}
                for ref, quantity in sorted(needed.items())
                if baseline.get(ref, 0) + quantity > limits[ref]
            ]
            if overflow:
                raise Conflict(f"关联容量原子占用校验失败，整单失败：{canonical_json(overflow)}")

            for decision in result["decisions"]:
                rid = decision["reservation_id"]
                action = decision["action"]
                if action in {"retain", "reschedule", "release_quota"}:
                    # 先释放该预约既有的未释放占用，再按方案重建（reserved 无占用时为零行更新）
                    self.connection.execute(
                        "UPDATE capacity_holds SET released_at=? WHERE reservation_id=? AND released_at IS NULL",
                        (now, rid),
                    )
                if action == "retain":
                    self._insert_holds(rid, new_holds[rid], plan["snapshot_revision"], version_index, now)
                    self._update_reservation(rid, state="confirmed", now=now)
                elif action == "reschedule":
                    target = decision["proposed_enters_at"]
                    for move in decision["moves"]:
                        self.connection.execute(
                            "UPDATE reservation_requirements SET scope_key=? WHERE reservation_id=? "
                            "AND resource_kind=? AND resource_id=? AND scope_key=?",
                            (move["to_scope"], rid, move["resource_kind"], move["resource_id"], move["from_scope"]),
                        )
                    self._insert_holds(rid, new_holds[rid], plan["snapshot_revision"], version_index, now)
                    self.connection.execute(
                        "UPDATE reservations SET enters_at=?,state='rescheduled',revision=revision+1,updated_at=? "
                        "WHERE reservation_id=?",
                        (target, now, rid),
                    )
                elif action == "release_quota":
                    self._update_reservation(rid, state="released", now=now)
                elif action == "evacuate":
                    self._update_reservation(rid, state="evacuating", now=now)

            self.connection.execute(
                "UPDATE adjustment_plans SET state='confirmed',confirmed_at=? WHERE plan_id=?",
                (now, plan_id),
            )
            response = {
                "plan_id": plan_id,
                "state": "confirmed",
                "snapshot_revision": plan["snapshot_revision"],
                "confirmed_at": now,
                "actions": [
                    {"reservation_id": decision["reservation_id"], "action": decision["action"],
                     "proposed_enters_at": decision["proposed_enters_at"]}
                    for decision in result["decisions"]
                ],
                "evacuation_batch_ids": result["evacuation_batches"] and [
                    batch["batch_id"] for batch in result["evacuation_batches"]
                ],
            }
            self._idempotency_store("plan_confirm", idempotency_key, request_sha, response)
            self._audit("adjustment_plan", str(plan_id), "plan.confirmed", actor_id,
                        {"idempotency_key": idempotency_key, "snapshot_revision": plan["snapshot_revision"]})
        return response

    def _insert_holds(
        self,
        reservation_id: str,
        sources: Iterable[Mapping[str, Any]],
        snapshot_revision: int,
        version_index: Mapping[str, int],
        now: str,
    ) -> None:
        for source in sources:
            ref = resource_ref(source["resource_kind"], source["resource_id"], source["scope_key"])
            self.connection.execute(
                "INSERT INTO capacity_holds(reservation_id,resource_kind,resource_id,scope_key,quantity,"
                "snapshot_revision,version_id,occupied_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    reservation_id,
                    source["resource_kind"],
                    source["resource_id"],
                    source["scope_key"],
                    int(source["needed"]),
                    snapshot_revision,
                    version_index[ref],
                    now,
                ),
            )

    def _update_reservation(self, reservation_id: str, *, state: str, now: str) -> None:
        self.connection.execute(
            "UPDATE reservations SET state=?,revision=revision+1,updated_at=? WHERE reservation_id=?",
            (state, now, reservation_id),
        )

    # -------------------------------------------------------------- 查询

    def snapshot(self, actor_id: str, revision: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM carrying_snapshots WHERE revision=?", (revision,)
        ).fetchone()
        if row is None:
            raise NotFound("容量快照不存在")
        return {
            "snapshot_id": row["revision"],
            "revision": row["revision"],
            "manifest_sha256": row["manifest_sha256"],
            "created_at": row["created_at"],
            "manifest": json.loads(row["manifest_json"]),
        }

    def _live_pending_checklist(self, batch_id: str) -> list[dict[str, Any]]:
        return [
            {"code": f"evacuation_checklist:{row['step_code']}", "ack_key": row["ack_key"],
             "message": row["content"]}
            for row in self.connection.execute(
                "SELECT ack_key,step_code,content FROM evacuation_checklist WHERE batch_id=? AND status='pending' "
                "ORDER BY rowid",
                (batch_id,),
            ).fetchall()
        ]

    def plan(self, actor_id: str, plan_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        plan_row = self._load_plan(plan_id)
        result = json.loads(plan_row["result_json"])
        for decision in result["decisions"]:
            pending: list[dict[str, Any]] = []
            for need in self.connection.execute(
                "SELECT assistance_kind,headcount FROM assistance_needs WHERE reservation_id=? AND state='requested'",
                (decision["reservation_id"],),
            ).fetchall():
                pending.append({
                    "code": "assistance_unarranged",
                    "assistance_kind": need["assistance_kind"],
                    "headcount": int(need["headcount"]),
                    "message": f"重点人群协助 {need['assistance_kind']} 尚未安排落实",
                })
            if decision["action"] == "evacuate":
                pending.extend(self._live_pending_checklist(decision["evacuation_batch_id"]))
            decision["unresolved_safety_actions"] = pending
        return {
            "plan_id": plan_id,
            "state": plan_row["state"],
            "snapshot_revision": plan_row["snapshot_revision"],
            "manifest_sha256": plan_row["manifest_sha256"],
            "created_at": plan_row["created_at"],
            "confirmed_at": plan_row["confirmed_at"],
            **result,
        }

    def evacuation_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        batch = self.connection.execute(
            "SELECT b.*,p.state plan_state FROM evacuation_batches b "
            "JOIN adjustment_plans p ON p.plan_id=b.plan_id WHERE b.batch_id=?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise NotFound("疏散批次不存在")
        members = [
            dict(row) for row in self.connection.execute(
                "SELECT m.reservation_id,r.visitor_name,r.party_size,r.state FROM evacuation_batch_members m "
                "JOIN reservations r ON r.reservation_id=m.reservation_id WHERE m.batch_id=? ORDER BY m.rowid",
                (batch_id,),
            ).fetchall()
        ]
        checklist = [
            {"step_code": row["step_code"], "content": row["content"], "ack_key": row["ack_key"],
             "status": row["status"], "acked_by": row["acked_by"], "acked_at": row["acked_at"],
             "receipt_id": row["receipt_id"]}
            for row in self.connection.execute(
                "SELECT * FROM evacuation_checklist WHERE batch_id=? ORDER BY rowid", (batch_id,)
            ).fetchall()
        ]
        return {
            "batch_id": batch_id,
            "plan_id": int(batch["plan_id"]),
            "plan_state": batch["plan_state"],
            "sequence_no": int(batch["sequence_no"]),
            "sector_key": batch["sector_key"],
            "assembly_point": batch["assembly_point"],
            "alert_ids": json.loads(batch["alert_ids_json"]),
            "headcount": int(batch["headcount"]),
            "assistance_headcount": int(batch["assistance_headcount"]),
            "status": batch["status"],
            "members": members,
            "checklist": checklist,
            "pending_steps": [item["step_code"] for item in checklist if item["status"] == "pending"],
        }

    # -------------------------------------------------------------- 疏散回执

    def acknowledge_evacuation(
        self,
        actor_id: str,
        batch_id: str,
        steps: Sequence[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "evacuation.ack")
        normalized_steps = list(dict.fromkeys(steps))
        invalid = [step for step in normalized_steps if step not in EVACUATION_STEPS]
        if invalid:
            raise ValidationFailed(f"未知交接步骤：{','.join(invalid)}")
        if not normalized_steps:
            raise ValidationFailed("steps 至少包含一个交接步骤")
        request_sha = digest({"batch_id": batch_id, "steps": normalized_steps})
        stored = self._idempotency_lookup("evacuation_receipt", idempotency_key, request_sha)
        if stored is not None:
            return stored

        now = self._now()
        with transaction(self.connection, immediate=True):
            batch = self.connection.execute(
                "SELECT b.*,p.state plan_state FROM evacuation_batches b "
                "JOIN adjustment_plans p ON p.plan_id=b.plan_id WHERE b.batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise NotFound("疏散批次不存在")
            if batch["plan_state"] != "confirmed":
                raise InvalidState("批次所属承载调整方案尚未确认，不能执行交接")

            known = {
                row["step_code"]: row
                for row in self.connection.execute(
                    "SELECT * FROM evacuation_checklist WHERE batch_id=?", (batch_id,)
                ).fetchall()
            }
            missing = [step for step in normalized_steps if step not in known]
            if missing:
                raise ValidationFailed(f"交接步骤不属于该批次：{','.join(missing)}")
            newly_acked = [step for step in normalized_steps if known[step]["status"] == "pending"]

            cursor = self.connection.execute(
                "INSERT INTO evacuation_receipts(batch_id,idempotency_key,request_sha256,completed_steps_json,"
                "released_reservation_ids_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    idempotency_key,
                    request_sha,
                    canonical_json(normalized_steps),
                    canonical_json([]),
                    "in_progress",
                    actor_id,
                    now,
                ),
            )
            receipt_id = int(cursor.lastrowid)
            if newly_acked:
                self.connection.executemany(
                    "UPDATE evacuation_checklist SET status='acked',acked_by=?,acked_at=?,receipt_id=? "
                    "WHERE batch_id=? AND step_code=? AND status='pending'",
                    [(actor_id, now, str(receipt_id), batch_id, step) for step in newly_acked],
                )
            if batch["status"] == "planned":
                self.connection.execute(
                    "UPDATE evacuation_batches SET status='in_progress' WHERE batch_id=?", (batch_id,)
                )

            pending = [
                row["step_code"]
                for row in self.connection.execute(
                    "SELECT step_code FROM evacuation_checklist WHERE batch_id=? AND status='pending' ORDER BY rowid",
                    (batch_id,),
                ).fetchall()
            ]
            released_ids: list[str] = []
            final_status = "in_progress"
            if not pending:
                final_status = "completed"
                member_ids = [
                    row["reservation_id"]
                    for row in self.connection.execute(
                        "SELECT reservation_id FROM evacuation_batch_members WHERE batch_id=? ORDER BY rowid",
                        (batch_id,),
                    ).fetchall()
                ]
                for member_id in member_ids:
                    cursor = self.connection.execute(
                        "UPDATE capacity_holds SET released_at=?,release_receipt_id=? "
                        "WHERE reservation_id=? AND released_at IS NULL",
                        (now, receipt_id, member_id),
                    )
                    if cursor.rowcount > 0:
                        released_ids.append(member_id)
                    self.connection.execute(
                        "UPDATE reservations SET state='evacuated',revision=revision+1,updated_at=? "
                        "WHERE reservation_id=? AND state IN ('evacuating','checked_in')",
                        (now, member_id),
                    )
                self.connection.execute(
                    "UPDATE evacuation_batches SET status='completed' WHERE batch_id=?", (batch_id,)
                )
            self.connection.execute(
                "UPDATE evacuation_receipts SET released_reservation_ids_json=?,status=? WHERE receipt_id=?",
                (canonical_json(released_ids), final_status, receipt_id),
            )
            response = {
                "receipt_id": receipt_id,
                "batch_id": batch_id,
                "status": final_status,
                "acked_steps": normalized_steps,
                "newly_acked_steps": newly_acked,
                "released_reservation_ids": released_ids,
                "pending_steps": pending,
                "duplicate": False,
            }
            self._idempotency_store("evacuation_receipt", idempotency_key, request_sha, response)
            self._audit("evacuation_batch", batch_id, "evacuation.receipt", actor_id,
                        {"receipt_id": receipt_id, "steps": normalized_steps,
                         "released": released_ids})
        return response

    # -------------------------------------------------------------- 审计

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM carrying_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
