from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from visitor_orchestration.api import JsonApplication
from visitor_orchestration.clock import FrozenClock
from visitor_orchestration.errors import Conflict, InvalidState
from visitor_orchestration.planning import (
    EVACUATE,
    RESCHEDULE,
    RETAIN,
    DENY,
    booking_decisions,
    build_evacuation_batches,
    effective_capacity,
    slot_overlaps,
    weather_decisions,
)
from visitor_orchestration.service import VisitorOrchestrationService


DATE = "2026-10-01"


def _view(domain, resource_id, slot, revision, nominal, effective, *, closures=(), alert=False):
    return {
        "domain": domain,
        "resource_id": resource_id,
        "slot": slot,
        "revision": revision,
        "nominal_capacity": nominal,
        "effective_capacity": effective,
        "closures": list(closures),
        "alert_hard_closed": alert,
        "alert_ids": ["alert-1"] if alert else [],
    }


class PlanningTests(unittest.TestCase):
    def test_effective_capacity_applies_windows_and_hard_close(self) -> None:
        self.assertEqual(effective_capacity(100, [50, 50]), 25)
        self.assertEqual(effective_capacity(100, [0]), 0)
        self.assertEqual(effective_capacity(100, [50], hard_closed=True), 0)
        self.assertTrue(slot_overlaps(f"{DATE}T08:00", f"{DATE}T08:15:00Z", f"{DATE}T08:45:00Z"))
        self.assertFalse(slot_overlaps(f"{DATE}T08:30", f"{DATE}T08:00:00Z", f"{DATE}T08:30:00Z"))

    def test_booking_decisions_retain_reschedule_deny_with_sources(self) -> None:
        capacity = {
            "entry_slot|gate|2026-10-01T08:00": _view("entry_slot", "gate", f"{DATE}T08:00", 1, 10, 10),
            "entry_slot|gate|2026-10-01T08:30": _view("entry_slot", "gate", f"{DATE}T08:30", 1, 10, 10),
            "trail|canyon|2026-10-01T08:00": _view("trail", "canyon", f"{DATE}T08:00", 1, 10, 4),
            "trail|canyon|2026-10-01T08:30": _view("trail", "canyon", f"{DATE}T08:30", 1, 10, 10),
        }
        reservations = [
            {"reservation_id": "a", "party_size": 4, "entry_resource_id": "gate", "entry_slot": f"{DATE}T08:00",
             "trail_resource_id": "canyon", "created_at": "t1"},
            {"reservation_id": "b", "party_size": 4, "entry_resource_id": "gate", "entry_slot": f"{DATE}T08:00",
             "trail_resource_id": "canyon", "created_at": "t2"},
        ]
        decisions = booking_decisions(reservations, capacity, {})
        actions = {d["reservation_id"]: d["action"] for d in decisions}
        self.assertEqual(actions, {"a": RETAIN, "b": RESCHEDULE})
        second = decisions[1]
        self.assertEqual(second["target"]["entry_slot"], f"{DATE}T08:30")
        self.assertEqual(second["blocked_by"], "slot_oversubscribed")
        domains = {s["domain"] for s in second["capacity_sources"]}
        self.assertEqual(domains, {"entry_slot", "trail"})
        self.assertTrue(all(s["revision"] == 1 for s in second["capacity_sources"]))
        self.assertTrue(second["target_capacity_sources"])

    def test_hard_closed_capacity_denies_without_later_slot(self) -> None:
        capacity = {
            "entry_slot|gate|2026-10-01T08:00": _view("entry_slot", "gate", f"{DATE}T08:00", 1, 10, 0, alert=True),
        }
        reservations = [
            {"reservation_id": "a", "party_size": 2, "entry_resource_id": "gate", "entry_slot": f"{DATE}T08:00",
             "created_at": "t1"},
        ]
        decision = booking_decisions(reservations, capacity, {})[0]
        self.assertEqual(decision["action"], DENY)
        self.assertEqual(decision["blocked_by"], "weather_alert_closed")

    def test_on_site_visitor_is_evacuated_never_rescheduled_to_future(self) -> None:
        capacity = {
            "entry_slot|gate|2026-10-01T08:00": _view("entry_slot", "gate", f"{DATE}T08:00", 1, 10, 0, alert=True),
            "entry_slot|gate|2026-10-01T09:00": _view("entry_slot", "gate", f"{DATE}T09:00", 1, 10, 10),
            "entry_slot|gate|2026-10-02T09:00": _view("entry_slot", "gate", "2026-10-02T09:00", 1, 10, 10),
        }
        reservations = [
            {"reservation_id": "onsite", "party_size": 2, "entry_resource_id": "gate",
             "entry_slot": f"{DATE}T08:00", "state": "checked_in", "created_at": "t1",
             "assistance": [{"kind": "wheelchair"}]},
            {"reservation_id": "future", "party_size": 2, "entry_resource_id": "gate",
             "entry_slot": f"{DATE}T08:00", "state": "confirmed", "created_at": "t2"},
        ]
        decisions = weather_decisions(reservations, capacity, {}, {("entry_slot", "gate")})
        actions = {d["reservation_id"]: d["action"] for d in decisions}
        self.assertEqual(actions["onsite"], EVACUATE)
        self.assertEqual(actions["future"], RESCHEDULE)
        onsite = next(d for d in decisions if d["reservation_id"] == "onsite")
        self.assertEqual(onsite["target"], {})
        from visitor_orchestration.planning import safety_actions_for
        codes = {code for code, _detail in safety_actions_for(onsite)}
        self.assertIn("vehicle:dispatch", codes)
        self.assertIn("handover", codes)
        self.assertIn("evac_assist:wheelchair", codes)

    def test_evacuation_batches_prioritize_assistance_and_keep_party_together(self) -> None:
        evacuees = [
            {"reservation_id": "plain", "pax": 3, "assistance": []},
            {"reservation_id": "med", "pax": 2, "assistance": [{"kind": "medical"}]},
        ]
        shuttles = [{"resource_id": "s1", "slot": f"{DATE}T08:30", "seats": 5}]
        packed = build_evacuation_batches(evacuees, shuttles)
        self.assertEqual(len(packed["batches"]), 1)
        batch = packed["batches"][0]
        self.assertEqual(batch["priority_kind"], "medical")
        self.assertEqual([m["reservation_id"] for m in batch["members"]], ["med", "plain"])
        self.assertEqual(batch["occupied"], 5)
        self.assertEqual(len(batch["members"][0]["checklist"]), 5)

    def test_without_shuttle_a_pending_vehicle_batch_is_formed(self) -> None:
        evacuees = [{"reservation_id": "x", "pax": 2, "assistance": []}]
        packed = build_evacuation_batches(evacuees, [])
        self.assertEqual(len(packed["batches"]), 1)
        self.assertTrue(packed["batches"][0]["vehicle_pending"])
        self.assertIsNone(packed["batches"][0]["shuttle_resource_id"])


class ServiceTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
        self.service = VisitorOrchestrationService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("agent", "agent"),
            ("risk", "risk"),
            ("rescue", "rescue"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_resource("plan", {
            "domain": "entry_slot", "resource_id": "gate", "name": "东门",
            "slot": f"{DATE}T08:00", "capacity": 4})
        self.service.register_resource("plan", {
            "domain": "trail", "resource_id": "canyon", "name": "峡谷步道",
            "slot": f"{DATE}T08:00", "capacity": 4, "direction": "up"})
        self.service.register_resource("plan", {
            "domain": "entry_slot", "resource_id": "gate", "name": "东门",
            "slot": f"{DATE}T08:30", "capacity": 4})
        self.service.register_resource("plan", {
            "domain": "trail", "resource_id": "canyon", "name": "峡谷步道",
            "slot": f"{DATE}T08:30", "capacity": 4, "direction": "up"})

    def tearDown(self) -> None:
        self.connection.close()

    def _reservation(self, rid, party, slot="08:00", key=None):
        return self.service.submit_reservation("agent", {
            "reservation_id": rid,
            "party_size": party,
            "entry_resource_id": "gate",
            "entry_slot": f"{DATE}T{slot}",
            "trail_resource_id": "canyon",
            "trail_direction": "up",
            "idempotency_key": key or f"key-{rid}",
        })

    def test_reservation_idempotency_replay_and_payload_conflict(self) -> None:
        payload = {
            "reservation_id": "r1", "party_size": 2, "entry_resource_id": "gate",
            "entry_slot": f"{DATE}T08:00", "trail_resource_id": "canyon", "trail_direction": "up",
            "idempotency_key": "idem-1",
        }
        first = self.service.submit_reservation("agent", payload)
        self.assertEqual(first, self.service.submit_reservation("agent", payload))
        with self.assertRaises(Conflict):
            self.service.submit_reservation("agent", {**payload, "party_size": 3})

    def test_confirm_atomically_occupies_all_linked_resources(self) -> None:
        self._reservation("r1", 2)
        self._reservation("r2", 2)
        plan = self.service.create_booking_plan("agent", DATE)
        confirmed = self.service.confirm_booking_plan("agent", plan["plan_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        held = self.connection.execute(
            "SELECT COALESCE(SUM(units),0) AS units FROM reservation_resources WHERE state='held'"
        ).fetchone()["units"]
        self.assertEqual(held, 8)  # 两单 × (入园 2 + 步道 2)

    def test_capacity_revision_change_fails_whole_plan_without_partial_occupancy(self) -> None:
        self._reservation("r1", 2)
        self._reservation("r2", 2)
        plan = self.service.create_booking_plan("agent", DATE)
        self.service.adjust_capacity("plan", {
            "domain": "trail", "resource_id": "canyon", "slot": f"{DATE}T08:00", "capacity": 100})
        with self.assertRaises(InvalidState):
            self.service.confirm_booking_plan("agent", plan["plan_id"])
        held = self.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_resources WHERE state='held'"
        ).fetchone()["c"]
        self.assertEqual(held, 0)  # 整单失败，不能留下任何部分占用
        self.assertEqual(self.service.plan(plan["plan_id"], actor_id="audit")["state"], "failed")
        states = {
            row["reservation_id"]: row["state"]
            for row in self.connection.execute("SELECT reservation_id,state FROM reservations").fetchall()
        }
        self.assertEqual(states, {"r1": "draft", "r2": "draft"})

    def test_handover_receipt_does_not_release_quota_twice(self) -> None:
        self._reservation("r1", 2)
        plan = self.service.confirm_booking_plan("agent", self.service.create_booking_plan("agent", DATE)["plan_id"])
        self.service.check_in("agent", "r1")
        self.service.issue_alert("risk", {
            "alert_id": "a1", "level": "orange", "title": "预警",
            "issued_at": f"{DATE}T08:05:00Z",
            "affected_resources": [{"domain": "trail", "resource_id": "canyon"}],
        })
        self.service.register_resource("plan", {
            "domain": "shuttle", "resource_id": "s1", "name": "摆渡",
            "slot": f"{DATE}T08:30", "capacity": 20})
        weather = self.service.create_weather_plan("risk", "a1")
        confirmed = self.service.confirm_weather_plan("risk", weather["plan_id"])
        batch_id = confirmed["evacuation"]["batches"][0]["batch_id"]
        self.service.dispatch_batch("rescue", batch_id, "crew-1")
        self.service.arrive_batch("rescue", batch_id)
        first = self.service.handover_batch("rescue", batch_id, "救助站", "rcpt-1")
        second = self.service.handover_batch("rescue", batch_id, "救助站", "rcpt-1")
        self.assertEqual(first, second)  # 重复回执回放首次结果
        self.assertEqual(first["released_reservations"], ["r1"])
        released_versions = self.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_resources WHERE reservation_id='r1' AND state='released'"
        ).fetchone()["c"]
        self.assertEqual(released_versions, 2)  # 入园 + 步道各释放一次，不是两次回执 ×2
        held = self.connection.execute(
            "SELECT COUNT(*) AS c FROM reservation_resources WHERE reservation_id='r1' AND state='held'"
        ).fetchone()["c"]
        self.assertEqual(held, 0)
        self.assertEqual(self.service.reservation_status("agent", "r1")["state"], "evacuated")

    def test_plan_query_names_sources_and_open_safety_actions(self) -> None:
        self._reservation("r1", 6)  # 超过容量 4，当日没有任何可承载时段
        plan = self.service.create_booking_plan("agent", DATE)
        decision = plan["decisions"][0]
        self.assertEqual(decision["action"], DENY)
        self.assertTrue(all("revision" in source and "effective_capacity" in source
                            for source in decision["capacity_sources"]))
        open_codes = {item["code"] for item in plan["open_safety_actions"]}
        self.assertIn("notify:cancel", open_codes)
        self.service.confirm_booking_plan("agent", plan["plan_id"])
        after = self.service.plan(plan["plan_id"], actor_id="audit")
        self.assertTrue(after["open_safety_actions"])

    def test_api_health_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/plans/1")
        self.assertEqual(missing_actor.status, 422)


if __name__ == "__main__":
    unittest.main()
