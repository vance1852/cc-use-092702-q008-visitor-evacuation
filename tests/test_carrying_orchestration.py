"""统一承载编排的领域规则、事务边界、疏散回执与 HTTP 边界测试。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from carrying_orchestration.api import JsonApplication
from carrying_orchestration.clock import FrozenClock
from carrying_orchestration.errors import Conflict, InvalidState
from carrying_orchestration.service import CarryingOrchestrationService


NOW = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)  # 国庆 10:00 北京时间


class CarryingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(NOW)
        self.service = CarryingOrchestrationService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("dispatch", "dispatcher"),
            ("risk", "risk"),
            ("ranger", "ranger"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.register_catalog()

    def tearDown(self) -> None:
        self.connection.close()

    def register_catalog(self) -> None:
        versions = [
            ("entry-slot", "gate-main", "2026-10-01T01:00:00Z", 100, "entry-0900-v1"),
            ("entry-slot", "gate-main", "2026-10-01T03:00:00Z", 100, "entry-1100-v1"),
            ("trail-direction", "canyon-eastbound", "2026-10-01", 60, "trail-east-v1"),
            ("shuttle", "shuttle-line-a", "2026-10-01", 80, "shuttle-a-v1"),
            ("parking", "lot-north", "2026-10-01", 70, "parking-n-v1"),
        ]
        for kind, rid, scope, capacity, revision in versions:
            self.service.register_capacity_version("plan", {
                "resource_kind": kind, "resource_id": rid, "scope_key": scope,
                "capacity": capacity, "source_revision": revision,
                "effective_from": "2026-09-25T00:00:00Z",
            })

    def reservation(self, number: int, *, slot: str = "2026-10-01T01:00:00Z", party: int = 2,
                    assistance: bool = False) -> dict[str, object]:
        needs = [{"assistance_kind": "wheelchair", "headcount": 1}] if assistance else []
        return {
            "reservation_id": f"res-{number}",
            "visitor_name": f"游客{number}",
            "contact": "13800000000",
            "party_size": party,
            "enters_at": slot,
            "requirements": [
                {"resource_kind": "entry-slot", "resource_id": "gate-main", "scope_key": slot, "quantity": party},
                {"resource_kind": "trail-direction", "resource_id": "canyon-eastbound", "scope_key": "2026-10-01", "quantity": party},
                {"resource_kind": "shuttle", "resource_id": "shuttle-line-a", "scope_key": "2026-10-01", "quantity": party},
                {"resource_kind": "parking", "resource_id": "lot-north", "scope_key": "2026-10-01", "quantity": party},
            ],
            "assistance_needs": needs,
            "idempotency_key": f"idem-res-{number}",
        }

    def submit(self, number: int, **kwargs) -> dict[str, object]:
        return self.service.submit_reservation("dispatch", self.reservation(number, **kwargs))

    def decisions_by_id(self, plan: dict[str, object]) -> dict[str, dict[str, object]]:
        return {row["reservation_id"]: row for row in plan["decisions"]}  # type: ignore[index]

    def test_snapshot_plan_retain_and_capacity_sources(self) -> None:
        self.submit(1)
        plan = self.service.generate_adjustment_plan("dispatch")
        decision = self.decisions_by_id(plan)["res-1"]
        self.assertEqual(decision["action"], "retain")
        self.assertTrue(any("capacity_sufficient" in reason for reason in decision["reasons"]))
        kinds = {source["resource_kind"] for source in decision["capacity_sources"]}
        self.assertEqual(kinds, {"entry-slot", "trail-direction", "shuttle", "parking"})
        for source in decision["capacity_sources"]:
            self.assertIsNotNone(source["version_id"])
            self.assertIn("容量版本", source["basis"])
        self.assertEqual(plan["snapshot_revision"], 1)

    def test_confirm_atomically_occupies_linked_resources(self) -> None:
        self.submit(1, party=3)
        plan = self.service.generate_adjustment_plan("dispatch")
        confirmed = self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-1")
        self.assertEqual(confirmed["state"], "confirmed")
        holds = self.connection.execute(
            "SELECT resource_kind,scope_key,quantity,snapshot_revision FROM capacity_holds WHERE released_at IS NULL "
            "ORDER BY resource_kind"
        ).fetchall()
        self.assertEqual(len(holds), 4)
        self.assertTrue({row["quantity"] for row in holds} <= {3})
        self.assertEqual({row["snapshot_revision"] for row in holds}, {1})
        # 确认回执幂等：重复确认返回同一结果，不重复占用
        again = self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-1")
        self.assertEqual(again, confirmed)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) c FROM capacity_holds WHERE reservation_id='res-1'"
        ).fetchone()["c"], 4)
        with self.assertRaises(InvalidState):
            self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-2")

    def test_capacity_version_change_fails_whole_order(self) -> None:
        self.submit(1, party=2)
        plan = self.service.generate_adjustment_plan("dispatch")
        # 生成方案之后、确认之前，摆渡车容量来源发布新版本
        self.service.register_capacity_version("plan", {
            "resource_kind": "shuttle", "resource_id": "shuttle-line-a", "scope_key": "2026-10-01",
            "capacity": 40, "source_revision": "shuttle-a-v2",
            "effective_from": "2026-09-30T01:00:00Z",
        })
        with self.assertRaises(Conflict) as ctx:
            self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-x")
        self.assertIn("容量版本已变化", str(ctx.exception))
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) c FROM capacity_holds"
        ).fetchone()["c"], 0)
        self.assertEqual(
            self.connection.execute("SELECT state FROM reservations WHERE reservation_id='res-1'").fetchone()["state"],
            "reserved",
        )

    def test_linked_resource_shortage_marks_release_with_reason_and_source(self) -> None:
        # 50 个预约各 2 人共 100 人：入园名额 100、停车 70 均够，步道方向 60 最先成为瓶颈
        for number in range(50):
            self.submit(number, party=2)
        plan = self.service.generate_adjustment_plan("dispatch")
        decisions = list(self.decisions_by_id(plan).values())
        retained = [row for row in decisions if row["action"] == "retain"]
        released = [row for row in decisions if row["action"] == "release_quota"]
        self.assertEqual(len(retained), 30)
        self.assertEqual(len(released), 20)
        sample = released[0]
        self.assertTrue(any("capacity_insufficient" in reason and "trail-direction" in reason
                            for reason in sample["reasons"]))
        self.assertTrue(any("no_alternative" in reason for reason in sample["reasons"]))
        trail_source = next(source for source in sample["capacity_sources"]
                            if source["resource_kind"] == "trail-direction")
        self.assertEqual(trail_source["effective_capacity"], 60)
        self.assertEqual(trail_source["source_revision"], "trail-east-v1")

    def test_checked_in_visitor_is_never_auto_rescheduled(self) -> None:
        self.submit(1, party=2)
        plan = self.service.generate_adjustment_plan("dispatch")
        self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-1")
        self.service.check_in("ranger", "res-1")
        # 入园名额出现更宽裕的未来时段，且本时段步道被临时关闭
        self.service.register_closure_window("risk", {
            "window_id": "win-trail-1", "resource_kind": "trail-direction",
            "resource_id": "canyon-eastbound", "scope_key": "2026-10-01",
            "starts_at": "2026-09-30T01:30:00Z", "ends_at": "2026-09-30T05:00:00Z",
            "capacity_percent": 0, "reason": "峡谷步道抢修",
        })
        self.clock.advance(hours=1)
        new_plan = self.service.generate_adjustment_plan("risk")
        decision = self.decisions_by_id(new_plan)["res-1"]
        self.assertEqual(decision["action"], "evacuate")
        self.assertNotIn("reschedule", decision["action"])
        self.assertTrue(any("already_inside" in reason for reason in decision["reasons"]))
        self.assertIsNone(decision["proposed_enters_at"])

    def _trigger_evacuation_plan(self) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        self.submit(1, party=2, assistance=True)
        self.submit(2, party=3)
        plan = self.service.generate_adjustment_plan("dispatch")
        self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-1")
        self.service.check_in("ranger", "res-1")
        self.service.check_in("ranger", "res-2")
        self.service.trigger_weather_alert("risk", {
            "alert_id": "alert-orange-1", "level": "orange", "title": "峡谷强降雨",
            "starts_at": "2026-09-30T03:30:00Z", "ends_at": "2026-10-01T00:00:00Z",
            "closed_resources": ["trail-direction|canyon-eastbound"],
            "reason": "气象部门橙色预警",
        })
        self.clock.advance(hours=2)
        evacuation_plan = self.service.generate_adjustment_plan("risk")
        decisions = self.decisions_by_id(evacuation_plan)
        for rid in ("res-1", "res-2"):
            self.assertEqual(decisions[rid]["action"], "evacuate")
            self.assertTrue(decisions[rid]["evacuation_batch_id"])
            pending_codes = {item["code"] for item in decisions[rid]["unresolved_safety_actions"]}
            self.assertIn("evacuation_checklist:handover_received", pending_codes)
        # 重点人群人数多的预约排在批次前面
        batch_view = self.service.evacuation_batch("ranger", decisions["res-1"]["evacuation_batch_id"])
        self.assertEqual(batch_view["status"], "planned")
        self.assertEqual([m["reservation_id"] for m in batch_view["members"]], ["res-1", "res-2"])
        self.assertEqual(batch_view["headcount"], 5)
        self.assertEqual(batch_view["assistance_headcount"], 1)
        self.assertEqual(
            [step["step_code"] for step in batch_view["checklist"]],
            ["notify_visitors", "headcount_assembly", "assistance_ready", "transport_dispatch", "handover_received"],
        )
        return evacuation_plan, decisions

    def test_weather_alert_forms_evacuation_batches_and_checklist(self) -> None:
        self._trigger_evacuation_plan()

    def test_duplicate_evacuation_receipt_does_not_release_twice(self) -> None:
        evacuation_plan, decisions = self._trigger_evacuation_plan()
        self.service.confirm_plan("dispatch", evacuation_plan["plan_id"], "confirm-2")
        batch_id = decisions["res-1"]["evacuation_batch_id"]
        steps = ["notify_visitors", "headcount_assembly", "assistance_ready", "transport_dispatch"]
        first = self.service.acknowledge_evacuation("ranger", batch_id, steps, "receipt-1")
        self.assertEqual(first["status"], "in_progress")
        self.assertEqual(first["newly_acked_steps"], steps)
        # 重复回执：幂等返回，不产生新的名额释放
        replay = self.service.acknowledge_evacuation("ranger", batch_id, steps, "receipt-1")
        self.assertEqual(replay, first)
        active_holds = self.connection.execute(
            "SELECT COUNT(*) c FROM capacity_holds WHERE released_at IS NULL"
        ).fetchone()["c"]
        self.assertEqual(active_holds, 8)  # 在园两单各 4 项占用仍未释放
        # 最终交接完成：释放一次并把预约置为 evacuated
        final = self.service.acknowledge_evacuation("ranger", batch_id, ["handover_received"], "receipt-2")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(sorted(final["released_reservation_ids"]), ["res-1", "res-2"])
        final_replay = self.service.acknowledge_evacuation("ranger", batch_id, ["handover_received"], "receipt-2")
        self.assertEqual(final_replay, final)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(DISTINCT release_receipt_id) c FROM capacity_holds WHERE released_at IS NOT NULL"
        ).fetchone()["c"], 1)
        states = {
            row["reservation_id"]: row["state"]
            for row in self.connection.execute("SELECT reservation_id,state FROM reservations").fetchall()
        }
        self.assertEqual(states, {"res-1": "evacuated", "res-2": "evacuated"})
        completed = self.service.evacuation_batch("ranger", batch_id)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["pending_steps"], [])

    def test_reschedule_to_future_slot_moves_entry_only_and_keeps_linked_resources(self) -> None:
        # 把 09:00 入园名额压到 0，11:00 时段仍有 100
        self.service.register_capacity_version("plan", {
            "resource_kind": "entry-slot", "resource_id": "gate-main", "scope_key": "2026-10-01T01:00:00Z",
            "capacity": 0, "source_revision": "entry-0900-v2",
            "effective_from": "2026-09-30T00:00:00Z",
        })
        self.submit(1, party=2)
        plan = self.service.generate_adjustment_plan("dispatch")
        decision = self.decisions_by_id(plan)["res-1"]
        self.assertEqual(decision["action"], "reschedule")
        self.assertEqual(decision["proposed_enters_at"], "2026-10-01T03:00:00Z")
        self.assertEqual(len(decision["moves"]), 1)
        self.service.confirm_plan("dispatch", plan["plan_id"], "confirm-r")
        row = self.connection.execute(
            "SELECT state,enters_at FROM reservations WHERE reservation_id='res-1'"
        ).fetchone()
        self.assertEqual(row["state"], "rescheduled")
        self.assertEqual(row["enters_at"], "2026-10-01T03:00:00Z")
        scopes = {
            (item["resource_kind"], item["scope_key"])
            for item in self.connection.execute(
                "SELECT resource_kind,scope_key FROM capacity_holds WHERE released_at IS NULL"
            ).fetchall()
        }
        self.assertIn(("entry-slot", "2026-10-01T03:00:00Z"), scopes)
        self.assertIn(("shuttle", "2026-10-01"), scopes)
        self.assertNotIn(("entry-slot", "2026-10-01T01:00:00Z"), scopes)

    def test_closure_window_scales_capacity_and_is_cited_as_source(self) -> None:
        self.service.register_closure_window("risk", {
            "window_id": "win-shuttle-1", "resource_kind": "shuttle",
            "resource_id": "shuttle-line-a", "scope_key": "2026-10-01",
            "starts_at": "2026-10-01T01:00:00Z", "ends_at": "2026-10-01T03:00:00Z",
            "capacity_percent": 50, "reason": "接驳运力减半",
        })
        for number in range(25):
            self.submit(number, party=2)
        plan = self.service.generate_adjustment_plan("dispatch")
        decisions = list(self.decisions_by_id(plan).values())
        retained = [row for row in decisions if row["action"] == "retain"]
        # 摆渡车 80*50%=40，最多保留 20 单（40 人）
        self.assertEqual(sum(row["party_size"] for row in retained), 40)
        source = next(
            source for row in retained for source in row["capacity_sources"]
            if source["resource_kind"] == "shuttle"
        )
        self.assertEqual(source["effective_capacity"], 40)
        self.assertEqual(source["closure_window_ids"], ["win-shuttle-1"])
        self.assertIn("win-shuttle-1", source["basis"])

    def test_unresolved_assistance_is_reported_and_clears_after_arrangement(self) -> None:
        self.submit(1, assistance=True)
        plan = self.service.generate_adjustment_plan("dispatch")
        decision = self.decisions_by_id(plan)["res-1"]
        self.assertTrue(any(item["code"] == "assistance_unarranged"
                            for item in decision["unresolved_safety_actions"]))
        self.service.arrange_assistance("dispatch", "res-1", "wheelchair")
        plan_again = self.service.generate_adjustment_plan("dispatch")
        decision_again = self.decisions_by_id(plan_again)["res-1"]
        self.assertFalse(any(item["code"] == "assistance_unarranged"
                             for item in decision_again["unresolved_safety_actions"]))

    def test_plan_query_explains_sources_and_open_safety_actions(self) -> None:
        evacuation_plan, decisions = self._trigger_evacuation_plan()
        queried = self.service.plan("audit", evacuation_plan["plan_id"])
        decision = self.decisions_by_id(queried)["res-1"]
        self.assertTrue(any(source["alert_ids"] == ["alert-orange-1"]
                            for source in decision["capacity_sources"]))
        codes = {item["code"] for item in decision["unresolved_safety_actions"]}
        self.assertIn("evacuation_checklist:notify_visitors", codes)

    def test_audit_chain_detects_tampering(self) -> None:
        self.submit(1)
        self.service.generate_adjustment_plan("dispatch")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE carrying_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_boundary_and_role_enforcement(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("POST", "/reservations", {"X-Actor-Id": "ranger"},
                              json.dumps(self.reservation(9), ensure_ascii=False).encode("utf-8"))
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")
        response = app.handle("POST", "/adjustment_plans", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 403)
        missing = app.handle("GET", "/adjustment_plans/999", {"X-Actor-Id": "audit"})
        self.assertEqual(missing.status, 404)


if __name__ == "__main__":
    unittest.main()
