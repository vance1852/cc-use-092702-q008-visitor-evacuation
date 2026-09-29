"""统一承载编排应用服务：容量版本快照、保留/调整方案、原子占用与应急疏散。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Iterable, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import CapacityResource, ClosureWindow, ReservationRequest, WeatherAlert
from .planning import (
    DENY,
    EVACUATE,
    RESCHEDULE,
    RETAIN,
    booking_decisions,
    build_evacuation_batches,
    canonical_json,
    digest,
    effective_capacity,
    safety_actions_for,
    slot_overlaps,
    weather_decisions,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"resource.write", "capacity.write", "report.read"},
    "agent": {"reservation.write", "booking.run", "booking.confirm", "safety.write", "report.read"},
    "risk": {"closure.write", "alert.write", "weather.run", "weather.confirm", "report.read"},
    "rescue": {"evacuation.execute", "safety.write", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

ACTIVE_BOOKING_STATES = ("confirmed", "rescheduled", "checked_in")


class VisitorOrchestrationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM orch_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM orch_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO orch_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
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

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO orch_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ---------------------------------------------------------- 容量目录管理

    def register_resource(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource.write")
        resource = CapacityResource.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capacity_resources(domain,resource_id,slot,direction,name,capacity,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        resource.domain,
                        resource.resource_id,
                        resource.slot,
                        resource.direction,
                        resource.name,
                        resource.capacity,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("capacity_resource", f"{resource.domain}:{resource.resource_id}:{resource.slot}",
                            "resource.registered", actor_id,
                            {"domain": resource.domain, "resource_id": resource.resource_id, "slot": resource.slot})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该资源分时容量已经登记") from exc
        return {
            "domain": resource.domain,
            "resource_id": resource.resource_id,
            "slot": resource.slot,
            "revision": 1,
            "capacity": resource.capacity,
        }

    def adjust_capacity(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """容量调整产生新版本；引用旧版本的任何未确认整单在确认时失败。"""
        self._require(actor_id, "capacity.write")
        domain = str(raw.get("domain", "")).strip()
        if domain not in {"entry_slot", "trail", "shuttle", "parking"}:
            raise ValidationFailed("domain 必须是 entry_slot、trail、shuttle 或 parking")
        resource_id = str(raw.get("resource_id", "")).strip()
        slot = str(raw.get("slot", "")).strip()
        if isinstance(raw.get("capacity"), bool) or not isinstance(raw.get("capacity"), int) or raw["capacity"] < 0:
            raise ValidationFailed("capacity 必须是非负整数")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE capacity_resources SET capacity=?,revision=revision+1 "
                "WHERE domain=? AND resource_id=? AND slot=? AND state='active'",
                (raw["capacity"], domain, resource_id, slot),
            )
            if cursor.rowcount != 1:
                raise NotFound("资源分时容量不存在")
            revision = self.connection.execute(
                "SELECT revision FROM capacity_resources WHERE domain=? AND resource_id=? AND slot=?",
                (domain, resource_id, slot),
            ).fetchone()["revision"]
            self._audit("capacity_resource", f"{domain}:{resource_id}:{slot}", "capacity.adjusted", actor_id,
                        {"revision": revision, "capacity": raw["capacity"]})
        return {"domain": domain, "resource_id": resource_id, "slot": slot, "revision": revision, "capacity": raw["capacity"]}

    def announce_closure(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "closure.write")
        window = ClosureWindow.from_dict(raw)
        target = self.connection.execute(
            "SELECT 1 FROM capacity_resources WHERE domain=? AND resource_id=? AND state='active' LIMIT 1",
            (window.domain, window.resource_id),
        ).fetchone()
        if target is None:
            raise NotFound("关闭窗口对应的资源不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO closure_windows(window_id,domain,resource_id,starts_at,ends_at,capacity_percent,"
                    "reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (window.window_id, window.domain, window.resource_id, window.starts_at, window.ends_at,
                     window.capacity_percent, window.reason, actor_id, self._now()),
                )
                self._audit("closure_window", window.window_id, "closure.announced", actor_id,
                            {"domain": window.domain, "resource_id": window.resource_id, "reason": window.reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("关闭窗口编号冲突") from exc
        return {"window_id": window.window_id, "state": "announced"}

    def issue_alert(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "alert.write")
        alert = WeatherAlert.from_dict(raw)
        missing = [
            f"{domain}:{resource_id}"
            for domain, resource_id in alert.affected_resources
            if self.connection.execute(
                "SELECT 1 FROM capacity_resources WHERE domain=? AND resource_id=? AND state='active' LIMIT 1",
                (domain, resource_id),
            ).fetchone() is None
        ]
        if missing:
            raise NotFound(f"预警影响的资源不存在：{', '.join(missing)}")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO weather_alerts(alert_id,level,title,issued_at,affected_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (alert.alert_id, alert.level, alert.title, alert.issued_at,
                     canonical_json([{"domain": d, "resource_id": r} for d, r in alert.affected_resources]),
                     actor_id, self._now()),
                )
                self._audit("weather_alert", alert.alert_id, "alert.issued", actor_id,
                            {"level": alert.level, "affected": len(alert.affected_resources)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("预警编号冲突") from exc
        return {"alert_id": alert.alert_id, "level": alert.level, "state": "open"}

    # -------------------------------------------------------------- 容量快照

    def _live_inputs(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row], dict[tuple[str, str], list[str]]]:
        resources = self.connection.execute(
            "SELECT * FROM capacity_resources WHERE state='active' ORDER BY domain,resource_id,slot"
        ).fetchall()
        closures = self.connection.execute(
            "SELECT * FROM closure_windows WHERE state='announced' ORDER BY window_id"
        ).fetchall()
        alerts = self.connection.execute(
            "SELECT alert_id,affected_json FROM weather_alerts WHERE state='open'"
        ).fetchall()
        affected: dict[tuple[str, str], list[str]] = {}
        for alert in alerts:
            for item in json.loads(alert["affected_json"]):
                affected.setdefault((item["domain"], item["resource_id"]), []).append(alert["alert_id"])
        return resources, closures, affected

    def _derive_item(
        self,
        resource: sqlite3.Row,
        closures: Iterable[sqlite3.Row],
        affected: Mapping[tuple[str, str], list[str]],
    ) -> dict[str, Any]:
        overlapping = [
            row for row in closures
            if row["domain"] == resource["domain"]
            and row["resource_id"] == resource["resource_id"]
            and slot_overlaps(resource["slot"], row["starts_at"], row["ends_at"])
        ]
        alert_ids = affected.get((resource["domain"], resource["resource_id"]), [])
        hard_closed = bool(alert_ids)
        effective = effective_capacity(
            int(resource["capacity"]),
            [int(row["capacity_percent"]) for row in overlapping],
            hard_closed=hard_closed,
        )
        return {
            "domain": resource["domain"],
            "resource_id": resource["resource_id"],
            "slot": resource["slot"],
            "direction": resource["direction"],
            "revision": int(resource["revision"]),
            "nominal_capacity": int(resource["capacity"]),
            "effective_capacity": effective,
            "closures": [
                {
                    "window_id": row["window_id"],
                    "capacity_percent": int(row["capacity_percent"]),
                    "reason": row["reason"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                }
                for row in overlapping
            ],
            "alert_hard_closed": hard_closed,
            "alert_ids": sorted(alert_ids),
        }

    def _snapshot_for(self, actor_id: str) -> tuple[int, list[dict[str, Any]]]:
        """构建内容寻址的统一容量版本快照；内容不变则复用同一快照。"""
        resources, closures, affected = self._live_inputs()
        items = [self._derive_item(row, closures, affected) for row in resources]
        content = canonical_json([
            {key: item[key] for key in (
                "domain", "resource_id", "slot", "direction", "revision",
                "nominal_capacity", "effective_capacity", "closures", "alert_ids",
            )}
            for item in items
        ])
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self.connection.execute(
            "SELECT snapshot_id FROM capacity_snapshots WHERE content_sha256=?", (content_sha256,)
        ).fetchone()
        if existing is not None:
            return int(existing["snapshot_id"]), items
        cursor = self.connection.execute(
            "INSERT INTO capacity_snapshots(content_sha256,created_by,created_at) VALUES(?,?,?)",
            (content_sha256, actor_id, self._now()),
        )
        snapshot_id = int(cursor.lastrowid)
        for item in items:
            self.connection.execute(
                "INSERT INTO capacity_snapshot_items(snapshot_id,domain,resource_id,slot,direction,revision,"
                "nominal_capacity,effective_capacity,sources_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id, item["domain"], item["resource_id"], item["slot"], item["direction"],
                    item["revision"], item["nominal_capacity"], item["effective_capacity"],
                    canonical_json({
                        "closures": item["closures"],
                        "alert_hard_closed": item["alert_hard_closed"],
                        "alert_ids": item["alert_ids"],
                    }),
                ),
            )
        return snapshot_id, items

    # ------------------------------------------------------------- 预约登记

    def submit_reservation(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        request = ReservationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM orch_idempotency WHERE scope='reservation' AND idempotency_key=?",
            (request.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同预约内容")
            return json.loads(stored["response_json"])
        self._validate_references(request)
        response = {
            "reservation_id": request.reservation_id,
            "state": "draft",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO reservations(reservation_id,party_size,entry_resource_id,entry_slot,"
                    "trail_resource_id,trail_direction,shuttle_resource_id,parking_resource_id,assistance_json,"
                    "state,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.reservation_id, request.party_size, request.entry_resource_id, request.entry_slot,
                        request.trail_resource_id, request.trail_direction,
                        request.shuttle_resource_id, request.parking_resource_id,
                        canonical_json([
                            {"visitor_id": item.visitor_id, "kind": item.kind, "note": item.note}
                            for item in request.assistance
                        ]),
                        "draft", request.idempotency_key, actor_id, self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO orch_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('reservation',?,?,?,?)",
                    (request.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("reservation", request.reservation_id, "reservation.submitted", actor_id,
                            {"party_size": request.party_size, "entry_slot": request.entry_slot})
        except sqlite3.IntegrityError as exc:
            raise Conflict("预约编号或幂等键冲突") from exc
        return response

    def _validate_references(self, request: ReservationRequest) -> None:
        needed: list[tuple[str, str, str]] = [
            ("entry_slot", request.entry_resource_id, request.entry_slot)
        ]
        if request.trail_resource_id:
            needed.append(("trail", request.trail_resource_id, request.entry_slot))
        if request.shuttle_resource_id:
            needed.append(("shuttle", request.shuttle_resource_id, request.entry_slot))
        if request.parking_resource_id:
            needed.append(("parking", request.parking_resource_id, request.entry_slot))
        for domain, resource_id, slot in needed:
            row = self.connection.execute(
                "SELECT 1 FROM capacity_resources WHERE domain=? AND resource_id=? AND slot=? AND state='active'",
                (domain, resource_id, slot),
            ).fetchone()
            if row is None:
                raise NotFound(f"资源 {domain}:{resource_id} 在 {slot} 没有容量版本")
        if request.trail_resource_id and request.trail_direction:
            trail_row = self.connection.execute(
                "SELECT direction FROM capacity_resources WHERE domain='trail' AND resource_id=? AND slot=?",
                (request.trail_resource_id, request.entry_slot),
            ).fetchone()
            if trail_row is not None and trail_row["direction"] and trail_row["direction"] != request.trail_direction:
                raise ValidationFailed("步道方向容量与预约方向不一致")

    def check_in(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "reservation.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state,revision FROM reservations WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None:
                raise NotFound("预约不存在")
            if row["state"] not in {"confirmed", "rescheduled"}:
                raise InvalidState("只有已确认预约可以入园核销")
            self.connection.execute(
                "UPDATE reservations SET state='checked_in',revision=revision+1 WHERE reservation_id=? AND revision=?",
                (reservation_id, row["revision"]),
            )
            self._audit("reservation", reservation_id, "reservation.checked_in", actor_id, {})
        return {"reservation_id": reservation_id, "state": "checked_in"}

    def _reservation_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "party_size": row["party_size"],
            "entry_resource_id": row["entry_resource_id"],
            "entry_slot": row["entry_slot"],
            "trail_resource_id": row["trail_resource_id"],
            "shuttle_resource_id": row["shuttle_resource_id"],
            "parking_resource_id": row["parking_resource_id"],
            "assistance": json.loads(row["assistance_json"]),
            "state": row["state"],
            "revision": row["revision"],
            "created_at": row["created_at"],
        }

    def _committed_holds(self, exclude_reservations: Iterable[str] = ()) -> dict[str, int]:
        excluded = tuple(exclude_reservations)
        sql = (
            "SELECT domain,resource_id,slot,SUM(units) AS units FROM reservation_resources WHERE state='held'"
            + (" AND reservation_id NOT IN (%s)" % ",".join("?" * len(excluded)) if excluded else "")
            + " GROUP BY domain,resource_id,slot"
        )
        rows = self.connection.execute(sql, excluded).fetchall()
        return {f"{row['domain']}|{row['resource_id']}|{row['slot']}": int(row["units"]) for row in rows}

    # ------------------------------------------------------------- 方案生成

    def _capacity_map(self, items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            f"{item['domain']}|{item['resource_id']}|{item['slot']}": item for item in items
        }

    def _persist_plan(
        self,
        actor_id: str,
        kind: str,
        snapshot_id: int,
        alert_id: str | None,
        input_value: Mapping[str, Any],
        decisions: list[dict[str, Any]],
        reservation_revisions: Mapping[str, int],
    ) -> int:
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT plan_id FROM orchestration_plans WHERE kind=? AND snapshot_id=? AND input_sha256=?",
            (kind, snapshot_id, input_sha256),
        ).fetchone()
        if existing is not None:
            return int(existing["plan_id"])
        result = {"decisions": decisions}
        cursor = self.connection.execute(
            "INSERT INTO orchestration_plans(kind,snapshot_id,alert_id,input_sha256,result_json,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (kind, snapshot_id, alert_id, input_sha256, canonical_json(result), actor_id, self._now()),
        )
        plan_id = int(cursor.lastrowid)
        for decision in decisions:
            reservation_id = decision["reservation_id"]
            self.connection.execute(
                "INSERT INTO plan_decisions(plan_id,reservation_id,action,reason_code,reason_detail,blocked_by,"
                "capacity_sources_json,target_capacity_sources_json,target_json,from_revision) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    plan_id, reservation_id, decision["action"], decision["reason_code"],
                    decision["reason_detail"], decision.get("blocked_by"),
                    canonical_json(decision["capacity_sources"]),
                    canonical_json(decision.get("target_capacity_sources", ())),
                    canonical_json(decision.get("target", {})),
                    reservation_revisions.get(reservation_id),
                ),
            )
            for code, detail in safety_actions_for(decision):
                self.connection.execute(
                    "INSERT OR IGNORE INTO safety_action_ledger(plan_id,reservation_id,code,detail,opened_at) "
                    "VALUES(?,?,?,?,?)",
                    (plan_id, reservation_id, code, detail, self._now()),
                )
        return plan_id

    def create_booking_plan(self, actor_id: str, duty_date: str) -> dict[str, Any]:
        self._require(actor_id, "booking.run")
        with transaction(self.connection, immediate=True):
            snapshot_id, items = self._snapshot_for(actor_id)
            rows = self.connection.execute(
                "SELECT * FROM reservations WHERE state='draft' AND substr(entry_slot,1,10)=? "
                "ORDER BY created_at,reservation_id",
                (duty_date,),
            ).fetchall()
            if not rows:
                raise InvalidState("该日期没有待编排的草稿预约")
            reservations = [self._reservation_view(row) for row in rows]
            revisions = {row["reservation_id"]: row["revision"] for row in rows}
            decisions = booking_decisions(
                reservations,
                self._capacity_map(items),
                self._committed_holds(exclude_reservations=revisions),
            )
            input_value = {
                "kind": "booking",
                "snapshot": snapshot_id,
                "reservations": [
                    {"reservation_id": row["reservation_id"], "revision": row["revision"],
                     "entry_slot": row["entry_slot"], "party_size": row["party_size"]}
                    for row in rows
                ],
            }
            plan_id = self._persist_plan(actor_id, "booking", snapshot_id, None, input_value, decisions, revisions)
            self._audit("orchestration_plan", str(plan_id), "booking_plan.proposed", actor_id,
                        {"snapshot_id": snapshot_id, "reservations": len(rows)})
        return self.plan(plan_id, actor_id=actor_id)

    def create_weather_plan(self, actor_id: str, alert_id: str) -> dict[str, Any]:
        self._require(actor_id, "weather.run")
        with transaction(self.connection, immediate=True):
            alert = self.connection.execute(
                "SELECT * FROM weather_alerts WHERE alert_id=?", (alert_id,)
            ).fetchone()
            if alert is None:
                raise NotFound("气象预警不存在")
            if alert["state"] != "open":
                raise InvalidState("预警已经解除，不能再触发疏散编排")
            snapshot_id, items = self._snapshot_for(actor_id)
            rows = self.connection.execute(
                "SELECT * FROM reservations WHERE state IN (%s) ORDER BY created_at,reservation_id"
                % ",".join("?" * len(ACTIVE_BOOKING_STATES)),
                ACTIVE_BOOKING_STATES,
            ).fetchall()
            if not rows:
                raise InvalidState("当前没有在园或已确认的预约需要编排")
            affected = {(item["domain"], item["resource_id"]) for item in json.loads(alert["affected_json"])}
            reservations = [self._reservation_view(row) for row in rows]
            revisions = {row["reservation_id"]: row["revision"] for row in rows}
            decisions = weather_decisions(
                reservations,
                self._capacity_map(items),
                self._committed_holds(),
                sorted(affected),
            )
            input_value = {
                "kind": "weather",
                "snapshot": snapshot_id,
                "alert_id": alert_id,
                "alert_issued_at": alert["issued_at"],
                "reservations": [
                    {"reservation_id": row["reservation_id"], "revision": row["revision"], "state": row["state"]}
                    for row in rows
                ],
            }
            plan_id = self._persist_plan(
                actor_id, "weather", snapshot_id, alert_id, input_value, decisions, revisions
            )
            self._audit("orchestration_plan", str(plan_id), "weather_plan.proposed", actor_id,
                        {"snapshot_id": snapshot_id, "alert_id": alert_id})
            evacuation_id = self._ensure_evacuation(actor_id, alert_id, snapshot_id, plan_id, decisions, items, alert)
        return self.plan(plan_id, actor_id=actor_id)

    def _ensure_evacuation(
        self,
        actor_id: str,
        alert_id: str,
        snapshot_id: int,
        plan_id: int,
        decisions: list[dict[str, Any]],
        items: list[dict[str, Any]],
        alert: sqlite3.Row,
    ) -> str:
        existing = self.connection.execute(
            "SELECT evacuation_id FROM evacuations WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None:
            return str(existing["evacuation_id"])
        evacuee_ids = [d["reservation_id"] for d in decisions if d["action"] == EVACUATE]
        if not evacuee_ids:
            return ""
        reservation_map = {
            row["reservation_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM reservations WHERE reservation_id IN (%s)" % ",".join("?" * len(evacuee_ids)),
                evacuee_ids,
            ).fetchall()
        }
        evacuees = [
            {
                "reservation_id": reservation_id,
                "pax": reservation_map[reservation_id]["party_size"],
                "assistance": json.loads(reservation_map[reservation_id]["assistance_json"]),
            }
            for reservation_id in evacuee_ids
        ]
        threshold = alert["issued_at"][:16].replace("T", "T")
        shuttle_options = [
            {"resource_id": item["resource_id"], "slot": item["slot"], "seats": item["effective_capacity"]}
            for item in items
            if item["domain"] == "shuttle" and item["effective_capacity"] > 0 and item["slot"] >= threshold
        ]
        packed = build_evacuation_batches(evacuees, shuttle_options)
        evacuation_id = f"evac-{alert_id}-{uuid.uuid4().hex[:8]}"
        self.connection.execute(
            "INSERT INTO evacuations(evacuation_id,alert_id,snapshot_id,plan_id,created_by,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (evacuation_id, alert_id, snapshot_id, plan_id, actor_id, self._now()),
        )
        for batch in packed["batches"]:
            batch_id = f"{evacuation_id}-b{batch['sequence_no']}"
            self.connection.execute(
                "INSERT INTO evacuation_batches(batch_id,evacuation_id,sequence_no,assembly_point,"
                "shuttle_resource_id,shuttle_slot,seats,priority_kind) VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch_id, evacuation_id, batch["sequence_no"], batch["assembly_point"],
                    batch["shuttle_resource_id"], batch["shuttle_slot"], batch["seats"],
                    batch["priority_kind"],
                ),
            )
            for member in batch["members"]:
                self.connection.execute(
                    "INSERT INTO evacuation_batch_members(batch_id,reservation_id,pax,assistance_json,"
                    "checklist_json) VALUES(?,?,?,?,?)",
                    (
                        batch_id, member["reservation_id"], member["pax"],
                        canonical_json(member["assistance"]), canonical_json(member["checklist"]),
                    ),
                )
        self._audit("evacuation", evacuation_id, "evacuation.planned", actor_id,
                    {"alert_id": alert_id, "batches": len(packed["batches"]), "evacuees": len(evacuees)})
        return evacuation_id

    # ------------------------------------------------------- 方案确认（原子）

    def _stored_snapshot_views(self, snapshot_id: int) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM capacity_snapshot_items WHERE snapshot_id=?", (snapshot_id,)
        ).fetchall()
        views: dict[str, dict[str, Any]] = {}
        for row in rows:
            views[f"{row['domain']}|{row['resource_id']}|{row['slot']}"] = {
                "domain": row["domain"],
                "resource_id": row["resource_id"],
                "slot": row["slot"],
                "revision": int(row["revision"]),
                "nominal_capacity": int(row["nominal_capacity"]),
                "effective_capacity": int(row["effective_capacity"]),
                **json.loads(row["sources_json"]),
            }
        return views

    def _verify_snapshot_fresh(
        self,
        snapshot_id: int,
        referenced: set[str],
        closures: list[sqlite3.Row],
        affected: Mapping[tuple[str, str], list[str]],
    ) -> dict[str, dict[str, Any]]:
        """重新推导被引用资源的当前容量；任一关联容量版本变化即判定整单失效。"""
        stored = self._stored_snapshot_views(snapshot_id)
        current: dict[str, dict[str, Any]] = {}
        for key in sorted(referenced):
            domain, resource_id, slot = key.split("|", 2)
            row = self.connection.execute(
                "SELECT * FROM capacity_resources WHERE domain=? AND resource_id=? AND slot=? AND state='active'",
                (domain, resource_id, slot),
            ).fetchone()
            if row is None:
                raise InvalidState(f"容量来源已失效：{key} 不在当前容量目录")
            view = self._derive_item(row, closures, affected)
            old = stored.get(key)
            if old is None:
                raise InvalidState(f"容量来源缺失：{key} 不在方案快照 {snapshot_id} 中")
            if (
                view["revision"] != old["revision"]
                or view["effective_capacity"] != old["effective_capacity"]
                or [c["window_id"] for c in view["closures"]] != [c["window_id"] for c in old["closures"]]
                or view["alert_hard_closed"] != old["alert_hard_closed"]
                or sorted(view["alert_ids"]) != sorted(old["alert_ids"])
            ):
                raise InvalidState(f"容量版本已变化：{key}（快照 r{old['revision']} → 当前 r{view['revision']}）")
            current[key] = view
        return current

    def confirm_plan(self, actor_id: str, plan_id: int, *, permission: str) -> dict[str, Any]:
        self._require(actor_id, permission)
        try:
            with transaction(self.connection, immediate=True):
                result = self._confirm_plan(actor_id, plan_id, permission)
        except InvalidState:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE orchestration_plans SET state='failed',failure_reason=COALESCE(failure_reason,'容量版本变化导致整单失败'),"
                    "confirmed_at=? WHERE plan_id=? AND state='proposed'",
                    (self._now(), plan_id),
                )
                self.connection.execute(
                    "UPDATE evacuations SET state='superseded' WHERE plan_id=? AND state='planned'",
                    (plan_id,),
                )
            raise
        return result

    def _confirm_plan(self, actor_id: str, plan_id: int, permission: str) -> dict[str, Any]:
        plan = self.connection.execute(
            "SELECT * FROM orchestration_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFound("编排方案不存在")
        required_permission = "booking.confirm" if plan["kind"] == "booking" else "weather.confirm"
        if permission != required_permission:
            raise Forbidden(f"该方案必须由 {required_permission} 权限确认")
        if plan["state"] == "confirmed":
            return self.plan(plan_id, actor_id=actor_id)
        if plan["state"] != "proposed":
            raise InvalidState(f"方案处于 {plan['state']} 状态，不能确认")

        decision_rows = self.connection.execute(
            "SELECT * FROM plan_decisions WHERE plan_id=? ORDER BY reservation_id", (plan_id,)
        ).fetchall()
        _resources, closures, affected = self._live_inputs()

        # 重建每个决策的原时段需求与确认后实际占用需求：
        # retain/evacuate 占用原时段；reschedule 占用目标时段；deny 不占用。
        source_requirements: dict[str, list[list[Any]]] = {}
        applied_requirements: dict[str, list[list[Any]]] = {}
        referenced: set[str] = set()
        for row in decision_rows:
            origin = [
                [item["domain"], item["resource_id"], item["slot"], item["required_units"]]
                for item in json.loads(row["capacity_sources_json"])
            ]
            target = [
                [item["domain"], item["resource_id"], item["slot"], item["required_units"]]
                for item in json.loads(row["target_capacity_sources_json"])
            ]
            for item in origin + target:
                referenced.add(f"{item[0]}|{item[1]}|{item[2]}")
            source_requirements[row["reservation_id"]] = origin
            if row["action"] == RESCHEDULE:
                applied_requirements[row["reservation_id"]] = target
            elif row["action"] in {RETAIN, EVACUATE}:
                # 在园疏散者的既有占用代表"人还在现场、正在撤出"，不是新增放行，
                # 不参与生效容量超额校验；其占用行在交接完成前保持不动。
                applied_requirements[row["reservation_id"]] = origin if row["action"] == RETAIN else []
            else:
                applied_requirements[row["reservation_id"]] = []

        # 任一关联容量版本（修订版本、生效容量、关闭窗口、预警标记）变化，整单失败。
        self._verify_snapshot_fresh(int(plan["snapshot_id"]), referenced, closures, affected)
        stored = self._stored_snapshot_views(int(plan["snapshot_id"]))

        participating = [row["reservation_id"] for row in decision_rows]
        placeholders = ",".join("?" * len(participating))

        def held_total(domain: str, resource_id: str, slot: str, *, only_participating: bool) -> int:
            sql = (
                "SELECT COALESCE(SUM(units),0) AS units FROM reservation_resources "
                "WHERE state='held' AND domain=? AND resource_id=? AND slot=?"
            )
            params: list[Any] = [domain, resource_id, slot]
            if only_participating:
                sql += f" AND reservation_id IN ({placeholders})"
                params.extend(participating)
            return int(self.connection.execute(sql, params).fetchone()["units"])

        # 原子占用校验：非参与方占用保持不变，参与各方按方案重新计入，
        # 任一关联资源的方案后总量超过生效容量则整单失败（不写入任何占用）。
        applied_keys = {
            f"{d}|{r}|{s}"
            for requirements in applied_requirements.values()
            for d, r, s, _u in requirements
        }
        proposed_totals: dict[str, int] = {}
        for key in applied_keys:
            domain, resource_id, slot = key.split("|", 2)
            baseline = held_total(domain, resource_id, slot, only_participating=False) - held_total(
                domain, resource_id, slot, only_participating=True
            )
            proposed_totals[key] = baseline
        for requirements in applied_requirements.values():
            for domain, resource_id, slot, units in requirements:
                key = f"{domain}|{resource_id}|{slot}"
                proposed_totals[key] = proposed_totals.get(key, 0) + units
        for key, total in proposed_totals.items():
            if total > stored[key]["effective_capacity"]:
                raise InvalidState(
                    f"原子占用失败：{key} 方案后占用 {total} 超过生效容量 {stored[key]['effective_capacity']}"
                )

        now = self._now()
        for row in decision_rows:
            reservation_id = row["reservation_id"]
            target_meta = json.loads(row["target_json"])
            expected_revision = row["from_revision"]
            if plan["kind"] == "booking":
                new_state = {
                    RETAIN: "confirmed",
                    RESCHEDULE: "rescheduled",
                    DENY: "denied",
                }[row["action"]]
            else:
                new_state = {
                    EVACUATE: "evacuating",
                    RESCHEDULE: "rescheduled",
                    DENY: "cancelled",
                    RETAIN: None,
                }[row["action"]]
            cursor = self.connection.execute(
                "UPDATE reservations SET revision=revision+1"
                + (",entry_slot=?" if row["action"] == RESCHEDULE else "")
                + (",state=?" if new_state is not None else "")
                + " WHERE reservation_id=? AND revision=?",
                (
                    *((target_meta["entry_slot"],) if row["action"] == RESCHEDULE else ()),
                    *((new_state,) if new_state is not None else ()),
                    reservation_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState(f"预约 {reservation_id} 已被其他操作修改，整单确认失败")

            if row["action"] in {RETAIN, RESCHEDULE}:
                if row["action"] == RESCHEDULE:
                    # 改签：释放原时段占用，随后在目标时段重建。
                    self.connection.execute(
                        "UPDATE reservation_resources SET state='released',updated_at=? "
                        "WHERE reservation_id=? AND state='held'",
                        (now, reservation_id),
                    )
                for domain, resource_id, slot, units in applied_requirements[reservation_id]:
                    self.connection.execute(
                        "INSERT INTO reservation_resources(reservation_id,domain,resource_id,slot,units,state,"
                        "snapshot_id,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(reservation_id,domain,resource_id,slot) DO UPDATE SET units=excluded.units,"
                        "state='held',snapshot_id=excluded.snapshot_id,updated_at=excluded.updated_at",
                        (reservation_id, domain, resource_id, slot, units, "held", plan["snapshot_id"], now),
                    )
            elif row["action"] == DENY:
                # 预订拒绝 / 预警取消：释放整单既有占用（草稿单本无占用，操作为空操作）。
                self.connection.execute(
                    "UPDATE reservation_resources SET state='released',updated_at=? "
                    "WHERE reservation_id=? AND state='held'",
                    (now, reservation_id),
                )
            # EVACUATE 与预警保留：游客仍在园内，疏散完成交接前占用不释放。
            self.connection.execute(
                "UPDATE reservations SET latest_snapshot_id=? WHERE reservation_id=?",
                (plan["snapshot_id"], reservation_id),
            )

        self.connection.execute(
            "UPDATE orchestration_plans SET state='confirmed',confirmed_at=? WHERE plan_id=?",
            (now, plan_id),
        )
        if plan["kind"] == "weather":
            self.connection.execute(
                "UPDATE evacuations SET state='in_progress' WHERE plan_id=? AND state='planned'",
                (plan_id,),
            )
        self._audit("orchestration_plan", str(plan_id), "plan.confirmed", actor_id,
                    {"kind": plan["kind"], "decisions": len(decision_rows)})
        return self.plan(plan_id, actor_id=actor_id)

    def confirm_booking_plan(self, actor_id: str, plan_id: int) -> dict[str, Any]:
        return self.confirm_plan(actor_id, plan_id, permission="booking.confirm")

    def confirm_weather_plan(self, actor_id: str, plan_id: int) -> dict[str, Any]:
        return self.confirm_plan(actor_id, plan_id, permission="weather.confirm")

    # ----------------------------------------------------------- 疏散执行

    def _batch(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT b.*, e.state AS evacuation_state, e.plan_id FROM evacuation_batches b "
            "JOIN evacuations e ON e.evacuation_id=b.evacuation_id WHERE b.batch_id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFound("疏散批次不存在")
        return row

    def _batch_view(self, batch_id: str) -> dict[str, Any]:
        self._batch(batch_id)
        evacuation_id = self.connection.execute(
            "SELECT evacuation_id FROM evacuation_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()["evacuation_id"]
        evacuation = self.connection.execute(
            "SELECT evacuation_id,state FROM evacuations WHERE evacuation_id=?", (evacuation_id,)
        ).fetchone()
        batches = self._evacuation_batches(evacuation_id)
        return {
            "evacuation_id": evacuation_id,
            "evacuation_state": evacuation["state"],
            "batch": next(item for item in batches if item["batch_id"] == batch_id),
        }

    def dispatch_batch(self, actor_id: str, batch_id: str, crew_id: str) -> dict[str, Any]:
        self._require(actor_id, "evacuation.execute")
        if not crew_id.strip():
            raise ValidationFailed("crew_id 不能为空")
        with transaction(self.connection, immediate=True):
            row = self._batch(batch_id)
            if row["evacuation_state"] != "in_progress":
                raise InvalidState("疏散方案尚未确认，不能发车")
            if row["state"] != "planned":
                raise InvalidState(f"批次当前为 {row['state']}，无需重复发车")
            self.connection.execute(
                "UPDATE evacuation_batches SET state='dispatched',dispatched_at=?,crew_id=?,revision=revision+1 "
                "WHERE batch_id=? AND revision=?",
                (self._now(), crew_id.strip(), batch_id, row["revision"]),
            )
            for member in self.connection.execute(
                "SELECT reservation_id FROM evacuation_batch_members WHERE batch_id=?", (batch_id,)
            ).fetchall():
                self._mark_action(row["plan_id"], member["reservation_id"], "vehicle:dispatch", done=True, actor_id=actor_id)
            self._audit("evacuation_batch", batch_id, "batch.dispatched", actor_id, {"crew_id": crew_id})
        return self._batch_view(batch_id)

    def arrive_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "evacuation.execute")
        with transaction(self.connection, immediate=True):
            row = self._batch(batch_id)
            if row["state"] != "dispatched":
                raise InvalidState(f"批次当前为 {row['state']}，不能报到抵达")
            self.connection.execute(
                "UPDATE evacuation_batches SET state='arrived',arrived_at=?,revision=revision+1 WHERE batch_id=?",
                (self._now(), batch_id),
            )
            for member in self.connection.execute(
                "SELECT reservation_id FROM evacuation_batch_members WHERE batch_id=?", (batch_id,)
            ).fetchall():
                self._mark_action(row["plan_id"], member["reservation_id"], "headcount", done=True, actor_id=actor_id)
            self._audit("evacuation_batch", batch_id, "batch.arrived", actor_id, {})
        return self._batch_view(batch_id)

    def handover_batch(
        self,
        actor_id: str,
        batch_id: str,
        receiver: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "evacuation.execute")
        if not receiver.strip():
            raise ValidationFailed("receiver 不能为空")
        if not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        request_digest = digest({"batch_id": batch_id, "receiver": receiver.strip()})
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM orch_idempotency WHERE scope='handover' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("回执幂等键对应不同交接内容")
            return json.loads(stored["response_json"])  # 重复回执直接回放，绝不重复释放名额
        with transaction(self.connection, immediate=True):
            row = self._batch(batch_id)
            if row["state"] not in {"arrived", "handed_over"}:
                raise InvalidState(f"批次当前为 {row['state']}，不能交接")
            members = self.connection.execute(
                "SELECT * FROM evacuation_batch_members WHERE batch_id=? ORDER BY reservation_id", (batch_id,)
            ).fetchall()
            released_keys: list[str] = []
            handed: list[str] = []
            now = self._now()
            for member in members:
                if member["handover_state"] == "pending":
                    self.connection.execute(
                        "UPDATE evacuation_batch_members SET handover_state='handed_over',handed_over_at=?,receiver=? "
                        "WHERE batch_id=? AND reservation_id=? AND handover_state='pending'",
                        (now, receiver.strip(), batch_id, member["reservation_id"]),
                    )
                    handed.append(member["reservation_id"])
                    # 名额释放只发生一次：UPDATE 条件保证已是 released 的行不会再释放。
                    released_cursor = self.connection.execute(
                        "UPDATE reservation_resources SET state='released',updated_at=? "
                        "WHERE reservation_id=? AND state='held'",
                        (now, member["reservation_id"]),
                    )
                    if released_cursor.rowcount:
                        released_keys.append(member["reservation_id"])
                    self._mark_action(row["plan_id"], member["reservation_id"], "handover", done=True, actor_id=actor_id)
                    remaining = self.connection.execute(
                        "SELECT COUNT(*) AS c FROM evacuation_batch_members WHERE reservation_id=? AND handover_state='pending'",
                        (member["reservation_id"],),
                    ).fetchone()["c"]
                    if remaining == 0:
                        self.connection.execute(
                            "UPDATE reservations SET state='evacuated',revision=revision+1 WHERE reservation_id=?",
                            (member["reservation_id"],),
                        )
            pending_in_batch = self.connection.execute(
                "SELECT COUNT(*) AS c FROM evacuation_batch_members WHERE batch_id=? AND handover_state='pending'",
                (batch_id,),
            ).fetchone()["c"]
            if pending_in_batch == 0:
                self.connection.execute(
                    "UPDATE evacuation_batches SET state='handed_over',revision=revision+1 WHERE batch_id=?",
                    (batch_id,),
                )
            pending_batches = self.connection.execute(
                "SELECT COUNT(*) AS c FROM evacuation_batches WHERE evacuation_id=? AND state<>'handed_over'",
                (row["evacuation_id"],),
            ).fetchone()["c"]
            if pending_batches == 0:
                self.connection.execute(
                    "UPDATE evacuations SET state='completed' WHERE evacuation_id=?", (row["evacuation_id"],)
                )
            response = {
                "batch_id": batch_id,
                "state": "handed_over",
                "handed_over": handed,
                "released_reservations": released_keys,
                "replayed": False,
                "open_safety_actions": self._open_actions_for_batch(row["plan_id"], batch_id),
            }
            self.connection.execute(
                "INSERT INTO orch_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('handover',?,?,?,?)",
                (idempotency_key, request_digest, canonical_json(response), now),
            )
            self._audit("evacuation_batch", batch_id, "batch.handed_over", actor_id,
                        {"receiver": receiver, "released": released_keys})
        return response

    def _mark_action(
        self,
        plan_id: int,
        reservation_id: str,
        code_prefix: str,
        *,
        done: bool,
        actor_id: str,
    ) -> None:
        if done:
            self.connection.execute(
                "UPDATE safety_action_ledger SET state='done',done_at=?,done_by=? "
                "WHERE plan_id=? AND reservation_id=? AND code=? AND state='open'",
                (self._now(), actor_id, plan_id, reservation_id, code_prefix),
            )

    def complete_safety_action(self, actor_id: str, action_id: int) -> dict[str, Any]:
        self._require(actor_id, "safety.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM safety_action_ledger WHERE action_id=?", (action_id,)
            ).fetchone()
            if row is None:
                raise NotFound("安全动作不存在")
            if row["state"] == "done":
                return {"action_id": action_id, "state": "done", "replayed": True}
            self.connection.execute(
                "UPDATE safety_action_ledger SET state='done',done_at=?,done_by=? WHERE action_id=?",
                (self._now(), actor_id, action_id),
            )
            self._audit("safety_action", str(action_id), "safety.completed", actor_id,
                        {"reservation_id": row["reservation_id"], "code": row["code"]})
        return {"action_id": action_id, "state": "done", "replayed": False}

    # ---------------------------------------------------------------- 查询

    def _open_actions_for_batch(self, plan_id: int, batch_id: str) -> list[dict[str, Any]]:
        members = self.connection.execute(
            "SELECT reservation_id FROM evacuation_batch_members WHERE batch_id=?", (batch_id,)
        ).fetchall()
        ids = tuple(row["reservation_id"] for row in members)
        if not ids:
            return []
        rows = self.connection.execute(
            "SELECT action_id,reservation_id,code,detail FROM safety_action_ledger WHERE plan_id=? AND state='open' "
            "AND reservation_id IN (%s) ORDER BY action_id" % ",".join("?" * len(ids)),
            (plan_id, *ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def _open_actions(self, plan_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT action_id,reservation_id,code,detail,opened_at FROM safety_action_ledger "
            "WHERE plan_id=? AND state='open' ORDER BY action_id",
            (plan_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _decision_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reservation_id": row["reservation_id"],
            "action": row["action"],
            "reason_code": row["reason_code"],
            "reason_detail": row["reason_detail"],
            "blocked_by": row["blocked_by"],
            "capacity_sources": json.loads(row["capacity_sources_json"]),
            "target_capacity_sources": json.loads(row["target_capacity_sources_json"]),
            "target": json.loads(row["target_json"]),
        }

    def plan(self, plan_id: int, actor_id: str | None = None) -> dict[str, Any]:
        if actor_id is not None:
            self._require(actor_id, "report.read")
        plan_row = self.connection.execute(
            "SELECT * FROM orchestration_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan_row is None:
            raise NotFound("编排方案不存在")
        decisions = [
            self._decision_view(row)
            for row in self.connection.execute(
                "SELECT * FROM plan_decisions WHERE plan_id=? ORDER BY reservation_id", (plan_id,)
            ).fetchall()
        ]
        result = {
            "plan_id": plan_id,
            "kind": plan_row["kind"],
            "snapshot_id": plan_row["snapshot_id"],
            "alert_id": plan_row["alert_id"],
            "state": plan_row["state"],
            "failure_reason": plan_row["failure_reason"],
            "decisions": decisions,
            "open_safety_actions": self._open_actions(plan_id),
        }
        evacuation_row = self.connection.execute(
            "SELECT evacuation_id,state FROM evacuations WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if evacuation_row is not None:
            result["evacuation"] = {
                "evacuation_id": evacuation_row["evacuation_id"],
                "state": evacuation_row["state"],
                "batches": self._evacuation_batches(evacuation_row["evacuation_id"]),
            }
        return result

    def _evacuation_batches(self, evacuation_id: str) -> list[dict[str, Any]]:
        batches = []
        rows = self.connection.execute(
            "SELECT * FROM evacuation_batches WHERE evacuation_id=? ORDER BY sequence_no", (evacuation_id,)
        ).fetchall()
        for row in rows:
            members = self.connection.execute(
                "SELECT reservation_id,pax,assistance_json,handover_state,handed_over_at,receiver,checklist_json "
                "FROM evacuation_batch_members WHERE batch_id=? ORDER BY reservation_id",
                (row["batch_id"],),
            ).fetchall()
            batches.append({
                "batch_id": row["batch_id"],
                "sequence_no": row["sequence_no"],
                "assembly_point": row["assembly_point"],
                "shuttle_resource_id": row["shuttle_resource_id"],
                "shuttle_slot": row["shuttle_slot"],
                "seats": row["seats"],
                "priority_kind": row["priority_kind"],
                "state": row["state"],
                "crew_id": row["crew_id"],
                "members": [
                    {
                        "reservation_id": member["reservation_id"],
                        "pax": member["pax"],
                        "assistance": json.loads(member["assistance_json"]),
                        "handover_state": member["handover_state"],
                        "handed_over_at": member["handed_over_at"],
                        "receiver": member["receiver"],
                        "checklist": json.loads(member["checklist_json"]),
                    }
                    for member in members
                ],
            })
        return batches

    def evacuation(self, actor_id: str, evacuation_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM evacuations WHERE evacuation_id=?", (evacuation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("疏散任务不存在")
        return {
            "evacuation_id": evacuation_id,
            "alert_id": row["alert_id"],
            "snapshot_id": row["snapshot_id"],
            "plan_id": row["plan_id"],
            "state": row["state"],
            "batches": self._evacuation_batches(evacuation_id),
        }

    def reservation_status(self, actor_id: str, reservation_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("预约不存在")
        decision_rows = self.connection.execute(
            "SELECT d.*,p.kind AS plan_kind,p.state AS plan_state,p.confirmed_at FROM plan_decisions d "
            "JOIN orchestration_plans p ON p.plan_id=d.plan_id "
            "WHERE d.reservation_id=? ORDER BY d.plan_id",
            (reservation_id,),
        ).fetchall()
        holds = self.connection.execute(
            "SELECT domain,resource_id,slot,units,state,snapshot_id FROM reservation_resources "
            "WHERE reservation_id=? ORDER BY domain,resource_id,slot",
            (reservation_id,),
        ).fetchall()
        open_actions = self.connection.execute(
            "SELECT a.action_id,a.plan_id,a.code,a.detail,a.opened_at FROM safety_action_ledger a "
            "JOIN plan_decisions d ON d.plan_id=a.plan_id AND d.reservation_id=a.reservation_id "
            "WHERE a.reservation_id=? AND a.state='open' ORDER BY a.action_id",
            (reservation_id,),
        ).fetchall()
        return {
            "reservation_id": reservation_id,
            "state": row["state"],
            "revision": row["revision"],
            "entry_slot": row["entry_slot"],
            "party_size": row["party_size"],
            "holds": [dict(item) for item in holds],
            "decisions": [
                {
                    "plan_id": item["plan_id"],
                    "plan_kind": item["plan_kind"],
                    "plan_state": item["plan_state"],
                    "action": item["action"],
                    "reason_code": item["reason_code"],
                    "reason_detail": item["reason_detail"],
                    "blocked_by": item["blocked_by"],
                    "capacity_sources": json.loads(item["capacity_sources_json"]),
                    "target_capacity_sources": json.loads(item["target_capacity_sources_json"]),
                }
                for item in decision_rows
            ],
            "open_safety_actions": [dict(item) for item in open_actions],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM orch_audit_events ORDER BY event_id").fetchall()
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
